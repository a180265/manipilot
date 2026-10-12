"""P08 验收（2026-10-03 规格 §4.8.7/§4.8.8）：公网写入口走 InputControl 编排 +
人工核对出口。

- I31/I32：settings_commit_pending / input_record_invalid 类问题不可勾选清除；
- I35/I36：核对解除列明 open 问题（resolved 保留原审计），同编号重放=原结果、
  编号改内容=冲突；
- I37：核对不改 enabled、不改 unknown 事实；
- I49：expected_recovery_revision 不匹配 → 409；
- I50：设备仍有任务租约 → 409 占用拒绝；
- I55：GET input_recovery_required/recovery_revision 反映 guard 真实状态；
- 崩溃窗口：PUT 走编排后 prepared 意图在公网入口同样可对账完成。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from fastapi.testclient import TestClient

from api.app import create_app
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from runtime.store import Store
from tests.helpers import make_device_configs


@pytest.fixture()
def client(tmp_path):
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "real",
        "daemon_supervisor_enabled": False,
        "hotplug_discovery_enabled": False,
        "screen_ime_enabled": False,
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.runtime = runtime
    app.state.device_configs = make_device_configs(1)
    settings_root = tmp_path / "data"
    runtime.devices.settings_data_root = lambda device_id: settings_root
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-1", "online": True, "keyboardActive": True}]
    with TestClient(app) as c:
        runtime.devices._devices["arm-01"].update({
            "status": "online", "calibration_status": "validated",
            "ime_device_id": "k40-1", "ime_keyboard_active": True,
            "daemon_session": "sess-1"})
        yield c, runtime, settings_root


def _op(**extra):
    base = {"contract_version": "device-input-settings.v1",
            "operator_id": "操作员甲", "role": "operator", "reason": "P08",
            "correlation_id": "corr", "client_request_id": "req-1"}
    base.update(extra)
    return base


def _review_body(**extra):
    base = {"contract_version": "input-recovery-review.v1",
            "expected_recovery_revision": 1,
            "ime_device_id": "k40-1",
            "issue_ids": [], "candidate_removed": True, "editor_checked": True,
            "operator_id": "操作员甲", "role": "operator", "reason": "已核对现场",
            "correlation_id": "corr-rev", "client_request_id": "rev-1"}
    base.update(extra)
    return base


def _open_issue(runtime, code="ime_cancel_pending", detail=None):
    runtime.input_control.ensure_guard("arm-01")
    record = runtime.input_control.open_guard_issue(
        "arm-01", code=code, detail=detail)
    issue = record["issues"][-1]
    return issue["issue_id"], record["revision"]


def test_put_goes_through_control_orchestration_crash_window(tmp_path, client):
    """PUT 经 InputControl.write_settings：崩溃窗口留下的 prepared 意图
    可由同编号重放对账完成（公网入口全链生效）。"""
    c, runtime, root = client
    # 直接制造崩溃窗口：prepared 意图（关闭）+ 文件未落盘
    from runtime import input_settings as iset
    prepared = iset.prepare_update(
        "arm-01", data_root=root, enabled=False, ime_device_id=None,
        expected_revision=0, operator_id="操作员甲", reason="关",
        client_request_id="crash-1", calibration_ready=False)
    entry = prepared["stored"]["requests"]["crash-1"]
    intent = runtime.input_control.build_settings_write(
        device_id="arm-01", client_request_id="crash-1",
        call_context={"origin": "human_put", "operator_id": "a", "role": "operator",
                      "operation_id": "input_settings_update"},
        normalized_request={"enabled": False, "ime_device_id": None,
                            "expected_revision": 0,
                            "client_request_id": "crash-1"},
        before=entry["before"], after=entry["after"],
        proposed_file=prepared["stored"],
        before_file_digest=prepared["before_file_digest"])
    runtime.input_control.insert_settings_write(intent)
    # 异编号新请求被 prepared 意图阻断（§4.8.3-1）
    r = c.put("/v1/devices/arm-01/input-settings", json={
        **_op(client_request_id="other"), "enabled": True,
        "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r.status_code == 409
    # 同编号重放：对账（before+关闭）→ 完成已授权关闭
    r2 = c.put("/v1/devices/arm-01/input-settings", json={
        **_op(client_request_id="crash-1"), "enabled": False,
        "expected_revision": 0})
    assert r2.status_code == 200, r2.text
    assert r2.json()["replayed"] is True
    assert iset.load_settings("arm-01", data_root=root)["enabled"] is False
    # GET 反映对账后真实状态
    got = c.get("/v1/devices/arm-01/input-settings").json()
    assert got["enabled"] is False and got["recovery_revision"] >= 1


def test_review_resolves_open_issue_and_is_idempotent(tmp_path, client):
    c, runtime, root = client
    issue_id, revision = _open_issue(runtime, code="ime_cancel_pending")
    body = _review_body(expected_recovery_revision=revision,
                        issue_ids=[issue_id])
    r = c.post("/v1/devices/arm-01/input-recovery/review", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["issue_ids"] == [issue_id]
    assert out["input_recovery_required"] is False
    # 同编号同内容重放=原结果
    r2 = c.post("/v1/devices/arm-01/input-recovery/review", json=body)
    assert r2.status_code == 200 and r2.json()["issue_ids"] == [issue_id]
    # 编号改内容=冲突
    r3 = c.post("/v1/devices/arm-01/input-recovery/review", json={
        **body, "issue_ids": ["ghost"]})
    assert r3.status_code == 409
    assert r3.json()["error"]["key"] == "request_conflict"


def test_review_cannot_clear_protected_issues(tmp_path, client):
    c, runtime, root = client
    for code in ("settings_commit_pending", "input_record_invalid",
                 "binding_change_pending"):
        issue_id, revision = _open_issue(runtime, code=code)
        r = c.post("/v1/devices/arm-01/input-recovery/review", json=_review_body(
            expected_recovery_revision=revision, issue_ids=[issue_id],
            client_request_id=f"rev-{code}"))
        assert r.status_code == 409, code
        assert r.json()["error"]["key"] == "issue_not_reviewable"


def test_review_rejects_stale_revision_and_busy_device(tmp_path, client):
    c, runtime, root = client
    issue_id, revision = _open_issue(runtime)
    # I49：恢复版本不匹配 → 409
    r = c.post("/v1/devices/arm-01/input-recovery/review", json=_review_body(
        expected_recovery_revision=revision + 5, issue_ids=[issue_id]))
    assert r.status_code == 409
    assert r.json()["error"]["key"] == "revision_conflict"
    # I50：设备仍有任务租约 → 409 占用
    lease = runtime.leases.acquire("arm-01", "busy-task")
    try:
        r2 = c.post("/v1/devices/arm-01/input-recovery/review", json=_review_body(
            expected_recovery_revision=revision, issue_ids=[issue_id]))
        assert r2.status_code == 409
        assert r2.json()["error"]["key"] == "device_busy"
    finally:
        runtime.leases.release("arm-01", lease["lease_id"])


def test_get_reflects_guard_state_and_unknown_not_altered(tmp_path, client):
    c, runtime, root = client
    issue_id, revision = _open_issue(runtime)
    got = c.get("/v1/devices/arm-01/input-settings").json()
    # I55：GET 反映 guard 真实状态
    assert got["input_recovery_required"] is True
    assert got["recovery_revision"] == revision
    # 开关不受核对影响（I37：核对只解除列明问题，不改 enabled）
    r = c.put("/v1/devices/arm-01/input-settings", json={
        **_op(client_request_id="keep"), "enabled": True,
        "expected_revision": 0, "ime_device_id": "k40-1"})
    assert r.status_code == 200
    assert r.json()["settings"]["enabled"] is True
    got2 = c.get("/v1/devices/arm-01/input-settings").json()
    assert got2["input_recovery_required"] is True
