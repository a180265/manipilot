"""阶段 A 验收测试：配置、十个虚拟设备、驱动假实现、心跳、能力注册、中文状态。

退出条件：十个设备可注册并稳定显示；状态映射测试通过。
"""
from __future__ import annotations

import asyncio

import pytest

from runtime.capabilities import CapabilityRegistry, BASE_CAPABILITIES
from runtime.config import load_device_configs, load_platform_config
from runtime.devices import DeviceManager
from runtime.errors import ApiError
from runtime.status import (
    CAMERA_QUALITY,
    DEVICE_STATUS,
    TASK_STATUS,
    UI_STATE,
    VIEW_SESSION_STATUS,
    display,
)
from runtime.store import Store
from runtime.audit import AuditLog
from runtime.events import EventBus
from tests.helpers import write_ten_device_root


def make_manager(tmp_path) -> DeviceManager:
    store = Store(tmp_path / "test.db")
    events = EventBus()
    audit = AuditLog(store, events)
    manager = DeviceManager(store, audit, events, hardware_mode="simulation")
    return manager


def load_stage_a_devices(tmp_path) -> list[dict]:
    configs = load_device_configs(write_ten_device_root(tmp_path))
    assert len(configs) == 10, "必须提供十个虚拟设备配置"
    return configs


def test_all_status_keys_have_chinese_display():
    """术语规范：所有内部键必须登记中文显示，测试同时断言键与中文。"""
    assert display(DEVICE_STATUS, "ready") == "可用"
    assert display(DEVICE_STATUS, "busy") == "忙碌"
    assert display(DEVICE_STATUS, "returning_home") == "正在归位"
    assert display(DEVICE_STATUS, "quarantined") == "隔离"
    assert display(TASK_STATUS, "confirmed") == "已发送或已完成"
    assert display(TASK_STATUS, "failed") == "明确失败"
    assert display(TASK_STATUS, "unknown") == "结果未知"
    assert display(UI_STATE, "wechat_main") == "微信主界面"
    assert display(UI_STATE, "captcha") == "验证码页"
    assert display(UI_STATE, "risk_control") == "风控页"
    assert display(VIEW_SESSION_STATUS, "valid") == "有效"
    assert display(VIEW_SESSION_STATUS, "unavailable") == "画面不可用"
    assert display(CAMERA_QUALITY, "clear") == "清晰"
    # 未登记的键必须报错，禁止静默回退英文
    with pytest.raises(KeyError):
        display(DEVICE_STATUS, "not_registered_key")


def test_ten_devices_register_and_become_ready(tmp_path):
    """退出条件：十个设备可注册并稳定显示中文状态。"""
    manager = make_manager(tmp_path)
    devices = asyncio.run(manager.register_all(load_stage_a_devices(tmp_path)))

    assert len(devices) == 10
    for device in devices:
        assert device["status"] == "ready"
        assert device["status_display"] == "可用"
        assert device["current_ui_state_display"] in UI_STATE.values()
        assert device["camera_quality_display"] == "清晰"
        assert device["last_heartbeat_at"] is None or device["last_heartbeat_at"].endswith("Z")
        assert device["binding_mode"] == "simulation"
        # 仿真槽位账号为 sim-acc-*；真机槽位（arm-01）账号待 业务 联调绑定（pending-real-acc-*）
        assert device["wechat_account_id"].startswith(("sim-acc-", "pending-real-acc-"))

    # 十台设备编号唯一、槽位唯一、摄像头唯一、仿真账号唯一
    device_ids = [d["device_id"] for d in devices]
    assert len(set(device_ids)) == 10
    assert len({d["arm_slot"] for d in devices}) == 10
    assert len({d["camera_id"] for d in devices}) == 10
    assert len({d["wechat_account_id"] for d in devices}) == 10


def test_capability_registry_bootstrap(tmp_path):
    """能力注册：平台只内置与硬件无关的基础能力，全部带中文名称。"""
    store = Store(tmp_path / "test.db")
    registry = CapabilityRegistry(store)
    registry.bootstrap()
    capabilities = registry.all()
    ids = {c["capability_id"] for c in capabilities}
    for required in ["arm.home", "arm.move_preset", "arm.click", "arm.stop", "camera.capture", "camera.confirm_view", "input.type_text", "camera.live_view"]:
        assert required in ids
    for capability in capabilities:
        assert capability["name_display"], capability
        assert capability["risk_level"] in ("低", "中", "高")
        assert capability["source"] == "platform"


def test_fake_arm_fault_injection(tmp_path):
    """fake 机械臂：错误必须是结构化类别 + 中文说明。"""
    from drivers import FakeArmDriver
    from drivers.arm import DriverError

    async def scenario():
        arm = FakeArmDriver("arm-01")
        await arm.connect()
        state = await arm.read_state()
        assert state["connected"] and state["position"] == "home"
        await arm.move_preset("pos-a")
        assert (await arm.read_state())["position"] == "pos-a"
        await arm.go_home()
        assert (await arm.read_state())["position"] == "home"

        arm.faults["fail_move_preset"] = True
        with pytest.raises(DriverError) as exc_info:
            await arm.move_preset("pos-b")
        assert exc_info.value.category == "driver_motion_failed"
        assert exc_info.value.category_display == "运动失败"

        arm.faults.clear()
        arm.faults["disconnect_after"] = True
        await arm.move_preset("pos-c")
        assert not (await arm.read_state())["connected"]
        with pytest.raises(DriverError) as exc_info:
            await arm.move_preset("pos-d")
        assert exc_info.value.category == "driver_disconnected"

        await arm.estop()
        assert (await arm.read_state())["estop_latched"]
        arm.reconnect()
        with pytest.raises(DriverError) as exc_info:
            await arm.move_preset("pos-e")
        assert exc_info.value.category == "driver_estop_latched"
        arm.reset_estop("操作员甲")
        await arm.go_home()

    asyncio.run(scenario())


def test_fake_camera_states_and_quality(tmp_path):
    """fake 摄像头：遮挡/过曝/冻结可识别；无法确认时返回未确认原因。"""
    from drivers import FakeCameraDriver

    async def scenario():
        camera = FakeCameraDriver("cam-01", ui_state="wechat_main")
        await camera.open()
        confirm = await camera.confirm_main_view("wechat_main")
        assert confirm["confirmed"] is True

        camera.set_quality("blocked")
        confirm = await camera.confirm_main_view("wechat_main")
        assert confirm["confirmed"] is False
        assert "画面质量异常" in confirm["reason"]

        camera.set_quality("clear")
        camera.set_ui_state("captcha")
        confirm = await camera.confirm_main_view("wechat_main")
        assert confirm["confirmed"] is False
        assert confirm["ui_state_display"] == "验证码页"

        camera.set_ui_state("wechat_main", confidence=0.4)
        confirm = await camera.confirm_main_view("wechat_main")
        assert confirm["confirmed"] is False
        assert "置信度" in confirm["reason"]

        # 冻结：两帧哈希一致
        camera.set_ui_state("wechat_main", confidence=0.97)
        camera.set_quality("frozen")
        frame1 = camera.capture_sync()
        frame2 = camera.capture_sync()
        assert frame1["sha256"] == frame2["sha256"]
        quality = await camera.check_quality()
        assert quality["quality"] == "frozen"
        assert quality["quality_display"] == "画面冻结"

    asyncio.run(scenario())


def test_fake_camera_png_is_real_image(tmp_path):
    """fake 摄像头产出真实 PNG 文件，可作为证据保存。"""
    from drivers import FakeCameraDriver

    camera = FakeCameraDriver("cam-01")
    frame = camera.capture_sync()
    assert frame["image"].startswith(b"\x89PNG\r\n\x1a\n")
    assert frame["width"] == 64 and frame["height"] == 48
    assert len(frame["sha256"]) == 64


def test_device_status_transitions_enforced(tmp_path):
    """状态机：不允许的状态迁移必须被拒绝。"""
    manager = make_manager(tmp_path)
    (device,) = asyncio.run(manager.register_all([load_stage_a_devices(tmp_path)[0]]))
    assert device["status"] == "ready"

    manager.pause("arm-01", "操作员暂停")
    assert manager.get("arm-01")["status_display"] == "暂停"
    manager.resume("arm-01", "操作员恢复")
    assert manager.get("arm-01")["status_display"] == "可用"

    # 可用 -> 恢复中 不允许
    with pytest.raises(ApiError) as exc_info:
        manager._set_status("arm-01", "recovering", "非法迁移")
    assert exc_info.value.key == "state_transition_invalid"

    manager.quarantine("arm-01", "疑似异常，人工隔离")
    assert manager.get("arm-01")["status_display"] == "隔离"
    manager.unquarantine("arm-01", "人工确认解除")
    assert manager.get("arm-01")["status_display"] == "恢复中"


def test_home_check_and_manual_path(tmp_path):
    """归位检查：归位失败时设备不能领取新任务，需人工路径。"""
    manager = make_manager(tmp_path)
    asyncio.run(manager.register_all([load_stage_a_devices(tmp_path)[0]]))
    manager.driver("arm-01").faults["fail_home"] = True

    result = asyncio.run(manager.home_check("arm-01"))
    assert result["home_ok"] is False
    taskable, reason = manager.is_taskable("arm-01")
    assert taskable is False
    assert reason == "home_not_confirmed"

    # 人工确认归位后恢复
    manager.mark_home_ok("arm-01", "操作员甲", "现场人工确认机械臂已回安全位置")
    taskable, _ = manager.is_taskable("arm-01")
    assert taskable is True


def test_real_mode_requires_confirmation(tmp_path, monkeypatch):
    """确认表未全确认时，真机槽位注册必须被拒绝（hardware_unconfirmed）。"""
    import json

    import drivers.real_stubs as real_stubs

    pending_table = tmp_path / "硬件输入确认表.json"
    pending_table.write_text(
        json.dumps({"all_confirmed": False, "items": [{"input": "机械臂型号", "status": "待确认"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(real_stubs, "CONFIRMATION_TABLE", pending_table)
    store = Store(tmp_path / "test.db")
    events = EventBus()
    audit = AuditLog(store, events)
    manager = DeviceManager(store, audit, events, hardware_mode="real")
    with pytest.raises(ApiError) as exc_info:
        manager.register_device(load_stage_a_devices(tmp_path)[0])
    assert exc_info.value.key == "hardware_unconfirmed"


def test_platform_config_falls_back_to_simulation(tmp_path):
    """配置回退：真机模式但确认表未确认时，强制回退仿真。"""
    import json

    root = tmp_path
    (root / "configs").mkdir()
    platform = json.loads((load_platform_config().root / "configs" / "platform.json").read_text(encoding="utf-8"))
    platform["hardware_mode"] = "real"
    (root / "configs" / "platform.json").write_text(json.dumps(platform, ensure_ascii=False), encoding="utf-8")
    config = load_platform_config(root)
    assert config.hardware_mode == "simulation"
    assert "仿真" in config.get("hardware_mode_note")
