"""devices.json 并发/原子写回归（2026-10-29 从 WX 移植）。

背景：8 处写入方"读整表→改一处→写整表"且无锁、固定临时名 devices.json.tmp；
两进程同写会把生效文件写成半截 JSON。修复=flock + mkstemp + fsync + os.replace，
绑定变更改按设备读改写（update_device_config），只写静态字段。
"""
import json
import multiprocessing as mp
from pathlib import Path

import pytest

from runtime.config import load_device_configs, save_device_configs, STATIC_KEYS


def _devices(n: int, offset: int = 0) -> list[dict]:
    return [{"device_id": f"arm-{index + offset:02d}", "camera_id": f"cam-{index + offset:02d}"}
            for index in range(1, n + 1)]


def _writer(root: str, offset: int, rounds: int) -> None:
    for _ in range(rounds):
        save_device_configs(Path(root), _devices(3, offset))


def _hold_lock(root: str, seconds: float, ready) -> None:
    import time

    from runtime.config import device_config_lock

    with device_config_lock(Path(root), timeout=10.0):
        ready.set()
        time.sleep(seconds)


def test_save_leaves_no_shared_tmp_and_stays_parseable(tmp_path):
    save_device_configs(tmp_path, _devices(2))
    target = tmp_path / "configs" / "devices.json"
    assert json.loads(target.read_text(encoding="utf-8"))["devices"] == _devices(2)
    assert not (tmp_path / "configs" / "devices.json.tmp").exists()
    assert list((tmp_path / "configs").glob(".devices.json.*.tmp")) == []


def test_corrupt_payload_raises_without_overwriting(tmp_path):
    from runtime.errors import ApiError

    target = tmp_path / "configs" / "devices.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"devices": [', encoding="utf-8")
    with pytest.raises(ApiError) as excinfo:
        save_device_configs(tmp_path, _devices(1))
    assert excinfo.value.key == "device_config_invalid"
    assert target.read_text(encoding="utf-8") == '{"devices": ['


def test_update_device_config_does_not_lose_other_device_changes(tmp_path):
    from runtime.config import update_device_config

    save_device_configs(tmp_path, [
        {"device_id": "arm-01", "serial_port": "/dev/cu.OLD"},
        {"device_id": "arm-02", "serial_port": "/dev/cu.OTHER"},
    ])
    update_device_config(tmp_path, "arm-02", {"serial_port": "/dev/cu.NEW"})
    update_device_config(tmp_path, "arm-01", {"binding_note": "改备注"})
    stored = {item["device_id"]: item for item in load_device_configs(tmp_path)}
    assert stored["arm-02"]["serial_port"] == "/dev/cu.NEW", "别的设备的改动被覆盖（丢更新）"
    assert stored["arm-01"]["binding_note"] == "改备注"


def test_update_device_config_refuses_when_lock_is_held(tmp_path):
    from runtime.config import update_device_config
    from runtime.errors import ApiError

    save_device_configs(tmp_path, [{"device_id": "arm-01", "serial_port": "/dev/cu.OLD"}])
    ctx = mp.get_context("fork")
    ready = ctx.Event()
    holder = ctx.Process(target=_hold_lock, args=(str(tmp_path), 0.8, ready))
    holder.start()
    assert ready.wait(10)
    try:
        with pytest.raises(ApiError) as excinfo:
            update_device_config(tmp_path, "arm-01", {"wechat_account_id": "acc-x"}, timeout=0.15)
        assert excinfo.value.key == "device_config_busy"
    finally:
        holder.join(timeout=10)
    assert holder.exitcode == 0


def test_persist_projects_runtime_keys(tmp_path):
    entry = {key: "x" for key in STATIC_KEYS}
    entry.update({"daemon_session": "abc", "status": "ready", "camera_quality": 0.9})
    save_device_configs(tmp_path, [{"device_id": "arm-01", **entry}])
    stored = json.loads((tmp_path / "configs" / "devices.json").read_text(encoding="utf-8"))["devices"][0]
    assert not (set(stored) - set(STATIC_KEYS) - {"device_id"})


def test_non_list_devices_field_raises_instead_of_clearing(tmp_path):
    from runtime.config import update_device_config
    from runtime.errors import ApiError

    target = tmp_path / "configs" / "devices.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"devices": {"arm-01": {"device_id": "arm-01"}}}', encoding="utf-8")
    before = target.read_text(encoding="utf-8")
    with pytest.raises(ApiError) as excinfo:
        update_device_config(tmp_path, "arm-01", {"wechat_account_id": "acc-x"}, create_if_missing=True)
    assert excinfo.value.key == "device_config_invalid"
    assert target.read_text(encoding="utf-8") == before


def test_unknown_hold_blocks_rebind_and_tolerates_odd_shapes(tmp_path):
    """unknown 未核对时禁止换绑；缺键/None/空串/字符串形态都不能抛异常或误拦。"""
    import asyncio

    from runtime.audit import AuditLog
    from runtime.devices import DeviceManager
    from runtime.errors import ApiError
    from runtime.events import EventBus
    from runtime.store import Store

    def _manager(tmp_path):
        store = Store(tmp_path / "fleet.db")
        events = EventBus()
        return DeviceManager(store, AuditLog(store, events), events,
                             hardware_mode="simulation")

    def _key(manager, payload):
        """返回门禁是否命中：命中返回 device_unknown_hold，其余（含后续流程异常）返回其它值。

        这里只验证"门禁不误拦"，不验证后续换绑流程能走完（那需要完整注册的设备）；
        因此捕获全部异常，只关心 key 是否为 device_unknown_hold。
        """
        manager._devices["arm-01"] = {"device_id": "arm-01", "status": "paused",
                                      "binding_revision": 1, **payload}
        try:
            asyncio.run(manager.update_binding("arm-01", {"wechat_account_id": "acc-x"},
                                               operator_id="op", reason="t"))
        except Exception as exc:  # noqa: BLE001 - 只为区分门禁命中
            return getattr(exc, "key", type(exc).__name__)
        return None

    manager = _manager(tmp_path)
    assert _key(manager, {"unknown_hold_task_id": "task-9"}) == "device_unknown_hold"
    assert _key(manager, {"unknown_hold_task_ids": ""}) != "device_unknown_hold"
    assert _key(manager, {"unknown_hold_task_ids": [None, "", " "]}) != "device_unknown_hold"
    assert _key(manager, {"unknown_hold_task_ids": "task-x"}) == "device_unknown_hold"
    assert _key(manager, {}) != "device_unknown_hold"
