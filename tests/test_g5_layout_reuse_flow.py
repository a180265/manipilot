"""G5 组合验收（第二轮执行与验收 §5）：资料复用连续调用路径。

单一流程依次走过：
 ① 首台（store-1 / arm-01）正式四态：粗框（真实检测）→ 模板草稿/资产/四页
    （受控两帧真实匹配）→ 预览/发布 → 适配会话四页 → 预览/发布 →
 ② 导出闭合集（冻结包：适配→区域→模板→资产；manifest 语义核对）→
 ③ 第二个空 Store（store-2）：损坏包拒绝（无可见半包）→ 完整包导入预览/
    提交 → 正式模板/资产 GET（原字节/原定义）→ 不带 current/权限/校准资格 →
 ④ 第二台（store-2 / arm-02）独立粗框/v3/四态（独立校准提供器：
    导入不带来校准资格，写本台 v3 后才可用）→
 ⑤ 同根重启与发布重放（同号重放返回原响应、版本不覆盖、丢回包按原编号
    恢复）。

真实保留：Fleet HTTP 全路由、布局服务/存储/状态机、匹配与检测、操作台账、
wire 契约；只替换外部 IO：相机帧桥（受控合成帧归档 EvidenceStore）、v3
校准档案（D 侧受控夹具，独立提供器事实源）。隔离：两个独立临时根，零现场。
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
import zipfile
from pathlib import Path

import pytest
import numpy as np

from tests.layout_d2_helpers import (
    AppEnv, FrameBridgeFake, fixture_page_assets, fixture_page_definitions,
    load_contract, phone_scene, scene_frames_for_page,
)

STATES = ("wechat_list", "chat_ime_hidden", "chat_ime_empty", "chat_ime_text")
FRAME_SIZE = (1200, 2200)
REGION_RECT = {"x": 0.04, "y": 0.02, "width": 0.92, "height": 0.96}
PUBLISH_REQUEST_ID = "22222222-2222-4222-8222-222222222222"


class PageBridge(FrameBridgeFake):
    """将共享屏幕像素放入有背景余量的相机图；不替换任何匹配逻辑。"""

    def push(self, image):
        if image.shape[:2] == (2000, 1000):
            frame = np.full((2200, 1200, 3), 18, dtype=np.uint8)
            screen = image.copy()
            screen[:3, :] = screen[-3:, :] = 235
            screen[:, :3] = screen[:, -3:] = 235
            frame[100:2100, 100:1100] = screen
            image = frame
        return super().push(image)


def _write_calibration(root, device):
    from drivers import calibration_bundle as cb
    from tests.layout_d2_helpers import write_v3_bundle

    data_root = write_v3_bundle(
        root, device=device, homography=[1, 0, -100, 0, 1, -100, 0, 0, 1])
    bundle = cb.load_bundle(device, data_root)
    bundle["frame"] = {"width": FRAME_SIZE[0], "height": FRAME_SIZE[1]}
    bundle["cameraId"] = "cam-" + device[-2:]
    bundle["cameraDeviceUniqueId"] = "uvc-" + device[-2:]
    bundle["xy"].update({"validated": True, "maxResidualPx": .5})
    bundle["camera"]["cameraToScreen"]["maxErrorPx"] = .5
    bundle["screen"]["edgeModel"] = {
        "corners": [[100, 100], [1099, 100], [1099, 2099], [100, 2099]],
        "tolerancePx": 8,
    }
    cb.save_bundle(bundle, data_root)


def _read_headers(env, version: str) -> dict:
    return {**env.auth("operator-01"),
            "X-Correlation-Id": str(uuid.uuid4()),
            "X-Layout-Contract-Version": version}


def _edit_headers(env) -> dict:
    return {**env.auth("operator-01"), "Content-Type": "application/json"}


def _region_chain(env, device: str, bridge: FrameBridgeFake) -> dict:
    """第二台独立粗框：页面形状请求（rect + 冻结证据）→ 预览 → 发布。"""
    scene = phone_scene(width=1200, height=2200, phone=(100, 100, 1000, 2000))
    rect_evidence = bridge.push(scene)
    bridge.push(scene)
    r = env.client.post(
        f"/v1/devices/{device}/phone-search-region/preview",
        json={"contract_version": "phone-search-region-preview.v1",
              "correlation_id": str(uuid.uuid4()), "rect": REGION_RECT,
              "source_evidence_id": rect_evidence["evidence_id"]},
        headers=_edit_headers(env))
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["status"] == "verified", preview
    r = env.client.put(
        f"/v1/devices/{device}/phone-search-region",
        json={"contract_version": "phone-search-region-write.v1",
              "correlation_id": str(uuid.uuid4()),
              "client_request_id": str(uuid.uuid4()),
              "expected_revision": 0, "preview_id": preview["preview_id"],
              "region_digest": preview["region_digest"], "publish": True},
        headers=_edit_headers(env))
    assert r.status_code == 200, r.text
    assert r.json()["selected_version"] == 1
    saved = r.json()
    r = env.client.get(
        f"/v1/devices/{device}/phone-search-region/versions/1",
        headers={**env.auth("operator-01"),
                 "X-Correlation-Id": str(uuid.uuid4()),
                 "X-Layout-Contract-Version":
                     "phone-search-region-read.v1"})
    assert r.status_code == 200, r.text
    return {"selected_version": saved["selected_version"],
            "sha256": r.json()["record"]["sha256"]}


def _adaptation_four_state(env, device: str, template_ref: dict, *,
                           session_request_id: str | None = None,
                           publish_request_id: str | None = None) -> dict:
    """正式四态：会话 → 四页（受控两帧真实匹配）→ 预览 → 发布。"""
    client = env.client
    headers = _edit_headers(env)
    page_assets = fixture_page_assets()
    page_defs = fixture_page_definitions()
    session_body = {"client_request_id": session_request_id or str(uuid.uuid4()),
                    "template_ref": dict(template_ref),
                    "expected_revision": 0,
                    "keyboard_geometry_mode": "live_ime_v1",
                    "inherit_session_id": None, "inherit_states": [],
                    "contract_version": "device-layout-edit.v1",
                    "correlation_id": str(uuid.uuid4())}
    session = client.post(f"/v1/devices/{device}/layout-adaptation/sessions",
                          json=session_body, headers=headers)
    assert session.status_code == 200, session.text
    session_id = session.json()["session"]["capture_session_id"]
    revision = 1
    for state in STATES:
        scene_frames_for_page(env.bridge, page_assets, page_defs[state])
        capture = client.post(
            f"/v1/devices/{device}/layout-captures",
            json={"client_request_id": str(uuid.uuid4()),
                  "owner_kind": "session", "owner_id": session_id,
                  "expected_owner_revision": revision, "page_state": state,
                  "controlled_test_confirmed": True,
                  "contract_version": "layout-capture.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=headers)
        assert capture.status_code == 200, capture.text
        evidence_ids = [e["evidence_id"]
                        for e in capture.json()["capture"]["evidence"]]
        written = client.put(
            f"/v1/devices/{device}/layout-adaptation/sessions/{session_id}"
            f"/pages/{state}",
            json={"client_request_id": str(uuid.uuid4()),
                  "expected_session_revision": revision,
                  "source_evidence_ids": evidence_ids,
                  "local_overrides": [],
                  "contract_version": "device-layout-edit.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=headers)
        assert written.status_code == 200, written.text
        revision = written.json()["session_revision"]
    preview = client.post(f"/v1/devices/{device}/layout-adaptation/preview",
                          json={"capture_session_id": session_id,
                                "expected_session_revision": revision,
                                "contract_version": "device-layout-edit.v1",
                                "correlation_id": str(uuid.uuid4())},
                          headers=headers)
    assert preview.status_code == 200, preview.text
    publish_body = {"client_request_id":
                    publish_request_id or str(uuid.uuid4()),
                    "expected_revision": 0,
                    "preview_id": preview.json()["preview_id"],
                    "accepted_items": preview.json()["accepted_items"],
                    "contract_version": "device-layout-edit.v1",
                    "correlation_id": str(uuid.uuid4())}
    publish = client.put(f"/v1/devices/{device}/layout-adaptation",
                         json=publish_body, headers=headers)
    assert publish.status_code == 200, publish.text
    return {"publish": publish.json(), "publish_body": publish_body,
            "headers": headers}


def _register_arm02(env) -> None:
    """向 store-2 运行时注册第二台（独立 daemon 地址；一台机械臂一个持有者）。"""
    import asyncio as _asyncio
    cfg = {"device_id": "arm-02", "name_display": "设备 arm-02",
           "arm_slot": 2, "camera_id": "cam-02",
           "wechat_account_id": "sim-acc-02",
           "camera_device_unique_id": "uvc-02", "binding_revision": 7,
           "daemon_addr": "127.0.0.1:18795",
           "expected_frame_width": FRAME_SIZE[0],
           "expected_frame_height": FRAME_SIZE[1]}
    _asyncio.run(env.runtime.devices.register_and_selfcheck(cfg))


def _template_chain_with_metadata(env) -> dict:
    """draft_create → 资产上传 → 兼容元数据（先于页面，§5）→ 四页
    （受控两帧真实匹配）→ template_publish。与 publish_template_chain
    同链，补充冻结 fixture 的 compatibility 元数据步。"""
    import hashlib as _hashlib
    c = env.client
    headers = _edit_headers(env)
    page_assets = fixture_page_assets()
    page_defs = fixture_page_definitions()
    created = c.post("/v1/layout-template-drafts", json={
        "client_request_id": str(uuid.uuid4()), "device_id": "arm-01",
        "base_template_ref": None,
        "contract_version": "ui-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=headers)
    assert created.status_code == 200, created.text
    draft_id = created.json()["draft_id"]
    tile = page_assets["anchor-tile"]
    upload = c.post(f"/v1/layout-template-drafts/{draft_id}/assets", json={
        "client_request_id": str(uuid.uuid4()), "expected_revision": 1,
        "asset": {"id": "anchor-tile",
                  "relative_path": "assets/anchor-tile.png",
                  "sha256": _hashlib.sha256(tile).hexdigest(),
                  "media_type": "image/png", "width": 40, "height": 40},
        "content_base64": base64.b64encode(tile).decode("ascii"),
        "source_evidence_ids": [], "privacy_confirmed": True,
        "contract_version": "ui-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=headers)
    assert upload.status_code == 200, upload.text
    revision = upload.json()["revision"]
    examples, _cases = load_contract().all_fixtures()
    metadata_body = dict(examples["http-draft_metadata-request"]["body"])
    metadata_body["expected_revision"] = revision
    metadata_body["client_request_id"] = str(uuid.uuid4())
    metadata_body["correlation_id"] = str(uuid.uuid4())
    metadata = c.put(f"/v1/layout-template-drafts/{draft_id}/metadata",
                     json=metadata_body, headers=headers)
    assert metadata.status_code == 200, metadata.text
    revision = metadata.json()["draft"]["revision"]
    for state in STATES:
        scene_frames_for_page(env.bridge, page_assets, page_defs[state])
        capture = c.post("/v1/devices/arm-01/layout-captures", json={
            "client_request_id": str(uuid.uuid4()), "owner_kind": "draft",
            "owner_id": draft_id, "expected_owner_revision": 1,
            "page_state": state, "controlled_test_confirmed": True,
            "contract_version": "layout-capture.v1",
            "correlation_id": str(uuid.uuid4())}, headers=headers)
        assert capture.status_code == 200, capture.text
        evidence_ids = [e["evidence_id"]
                        for e in capture.json()["capture"]["evidence"]]
        written = c.put(
            f"/v1/layout-template-drafts/{draft_id}/pages/{state}",
            json={"client_request_id": str(uuid.uuid4()),
                  "expected_revision": revision,
                  "source_evidence_ids": evidence_ids,
                  "page_definition": page_defs[state],
                  "contract_version": "ui-layout-edit.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=headers)
        assert written.status_code == 200, written.text
        revision = written.json()["revision"]
    published = c.post("/v1/layout-templates", json={
        "client_request_id": str(uuid.uuid4()),
        "template_id": "wechat-controlled",
        "expected_parent_version": None, "draft_id": draft_id,
        "expected_draft_revision": revision,
        "contract_version": "ui-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())}, headers=headers)
    assert published.status_code == 200, published.text
    ref = published.json()["template_ref"]
    return {"template_id": ref["id"], "sha256": ref["sha256"],
            "draft_id": draft_id, "version": ref["version"]}


def test_g5_layout_reuse_full_flow(tmp_path):
    tmp1 = tmp_path / "store-1"
    tmp2 = tmp_path / "store-2"
    tmp1.mkdir()
    tmp2.mkdir()

    # ================= ① 首台：粗框 + 四态 + 发布 =================
    env1 = AppEnv(tmp1, devices=1, frame_size=FRAME_SIZE)
    try:
        env1.runtime.devices._devices["arm-01"].update({
            "status": "online", "calibration_status": "validated",
            "ime_online": True, "ime_device_id": "k40-1",
            "ime_keyboard_active": True, "daemon_session": "sess-1",
            "camera_id": "cam-01", "binding_revision": 7,
            "daemon_binding_id": "arm-bind-01"})
        # 首台已校准：v3 档案为屏幕侧事实源（草稿绑定快照/screen_descriptor）
        _write_calibration(tmp1, "arm-01")
        bridge1 = PageBridge(env1.runtime, width=FRAME_SIZE[0], height=FRAME_SIZE[1])
        env1.bridge = bridge1
        region1 = _region_chain(env1, "arm-01", bridge1)
        assert region1["selected_version"] == 1
        # 模板草稿→资产→兼容元数据→四页（受控两帧真实匹配）→发布
        template = _template_chain_with_metadata(env1)
        assert template["template_id"] == "wechat-controlled"
        # 适配会话四态→预览→发布（原定义进入不可变适配版本）
        from tests.layout_d2_helpers import publish_adaptation_chain
        adaptation1 = publish_adaptation_chain(env1, "operator-01", template)
        ref1 = adaptation1["adaptation_ref"]
        assert ref1["id"] == "arm-01" and ref1["version"] == 1

        # 首台正式模板版本（对比基准：第二台导入后的原定义一致性）
        r = env1.client.get(
            "/v1/layout-templates/wechat-controlled/versions/1",
            headers=_read_headers(env1, "ui-layout-template-read.v1"))
        assert r.status_code == 200, r.text
        template1 = r.json()
        assert template1["record"]["pages"], "首页定义必须有四态页"
        r = env1.client.get(
            "/v1/layout-templates/wechat-controlled/versions/1"
            "/assets/anchor-tile",
            headers=_read_headers(env1, "ui-layout-template-read.v1"))
        assert r.status_code == 200
        asset_bytes = r.content

        # ================= ② 导出闭合集（冻结包） =================
        export = env1.client.post(
            "/v1/layout-packages/export",
            json={"client_request_id": str(uuid.uuid4()),
                  "roots": [{"kind": "adaptation",
                             "ref": {"id": "arm-01",
                                     "version": ref1["version"],
                                     "sha256": ref1["sha256"]}},
                            {"kind": "region", "ref": {"id": "arm-01",
                                                       "version": 1,
                                                       "sha256":
                                                           region1["sha256"]}}],
                  "contract_version": "layout-package.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=env1.auth("operator-01"))
        assert export.status_code == 200, export.text
        manifest = export.json()["manifest"]
        package_id = export.json()["package_id"]
        assert manifest["privacy_scope"] == "sanitized_layout_only", \
            "导出包不得携带权限/现场身份（sanitized_layout_only）"
        kinds = {e["kind"] for e in manifest["entries"]}
        assert {"record", "asset"} <= kinds
        r = env1.client.get(
            f"/v1/layout-packages/{package_id}/download",
            headers=_read_headers(env1, "layout-package.v1"))
        assert r.status_code == 200 and r.content[:2] == b"PK"
        blob = r.content
        # 独立校验：冻结离线校验器按包语义检查导出包（防自导自入）
        from tests.layout_d2_helpers import load_contract
        load_contract().check_archive(blob)
    finally:
        env1.close()  # 令牌环境随实例释放（同一时刻只有一个 Store 在线）

    # ================= ③ 第二个空 Store：导入 → 正式 GET =================
    env2 = AppEnv(tmp2, devices=1)
    try:
        _register_arm02(env2)
        for device, arm_binding in (("arm-01", "arm-bind-01"),
                                    ("arm-02", "arm-bind-02")):
            env2.runtime.devices._devices[device].update({
                "status": "online", "calibration_status": "validated",
                "ime_online": True, "ime_device_id": "k40-1",
                "ime_keyboard_active": True, "daemon_session": "sess-1",
                "camera_id": f"cam-{device[-2:]}", "binding_revision": 7,
                "daemon_binding_id": arm_binding})
        bridge2 = PageBridge(env2.runtime, device="arm-02",
                                  width=FRAME_SIZE[0], height=FRAME_SIZE[1],
                                  binding={"camera_id": "cam-02",
                                           "camera_device_unique_id": "uvc-02",
                                           "camera_binding_id":
                                               "33333333-3333-4333-8333-"
                                               "333333333333",
                                           "arm_binding_id": "arm-bind-02"})
        env2.bridge = bridge2

        # ---- 失败项只阻断依赖：损坏资产字节 → 整包拒绝、无可见半包 ----
        src_zip = zipfile.ZipFile(io.BytesIO(blob))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for info in src_zip.infolist():
                data = src_zip.read(info.filename)
                if "/assets/" in info.filename:
                    data = data[:-1] + bytes([data[-1] ^ 0xFF])
                out.writestr(info.filename, data)
        r = env2.client.post(
            "/v1/layout-packages/import/preview",
            json={"archive_base64": base64.b64encode(buf.getvalue()).decode(),
                  "archive_sha256":
                      hashlib.sha256(buf.getvalue()).hexdigest(),
                  "privacy_confirmed": True,
                  "contract_version": "layout-package.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=_edit_headers(env2))
        assert r.status_code in (422, 503), r.text
        assert r.json()["contract_version"] == "layout-error.v1"
        r = env2.client.get("/v1/layout-templates?limit=20",
                            headers=_read_headers(env2, "ui-layout-list.v1"))
        assert r.status_code == 200 and r.json()["items"] == [], \
            "失败导入不得留下可见半包"

        # ---- 完整包导入：预览 + 提交 ----
        r = env2.client.post(
            "/v1/layout-packages/import/preview",
            json={"archive_base64": base64.b64encode(blob).decode("ascii"),
                  "archive_sha256": hashlib.sha256(blob).hexdigest(),
                  "privacy_confirmed": True,
                  "contract_version": "layout-package.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=_edit_headers(env2))
        assert r.status_code == 200, r.text
        import_preview = r.json()
        assert import_preview["can_import"] is True, import_preview["items"]
        conflicts = [item for item in import_preview["items"]
                     if item["status"] == "conflict"]
        assert conflicts == [], "完整包导入不得有冲突项"
        assert import_preview["items"], "预览必须列出记录资源"
        r = env2.client.post(
            "/v1/layout-packages/import",
            json={"client_request_id": str(uuid.uuid4()),
                  "preview_id": import_preview["preview_id"],
                  "archive_sha256": import_preview["archive_sha256"],
                  "contract_version": "layout-package.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=_edit_headers(env2))
        assert r.status_code == 200, r.text
        commit = r.json()
        imported_items = [item for item in commit["items"]
                          if item["status"] == "new"]
        assert imported_items, "导入必须落地新资源"
        assert all(item["status"] in ("new", "reuse")
                   for item in commit["items"])
        assert commit["pointers_changed"] is False, "导入不得改写 current"

        # ---- 正式 GET：原定义 + 原字节 ----
        r = env2.client.get(
            "/v1/layout-templates/wechat-controlled/versions/1",
            headers=_read_headers(env2, "ui-layout-template-read.v1"))
        assert r.status_code == 200, r.text
        template2 = r.json()
        assert template2["record"]["pages"] == \
            template1["record"]["pages"], "原页定义一致（逐页相等）"
        assert template2["record"]["sha256"] == \
            template1["record"]["sha256"]
        template_sha = template1["record"]["sha256"]
        r = env2.client.get(
            "/v1/layout-templates/wechat-controlled/versions/1"
            "/assets/anchor-tile",
            headers=_read_headers(env2, "ui-layout-template-read.v1"))
        assert r.status_code == 200
        assert r.content == asset_bytes, "资产必须逐字节一致（原字节）"
        assert r.headers["ETag"] == \
            f'"{hashlib.sha256(asset_bytes).hexdigest()}"'

        # ---- 导入不带 current / 权限 / 校准资格 ----
        for device in ("arm-01", "arm-02"):
            r = env2.client.get(f"/v1/devices/{device}/layout-adaptation",
                                headers=_read_headers(env2, "device-layout-read.v1"))
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "unregistered", \
                "导入不得设置 current 指针"
        bundle2 = env2.runtime.layout_bundle(tmp2 / "layout.db")
        catalog_rows = bundle2["state_store"].state_scan("catalog")
        assert catalog_rows, "导入资源必须经 catalog 提交（正式解析器可见）"
        assert all(row["value"]["resource"]["kind"] in
                   ("template", "region", "adaptation")
                   for row in catalog_rows), "catalog 不得含权限/许可资源"
        assert bundle2["state_store"].adaptation_pointer_get("arm-01") is None
        assert bundle2["state_store"].adaptation_pointer_get("arm-02") is None

        # ================= ④ 第二台独立粗框/v3/四态 =================
        # 校准资格不被导入：无本台 v3 时四态抓页拒绝（独立提供器事实源）
        scene = phone_scene(width=1200, height=2200,
                            phone=(100, 100, 1000, 2000))
        bridge2.push(scene), bridge2.push(scene)
        r = env2.client.post(
            "/v1/devices/arm-02/layout-captures",
            json={"client_request_id": str(uuid.uuid4()),
                  "owner_kind": "search_region", "owner_id": None,
                  "expected_owner_revision": 0, "page_state": None,
                  "controlled_test_confirmed": True,
                  "contract_version": "layout-capture.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=_edit_headers(env2))
        assert r.status_code == 200  # 粗框采集不要求校准（规格 §5）
        # 四态适配会话要求本台 v3 屏幕侧事实（binding_snapshot.screen_descriptor）：
        # 导入资料不携带校准资格 → 本台 v3 缺失时会话创建被拒（冻结错误外壳）
        session_probe = env2.client.post(
            "/v1/devices/arm-02/layout-adaptation/sessions",
            json={"client_request_id": str(uuid.uuid4()),
                  "template_ref": {"id": "wechat-controlled", "version": 1,
                                   "sha256": template_sha},
                  "expected_revision": 0,
                  "keyboard_geometry_mode": "live_ime_v1",
                  "inherit_session_id": None, "inherit_states": [],
                  "contract_version": "device-layout-edit.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=_edit_headers(env2))
        assert session_probe.status_code in (409, 422, 503), \
            session_probe.text
        assert session_probe.json()["contract_version"] == "layout-error.v1"
        # 第二台自己的 v3（独立校准提供器事实源）→ 独立粗框 → 独立四态
        _write_calibration(tmp2, "arm-02")
        _region_chain(env2, "arm-02", bridge2)
        result2 = _adaptation_four_state(
            env2, "arm-02",
            {"id": "wechat-controlled", "version": 1,
             "sha256": template_sha},
            publish_request_id=PUBLISH_REQUEST_ID)
        ref2 = result2["publish"]["adaptation_ref"]
        assert ref2["id"] == "arm-02" and ref2["version"] == 1
        r = env2.client.get("/v1/devices/arm-02/layout-adaptation",
                            headers=_read_headers(env2, "device-layout-read.v1"))
        assert r.status_code == 200 and r.json()["status"] == "verified", r.text
        assert r.json()["selected_ref"]["version"] == 1
        assert r.json()["execution_blocked"] is False

        # ================= ⑤ 发布重放（丢回包按原编号恢复） =================
        replay = env2.client.put(
            "/v1/devices/arm-02/layout-adaptation",
            json=result2["publish_body"], headers=result2["headers"])
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True, "同号重放返回原响应"
        assert replay.json()["adaptation_ref"] == ref2, "版本不覆盖"
        state2 = env2.runtime.layout_bundle(tmp2 / "layout.db")["state_store"]
        assert state2.read_adaptation_version("arm-02", 2) is None, \
            "重放不得创建新版本"
        r = env2.client.get(
            f"/v1/layout-operations/by-request/{PUBLISH_REQUEST_ID}",
            headers=_read_headers(env2, "layout-operation.v1"))
        assert r.status_code == 200, r.text
        operation = r.json()
        original = operation.get("original_response") or {}
        assert (original.get("body") or {}).get("adaptation_ref", {}) \
            .get("version") == 1, "丢回包按原编号恢复原响应"
    finally:
        env2.close()

    # ---- 同根重启（store-2 同一临时根新 app 实例） ----
    env2r = AppEnv(tmp2, devices=1)
    try:
        _register_arm02(env2r)
        env2r.runtime.devices._devices["arm-02"].update({
            "status": "online", "calibration_status": "validated",
            "ime_online": True, "ime_device_id": "k40-1",
            "ime_keyboard_active": True, "daemon_session": "sess-1",
            "camera_id": "cam-02", "binding_revision": 7,
            "daemon_binding_id": "arm-bind-02"})
        # 导入资料与发布版本跨重启保持（真实持久化）
        r = env2r.client.get(
            "/v1/layout-templates/wechat-controlled/versions/1"
            "/assets/anchor-tile",
            headers=_read_headers(env2r, "ui-layout-template-read.v1"))
        assert r.status_code == 200 and r.content == asset_bytes
        r = env2r.client.get("/v1/devices/arm-02/layout-adaptation",
                             headers=_read_headers(env2r, "device-layout-read.v1"))
        assert r.status_code == 200
        assert r.json()["selected_ref"]["version"] == 1, "重启不丢发布版本"
        # 重启后同号重放仍返回原响应（台账持久），版本不覆盖
        headers = _edit_headers(env2r)
        state2r = env2r.runtime.layout_bundle(
            tmp2 / "layout.db")["state_store"]
        record2 = state2r.read_adaptation_version("arm-02", 1)
        # 丢回包场景：客户端丢失原 preview_id 无法重组同内容重放——
        # 按原编号（client_request_id）从操作台账恢复原响应
        r = env2r.client.get(
            f"/v1/layout-operations/by-request/{PUBLISH_REQUEST_ID}",
            headers=_read_headers(env2r, "layout-operation.v1"))
        assert r.status_code == 200
        original = (r.json().get("original_response") or {}).get("body", {})
        assert original.get("adaptation_ref", {}).get("version") == 1, \
            "重启后丢回包仍按原编号恢复原响应"
        assert original.get("adaptation_ref", {}).get("sha256") == \
            ref2["sha256"]
        # 同号异内容（拿不到原内容时的重试形态）必须拒绝，不覆盖版本
        conflict = env2r.client.put(
            "/v1/devices/arm-02/layout-adaptation",
            json={"client_request_id": PUBLISH_REQUEST_ID,
                  "expected_revision": 1,
                  "preview_id": str(uuid.uuid4()),
                  "accepted_items": [],
                  "contract_version": "device-layout-edit.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers=headers)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["code"] == "layout_request_conflict"
        assert state2r.read_adaptation_version("arm-02", 2) is None, \
            "重放/冲突重试不得创建新版本"
        # 第二台区域版本跨重启可读（独立粗框持久）
        r = env2r.client.get("/v1/devices/arm-02/phone-search-region",
                             headers=_read_headers(
                                 env2r, "phone-search-region-read.v1"))
        assert r.status_code == 200
        assert r.json()["selected"]["version"] == 1
        assert record2 is not None
    finally:
        env2r.close()
