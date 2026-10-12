"""换口重枚举（2026-09-30）：同一适配器换 USB 口后按硬件身份自动重绑，
不再误判"已移除"；真拔出（无身份匹配）仍走移除路径。"""
import asyncio
from copy import deepcopy

import pytest

from runtime.devices import DeviceManager


class FakeStore:
    def __init__(self):
        self.devices = {}

    def save_device(self, device):
        self.devices[device["device_id"]] = dict(device)

    def get_device(self, device_id):
        return dict(self.devices.get(device_id) or {})


class FakeAudit:
    def record(self, *a, **k):
        pass


class FakeEvents:
    def __init__(self):
        self.published = []

    def publish(self, kind, payload):
        self.published.append((kind, payload))


def make_manager():
    return DeviceManager(FakeStore(), FakeAudit(), FakeEvents(),
                         hardware_mode="simulation",
                         hotplug_discovery_enabled=True,
                         discovery_interval_seconds=999,
                         config_root=None)


def seed(mgr, device_id, port):
    from types import SimpleNamespace
    mgr._arms[device_id] = SimpleNamespace(close=lambda: None)
    mgr._cameras[device_id] = SimpleNamespace(close=lambda: None)
    mgr._registered_configs = [{"device_id": device_id, "serial_port": port,
                                "arm_slot": 1, "driver_mode": "daemon",
                                "camera_id": "cam-01",
                                "camera_mode": "multi_channel",
                                "camera_device_unique_id": "0x2A597523A9M7",
                                "camera_frame_url": "http://127.0.0.1:8790/cameras/cam-01/latest.jpg",
                                "camera_status_url": "http://127.0.0.1:8790/cameras/cam-01/status",
                                "daemon_addr": "127.0.0.1:8795",
                                "hardware_mode": "real"}]
    mgr._devices[device_id] = {"device_id": device_id, "status": "ready",
                               "presence_state": "present",
                               "daemon_session": "s1"}
    mgr._last_presence[device_id] = True
    mgr._last_serial_identity[device_id] = ("0x2A59", "0x7523", "A9M7ABCDEF")


def run_scan(mgr, ports):
    asyncio.get_event_loop().run_until_complete(mgr.scan_discovery(ports=ports))


def port(path, *, vid="0x2A59", pid="0x7523", serial="A9M7ABCDEF"):
    return {"path": path, "vid": vid, "pid": pid, "serial_number": serial,
            "manufacturer": "wch", "product": "USB-SERIAL",
            "fingerprint": f"fp-{path}"}


def test_reenumerated_same_hardware_rebinds_without_removal(tmp_path, monkeypatch):
    mgr = make_manager()
    monkeypatch.setattr(mgr, "_config_root", tmp_path)
    seed(mgr, "arm-01", "/dev/cu.usbserial-21110")
    # 原路径消失；未知新端口携带同一硬件身份（换 USB 口重插）
    run_scan(mgr, [port("/dev/cu.usbserial-21130")])

    cfg = mgr._registered_configs[0]
    assert cfg["serial_port"] == "/dev/cu.usbserial-21130"
    dev = mgr._devices["arm-01"]
    assert dev["presence_state"] != "removed", "同身份换口不得判为移除"
    kinds = [k for k, _ in mgr._events.published]
    assert "device.serial_reenumerated" in kinds
    assert not any(k == "device.discovered" and p.get("requires_registration")
                   for k, p in mgr._events.published)


def test_reenumerated_config_persisted_to_devices_json(tmp_path, monkeypatch):
    mgr = make_manager()
    monkeypatch.setattr(mgr, "_config_root", tmp_path)
    (tmp_path / "configs").mkdir()
    import json
    (tmp_path / "configs" / "devices.json").write_text(json.dumps({"devices": [
        {"device_id": "arm-01", "serial_port": "/dev/cu.usbserial-21110"}]}))
    seed(mgr, "arm-01", "/dev/cu.usbserial-21110")
    run_scan(mgr, [port("/dev/cu.usbserial-21130")])

    saved = json.loads((tmp_path / "configs" / "devices.json").read_text())
    assert saved["devices"][0]["serial_port"] == "/dev/cu.usbserial-21130"


def test_genuine_unplug_still_removes_device(tmp_path, monkeypatch):
    mgr = make_manager()
    monkeypatch.setattr(mgr, "_config_root", tmp_path)
    seed(mgr, "arm-01", "/dev/cu.usbserial-21110")
    # 原路径消失，且没有同身份的新端口（真拔出）
    run_scan(mgr, [])

    cfg = mgr._registered_configs[0]
    assert cfg["serial_port"] == "/dev/cu.usbserial-21110", "真拔出不改绑定"
    dev = mgr._devices["arm-01"]
    assert dev["presence_state"] == "removed"


def test_identity_recorded_when_bound_port_present(tmp_path, monkeypatch):
    mgr = make_manager()
    monkeypatch.setattr(mgr, "_config_root", tmp_path)
    seed(mgr, "arm-01", "/dev/cu.usbserial-21110")
    mgr._last_serial_identity.pop("arm-01")
    # 绑定路径在场：身份应被记录（供日后换口比对）
    run_scan(mgr, [port("/dev/cu.usbserial-21110")])
    assert mgr._last_serial_identity.get("arm-01") == ("0x2A59", "0x7523", "A9M7ABCDEF")


def test_auto_assign_usb_serial_to_first_free_slot(tmp_path, monkeypatch):
    """用户拍板 2026-09-30：新 USB 串口自动排序绑定——第一个空闲槽位
    （arm-01 优先）；非 USB 口（蓝牙/调试口）不参与；无空闲槽位才只发
    requires_registration。分配只改绑定，连接仍需调试台操作。"""
    import asyncio

    mgr = make_manager()
    monkeypatch.setattr(mgr, "_config_root", tmp_path)
    (tmp_path / "configs").mkdir()
    import json as _json
    (tmp_path / "configs" / "devices.json").write_text(_json.dumps({"devices": [
        {"device_id": "arm-01", "arm_slot": 1,
         "serial_port": "/dev/cu.usbserial-11110"},
        {"device_id": "arm-02", "arm_slot": 2,
         "serial_port": "/dev/cu.usbserial-22220"}]}))
    mgr._registered_configs = _json.loads(
        (tmp_path / "configs" / "devices.json").read_text())["devices"]
    for cfg in mgr._registered_configs:
        mgr._devices[cfg["device_id"]] = {"device_id": cfg["device_id"],
                                          "status": "ready",
                                          "presence_state": "removed"}
        mgr._last_presence[cfg["device_id"]] = False

    ports = [{"path": "/dev/cu.debug-console", "vid": None, "pid": None,
              "serial_number": None, "manufacturer": None, "product": None,
              "fingerprint": "fp-bt"},
             {"path": "/dev/cu.Bluetooth-Incoming-Port", "vid": None,
              "pid": None, "serial_number": None, "manufacturer": None,
              "product": None, "fingerprint": "fp-blk"},
             {"path": "/dev/cu.usbserial-33330", "vid": "0x2A59",
              "pid": "0x7523", "serial_number": "B2", "manufacturer": "wch",
              "product": "USB-SERIAL", "fingerprint": "fp-new-1"},
             {"path": "/dev/cu.usbmodem0001", "vid": "0x2A59",
              "pid": "0x7523", "serial_number": "B3", "manufacturer": "wch",
              "product": "USB-SERIAL", "fingerprint": "fp-new-2"}]
    asyncio.get_event_loop().run_until_complete(mgr.scan_discovery(ports=ports))

    cfgs = {c["device_id"]: c["serial_port"] for c in mgr._registered_configs}
    # 两台都空闲 → 按 arm_slot 排序：arm-01 拿路径序第一的 usbserial，arm-02 拿 usbmodem
    assert cfgs["arm-01"] == "/dev/cu.usbserial-33330"
    assert cfgs["arm-02"] == "/dev/cu.usbmodem0001"
    saved = _json.loads((tmp_path / "configs" / "devices.json").read_text())
    saved_map = {d["device_id"]: d["serial_port"] for d in saved["devices"]}
    assert saved_map == cfgs, "自动分配必须持久化到 devices.json"
    kinds = [k for k, _ in mgr._events.published]
    assert kinds.count("device.serial_auto_assigned") == 2
    # 非 USB 系统口（蓝牙/调试口）仍发一次性注册事件（不自动分配），可接受
    assigned = [p for k, p in mgr._events.published
                if k == "device.serial_auto_assigned"]
    assert sorted(p["device_id"] for p in assigned) == ["arm-01", "arm-02"]


def test_auto_assign_skips_when_all_slots_occupied(tmp_path, monkeypatch):
    import asyncio

    mgr = make_manager()
    monkeypatch.setattr(mgr, "_config_root", tmp_path)
    seed(mgr, "arm-01", "/dev/cu.usbserial-21110")  # 槽位被在场端口占用
    ports = [{"path": "/dev/cu.usbserial-21110", "vid": "0x2A59",
              "pid": "0x7523", "serial_number": "A9M7", "manufacturer": "wch",
              "product": "USB-SERIAL", "fingerprint": "fp-occup"},
             {"path": "/dev/cu.usbserial-33330", "vid": "0x2A59",
              "pid": "0x7523", "serial_number": "Z9", "manufacturer": "wch",
              "product": "USB-SERIAL", "fingerprint": "fp-new"}]
    asyncio.get_event_loop().run_until_complete(mgr.scan_discovery(ports=ports))
    assert mgr._registered_configs[0]["serial_port"] == "/dev/cu.usbserial-21110"
    assert mgr._last_presence.get("arm-01") is True
    kinds = [k for k, _ in mgr._events.published]
    assert "device.serial_auto_assigned" not in kinds
