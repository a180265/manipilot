"""C 组验收测试：设备菜单绑定 API、一对一冲突、热插拔租约撤销与配置白名单。

覆盖：
- 十设备绑定表加载校验（编号/槽位/串口/daemon/摄像头/输入法/微信账号无重复）；
- 废弃配置文件不被启动流程读取；
- 绑定查看/变更/禁用/恢复 API：操作人、中文原因、二次确认、版本递增、审计、
  待校准门禁、写回绑定表；
- 重复摄像头/串口/daemon/输入法/微信账号被后端拒绝（中文冲突）；
- 拔出/重绑定撤销租约；attach 绑定变化递增 binding_revision；
- 设备管理器重启后重新自检，旧心跳不冒充实时。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from fastapi.testclient import TestClient

from api.app import create_app
from runtime.audit import AuditLog
from runtime.config import (PlatformConfig, load_device_configs,
                            save_device_configs, validate_device_configs)
from runtime.devices import DeviceManager
from runtime.errors import ApiError
from runtime.events import EventBus
from runtime.runtime import ArmFleetRuntime
from runtime.store import Store


def make_runtime(tmp_path, devices=None):
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
        "hotplug_discovery_enabled": False,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.device_configs = [] if devices is None else devices
    return runtime, app


def _op(reason="C 组联调绑定变更", *, dangerous=False, operator="操作员甲"):
    body = {"operator_id": operator, "role": "operator", "reason": reason,
            "correlation_id": "corr-c-1", "client_request_id": f"req-c-{reason}-{dangerous}"}
    if dangerous:
        body["confirmation_token"] = "确认执行"
    return body


def test_device_config_validation_rejects_duplicates(tmp_path):
    """验收 1：绑定表一对一校验，重复字段在加载阶段被拒绝（中文冲突）。"""
    devices = [
        {"device_id": "arm-01", "arm_slot": 1, "camera_id": "cam-01", "wechat_account_id": "acc-1"},
        {"device_id": "arm-02", "arm_slot": 2, "camera_id": "cam-01", "wechat_account_id": "acc-2"},
    ]
    try:
        validate_device_configs(devices)
        raise AssertionError("重复 camera_id 必须被拒绝")
    except ApiError as exc:
        assert exc.key == "device_config_conflict"
        assert "摄像头编号" in exc.detail and "cam-01" in exc.detail
    for field, value, label in (("serial_port", "/dev/cu.a", "串口"),
                                ("daemon_addr", "127.0.0.1:8795", "daemon"),
                                ("ime_device_id", "ime-1", "输入法"),
                                ("wechat_account_id", "acc-1", "微信账号"),
                                ("camera_device_unique_id", "uvc-1", "物理摄像头唯一标识"),
                                ("arm_slot", 3, "槽位"),
                                ("device_id", "arm-01", "编号")):
        broken = [
            {"device_id": "arm-01", "arm_slot": 1},
            {"device_id": "arm-02", "arm_slot": 3, field: value},
        ]
        broken[0][field] = value
        try:
            validate_device_configs(broken)
            raise AssertionError(f"重复 {field} 必须被拒绝")
        except ApiError as exc:
            assert exc.key == "device_config_conflict", field


def test_real_multichannel_camera_url_must_match_camera_id(tmp_path):
    from runtime.config import validate_device_configs

    bad = [{
        "device_id": "arm-01", "driver_mode": "daemon", "binding_mode": "real",
        "camera_id": "cam-01", "camera_mode": "multi_channel",
        "camera_frame_url": "http://127.0.0.1:8790/cameras/cam-02/latest.jpg",
    }]
    with pytest.raises(ApiError, match="未指向本设备"):
        validate_device_configs(bad)


def test_config_loader_ignores_deprecated_files(tmp_path):
    """验收 10：启动只读取白名单 devices.json，同目录旧配置不被误读。"""
    (tmp_path / "configs").mkdir(parents=True)
    (tmp_path / "configs" / "devices.json").write_text(
        json.dumps({"devices": [{"device_id": "arm-live", "arm_slot": 1}]}, ensure_ascii=False),
        encoding="utf-8")
    # 伪造的废弃/备份绑定表：如果被误读会因 device_id 冲突导致加载失败
    (tmp_path / "configs" / "devices.old.json").write_text(
        json.dumps({"devices": [{"device_id": "arm-live", "arm_slot": 2}]}, ensure_ascii=False),
        encoding="utf-8")
    (tmp_path / "configs" / "devices.backup.json").write_text("{}", encoding="utf-8")
    devices = load_device_configs(tmp_path)
    assert [d["device_id"] for d in devices] == ["arm-live"]
    assert devices[0]["arm_slot"] == 1, "废弃文件不得覆盖白名单绑定表"


def test_binding_view_and_update_via_api(tmp_path):
    """验收 3/5：绑定查看/变更 API —— 操作人、二次确认、版本递增、审计、待校准。"""
    runtime, app = make_runtime(tmp_path, devices=[
        {"device_id": "arm-c1", "arm_slot": 31, "camera_id": "cam-c1",
         "wechat_account_id": "wx-c1", "ime_device_id": "ime-c1", "sim_calibrated": True}])
    with TestClient(app) as client:
        # 绑定事实视图：内部键 + 中文说明
        view = client.get("/v1/devices/arm-c1/binding").json()
        assert view["binding"]["camera_id"] == "cam-c1"
        assert view["binding_revision"] == 1
        assert "摄像头编号" in view["field_descriptions"]["camera_id"]
        assert view["recent_audit"] is not None

        # 缺少二次确认令牌 → 拒绝
        resp = client.put("/v1/devices/arm-c1/binding",
                          json={**_op(), "ime_device_id": "ime-new"})
        assert resp.status_code == 422 or resp.status_code == 400, resp.text
        # 缺少操作原因 → 拒绝
        resp = client.put("/v1/devices/arm-c1/binding",
                          json={**_op(reason="", dangerous=True), "ime_device_id": "ime-new"})
        assert resp.status_code == 400, resp.text

        # 合法变更：版本递增 + 待校准 + 设备暂停 + 审计 + 写回绑定表
        resp = client.put("/v1/devices/arm-c1/binding",
                          json={**_op(reason="binding checked", dangerous=True), "ime_device_id": "ime-c1-new",
                                "camera_id": "cam-c1-new"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["binding_revision"] == 2
        assert body["binding"]["ime_device_id"] == "ime-c1-new"
        assert body["binding"]["camera_id"] == "cam-c1-new"
        assert body["calibration_status"] == "due"
        device = client.get("/v1/devices/arm-c1").json()
        assert device["status"] == "paused"
        assert device["binding_revision"] == 2
        assert "device_binding_disabled" not in device["eligibility"]["blocking_checks"]
        assert "calibration_due" in device["eligibility"]["blocking_checks"]

        # 审计包含操作人与中文动作
        audit = [e for e in runtime.audit.list_events(limit=20, device_id="arm-c1")
                 if e["action"] == "device_binding_update"]
        assert audit and audit[0]["operator_id"] == "操作员甲"
        assert audit[0]["reason"] == "binding checked"
        assert audit[0]["action_display"] == "设备绑定变更"

        # 幂等：同一 client_request_id 重复提交返回原结果，不再次递增
        resp2 = client.put("/v1/devices/arm-c1/binding",
                           json={**_op(reason="binding checked", dangerous=True), "ime_device_id": "ime-c1-new",
                                 "camera_id": "cam-c1-new"})
        assert resp2.json()["binding_revision"] == 2

        # 绑定表已由后端写回 configs/devices.json
        saved = json.loads((tmp_path / "configs" / "devices.json").read_text(encoding="utf-8"))
        stored = next(d for d in saved["devices"] if d["device_id"] == "arm-c1")
        assert stored["ime_device_id"] == "ime-c1-new"


def test_binding_options_resolve_server_registered_resources(tmp_path):
    """设备菜单只提交资源编号，真实地址由服务端绑定表解析。"""
    runtime, app = make_runtime(tmp_path, devices=[
        {"device_id": "arm-res-1", "arm_slot": 34, "camera_id": "cam-res-1",
         "camera_frame_url": "http://camera-a/cameras/cam-res-1/latest.jpg",
         "camera_status_url": "http://camera-a/cameras/cam-res-1/status",
         "daemon_addr": "127.0.0.1:8895", "serial_port": "/dev/cu.res1",
         "wechat_account_id": "wx-res-1", "sim_calibrated": True},
        {"device_id": "arm-res-2", "arm_slot": 35, "camera_id": "cam-res-2",
         "camera_frame_url": "http://camera-b/cameras/cam-res-2/latest.jpg",
         "camera_status_url": "http://camera-b/cameras/cam-res-2/status",
         "daemon_addr": "127.0.0.1:8896", "serial_port": "/dev/cu.res2",
         "wechat_account_id": "wx-res-2", "sim_calibrated": True},
    ])
    with TestClient(app) as client:
        options = client.get("/v1/devices/arm-res-1/binding/options")
        assert options.status_code == 200, options.text
        body = options.json()
        assert any(item["resource_id"] == "camera:cam-res-2" for item in body["camera_options"])
        assert any(item["resource_id"] == "daemon:arm-res-2" for item in body["daemon_options"])
        assert any(item["resource_id"] == "serial:arm-res-2" for item in body["serial_options"])
        response = client.put(
            "/v1/devices/arm-res-1/binding",
            json={**_op("换绑已登记资源", dangerous=True),
                  "camera_binding_id": "camera:cam-res-2"},
        )
        # 目标摄像头已被 arm-res-2 占用，服务端必须拒绝，而不是接受前端伪造地址。
        assert response.status_code == 409, response.text


def test_camera_id_binding_change_rebuilds_camera_driver(tmp_path):
    """摄像头通道变更必须重建驱动，不能只更新设备镜像。"""
    runtime, app = make_runtime(tmp_path, devices=[
        {"device_id": "arm-c1", "arm_slot": 32, "camera_id": "cam-c1",
         "wechat_account_id": "wx-c1", "sim_calibrated": True}])
    with TestClient(app) as client:
        old_camera = runtime.devices._cameras["arm-c1"]
        response = client.put(
            "/v1/devices/arm-c1/binding",
            json={**_op("更换摄像头通道", dangerous=True), "camera_id": "cam-c9"},
        )
        assert response.status_code == 200, response.text
        assert runtime.devices._cameras["arm-c1"] is not old_camera
        assert runtime.devices._cameras["arm-c1"].device_id == "cam-c9"
        assert response.json()["binding"]["camera_id"] == "cam-c9"


def test_camera_route_url_change_invalidates_binding_and_recalibration(tmp_path):
    """同一 camera_id 更换取流/状态地址也必须递增版本并进入待校准。"""
    runtime, app = make_runtime(tmp_path, devices=[
        {"device_id": "arm-route", "arm_slot": 33, "camera_id": "cam-route",
         "camera_frame_url": "http://camera-a/cameras/cam-route/latest.jpg",
         "camera_status_url": "http://camera-a/cameras/cam-route/status",
         "wechat_account_id": "wx-route", "sim_calibrated": True},
    ])
    with TestClient(app) as client:
        before = client.get("/v1/devices/arm-route/binding").json()
        response = client.put(
            "/v1/devices/arm-route/binding",
            json={**_op("切换摄像头取流桥", dangerous=True),
                  "camera_frame_url": "http://camera-b/cameras/cam-route/latest.jpg",
                  "camera_status_url": "http://camera-b/cameras/cam-route/status"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["binding_revision"] == before["binding_revision"] + 1
        assert body["binding"]["camera_frame_url"].startswith("http://camera-b/")
        assert body["binding"]["camera_status_url"].startswith("http://camera-b/")
        assert body["calibration_status"] == "due"
        assert client.get("/v1/devices/arm-route").json()["status"] == "paused"

def test_binding_conflict_rejected_by_backend(tmp_path):
    """验收 6：重复摄像头/串口/daemon/输入法/微信账号被后端拒绝并给中文冲突。"""
    runtime, app = make_runtime(tmp_path, devices=[
        {"device_id": "arm-c1", "arm_slot": 41, "camera_id": "cam-c1",
         "wechat_account_id": "wx-c1", "sim_calibrated": True},
        # arm-c2 预先占用串口/daemon/输入法，验证这些字段同样一对一
        {"device_id": "arm-c2", "arm_slot": 42, "camera_id": "cam-c2",
         "wechat_account_id": "wx-c2", "serial_port": "/dev/cu.same",
         "daemon_addr": "127.0.0.1:8796", "ime_device_id": "ime-shared",
         "sim_calibrated": True},
    ])
    with TestClient(app) as client:
        for field, value, label in (("camera_id", "cam-c2", "摄像头"),
                                    ("wechat_account_id", "wx-c2", "微信账号"),
                                    ("arm_slot", 42, "槽位"),
                                    ("serial_port", "/dev/cu.same", "串口"),
                                    ("daemon_addr", "127.0.0.1:8796", "daemon"),
                                    ("ime_device_id", "ime-shared", "输入法")):
            resp = client.put("/v1/devices/arm-c1/binding",
                              json={**_op(dangerous=True), field: value})
            assert resp.status_code == 409, f"{field} 冲突应被拒绝：{resp.text}"
            assert "占用" in resp.text or label in resp.text, field
        # 冲突请求不改变绑定版本，也不改变设备状态（校验先于暂停）
        assert client.get("/v1/devices/arm-c1/binding").json()["binding_revision"] == 1
        assert client.get("/v1/devices/arm-c1").json()["status"] == "ready"


def test_binding_disable_and_enable(tmp_path):
    """验收：禁用绑定不能接任务；恢复后进入待校准门禁。"""
    runtime, app = make_runtime(tmp_path, devices=[
        {"device_id": "arm-c1", "arm_slot": 51, "camera_id": "cam-c1",
         "wechat_account_id": "wx-c1", "sim_calibrated": True}])
    with TestClient(app) as client:
        resp = client.post("/v1/devices/arm-c1/binding/disable", json=_op("旧摄像头停用检修"))
        assert resp.status_code == 200
        device = client.get("/v1/devices/arm-c1").json()
        assert device["binding_enabled"] is False
        assert "device_binding_disabled" in device["eligibility"]["blocking_checks"]
        assert device["status"] == "paused"

        resp = client.post("/v1/devices/arm-c1/binding/enable", json=_op("检修完成恢复绑定"))
        assert resp.status_code == 200
        view = client.get("/v1/devices/arm-c1/binding").json()
        assert view["binding_enabled"] is True
        assert view["calibration_status"] == "due", "恢复后必须重新校准才能托管"


def test_remove_releases_active_lease(tmp_path):
    """验收 3：设备拔出释放活动租约、撤销资格。"""
    async def scenario():
        store = Store(tmp_path / "fleet.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")
        from runtime.leases import LeaseManager

        leases = LeaseManager(store, manager)
        manager.set_lease_manager(leases)
        await manager.register_and_selfcheck(
            {"device_id": "arm-c1", "arm_slot": 61, "camera_id": "cam-c1",
             "wechat_account_id": "wx-c1", "sim_calibrated": True})
        lease = leases.acquire("arm-c1", "task-lease-1")
        assert leases.get("arm-c1")["lease_id"] == lease["lease_id"]
        await manager.remove_device("arm-c1", "USB 拔出")
        assert leases.get("arm-c1") is None, "拔出必须撤销活动租约"
        assert manager.get("arm-c1")["presence_state"] == "removed"
        assert manager.eligibility("arm-c1", require_input=True)["can_accept_task"] is False

    asyncio.run(scenario())


def test_attach_bumps_binding_revision_and_revokes_calibration(tmp_path):
    """验收 4/5：重绑定递增 binding_revision、待校准并撤销托管。"""
    async def scenario():
        store = Store(tmp_path / "fleet.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")
        await manager.register_and_selfcheck(
            {"device_id": "arm-c1", "arm_slot": 71, "camera_id": "cam-c1",
             "wechat_account_id": "wx-c1", "sim_calibrated": True})
        assert manager.get("arm-c1")["binding_revision"] == 1
        # 相同绑定重挂载：版本不变
        await manager.attach_device({"device_id": "arm-c1"})
        assert manager.get("arm-c1")["binding_revision"] == 1
        # 绑定变化重挂载：版本递增 + 待校准 + 未托管
        device = await manager.attach_device({"device_id": "arm-c1", "camera_id": "cam-c9"})
        assert device["binding_revision"] == 2
        assert device["calibration_status"] == "due"
        assert device["device_mode"] == "unmanaged"
        assert manager.eligibility("arm-c1", require_input=True)["can_accept_task"] is False

    asyncio.run(scenario())


def test_restart_reregisters_and_does_not_fake_live_heartbeat(tmp_path):
    """验收 9：设备管理器重启后重新注册自检，旧心跳不得冒充实时。"""
    async def scenario():
        config = PlatformConfig({
            "db_path": str(tmp_path / "fleet.db"),
            "evidence_dir": str(tmp_path / "evidence"),
            "plugin_dir": str(tmp_path / "plugins"),
            "hardware_mode": "simulation",
            "hotplug_discovery_enabled": False,
        }, root=tmp_path)
        first = ArmFleetRuntime(config)
        await first.startup(device_configs=[
            {"device_id": "arm-c1", "arm_slot": 81, "camera_id": "cam-c1",
             "wechat_account_id": "wx-c1", "sim_calibrated": True}])
        await first.shutdown()

        second = ArmFleetRuntime(PlatformConfig({
            "db_path": str(tmp_path / "fleet.db"),
            "evidence_dir": str(tmp_path / "evidence"),
            "plugin_dir": str(tmp_path / "plugins"),
            "hardware_mode": "simulation",
            "hotplug_discovery_enabled": False,
        }, root=tmp_path))
        await second.startup()
        try:
            device = second.devices.get("arm-c1")
            assert device["status"] == "ready", "重启后必须重新自检并发布可用状态"
            # 心跳时间必须晚于重启后的注册时间（新进程实时心跳），旧心跳不得冒充实时
            assert (device.get("last_heartbeat_at") or device["registered_at"]) >= device["registered_at"]
            # 旧租约全部失效
            assert second.leases.get("arm-c1") is None
        finally:
            await second.shutdown()

    asyncio.run(scenario())


def test_save_device_configs_roundtrip(tmp_path):
    """绑定表写回：保留顶层注释字段并保持一对一约束。"""
    (tmp_path / "configs").mkdir(parents=True)
    (tmp_path / "configs" / "devices.json").write_text(
        json.dumps({"comment": "测试绑定表", "binding_mode": "mixed", "devices": []}, ensure_ascii=False),
        encoding="utf-8")
    save_device_configs(tmp_path, [{"device_id": "arm-x", "arm_slot": 1}])
    payload = json.loads((tmp_path / "configs" / "devices.json").read_text(encoding="utf-8"))
    assert payload["comment"] == "测试绑定表"
    assert payload["devices"][0]["device_id"] == "arm-x"


def test_heavy_binding_change_reattaches_and_rechecks(tmp_path):
    """daemon/串口等重字段变更走挂载路径：重建自检、版本递增、待校准。"""
    runtime, app = make_runtime(tmp_path, devices=[
        {"device_id": "arm-c1", "arm_slot": 91, "camera_id": "cam-c1",
         "wechat_account_id": "wx-c1", "sim_calibrated": True}])
    with TestClient(app) as client:
        resp = client.put("/v1/devices/arm-c1/binding",
                          json={**_op("更换串口到新机械臂", dangerous=True),
                                "serial_port": "/dev/cu.usbserial-newport"})
        assert resp.status_code == 200, resp.text
        view = resp.json()
        assert view["binding"]["serial_port"] == "/dev/cu.usbserial-newport"
        assert view["binding_revision"] == 2
        assert view["calibration_status"] == "due"
        # 挂载路径完成自检，但绑定变化仍保持暂停，直到重新校准放行
        device = client.get("/v1/devices/arm-c1").json()
        assert device["status"] == "paused"
        assert "calibration_due" in device["eligibility"]["blocking_checks"]


def test_arm_slot_type_mismatch_still_detected(tmp_path):
    """回归：arm_slot 数字与字符串形式不得绕过重复检测。"""
    async def scenario():
        store = Store(tmp_path / "fleet.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")
        await manager.register_and_selfcheck(
            {"device_id": "arm-c1", "arm_slot": 42, "camera_id": "cam-c1",
             "wechat_account_id": "wx-c1", "sim_calibrated": True})
        try:
            await manager.register_and_selfcheck(
                {"device_id": "arm-c2", "arm_slot": "42", "camera_id": "cam-c2",
                 "wechat_account_id": "wx-c2", "sim_calibrated": True})
            raise AssertionError("槽位 42/'42' 应视为重复")
        except ApiError as exc:
            assert exc.key == "device_binding_conflict"
            assert "机械臂槽位" in exc.detail

    asyncio.run(scenario())


def test_discovery_records_hardware_fingerprint(tmp_path):
    """验收 3：绑定视图携带硬件指纹（发现扫描时从串口指纹解析）。"""
    async def scenario():
        store = Store(tmp_path / "fleet.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")
        await manager.register_and_selfcheck(
            {"device_id": "arm-c1", "arm_slot": 92, "camera_id": "cam-c1",
             "wechat_account_id": "wx-c1", "serial_port": "/dev/cu.usb-fp",
             "sim_calibrated": True})
        ports = [{"path": "/dev/cu.usb-fp", "fingerprint": "fp-abc123",
                  "vid": 1, "pid": 2, "serial_number": "SN1",
                  "manufacturer": "m", "product": "p"}]
        await manager.scan_discovery(ports=ports)
        view = manager.binding_view("arm-c1")
        assert view["hardware_fingerprint"] == "fp-abc123", "绑定视图必须携带硬件指纹"

    asyncio.run(scenario())


def test_error_keys_cover_device_gating():
    """跨界（A↔C）：资格拒绝必须是 4xx，WX 才会判“明确失败”而不是“结果未知”。"""
    from runtime.errors import ApiError

    for key, expected_http in (("device_not_eligible", 409), ("device_binding_disabled", 409)):
        error = ApiError(key, "测试")
        assert error.http == expected_http, key
        assert error.to_payload()["error"]["title"]


def test_binding_update_rejected_while_device_busy(tmp_path):
    """跨界：任务执行中禁止变更绑定（不释放执行中任务的租约）。"""
    async def scenario():
        store = Store(tmp_path / "fleet.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")
        from runtime.leases import LeaseManager

        leases = LeaseManager(store, manager)
        manager.set_lease_manager(leases)
        await manager.register_and_selfcheck(
            {"device_id": "arm-c1", "arm_slot": 94, "camera_id": "cam-c1",
             "wechat_account_id": "wx-c1", "sim_calibrated": True})
        lease = leases.acquire("arm-c1", "task-busy-1")
        manager._set_status("arm-c1", "busy", f"执行任务 task-busy-1")
        try:
            await manager.update_binding("arm-c1", {"wechat_account_id": "wx-new"},
                                         operator_id="操作员甲", reason="执行中尝试改绑定")
            raise AssertionError("忙碌设备必须拒绝绑定变更")
        except ApiError as exc:
            assert exc.key == "device_busy"
            assert "禁止变更绑定" in exc.detail
        assert leases.get("arm-c1")["lease_id"] == lease["lease_id"], "执行中租约不得被绑定变更撤销"
        manager.resume("arm-c1", "测试恢复")

    asyncio.run(scenario())


def test_attach_failure_marks_device_offline(tmp_path):
    """跨界（C↔D）：重挂载配置无效（如清空多路取流地址）→ 设备显式离线并告警。"""
    async def scenario():
        store = Store(tmp_path / "fleet.db")
        events = EventBus()
        manager = DeviceManager(store, AuditLog(store, events), events, hardware_mode="simulation")
        await manager.register_and_selfcheck(
            {"device_id": "arm-c1", "arm_slot": 95, "camera_id": "cam-c1",
             "wechat_account_id": "wx-c1", "sim_calibrated": True})
        original = manager.register_and_selfcheck

        async def broken(cfg, **kwargs):
            raise ApiError("camera_config_invalid", "设备 arm-c1 缺少 camera_frame_url")

        manager.register_and_selfcheck = broken
        try:
            with pytest.raises(ApiError):
                await manager.attach_device({"device_id": "arm-c1", "camera_frame_url": None})
        finally:
            manager.register_and_selfcheck = original
        device = manager.get("arm-c1")
        assert device["status"] == "offline", "挂载失败必须显式离线，不得停留半挂载状态"
        assert "重挂载失败" in (device.get("status_reason") or "")

    asyncio.run(scenario())


def test_tasks_on_same_device_execute_serially(tmp_path):
    """验收 8：单台设备任务串行（执行区间不相交），不并发执行。"""
    from runtime.util import parse_iso
    from tests.helpers import make_device_configs, make_runtime as helper_make_runtime, probe_payload

    async def scenario():
        runtime = helper_make_runtime(tmp_path)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            step = {"action_display": "串行探针步骤", "is_action": True, "sleep": 0.2}
            submitted = [runtime.submit_task(
                probe_payload([dict(step)], key=f"serial-{index}"),
                source="console",
                context={"operator_id": "操作员甲", "reason": "同设备串行验收"},
            ) for index in range(3)]
            finished = []
            for task in submitted:
                for _ in range(300):
                    current = runtime.get_task(task["task_id"])
                    if current["status"] in ("confirmed", "failed", "unknown", "cancelled"):
                        break
                    await asyncio.sleep(0.02)
                assert current["status"] == "confirmed", current.get("failure_reason")
                finished.append(current)
            spans = sorted(
                (parse_iso(t["started_at"]), parse_iso(t["finished_at"])) for t in finished)
            for (_, first_end), (second_start, _) in zip(spans, spans[1:]):
                assert second_start >= first_end, "同一设备的任务执行区间不得重叠"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
