"""R2-23 布局导出/下载与全部布局写入口的真实身份与授权（契约 §2）。

断言正确行为（修前必败）：
- 服务端 principal 不由正文 operator_id/principal_id/role 默认或覆盖；
- 缺可信身份提供器：写/导出/导入/取证类入口 403（冻结 layout-error.v1 外壳、
  code=layout_permission_denied、中文 message、motion_started=false），只读共享
  模板按现状放行；
- 配置受信转发后：合法作者可导出/下载/导入；A 的包对 B 不可见（无 manifest/
  文件字节泄漏）；导出闭包逐资源授权；
- 拒绝用例无副作用（不产生包记录/文件/指针变化）。
"""
from __future__ import annotations

import json
import uuid

import pytest

from tests.layout_d2_helpers import (AppEnv, PRINCIPAL_HEADER, TOKEN_ENV,
                                     identity_headers, make_token_file)


@pytest.fixture()
def env(tmp_path):
    with AppEnv(tmp_path) as e:
        # 草稿链需要本台绑定/来源身份（v3 + 最近归档帧）——帧桥替身归档
        from tests.layout_d2_helpers import (FrameBridgeFake, phone_scene,
                                             write_v3_bundle)
        write_v3_bundle(tmp_path)
        bridge = FrameBridgeFake(e.runtime, width=1000, height=2000)
        scene = phone_scene()
        bridge.push(scene)
        bridge.push(scene)
        e.bridge = bridge
        # 资料前提：本台已保存粗框（适配 session 需要 installation_region_ref）。
        # 用正式 region 服务走一次真实预览+发布（设置数据，不属于断言目标）。
        svc = e.runtime.layout_bundle(tmp_path / "layout.db")["region_svc"]
        svc.configure(
            evidence_get=e.runtime.evidence.get,
            frame_reader=e.runtime.evidence.read_source_frame_bytes,
            latest_frames=e.runtime.evidence.latest_source_frames_v2,
            arm_binding=lambda device: "arm-bind-01",
            device_idle=lambda device: True,
            binding_snapshot=lambda device:
                "33333333-3333-4333-8333-333333333333")
        setup_bridge = FrameBridgeFake(e.runtime)  # 640x480（与声明一致）
        setup_bridge.push(phone_scene())
        setup_bridge.push(phone_scene())
        evidence_id = e.runtime.evidence.latest_source_frame_v2("arm-01")[
            "evidence_id"]
        preview = svc.preview("arm-01", {
            "rect": {"x": 0.2, "y": 0.12, "width": 0.6, "height": 0.68},
            "source_evidence_id": evidence_id}, "setup")
        svc.save("arm-01", {
            "client_request_id": "00000000-0000-4000-8000-0000000000aa",
            "expected_revision": 0, "preview_id": preview["preview_id"],
            "region_digest": preview["region_digest"], "publish": True},
            "setup")
        yield e


@pytest.fixture()
def open_env(tmp_path):
    """未配置身份提供器的部署（默认）：写/导出/导入必须拒绝。"""
    with AppEnv(tmp_path, token=False) as e:
        yield e


def _get_headers(env, principal: str, version: str) -> dict:
    """Fleet GET 契约公共头 + 受信转发身份。"""
    return {**env.auth(principal), "X-Correlation-Id": str(uuid.uuid4()),
            "X-Layout-Contract-Version": version}


def _error_shape(body: dict) -> dict:
    """冻结 layout-error.v1 外壳（body 直接是 error 对象）。"""
    assert body["contract_version"] == "layout-error.v1"
    assert body["motion_started"] is False
    assert body["retryable"] is False
    assert body["operation_id"] is None
    uuid.UUID(body["correlation_id"])
    assert body["message"]
    return body


def test_r2_23_self_reported_operator_cannot_export(open_env):
    """无认证自称 operator：导出拒绝（正文 principal_id 不构成身份），
    且不产生任何包记录/文件（无副作用）。"""
    c = open_env.client
    r = c.post("/v1/layout-packages/export", json={
        "principal_id": "operator",
        "client_request_id": str(uuid.uuid4()),
        "roots": [{"kind": "template", "ref": {"id": "tpl-1", "version": 1,
                                               "sha256": "0" * 64}}],
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())})
    assert r.status_code == 403, r.text
    _error_shape(r.json())
    assert r.json()["code"] == "layout_permission_denied"
    assert r.headers["X-Layout-Contract-Version"] == "layout-error.v1"
    assert r.headers["Cache-Control"] == "no-store"
    # 无副作用：没有导出包文件落盘
    exports_dir = open_env.tmp_path / "data" / "layout_exports"
    assert not exports_dir.exists() or not any(exports_dir.iterdir())


def test_r2_23_unconfigured_writes_rejected_shared_templates_readable(open_env):
    """未配置身份：模板列表/版本/资产（共享只读）按现状放行；
    草稿创建/采集/发布等写入口 403 并给出明确前置说明。"""
    c = open_env.client
    corr = str(uuid.uuid4())
    r = c.get("/v1/layout-templates?limit=5",
              headers={"X-Correlation-Id": corr,
                       "X-Layout-Contract-Version": "ui-layout-list.v1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contract_version"] == "ui-layout-list.v1"
    assert r.headers["X-Layout-Contract-Version"] == "ui-layout-list.v1"
    r = c.post("/v1/layout-template-drafts", json={
        "client_request_id": str(uuid.uuid4()), "device_id": "arm-01",
        "base_template_ref": None,
        "contract_version": "ui-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())})
    assert r.status_code == 403, r.text
    message = r.json()["message"]
    # 明确前置：指出缺可信身份提供器，正文身份不构成认证
    assert "身份" in message
    r = c.post("/v1/devices/arm-01/layout-captures", json={
        "client_request_id": str(uuid.uuid4()), "owner_kind": "search_region",
        "owner_id": None, "expected_owner_revision": 0, "page_state": None,
        "controlled_test_confirmed": True,
        "contract_version": "layout-capture.v1",
        "correlation_id": str(uuid.uuid4())})
    assert r.status_code == 403, r.text
    assert r.json()["code"] == "layout_permission_denied"


def test_r2_23_body_principal_cannot_change_server_identity(env):
    """正文 principal_id 与受信转发主体并存：冻结闭集根本不接受正文身份
    字段（422）；合法导出的归属=已验证转发主体，正文无从覆盖。"""
    c = env.client
    draft = _publish_template_chain(env, "author-a")
    auth = {**env.auth("author-a"), "Content-Type": "application/json"}
    # 正文冒称另一身份：冻结闭集拒绝该字段（服务端身份不由正文提供）
    r = c.post("/v1/layout-packages/export", json={
        "principal_id": "author-b",
        "client_request_id": str(uuid.uuid4()),
        "roots": [{"kind": "template",
                   "ref": {"id": draft["template_id"], "version": 1,
                           "sha256": draft["sha256"]}}],
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())}, headers=auth)
    assert r.status_code == 422, r.text
    # 合法导出（无正文身份）：包记录身份=已验证主体 author-a
    export = c.post("/v1/layout-packages/export", json={
        "client_request_id": str(uuid.uuid4()),
        "roots": [{"kind": "template",
                   "ref": {"id": draft["template_id"], "version": 1,
                           "sha256": draft["sha256"]}}],
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())}, headers=auth)
    assert export.status_code == 200, export.text
    package_id = export.json()["package_id"]
    r = c.get(f"/v1/layout-packages/{package_id}",
              headers=_get_headers(env, "author-a", "layout-package.v1"))
    assert r.status_code == 200, r.text
    assert r.json()["manifest"]["created_by"] == "author-a"


def test_r2_23_owner_a_package_hidden_from_b(env):
    """A 下载 B 包：合法只读用户/另一主体不得读取他人包的 manifest/ZIP 字节，
    也不得经下载获得文件。"""
    c = env.client
    runtime = env.runtime
    tmp_path = env.tmp_path
    draft = _publish_template_chain(env, "author-a")
    export = c.post("/v1/layout-packages/export", json={
        "client_request_id": str(uuid.uuid4()),
        "roots": [{"kind": "template",
                   "ref": {"id": draft["template_id"], "version": 1,
                           "sha256": draft["sha256"]}}],
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())},
        headers=env.auth("author-a"))
    assert export.status_code == 200, export.text
    package_id = export.json()["package_id"]
    for path in (f"/v1/layout-packages/{package_id}",
                 f"/v1/layout-packages/{package_id}/download"):
        r = c.get(path, headers=_get_headers(env, "author-b",
                                             "layout-package.v1"))
        assert r.status_code == 403, (path, r.text)
        _error_shape(r.json())
        assert b"PK" not in r.content
        assert "anchor" not in r.text  # manifest 内容不泄漏
    # 作者本人仍可读/下载（合法流程仍可用）
    ok = c.get(f"/v1/layout-packages/{package_id}/download",
               headers=_get_headers(env, "author-a", "layout-package.v1"))
    assert ok.status_code == 200
    assert ok.content[:2] == b"PK"
    assert ok.headers["Content-Disposition"] == \
        "attachment; filename=layout-package.zip"


def test_r2_23_export_closure_authorizes_each_resource(env):
    """导出闭包逐资源授权： adaptation 根属于他人（不同已验证主体）时
    整包拒绝，不返回 manifest。"""
    c = env.client
    runtime = env.runtime
    tmp_path = env.tmp_path
    from tests.layout_d2_helpers import publish_adaptation_chain, publish_template_chain
    template = publish_template_chain(env, "author-a", template_id="tpl-a")
    session = publish_adaptation_chain(env, "author-a", template)
    r = c.post("/v1/layout-packages/export", json={
        "client_request_id": str(uuid.uuid4()),
        "roots": [{"kind": "adaptation",
                   "ref": {"id": "arm-01",
                           "version": session["adaptation_ref"]["version"],
                           "sha256": session["adaptation_ref"]["sha256"]}}],
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())},
        headers=env.auth("author-b"))
    assert r.status_code == 403, r.text
    _error_shape(r.json())
    assert r.json()["code"] == "layout_permission_denied"
    # 作者本人导出成功
    ok = c.post("/v1/layout-packages/export", json={
        "client_request_id": str(uuid.uuid4()),
        "roots": [{"kind": "adaptation",
                   "ref": {"id": "arm-01",
                           "version": session["adaptation_ref"]["version"],
                           "sha256": session["adaptation_ref"]["sha256"]}}],
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())},
        headers=env.auth("author-a"))
    assert ok.status_code == 200, ok.text
    assert ok.json()["archive_sha256"]


def test_r2_23_import_requires_identity_and_preview_owner(env):
    """导入预览/提交需要身份；预览行按原身份归属（他人 preview 提交 403）。"""
    c = env.client
    runtime = env.runtime
    tmp_path = env.tmp_path
    draft = _publish_template_chain(env, "author-a")
    export = c.post("/v1/layout-packages/export", json={
        "client_request_id": str(uuid.uuid4()),
        "roots": [{"kind": "template",
                   "ref": {"id": draft["template_id"], "version": 1,
                           "sha256": draft["sha256"]}}],
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())},
        headers=env.auth("author-a"))
    import base64
    blob = c.get(f"/v1/layout-packages/{export.json()['package_id']}/download",
                 headers=_get_headers(env, "author-a",
                                      "layout-package.v1")).content
    # 未认证导入拒绝
    r = c.post("/v1/layout-packages/import/preview", json={
        "archive_base64": base64.b64encode(blob).decode("ascii"),
        "archive_sha256": export.json()["archive_sha256"],
        "privacy_confirmed": True,
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())})
    assert r.status_code == 403, r.text
    # B 预览成功后，A 用 B 的 preview 提交：403（跨身份）
    preview_b = c.post("/v1/layout-packages/import/preview", json={
        "archive_base64": base64.b64encode(blob).decode("ascii"),
        "archive_sha256": export.json()["archive_sha256"],
        "privacy_confirmed": True,
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())},
        headers=env.auth("author-b"))
    assert preview_b.status_code == 200, preview_b.text
    r = c.post("/v1/layout-packages/import", json={
        "client_request_id": str(uuid.uuid4()),
        "preview_id": preview_b.json()["preview_id"],
        "archive_sha256": export.json()["archive_sha256"],
        "contract_version": "layout-package.v1",
        "correlation_id": str(uuid.uuid4())},
        headers=env.auth("author-a"))
    assert r.status_code == 403, r.text


def test_r2_23_token_mismatch_rejected(env):
    """令牌不匹配（同头部、错令牌）拒绝；principal 头非法形状拒绝。"""
    c = env.client
    runtime = env.runtime
    tmp_path = env.tmp_path
    bad = {"X-Layout-Forwarder-Token": "0" * 64, PRINCIPAL_HEADER: "intruder"}
    r = c.post("/v1/layout-template-drafts", json={
        "client_request_id": str(uuid.uuid4()), "device_id": "arm-01",
        "base_template_ref": None,
        "contract_version": "ui-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=bad)
    assert r.status_code == 403, r.text
    weird = env.auth("author-a")
    weird[PRINCIPAL_HEADER] = "../etc-passwd"
    r = c.post("/v1/layout-template-drafts", json={
        "client_request_id": str(uuid.uuid4()), "device_id": "arm-01",
        "base_template_ref": None,
        "contract_version": "ui-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=weird)
    assert r.status_code == 403, r.text


def test_r2_23_operations_and_region_reads_require_identity(open_env):
    """操作查询（跨身份防泄漏）与设备资料读取需要身份；无认证 403。"""
    c = open_env.client
    r = c.get(f"/v1/layout-operations/{uuid.uuid4()}")
    assert r.status_code == 403, r.text
    r = c.get("/v1/devices/arm-01/phone-search-region")
    assert r.status_code == 403, r.text
    _error_shape(r.json())


# ---------------- 场景装配（共享冻结链构造器；见 layout_d2_helpers） ----------------


def _publish_template_chain(env, principal: str) -> dict:
    from tests.layout_d2_helpers import publish_template_chain
    return publish_template_chain(env, principal, template_id=f"tpl-{principal}")
