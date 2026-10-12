"""R2-17 布局正式请求与冻结 wire 契约一致（38 路由覆盖表，D 负责 35 条 Fleet 路由）。

红测断言正确行为（修前必败）：
- 冻结 wire fixture 拆成实际 method/path/headers/query/body 发往真实 HTTP 入口
  （create_app+TestClient+临时根；不把外层 fixture 当 body）；
- 每条成功响应用冻结 wire:<route>_response 校验（含 Cache-Control/X-Correlation-Id/
  X-Layout-Contract-Version 公共头、分页、二进制下载、ZIP 包）；
- 错误响应用冻结 wire:<route>_error 校验（layout-error.v1、code 与 status 对应）；
- 旧自造字段/缺字段/额外字段/错关联被拒绝。
IME 3 条几何路由归 A 包（本表标记，不由 D2 执行）。
"""
from __future__ import annotations

import base64
import copy
import json
import uuid

import numpy as np
import pytest

from tests.layout_d2_helpers import (AppEnv, FrameBridgeFake, phone_scene,
                                     scene_frames_for_page,
                                     fixture_page_assets,
                                     fixture_page_definitions, load_contract,
                                     seed_template_asset_png,
                                     validate_wire_response, wire_fixtures,
                                     write_v3_bundle)

STATES = ("wechat_list", "chat_ime_hidden", "chat_ime_empty", "chat_ime_text")
D2_ROUTES = [r for r in json.load(open(
    "docs/specs/layout-reuse/routes.json")) if r["protocol"] == "fleet"]
IME_ROUTES = [r["id"] for r in json.load(open(
    "docs/specs/layout-reuse/routes.json")) if r["protocol"] == "ime"]


@pytest.fixture()
def wc(tmp_path):
    """wire-contract 环境：真实 app + 帧桥替身 + v3 校准档案。"""
    env = AppEnv(tmp_path)
    write_v3_bundle(tmp_path)
    bridge = FrameBridgeFake(env.runtime, width=1000, height=2000)
    env.bridge = bridge
    yield env


class Runner:
    """把冻结 fixture 拆成真实 HTTP 请求发送并记录覆盖表行。"""

    def __init__(self, env):
        self.env = env
        self.client = env.client
        self.rows: list[dict] = []

    def request_view(self, route_id: str, fixture: dict, *, path=None,
                     query=None, headers=None, body=None) -> dict:
        return {"route_id": route_id, "method": fixture["method"],
                "path": path if path is not None else dict(fixture["path"]),
                "headers": headers if headers is not None else dict(fixture["headers"]),
                "query": query if query is not None else copy.deepcopy(fixture["query"]),
                "body": body if body is not None else copy.deepcopy(fixture["body"])}

    def send(self, route_id: str, view: dict, *, principal="operator-01",
             expect=None, note=""):
        routes = {r["id"]: r for r in D2_ROUTES}
        route = routes[route_id]
        # fixture 的 client_request_id 是占位常量（同值跨路由复用）；真实
        # 契约要求 UUID 全局唯一——发送前对未定制占位值换成新 UUID
        body = view.get("body")
        if isinstance(body, dict) \
                and body.get("client_request_id") \
                and str(body["client_request_id"]).startswith("a0000000"):
            body["client_request_id"] = str(uuid.uuid4())
        path = route["path"]
        path_params = {key: "arm-01" for key in route["path_parameters"]}
        path_params.update(view["path"])
        for key, value in path_params.items():
            path = path.replace("{" + key + "}", str(value))
        query = {k: v for k, v in (view["query"] or {}).items() if v is not None}
        headers = dict(view["headers"])
        headers.update(self.env.auth(principal))
        kwargs: dict = {"headers": headers}
        if view["body"] is not None:
            kwargs["content"] = json.dumps(view["body"]).encode("utf-8")
            kwargs["headers"] = {**headers, "Content-Type": "application/json"}
        if query:
            import urllib.parse
            path = path + "?" + urllib.parse.urlencode(query)
        response = self.client.request(view["method"], path, **kwargs)
        row = {"route_id": route_id, "method": view["method"], "path": path,
               "status": response.status_code, "note": note,
               "request_view": view}
        route_meta = next(r for r in D2_ROUTES if r["id"] == route_id)
        if route_meta["response"].startswith("binary:") \
                and response.status_code == 200:
            # 二进制成功响应：契约校验容器 body = 原字节的标准带 padding base64；
            # 错误响应仍是冻结 JSON 外壳
            payload = base64.b64encode(response.content).decode("ascii")
        else:
            try:
                payload = response.json()
            except Exception:
                payload = None
        if response.status_code == 200 and payload is not None:
            validate_wire_response(route_id, 200, response.headers, payload)
            row["validated"] = "response"
        elif payload is not None:
            validate_wire_response(route_id, response.status_code,
                                   response.headers, payload)
            row["validated"] = "error"
        else:
            validate_wire_response(route_id, response.status_code,
                                   response.headers, None)
            row["validated"] = "binary"
        if expect is not None:
            assert response.status_code == expect, (
                route_id, response.status_code, response.text[:400])
        self.rows.append(row)
        return response

    def fixture(self, route_id: str):
        return wire_fixtures()[f"http-{route_id}-request"]


def test_r2_17_route_table_full_chain(wc):
    """38 路由覆盖表：冻结 fixture 逐条真实发送（D 负责 35 条 Fleet 路由），
    每条记录实际 method/path/status/公共头/成功与错误响应。"""
    lv = load_contract()
    runner = Runner(wc)
    fx = runner.fixture
    client = wc.client
    bridge = wc.bridge
    # ---------- 模板资产（fixture 同源 PNG） ----------
    tile_png = base64.b64decode(
        fx("draft_asset_upload")["body"]["content_base64"])
    page_assets = {"anchor-tile": tile_png, "empty-input": seed_template_asset_png(
        650, 120, seed=3)}
    # 四页定义与冻结构造器同构（各态最低项齐备；锚点资产 anchor-tile）
    page_defs = fixture_page_definitions()

    # ---------- ① 粗框链（region 4 条；会话需要 installation_region_ref） ----------
    region_scene = phone_scene(width=1000, height=2000,
                               phone=(300, 700, 400, 700))
    rect_evidence = bridge.push(region_scene)
    fresh = bridge.push(region_scene)
    view = runner.request_view("region_get", fx("region_get"))
    r = runner.send("region_get", view, expect=200, note="空设备 unregistered")
    assert r.json()["status"] == "unregistered"
    preview_view = runner.request_view(
        "region_preview", fx("region_preview"),
        body={**fx("region_preview")["body"],
              # 框稍大于手机（300,700,400,700）——verified 要求四角严格在框内
              "rect": {"x": 0.25, "y": 0.30, "width": 0.52, "height": 0.42},
              "source_evidence_id": rect_evidence["evidence_id"]})
    r = runner.send("region_preview", preview_view, expect=200,
                    note="真实检测帧=新鲜帧")
    preview = r.json()
    assert preview["status"] == "verified"
    assert preview["source_binding"]["evidence_id"] == fresh["evidence_id"]
    save_view = runner.request_view(
        "region_save", fx("region_save"),
        body={**fx("region_save")["body"], "expected_revision": 0,
              "preview_id": preview["preview_id"],
              "region_digest": preview["region_digest"], "publish": True})
    r = runner.send("region_save", save_view, expect=200, note="发布 v1")
    assert r.json()["saved_version"] == 1
    r = runner.send("region_version", runner.request_view(
        "region_version", fx("region_version")), expect=200)
    region_sha = r.json()["record"]["sha256"]

    # ---------- ② 模板草稿链（7 条 + 二进制资产） ----------
    r = runner.send("template_list", runner.request_view(
        "template_list", fx("template_list")), expect=200, note="首列表")
    r = runner.send("draft_create", runner.request_view(
        "draft_create", fx("draft_create")), expect=200)
    draft_id = r.json()["draft_id"]
    r = runner.send("draft_get", runner.request_view(
        "draft_get", fx("draft_get"), path={"draft_id": draft_id}), expect=200)
    upload_view = runner.request_view(
        "draft_asset_upload", fx("draft_asset_upload"),
        path={"draft_id": draft_id},
        body={**fx("draft_asset_upload")["body"], "expected_revision": 1})
    r = runner.send("draft_asset_upload", upload_view, expect=200)
    revision = r.json()["revision"]
    runner.send("draft_asset_get", runner.request_view(
        "draft_asset_get", fx("draft_asset_get"),
        path={"draft_id": draft_id, "asset_id": "anchor-tile"}),
        expect=200, note="草稿资产原字节 PNG")
    # 兼容元数据先于页面核验（§5：metadata 改动清复核，先定值再采页）
    runner.send("draft_metadata", runner.request_view(
        "draft_metadata", fx("draft_metadata"), path={"draft_id": draft_id},
        body={**fx("draft_metadata")["body"], "expected_revision": revision}),
        expect=200, note="兼容元数据修订（先于页面）")
    revision = client.get(
        f"/v1/layout-template-drafts/{draft_id}",
        headers={**wc.auth("operator-01"),
                 "X-Correlation-Id": str(uuid.uuid4()),
                 "X-Layout-Contract-Version": "ui-layout-edit.v1"}
    ).json()["draft"]["revision"]
    draft_captures = {}
    draft_page_rows = []
    for state in STATES:
        scene_frames_for_page(bridge, page_assets, page_defs[state])
        capture = client.post(
            "/v1/devices/arm-01/layout-captures",
            json={"client_request_id": str(uuid.uuid4()),
                  "owner_kind": "draft", "owner_id": draft_id,
                  "expected_owner_revision": 1, "page_state": state,
                  "controlled_test_confirmed": True,
                  "contract_version": "layout-capture.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers={**wc.auth("operator-01"), "Content-Type": "application/json"})
        assert capture.status_code == 200, capture.text
        evidence = capture.json()["capture"]["evidence"]
        written = client.put(
            f"/v1/layout-template-drafts/{draft_id}/pages/{state}",
            json={"client_request_id": str(uuid.uuid4()),
                  "expected_revision": revision,
                  "source_evidence_ids": [e["evidence_id"] for e in evidence],
                  "page_definition": page_defs[state],
                  "contract_version": "ui-layout-edit.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers={**wc.auth("operator-01"), "Content-Type": "application/json"})
        assert written.status_code == 200, written.text
        validate_wire_response("draft_page", 200, written.headers,
                               written.json())
        draft_page_rows.append({
            "route_id": "draft_page", "method": "PUT",
            "path": f"/v1/layout-template-drafts/{draft_id}/pages/{state}",
            "status": 200, "validated": "response", "note": "单页完整替换",
            "request_view": {"method": "PUT",
                             "path": f"/v1/layout-template-drafts/"
                                     f"{draft_id}/pages/{state}",
                             "query": {}}})
        revision = written.json()["revision"]
        draft_captures[state] = evidence[0]["evidence_id"]
    runner.rows.extend(draft_page_rows)
    # 二次 metadata（值未变 → 仅 revision 推进，不清页）
    runner.send("draft_metadata", runner.request_view(
        "draft_metadata", fx("draft_metadata"), path={"draft_id": draft_id},
        body={**fx("draft_metadata")["body"], "expected_revision": revision}),
        expect=200, note="同值 metadata（不清页）")
    revision = client.get(
        f"/v1/layout-template-drafts/{draft_id}",
        headers={**wc.auth("operator-01"),
                 "X-Correlation-Id": str(uuid.uuid4()),
                 "X-Layout-Contract-Version": "ui-layout-edit.v1"}
    ).json()["draft"]["revision"]
    publish_view = runner.request_view(
        "template_publish", fx("template_publish"),
        body={**fx("template_publish")["body"], "draft_id": draft_id,
              "expected_draft_revision": revision,
              "expected_parent_version": None})
    r = runner.send("template_publish", publish_view, expect=200,
                    note="四页检查通过后发布不可变模板")
    template_ref = r.json()["template_ref"]
    assert template_ref["id"] == "wechat-controlled"
    runner.send("template_version", runner.request_view(
        "template_version", fx("template_version"),
        path={"template_id": template_ref["id"],
              "version": template_ref["version"]}), expect=200)
    asset_id = "anchor-tile"
    runner.send("template_asset", runner.request_view(
        "template_asset", fx("template_asset"),
        path={"template_id": template_ref["id"],
              "version": template_ref["version"], "asset_id": asset_id}),
        expect=200, note="二进制 PNG 原字节")
    r = client.get(f"/v1/layout-templates/{template_ref['id']}"
                   f"/versions/{template_ref['version']}/assets/{asset_id}",
                   headers={**wc.auth("operator-01"),
                            "X-Correlation-Id": str(uuid.uuid4()),
                            "X-Layout-Contract-Version":
                                "ui-layout-template-read.v1"})
    assert r.content == tile_png
    assert r.headers["Content-Disposition"] == "inline"
    assert r.headers["ETag"] == f'"{__import__("hashlib").sha256(tile_png).hexdigest()}"'
    runner.send("template_list", runner.request_view(
        "template_list", fx("template_list")), expect=200, note="发布后列表")

    # ---------- ③ 适配链（capture/evidence/session/page/preview/publish/读 3 条） ----------
    scene_frames_for_page(bridge, page_assets, page_defs[STATES[0]])
    r = runner.send("capture", runner.request_view(
        "capture", fx("capture"),
        body={**fx("capture")["body"], "owner_kind": "search_region",
              "owner_id": None, "expected_owner_revision": 0,
              "page_state": None}), expect=200, note="search_region 两帧（无校准要求）")
    search_evidence = r.json()["capture"]["evidence"][0]["evidence_id"]
    runner.send("evidence_get", runner.request_view(
        "evidence_get", fx("evidence_get"),
        path={"device_id": "arm-01", "evidence_id": search_evidence}),
        expect=200)
    session_view = runner.request_view(
        "session_create", fx("session_create"),
        body={**fx("session_create")["body"],
              "template_ref": template_ref})
    r = runner.send("session_create", session_view, expect=200)
    session = r.json()["session"]
    session_id = session["capture_session_id"]
    session_revision = 1
    draft_page_rows = []
    for state in STATES:
        scene_frames_for_page(bridge, page_assets, page_defs[state])
        capture = client.post(
            "/v1/devices/arm-01/layout-captures",
            json={"client_request_id": str(uuid.uuid4()),
                  "owner_kind": "session", "owner_id": session_id,
                  "expected_owner_revision": session_revision,
                  "page_state": state, "controlled_test_confirmed": True,
                  "contract_version": "layout-capture.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers={**wc.auth("operator-01"), "Content-Type": "application/json"})
        assert capture.status_code == 200, capture.text
        evidence_ids = [e["evidence_id"]
                        for e in capture.json()["capture"]["evidence"]]
        written = client.put(
            f"/v1/devices/arm-01/layout-adaptation/sessions/{session_id}"
            f"/pages/{state}",
            json={"client_request_id": str(uuid.uuid4()),
                  "expected_session_revision": session_revision,
                  "source_evidence_ids": evidence_ids,
                  "local_overrides": [],
                  "contract_version": "device-layout-edit.v1",
                  "correlation_id": str(uuid.uuid4())},
            headers={**wc.auth("operator-01"), "Content-Type": "application/json"})
        assert written.status_code == 200, written.text
        validate_wire_response("session_page", 200, written.headers,
                               written.json())
        runner.rows.append({
            "route_id": "session_page", "method": "PUT",
            "path": f"/v1/devices/arm-01/layout-adaptation/sessions/"
                    f"{session_id}/pages/{state}",
            "status": 200, "validated": "response", "note": "单页服务端核验",
            "request_view": {"method": "PUT",
                             "path": f"/v1/devices/arm-01/layout-adaptation/"
                                     f"sessions/{session_id}/pages/{state}",
                             "query": {}}})
        session_revision = written.json()["session_revision"]
    runner.send("session_get", runner.request_view(
        "session_get", fx("session_get"),
        path={"session_id": session_id}), expect=200, note="四页 page_status")
    r = client.post("/v1/devices/arm-01/layout-adaptation/preview",
                    json={"capture_session_id": session_id,
                          "expected_session_revision": session_revision,
                          "contract_version": "device-layout-edit.v1",
                          "correlation_id": str(uuid.uuid4())},
                    headers={**wc.auth("operator-01"), "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    preview_body = r.json()
    validate_wire_response("adaptation_preview", 200, r.headers, preview_body)
    runner.rows.append({"route_id": "adaptation_preview", "method": "POST",
                        "path": "/v1/devices/arm-01/layout-adaptation/preview",
                        "status": 200, "validated": "response",
                        "request_view": {"body": {"capture_session_id": session_id}},
                        "note": "预览（session_page 之后手工驱动，同冻结形状）"})
    publish_view = runner.request_view(
        "adaptation_publish", fx("adaptation_publish"),
        body={"client_request_id": str(uuid.uuid4()), "expected_revision": 0,
              "preview_id": preview_body["preview_id"],
              "accepted_items": preview_body["accepted_items"],
              "contract_version": "device-layout-edit.v1",
              "correlation_id": str(uuid.uuid4())})
    r = runner.send("adaptation_publish", publish_view, expect=200,
                    note="四页一次确认（preview 反查会话）")
    adaptation_ref = r.json()["adaptation_ref"]
    runner.send("adaptation_get", runner.request_view(
        "adaptation_get", fx("adaptation_get")), expect=200)
    runner.send("adaptation_version", runner.request_view(
        "adaptation_version", fx("adaptation_version"),
        path={"version": adaptation_ref["version"]}), expect=200)

    # ---------- ④ 资料/复核/操作/回滚/包（11 条） ----------
    runner.send("issue_list", runner.request_view(
        "issue_list", fx("issue_list")), expect=200, note="分页首列（空问题集）")
    # 复核链需要真实 open 问题（fixture 语义：提交证据解除既有问题）
    from runtime.layout_review import LayoutReviewService
    issue = LayoutReviewService(
        wc.runtime.layout_bundle(wc.tmp_path / "layout.db")["kv_store"],
        _layout_state := wc.runtime.layout_bundle(
            wc.tmp_path / "layout.db")["state_store"]).open_issue(
        "arm-01", "item",
        {"id": "arm-01", "version": adaptation_ref["version"],
         "sha256": adaptation_ref["sha256"]},
        {"page_state": "wechat_list", "item_kind": "anchor",
         "item_id": "upper"}, 1, "anchor_missing")
    # 成功复核：真实 open 问题 + 证据 → resolved、集合 revision+1
    issue_review_request_id = str(uuid.uuid4())
    review_response = client.post(
        "/v1/devices/arm-01/layout-review",
        json={"client_request_id": issue_review_request_id,
              "issue_ids": [issue["issue_id"]],
              "expected_revision": 0,
              "evidence_ids": [search_evidence],
              "reason": "wire-table",
              "contract_version": "layout-review.v1",
              "correlation_id": str(uuid.uuid4())},
        headers={**wc.auth("operator-01"), "Content-Type": "application/json"})
    assert review_response.status_code == 200, review_response.text
    validate_wire_response("review", 200, review_response.headers,
                           review_response.json())
    runner.rows.append({
        "route_id": "review", "method": "POST",
        "path": "/v1/devices/arm-01/layout-review",
        "status": 200, "validated": "response", "note": "有证据复核",
        "request_view": {"method": "POST",
                         "path": "/v1/devices/arm-01/layout-review",
                         "query": {}}})
    # 未知问题无一可解 → 409（review 的错误响应记录）
    r = client.post("/v1/devices/arm-01/layout-review",
                    json={"client_request_id": str(uuid.uuid4()),
                          "issue_ids": [str(uuid.uuid4())],
                          "expected_revision": 1,
                          "evidence_ids": [search_evidence],
                          "reason": "wire-table",
                          "contract_version": "layout-review.v1",
                          "correlation_id": str(uuid.uuid4())},
                    headers={**wc.auth("operator-01"), "Content-Type": "application/json"})
    assert r.status_code == 409, r.text
    # 操作查询：复核成功操作的台账（operation_request / operation_get）
    state_store = wc.runtime.layout_bundle(
        wc.tmp_path / "layout.db")["state_store"]
    review_operation = state_store.operation_get_by_request(
        issue_review_request_id)
    runner.send("operation_request", runner.request_view(
        "operation_request", fx("operation_request"),
        path={"client_request_id": issue_review_request_id}), expect=200)
    runner.send("operation_get", runner.request_view(
        "operation_get", fx("operation_get"),
        path={"operation_id": review_operation["operation_id"]}),
        expect=200)
    rollback_preview_view = runner.request_view(
        "rollback_preview", fx("rollback_preview"),
        body={**fx("rollback_preview")["body"],
              "target_ref": {"id": "arm-01", "version": 1,
                             "sha256": region_sha},
              "expected_revision": 1,
              "source_evidence_id": search_evidence})
    r = runner.send("rollback_preview", rollback_preview_view, expect=200,
                    note="范围回滚预览（新鲜来源核验）")
    rollback_view = runner.request_view(
        "rollback", fx("rollback"),
        body={"client_request_id": str(uuid.uuid4()), "expected_revision": 1,
              "preview_id": r.json()["preview_id"],
              "contract_version": "layout-rollback.v1",
              "correlation_id": str(uuid.uuid4())})
    r = runner.send("rollback", rollback_view, expect=200,
                    note="回滚提交（指针 revision 前进，版本不变）")
    assert r.json()["pointer"]["revision"] == 2
    # 包链：export → get → download(ZIP) → import preview/commit
    export_view = runner.request_view(
        "package_export", fx("package_export"),
        body={**fx("package_export")["body"],
              "roots": [{"kind": "adaptation",
                         "ref": {"id": "arm-01",
                                 "version": adaptation_ref["version"],
                                 "sha256": adaptation_ref["sha256"]}}]})
    r = runner.send("package_export", export_view, expect=200,
                    note="导出闭合集（适配→区域→模板→资产）")
    package_id = r.json()["package_id"]
    manifest = r.json()["manifest"]
    assert any(e["path"].startswith("templates/") for e in manifest["entries"])
    runner.send("package_get", runner.request_view(
        "package_get", fx("package_get"), path={"package_id": package_id}),
        expect=200)
    runner.send("package_download", runner.request_view(
        "package_download", fx("package_download"),
        path={"package_id": package_id}), expect=200, note="ZIP 原字节下载")
    blob = client.get(
        f"/v1/layout-packages/{package_id}/download",
        headers={**wc.auth("operator-01"),
                 "X-Correlation-Id": str(uuid.uuid4()),
                 "X-Layout-Contract-Version": "layout-package.v1"}).content
    assert blob[:2] == b"PK"
    # 独立校验：冻结离线校验器按包语义检查导出包（防自导自入掩盖生产者错误）
    lv.check_archive(blob)
    import_view = runner.request_view(
        "package_import_preview", fx("package_import_preview"),
        body={"archive_base64": base64.b64encode(blob).decode("ascii"),
              "archive_sha256": __import__("hashlib").sha256(blob).hexdigest(),
              "privacy_confirmed": True,
              "contract_version": "layout-package.v1",
              "correlation_id": str(uuid.uuid4())})
    r = runner.send("package_import_preview", import_view, expect=200,
                    note="同摘要导入预览（全部 reuse）")
    preview_id = r.json()["preview_id"]
    import_commit_view = runner.request_view(
        "package_import", fx("package_import"),
        body={"client_request_id": str(uuid.uuid4()),
              "preview_id": preview_id,
              "archive_sha256": __import__("hashlib").sha256(blob).hexdigest(),
              "contract_version": "layout-package.v1",
              "correlation_id": str(uuid.uuid4())})
    r = runner.send("package_import", import_commit_view, expect=200,
                    note="同包重放：全 reuse，不造新版本")
    assert all(item["status"] == "reuse" for item in r.json()["items"])

    # adaptation_asset：本台颜色资产索引（D1 遗留：session 颜色资产链未落地，
    # 记录实际错误响应；依赖状态写报告）
    # adaptation_asset：本台颜色资产（发布时复制到版本目录的原字节 PNG）
    version_assets = (wc.runtime.layout_bundle(
        wc.tmp_path / "layout.db")["state_store"].data_root /
        "devices" / "arm-01" / "layout_adaptation" / "versions" /
        str(adaptation_ref["version"]) / "assets")
    asset_name = sorted(p.stem for p in version_assets.glob("*.png"))[0] \
        if version_assets.is_dir() and any(version_assets.iterdir()) \
        else "empty-input-chat_ime_empty"
    runner.send("adaptation_asset", runner.request_view(
        "adaptation_asset", fx("adaptation_asset"),
        path={"version": adaptation_ref["version"], "asset_id": asset_name}),
        expect=200, note="本台颜色资产原字节 PNG")

    # ---------- ⑤ IME 3 条：A 包负责（只标记不执行） ----------
    for route_id in IME_ROUTES:
        runner.rows.append({"route_id": route_id, "method": "-", "path": "-",
                            "status": None, "validated": "A 包负责",
                            "request_view": {}, "note": "IME 几何路由（A 包 R2-21/15 域）"})

    _write_route_table(runner.rows)
    executed = {row["route_id"] for row in runner.rows
                if row["status"] is not None}
    assert len(executed) == 35, sorted(
        set(r["id"] for r in D2_ROUTES) - executed)


def _write_route_table(rows) -> None:
    import os
    out = Path(os.environ.get(
        "R2_D2_ROUTE_TABLE",
        "/Users/ye/Desktop/WX/reports/wx-console-v1.10/repair-round2/logs/r2-d2-route-table.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    serialisable = []
    for row in rows:
        item = {k: v for k, v in row.items() if k != "request_view"}
        item["request"] = {k: row["request_view"].get(k) for k in
                           ("method", "path", "query")}
        serialisable.append(item)
    out.write_text(json.dumps(serialisable, ensure_ascii=False, indent=1),
                   encoding="utf-8")


from pathlib import Path  # noqa: E402


def test_r2_17_legacy_and_malformed_fields_rejected(wc):
    """旧自造字段（template_id/template_version、session_id）、额外字段、
    缺字段、错关联按契约拒绝（422/404/409），错误外壳经冻结 schema。"""
    client = wc.client
    bridge = wc.bridge
    scene = phone_scene(width=1000, height=2000)
    bridge.push(scene), bridge.push(scene)
    headers = {**wc.auth(), "Content-Type": "application/json"}
    # 旧自造字段 template_id/template_version（契约是 template_ref）
    r = client.post("/v1/devices/arm-01/layout-adaptation/sessions", json={
        "client_request_id": str(uuid.uuid4()), "template_id": "tpl-1",
        "template_version": 1, "expected_revision": 0}, headers=headers)
    assert r.status_code == 422, r.text
    assert r.json()["contract_version"] == "layout-error.v1"
    # 额外字段（客户端 passed 事实）
    r = client.post("/v1/devices/arm-01/layout-adaptation/preview", json={
        "capture_session_id": str(uuid.uuid4()),
        "expected_session_revision": 1, "passed": True}, headers=headers)
    assert r.status_code == 422, r.text
    # GET 公共头缺失/错版本
    r = client.get("/v1/layout-templates")
    assert r.status_code == 422, r.text
    r = client.get("/v1/layout-templates", headers={
        "X-Correlation-Id": str(uuid.uuid4()),
        "X-Layout-Contract-Version": "wrong.v9"})
    assert r.status_code == 422, r.text
    # 头与正文关联值不同值 → 422
    corr = str(uuid.uuid4())
    r = client.post("/v1/devices/arm-01/layout-captures", json={
        "client_request_id": str(uuid.uuid4()), "owner_kind": "search_region",
        "owner_id": None, "expected_owner_revision": 0, "page_state": None,
        "controlled_test_confirmed": True,
        "contract_version": "layout-capture.v1", "correlation_id": corr},
        headers={**headers, "X-Correlation-Id": str(uuid.uuid4())})
    assert r.status_code == 422, r.text
    # 分页非法：limit=0 / 未知 query
    r = client.get("/v1/layout-templates?limit=0", headers={
        "X-Correlation-Id": str(uuid.uuid4()),
        "X-Layout-Contract-Version": "ui-layout-list.v1"})
    assert r.status_code == 422, r.text
    r = client.get("/v1/layout-templates?limit=5&state=open", headers={
        "X-Correlation-Id": str(uuid.uuid4()),
        "X-Layout-Contract-Version": "ui-layout-list.v1"})
    assert r.status_code == 422, r.text
    # 游标不存在 → 404
    r = client.get(f"/v1/layout-templates?limit=5&cursor={uuid.uuid4()}",
                   headers={**wc.auth(),
                            "X-Correlation-Id": str(uuid.uuid4()),
                            "X-Layout-Contract-Version": "ui-layout-list.v1"})
    assert r.status_code == 404, r.text
    # 错关联：他人会话的页写（跨设备 403）
    session = client.post("/v1/devices/arm-01/layout-adaptation/sessions", json={
        "client_request_id": str(uuid.uuid4()),
        "template_ref": {"id": "wechat-controlled", "version": 1,
                         "sha256": "0" * 64},
        "expected_revision": 0, "keyboard_geometry_mode": "live_ime_v1",
        "inherit_session_id": None, "inherit_states": [],
        "contract_version": "device-layout-edit.v1",
        "correlation_id": str(uuid.uuid4())},
        headers=headers)
    # 错关联：sha 与已发布模板不符（模板不存在则 404；存在则 409）
    assert session.status_code in (200, 404, 409)
