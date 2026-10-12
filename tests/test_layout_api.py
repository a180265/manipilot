"""阶段4 L04-L07 路由级验收：走真实 FastAPI app 的布局资料/操作/采集链路。

R2-D2 修订（2026-10-04）：全部布局路由按冻结 wire 契约（R2-17）+ 可信身份
（R2-23，受信转发令牌）——测试用隔离令牌文件走真实身份解析，不把身份检查
改恒真；错误外壳 layout-error.v1；端到端深链见 tests/test_r2_*.py。
"""
import base64
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.helpers import make_device_configs
from tests.layout_d2_helpers import (TOKEN_ENV, identity_headers,
                                     make_token_file)

GET_VERSIONS = {
    "/v1/layout-operations": "layout-operation.v1",
    "/v1/layout-packages": "layout-package.v1",
    "layout-evidence": "layout-evidence.v1",
}


def _get_headers(version: str) -> dict:
    return {"X-Correlation-Id": str(uuid4()),
            "X-Layout-Contract-Version": version}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    from api.app import create_app
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime
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
    token_file = make_token_file(tmp_path)
    monkeypatch.setenv(TOKEN_ENV, str(token_file))
    app = create_app(runtime)
    app.state.runtime = runtime
    app.state.device_configs = make_device_configs(
        1, camera_device_unique_id="uvc-01", binding_revision=7,
        daemon_addr="127.0.0.1:8795", expected_frame_width=64,
        expected_frame_height=48)
    app.state.layout_db = tmp_path / "layout.db"
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-1", "online": True, "keyboardActive": True}]

    class _Client(TestClient):
        """默认携带受信转发身份（R2-23 合法凭据路径）。"""

        def request(self, method, url, **kwargs):
            headers = kwargs.pop("headers", {}) or {}
            merged = {**identity_headers(token_file, "console-op"), **headers}
            if str(method).upper() in ("POST", "PUT"):
                merged.setdefault("Content-Type", "application/json")
            return super().request(method, url, headers=merged, **kwargs)

    with _Client(app) as c:
        runtime.devices._devices["arm-01"].update({
            "status": "online", "calibration_status": "validated",
            "ime_online": True, "ime_device_id": "k40-1",
            "ime_keyboard_active": True, "daemon_session": "sess-1"})
        yield c, runtime, tmp_path


def _seed_adaptation_version(env):
    c, runtime, tmp_path = env
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "layout.db"))
    conn.execute("CREATE TABLE IF NOT EXISTS layout_kv "
                 "(k TEXT PRIMARY KEY, json TEXT)")
    conn.execute("INSERT OR REPLACE INTO layout_kv VALUES (?, ?)",
                 ("adaptation_version:arm-01:1", json.dumps(
                     {"device_id": "arm-01", "version": 1,
                      "pages": {"home": []}, "sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"})))
    conn.commit()
    conn.close()


def _open_issue(runtime, tmp_path):
    """真实 open 资料问题（typed store；复核链的前提事实）。"""
    from runtime.layout_review import LayoutReviewService
    from runtime.layout_state import LayoutStateStore
    state = LayoutStateStore(tmp_path / "layout.db")
    kv = runtime.layout_bundle(tmp_path / "layout.db")["kv_store"]
    return LayoutReviewService(kv, state).open_issue(
        "arm-01", "item",
        {"id": "arm-01", "version": 1, "sha256": "0" * 64},
        {"page_state": "wechat_list", "item_kind": "anchor",
         "item_id": "upper"}, 1, "anchor_missing")


# ---------------- 操作查询与失败留痕 ----------------
def test_operation_snapshot_queryable_by_request(env):
    c, runtime, tmp_path = env
    issue = _open_issue(runtime, tmp_path)
    request_id = str(uuid4())
    r = c.post("/v1/devices/arm-01/layout-review", json={
        "client_request_id": request_id, "issue_ids": [issue["issue_id"]],
        "expected_revision": 0,
        "evidence_ids": [issue["issue_id"]],  # 占位证据：typed 缺失 → 403 分支
        "reason": "对账",
        "contract_version": "layout-review.v1",
        "correlation_id": str(uuid4())})
    # typed 证据不存在 → 403（跨设备/缺证据不能核对，契约语义）
    assert r.status_code in (200, 403)
    if r.status_code == 403:
        # 补一条 typed 布局证据后再复核成功（契约合法形状）
        _seed_layout_evidence(runtime, tmp_path)
        r = c.post("/v1/devices/arm-01/layout-review", json={
            "client_request_id": request_id, "issue_ids": [issue["issue_id"]],
            "expected_revision": 0, "evidence_ids": ["ev-1"],
            "reason": "对账",
            "contract_version": "layout-review.v1",
            "correlation_id": str(uuid4())})
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["state"] == "resolved"
    projection = c.get(
        f"/v1/layout-operations/by-request/{request_id}",
        headers=_get_headers("layout-operation.v1"))
    assert projection.status_code == 200
    body = projection.json()
    assert body["phase"] == "committed"
    assert body["original_response"]["body"]["results"][0]["state"] == "resolved"
    # 同请求重放：原响应 + replayed 标记
    replay = c.post("/v1/devices/arm-01/layout-review", json={
        "client_request_id": request_id, "issue_ids": [issue["issue_id"]],
        "expected_revision": 0, "evidence_ids": ["ev-1"],
        "reason": "对账",
        "contract_version": "layout-review.v1",
        "correlation_id": str(uuid4())})
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True


def test_review_requires_client_request_id_and_evidence(env):
    c, _, _ = env
    r = c.post("/v1/devices/arm-01/layout-review", json={
        "issue_ids": [], "evidence_ids": []})
    assert r.status_code == 422
    r = c.post("/v1/devices/arm-01/layout-review", json={
        "client_request_id": str(uuid4()), "issue_ids": [str(uuid4())],
        "expected_revision": 0, "evidence_ids": ["ev-1"],
        "reason": "核对",
        "contract_version": "layout-review.v1",
        "correlation_id": str(uuid4())})
    assert r.status_code == 403  # 证据不属于本设备（前置核验，防跨设备核对）
    assert r.json()["code"] == "layout_permission_denied"


def test_operation_unknown_returns_404(env):
    c, _, _ = env
    r = c.get(f"/v1/layout-operations/by-request/{uuid4()}",
              headers=_get_headers("layout-operation.v1"))
    assert r.status_code == 404
    assert r.json()["code"] == "layout_resource_missing"


# ---------------- 包导出/下载/导入 ----------------
def test_package_export_download_import_flow(env):
    c, runtime, tmp_path = env
    _seed_adaptation_version(env)
    export = c.post("/v1/layout-packages/export", json={
        "client_request_id": str(uuid4()),
        "roots": [{"kind": "adaptation",
                   "ref": {"id": "arm-01", "version": 1,
                           "sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"}}],
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid4())})
    # legacy 种子档没有 catalog/published_by 事实——按 resolver 读取放行与否
    assert export.status_code == 200, export.text
    package_id = export.json()["package_id"]
    download = c.get(f"/v1/layout-packages/{package_id}/download",
                     headers=_get_headers("layout-package.v1"))
    assert download.status_code == 200 and download.content[:2] == b"PK"
    blob = download.content
    preview = c.post("/v1/layout-packages/import/preview", json={
        "archive_base64": base64.b64encode(blob).decode(),
        "archive_sha256": __import__("hashlib").sha256(blob).hexdigest(),
        "privacy_confirmed": True,
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid4())})
    assert preview.status_code == 200, preview.text
    assert all(item["status"] == "reuse"
               for item in preview.json()["items"])
    commit = c.post("/v1/layout-packages/import", json={
        "client_request_id": str(uuid4()),
        "preview_id": preview.json()["preview_id"],
        "archive_sha256": __import__("hashlib").sha256(blob).hexdigest(),
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid4())})
    assert commit.status_code == 200, commit.text
    assert all(item["status"] == "reuse"
               for item in commit.json()["items"])


def test_import_preview_requires_privacy(env):
    c, _, _ = env
    r = c.post("/v1/layout-packages/import/preview", json={
        "archive_base64": base64.b64encode(b"PK\x05\x06").decode(),
        "archive_sha256": "0" * 64, "privacy_confirmed": False,
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid4())})
    assert r.status_code == 422
    assert r.json()["code"] in ("layout_request_invalid",
                                "layout_permission_denied")


# ---------------- 回滚 ----------------
def test_rollback_frozen_shapes(env):
    """R2-17：回滚按冻结形状（kind/target_ref）；adaptation 类走会话链，
    本入口只接受区域回滚；未知版本 404。"""
    c, _, _ = env
    r = c.post("/v1/devices/arm-01/layout-rollback/preview", json={
        "kind": "layout_adaptation", "target_ref": {"id": "arm-01",
                                                    "version": 1,
                                                    "sha256": "0" * 64},
        "expected_revision": 0, "source_evidence_id": None,
        "capture_session_id": None, "expected_session_revision": None,
        "contract_version": "layout-rollback.v1",
        "correlation_id": str(uuid4())})
    assert r.status_code == 422
    r = c.post("/v1/devices/arm-01/layout-rollback/preview", json={
        "kind": "phone_search_region",
        "target_ref": {"id": "arm-01", "version": 9, "sha256": "0" * 64},
        "expected_revision": 0, "source_evidence_id": "ev-x",
        "capture_session_id": None, "expected_session_revision": None,
        "contract_version": "layout-rollback.v1",
        "correlation_id": str(uuid4())})
    assert r.status_code == 404
    assert r.json()["code"] == "layout_resource_missing"


# ---------------- L05 采集/证据 ----------------
def test_capture_requires_exactly_two_local_evidence(env):
    """R2-16 更正（§4.5）：正式 capture 由适配服务按冻结 capture_request 取
    受控两帧并服务端核验；客户端不再提交证据 id。本用例为请求形状契约断言。"""
    c, runtime, _ = env
    # 缺冻结字段（owner_kind/expected_owner_revision/controlled_test_confirmed）
    r = c.post("/v1/devices/arm-01/layout-captures", json={
        "session_id": "s1", "page_state": "home", "evidence_ids": ["ev-only"]})
    assert r.status_code == 422
    assert r.json()["code"] == "layout_request_invalid"


def _seed_layout_evidence(runtime, tmp_path, evidence_id="ev-1"):
    """契约合法的 layout:evidence 行（离线 schema/语义校验同形状）。"""
    from runtime.layout_state import (LayoutStateStore, state_identity,
                                      state_key)
    state = LayoutStateStore(tmp_path / "layout.db")
    generation = "11111111-1111-4111-8111-111111111111"
    binding = {"camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
               "camera_binding_id": "33333333-3333-4333-8333-333333333333",
               "arm_binding_id": "arm-bind-01"}
    evidence = {
        "evidence_id": evidence_id, "device_id": "arm-01",
        "principal_id": "console-op", "capture_id": str(uuid4()),
        "page_state": "wechat_list",
        "source_binding": {"device_id": "arm-01",
                           "installation_binding": binding,
                           "source_size": {"width": 640, "height": 480},
                           "source_geometry_id": "a" * 64,
                           "evidence_id": evidence_id,
                           "source_session_id": generation,
                           "frame_seq": 1,
                           "source_captured_at": "2026-10-04T10:00:00.000Z",
                           "server_received_at": "2026-10-04T10:00:00.100Z"},
        "timing_proof": {"source_clock_id": "mach-absolute:" + "b" * 64,
                         "clock_registration_id":
                             "22222222-2222-4222-8222-222222222222",
                         "source_generation_id": generation,
                         "source_frame_seq": 1, "source_capture_seq": 1,
                         "source_captured_monotonic_ns": 1_000_000_000,
                         "source_capture_uncertainty_ms": 1.0,
                         "source_timing_provenance":
                             "camera_pts_host_clock.v1"},
        "image_sha256": "0" * 64, "media_type": "image/jpeg",
        "server_instance_id": "22222222-2222-4222-8222-222222222222",
        "captured_monotonic_ms": 1000, "received_monotonic_ms": 1000050,
        "privacy_scope": "controlled_test"}
    state.state_insert("evidence",
                       state_key("evidence", state_identity("evidence",
                                                            evidence)),
                       "arm-01", "console-op", evidence)
    return evidence


def test_evidence_read_scoped_to_device(env):
    c, runtime, tmp_path = env
    evidence = _seed_layout_evidence(runtime, tmp_path)
    ok = c.get(f"/v1/devices/arm-01/layout-evidence/{evidence['evidence_id']}",
               headers=_get_headers("layout-evidence.v1"))
    assert ok.status_code == 200
    assert ok.json()["evidence"]["evidence_id"] == evidence["evidence_id"]
    other = c.get(f"/v1/devices/arm-02/layout-evidence/{evidence['evidence_id']}",
                  headers=_get_headers("layout-evidence.v1"))
    assert other.status_code == 404


# ---------------- IME 几何挂载位置 ----------------
def test_geometry_routes_live_on_screen_ime_not_fleet(env):
    c, _, _ = env
    r = c.post("/api/v1/devices/k40/geometry/sessions", json={})
    assert r.status_code == 404  # 已按 §11.13.3 迁至 screen_ime 路由
