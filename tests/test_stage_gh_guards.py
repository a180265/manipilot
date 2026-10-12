"""阶段 G/H 守卫测试：真机参数未确认时，系统必须停留在仿真并拒绝真机驱动。

守卫语义（不变）：
- 确认表 all_confirmed=false 或存在非"已确认"条目时，配置加载器强制回退仿真；
- 真机驱动在任何动作调用前必须检查确认表，未确认即拒绝；
- 十台设备绑定表必须一致且唯一。

2026-08-30 起项目方提供现场文档（机械臂参数清单等），确认表已逐项确认，
本文件改用临时确认表覆盖"未确认必须回退"的守卫行为本身。
"""
from __future__ import annotations

import json

import pytest

from drivers.real_stubs import RealHardwareUnavailable, RealArmDriverStub, hardware_confirmed
from runtime.config import load_platform_config

PROJECT_ROOT = load_platform_config().root


def _write_table(root, *, all_confirmed: bool, pending_item: str | None = None):
    configs = root / "configs"
    configs.mkdir(parents=True, exist_ok=True)
    items = [{"input": "机械臂型号", "status": "已确认", "value": "BACHIN-3C-TA4"}]
    if pending_item:
        items.append({"input": pending_item, "status": "待确认", "value": None})
    table = {
        "all_confirmed": all_confirmed and not pending_item,
        "items": items,
    }
    (configs / "硬件输入确认表.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")


def test_current_table_is_fully_confirmed_with_source_documents():
    """当前确认表：全部"已确认"并引用项目方现场文档（2026-08-30）。"""
    confirmed, reason = hardware_confirmed()
    assert confirmed is True, reason
    table = json.loads((PROJECT_ROOT / "configs" / "硬件输入确认表.json").read_text(encoding="utf-8"))
    assert table.get("source_documents"), "确认表必须引用项目方提供的依据文档"


def test_unconfirmed_table_still_forces_refusal(tmp_path, monkeypatch):
    """守卫本体：确认表存在待确认项时，真机驱动必须拒绝启用。"""
    import drivers.real_stubs as real_stubs

    pending_table = tmp_path / "硬件输入确认表.json"
    pending_table.write_text(
        json.dumps({"all_confirmed": False, "items": [{"input": "机械臂型号", "status": "待确认"}]}),
        encoding="utf-8",
    )
    confirmed, reason = hardware_confirmed(pending_table)
    assert confirmed is False
    assert "确认" in reason

    monkeypatch.setattr(real_stubs, "CONFIRMATION_TABLE", pending_table)
    stub = RealArmDriverStub("arm-01")
    with pytest.raises(RealHardwareUnavailable):
        import asyncio

        asyncio.run(stub.connect())


def test_platform_falls_back_to_simulation_without_confirmation(tmp_path):
    """配置加载器守卫：确认表未全确认时 hardware_mode 强制回退仿真。"""
    _write_table(tmp_path, all_confirmed=False, pending_item="摄像头")
    (tmp_path / "configs" / "platform.json").write_text(
        json.dumps({"hardware_mode": "real"}), encoding="utf-8"
    )
    config = load_platform_config(root=tmp_path)
    assert config.hardware_mode == "simulation"
    assert "回退仿真" in config.to_dict().get("hardware_mode_note", "")


def test_confirmed_table_allows_real_mode(tmp_path):
    """确认表全量确认后，真机模式允许生效（不再强制回退）。"""
    _write_table(tmp_path, all_confirmed=True)
    (tmp_path / "configs" / "platform.json").write_text(
        json.dumps({"hardware_mode": "real"}), encoding="utf-8"
    )
    config = load_platform_config(root=tmp_path)
    assert config.hardware_mode == "real"


def test_ten_device_binding_table_consistent(tmp_path):
    """十台设备绑定表一致性：编号/槽位/摄像头/账号绑定均唯一；arm-01 为唯一真机槽位。"""
    from runtime.config import load_device_configs
    from tests.helpers import write_ten_device_root

    devices = load_device_configs(write_ten_device_root(tmp_path))
    assert len(devices) == 10
    assert len({d["device_id"] for d in devices}) == 10
    assert len({d["arm_slot"] for d in devices}) == 10
    assert len({d["camera_id"] for d in devices}) == 10
    assert len({d["wechat_account_id"] for d in devices}) == 10
    for device in devices:
        assert device["device_id"] == f"arm-{device['arm_slot']:02d}"
        assert device["camera_id"] == f"cam-{device['arm_slot']:02d}"
    # arm-01 是唯一真机槽位；驱动模式可以是 real（直连）或 daemon（串口由 arm_daemon3 独占）
    real_devices = [d for d in devices if d.get("driver_mode") in ("real", "daemon")]
    assert len(real_devices) == 1, "G 阶段只允许 arm-01 一台真机绑定"
    assert real_devices[0]["device_id"] == "arm-01"
    assert real_devices[0]["driver_mode"] == "daemon"  # 驱动桥模式为现行方案
    assert real_devices[0]["serial_port"] == "/dev/test-arm-01"
    assert real_devices[0]["camera_device_name"] == "Test Camera"
