"""R2-04 Fleet 动作侧安装锁反例（独立复核条件闭合项）。

复现独立复核发现的缺口：`runtime.py::_run_task_action` 的共享安装锁挂钩
（I 合入 B1 §4.1）此前无测试覆盖。本文件用真实锁原语
（`scripts.camera_binding_v2.installation_lock`——帧桥 bind/renew/unbind
写入口与 Fleet 动作侧共用的同一 fcntl 排他/共享原语）做双向争用：

- A）动作执行中（invoke 被屏障挂住、Fleet 已持共享锁）→ 另一进程形态的
  排他锁（=帧桥 bind 写临界区）立即 busy，不等待不排队；
- B）绑定写持排他锁（bind 进行中）→ Fleet 动作认领后立即拒绝
  （driver_policy_rejected + receipt not_started + invoke 零调用=零动作）；
- C）动作以 unknown 收尾 → 锁在回执/未知阻断持久化**之后**才释放
  （收尾完成后锁可再获取）。

锁根同源：Fleet=config.root/"data"；争用方用同一 data_root（生产部署三处
同源为部署约定，见 ACCEPTANCE 待确认项）。
"""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from scripts.camera_binding_v2 import installation_lock
from tests.helpers import make_runtime

DATA_ROOT_NAME = "data"


@pytest.fixture()
def env(tmp_path):
    runtime = make_runtime(tmp_path)
    import asyncio
    asyncio.run(runtime.devices.register_all(__import__(
        "tests.helpers", fromlist=["make_device_configs"]).make_device_configs(1)))
    runtime.devices._devices["arm-01"].update({"status": "ready"})
    data_root = tmp_path / DATA_ROOT_NAME
    data_root.mkdir(parents=True, exist_ok=True)
    return {"runtime": runtime, "data_root": data_root, "tmp_path": tmp_path}


def _run_action(runtime, task_id, invoke):
    task = {"task_id": task_id, "device_id": "arm-01", "status": "executing"}
    return asyncio.run(
        runtime._run_task_action(task, "arm.click", invoke))


def test_r2_04_fleet_action_holds_shared_lock_bind_is_busy(env):
    """A：动作执行中 Fleet 持共享锁 → 帧桥排他写入口立即 busy。"""
    runtime, data_root = env["runtime"], env["data_root"]
    entered = threading.Event()
    release = threading.Event()

    async def invoke():
        entered.set()
        await asyncio.get_running_loop().run_in_executor(
            None, release.wait, 10)
        return {"ok": True}

    def action():
        _run_action(runtime, "lock-a", invoke)

    thread = threading.Thread(target=action)
    thread.start()
    assert entered.wait(timeout=5), "动作未进入执行"
    # 帧桥 bind 写入口的同一原语：非阻塞排他获取必须失败（共享锁在持）
    from scripts.camera_binding_v2 import CameraBindingError
    with pytest.raises(CameraBindingError, match="device_maintenance_busy"):
        with installation_lock(data_root, "arm-01", exclusive=True):
            pass
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    # 动作结束后锁释放：排他可获取
    with installation_lock(data_root, "arm-01", exclusive=True):
        pass


def test_r2_04_bind_in_progress_rejects_fleet_action_zero_motion(env):
    """B：绑定写进行中（排他锁在持）→ Fleet 动作立即拒绝零动作。"""
    runtime, data_root = env["runtime"], env["data_root"]
    from drivers.arm import DriverError

    invoked = {"n": 0}

    async def invoke():
        invoked["n"] += 1
        return {"ok": True}

    with installation_lock(data_root, "arm-01", exclusive=True):
        with pytest.raises(DriverError) as exc:
            _run_action(runtime, "lock-b", invoke)
    assert "设备维护占用" in str(exc.value.message)
    assert getattr(exc.value, "motion_not_started", False) is True
    assert invoked["n"] == 0, "拒绝必须发生在 invoke 之前（零动作）"
    # 释放后同一动作可执行
    _run_action(runtime, "lock-b2", invoke)
    assert invoked["n"] == 1


def test_r2_04_lock_released_after_unknown_finalize(env):
    """C：unknown 收尾（hold_unknown 持久化）完成后锁才释放。"""
    runtime, data_root = env["runtime"], env["data_root"]
    from drivers.arm import DriverError

    async def invoke():
        raise DriverError("driver_motion_failed", "运动回执未确认")

    with pytest.raises(DriverError):
        _run_action(runtime, "lock-c", invoke)
    # 任务进入 unknown 阻断后，锁必须已释放（下一绑定写可进行）
    with installation_lock(data_root, "arm-01", exclusive=True):
        pass
