"""C 组验收测试：十设备并发运行与单台故障隔离。

- 使用隔离十设备绑定表加载（含一对一校验），不依赖现场设备数量；
- arm-01（daemon 真机配置）在仿真模式下回退仿真驱动；
- 十台同时执行微信发送任务：arm-03 驱动级急停、arm-04 输入断连，
  其余八台正常完成；任务、租约、证据、审计按设备隔离不串台。
"""
from __future__ import annotations

import pathlib
import pytest
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")

import asyncio
import json
import uuid
from datetime import timedelta
from pathlib import Path

import httpx

from fakes.fake_wx import FakeWX
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from runtime.util import iso, sha256_text, utc_now
from tests.helpers import write_ten_device_root

FLEET_ROOT = Path(__file__).parents[1]
WX_PLUGIN_SOURCE = FLEET_ROOT / "examples" / "plugins" / "wx-wechat-adapter"
TERMINAL = {"confirmed", "failed", "unknown", "cancelled", "expired"}


def prepare_ten_device_root(tmp_path: Path) -> Path:
    """生成隔离十设备绑定表，daemon 槽位在仿真模式下回退。"""
    return write_ten_device_root(tmp_path)


def make_fleet(tmp_path: Path):
    root = prepare_ten_device_root(tmp_path)
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
        "wx_callback_base_url": "http://fake-wx.local",
        "heartbeat_interval_seconds": 1,
        "camera_check_interval_seconds": 2,
        "hotplug_discovery_enabled": False,
    }, root=root)
    runtime = ArmFleetRuntime(config)
    fake_wx = FakeWX()
    runtime.reporter.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake_wx.app), base_url="http://fake-wx.local"
    )
    return runtime, fake_wx


async def _wait_terminal(runtime, task_id, timeout=30.0):
    waited = 0.0
    while waited < timeout:
        task = runtime.get_task(task_id)
        if task["status"] in TERMINAL:
            return task
        await asyncio.sleep(0.05)
        waited += 0.05
    return runtime.get_task(task_id)


def _chat_send(device_id: str, index: int, account: str):
    outbox_id = f"outbox-ten-{index:02d}"
    text = f"十设备并发消息 {index:02d}。"
    return {
        "task_id": f"task-ten-{index:02d}", "task_type": "chat_send",
        "idempotency_key": outbox_id, "device_id": device_id,
        "wechat_account_id": account,
        "external_conversation_id": f"conv-ten-{index:02d}",
        "expires_at": iso(utc_now() + timedelta(seconds=300)),
        "payload": {"outbox_id": outbox_id, "conversation_id": f"conv-ten-{index:02d}",
                    "draft_id": f"draft-{outbox_id}", "text": text,
                    "text_hash": sha256_text(text),
                    "approved_at": iso(utc_now()), "opportunity_bindings": [],
                    "fact_snapshot_ids": [], "client_request_id": f"wx-req-{outbox_id}"},
        "contract_version": "chat-send.v1", "correlation_id": f"wx-corr-{outbox_id}",
    }


def test_ten_device_config_loads_with_unique_bindings(tmp_path):
    """验收 1：真实绑定表加载，编号/槽位/串口/daemon/摄像头/账号无重复。"""
    from runtime.config import load_device_configs

    devices = load_device_configs(prepare_ten_device_root(tmp_path))
    assert len(devices) == 10
    for field in ("device_id", "arm_slot", "camera_id", "wechat_account_id"):
        values = [d.get(field) for d in devices]
        assert len(set(values)) == 10, f"{field} 存在重复"


@needs_wx_example
def test_ten_devices_concurrent_isolation(tmp_path):
    """验收 8：十台并发，单台急停/断连不影响其他设备；数据按设备隔离。"""

    async def scenario():
        runtime, fake_wx = make_fleet(tmp_path)
        runtime.plugins.install(WX_PLUGIN_SOURCE, operator_id="操作员甲", reason="C 组十设备联调安装")
        runtime.plugins.activate("wx-wechat-adapter", operator_id="操作员甲", reason="C 组联调启用")
        # 真实绑定表不携带仿真专用字段；测试在内存中补充仿真窗口联动
        from runtime.config import load_device_configs

        configs = load_device_configs(runtime.config.root)
        for cfg in configs:
            cfg["sim_wechat_window"] = True
            cfg["sim_calibrated"] = True
        await runtime.startup(device_configs=configs)
        from tests.helpers import enable_input_switch
        enable_input_switch(runtime, tmp_path)
        try:
            devices = {d["device_id"]: d for d in runtime.devices.all()}
            assert len(devices) == 10
            ready = [d for d in devices.values() if d["status"] == "ready"]
            assert len(ready) == 10, "十台设备都应完成自检进入可用"

            # 故障注入：arm-03 运动放缓后在执行中触发驱动级急停（只影响该设备）；
            # arm-04 输入中断连（结果未知）。
            runtime.devices.driver("arm-03")._move_seconds = 0.3
            runtime.devices.input_driver("arm-04").faults["disconnect_after_chars"] = 3

            order = [f"arm-{i:02d}" for i in range(1, 11)]
            submitted = {}
            for index, device_id in enumerate(order, start=1):
                account = runtime.devices.get(device_id).get("wechat_account_id")
                payload = _chat_send(device_id, index, account)
                submitted[device_id] = runtime.submit_task(
                    payload, source="wx", context={"operator_id": "wx", "reason": "十设备并发验收"})

            # 等 arm-03 进入执行中，再触发急停：验证执行中急停只影响本机任务
            for _ in range(1000):
                if runtime.devices.get("arm-03")["status"] == "busy":
                    break
                await asyncio.sleep(0.005)
            await runtime.devices.driver("arm-03").estop()

            done = {}
            for device_id, task in submitted.items():
                done[device_id] = await _wait_terminal(runtime, task["task_id"])

            # arm-03 急停：任务明确失败（驱动拒绝运动），只影响本机
            assert done["arm-03"]["status"] == "failed", done["arm-03"].get("failure_reason")
            # 不再盲点返回；输入断连且归位未确认时必须暂停+机械未知互锁转人工
            # （F1 轮语义：hold_unknown=paused+home_ok=False+resume 硬守卫）。
            assert done["arm-04"]["status"] == "unknown"
            arm04 = runtime.devices.get("arm-04")
            assert arm04["status"] == "paused", arm04["status"]
            assert arm04.get("unknown_hold_task_id"), "必须保留机械未知人工门禁"
            assert arm04.get("home_ok") is False
            assert not any(item.get("target") == "back_button"
                           for item in runtime.devices.driver("arm-04").action_log)
            # 其余八台正常完成，各设备输入互不串台
            for device_id in order:
                if device_id in ("arm-03", "arm-04"):
                    continue
                assert done[device_id]["status"] == "confirmed", (
                    device_id, done[device_id].get("failure_reason") or done[device_id].get("uncertainty_reason"))
                typed = runtime.devices.input_driver(device_id).typed_text()
                assert typed == done[device_id]["payload"]["text"], "各设备输入互不串台"

            # 急停设备未产生任何输入；断连设备不重复执行（断连后输入内容可能为空或部分）
            assert runtime.devices.input_driver("arm-03").typed_text() == ""
            assert runtime.devices.input_driver("arm-04").typed_text().count("十设备并发消息") <= 1

            # 证据与任务按设备隔离：每台任务恰好一条，互不串台
            for device_id in order:
                tasks = [t for t in runtime.store.all_tasks() if t["device_id"] == device_id]
                assert len(tasks) == 1
            # 审计时间线按设备过滤不含其他设备条目
            timeline_devices = runtime.audit.list_events(limit=50, device_id="arm-05")
            assert all(e.get("device_id") in (None, "arm-05") for e in timeline_devices)

            # WX 回报对账：8 已发送 + 1 失败 + 1 结果未知（按 task_id 逐台核对）
            statuses = [r["status"] for r in fake_wx.results_by_task.values()]
            assert statuses.count("confirmed") == 8, statuses
            assert statuses.count("failed") == 1 and statuses.count("unknown") == 1
            assert len(statuses) == 10, "每台任务必须恰好回报一次"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
