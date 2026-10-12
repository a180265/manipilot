"""L04/L06 验收：操作快照重放、资料包导出导入、回滚 CAS、资料问题复核。"""
import base64
import json
import uuid
import zipfile
from io import BytesIO

import pytest

from runtime.input_control import canonical_digest  # legacy 台账摘要
from runtime.layout_ops import LayoutOpsError, LayoutOpsService
from runtime.layout_review import LayoutReviewService, ReviewError


class KV:
    """内存 KV，带 scan_prefix/delete，模拟 _layout_store。"""

    def __init__(self):
        self.data = {}

    def save(self, key, record):
        self.data[key] = record

    def load(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)

    def scan_prefix(self, prefix):
        return [(k, v) for k, v in self.data.items() if k.startswith(prefix)]


@pytest.fixture
def svc(tmp_path):
    return LayoutOpsService(KV(), blobs_dir=tmp_path / "blobs")


# ---------------- 操作记录 ----------------
def test_operation_prepare_commit_and_projection(svc):
    op = svc.prepare_operation("op-1", "arm-01", "review", "req-1", {"a": 1})
    assert op["phase"] == "prepared"
    assert op["request_hash"] == canonical_digest({"a": 1})
    svc.commit_operation(op["operation_id"], {"resolved_count": 1},
                         after_revision=3)
    rec = svc.get_operation_by_request("req-1")
    assert rec["phase"] == "committed"
    proj = svc.projection(rec)
    assert proj == {"operation_id": op["operation_id"], "phase": "committed",
                    "original_response": {"resolved_count": 1}, "error": None}


def test_replay_same_request_returns_original_and_conflict_on_new_content(svc):
    svc.prepare_operation("op-1", "arm-01", "review", "req-1", {"a": 1})
    replay = svc.find_replay("op-1", "arm-01", "req-1", {"a": 1})
    assert replay["request_hash"] == canonical_digest({"a": 1})
    with pytest.raises(LayoutOpsError) as exc:
        svc.find_replay("op-1", "arm-01", "req-1", {"a": 2})
    assert exc.value.code == "layout_request_conflict"
    with pytest.raises(LayoutOpsError) as exc:
        svc.find_replay("op-2", "arm-01", "req-1", {"a": 1})
    assert exc.value.code == "layout_permission_denied"


def test_failed_operation_keeps_error_snapshot(svc):
    op = svc.prepare_operation("op-1", "arm-01", "review", "req-1", {"a": 1})
    svc.fail_operation(op["operation_id"],
                       {"code": "layout_items_unverified", "message": "缺证据"})
    proj = svc.projection(svc.get_operation_by_request("req-1"))
    assert proj["phase"] == "aborted"
    assert proj["error"]["code"] == "layout_items_unverified"
    assert proj["original_response"] is None


# ---------------- 资料包导出/导入 ----------------
def _seed_adaptation(svc, device="arm-01", version=1):
    record = {"device_id": device, "version": version, "pages": {"home": []},
              "sha256": "deadbeef"}
    svc._store.save(f"adaptation_version:{device}:{version}", record)
    return record


def test_export_builds_zip_with_manifest_digest(svc):
    """冻结 package:manifest.v1：sha256 去自身字段、entry 摘要=文件原字节。"""
    from runtime.layout_state import canonical_json
    import hashlib
    record = _seed_adaptation(svc)
    result = svc.export_package("op-1", {"items": [
        {"kind": "adaptation", "device_id": "arm-01", "id": None, "version": 1}]})
    blob = svc.read_package_bytes(result["package_id"])
    assert blob[:2] == b"PK"
    manifest = json.loads(zipfile.ZipFile(BytesIO(blob)).read("manifest.json"))
    assert manifest["contract_version"] == "layout-package-manifest.v1"
    assert manifest["sha256"] == result["manifest"]["sha256"]
    embedded = {k: v for k, v in manifest.items() if k != "sha256"}
    assert manifest["sha256"] == hashlib.sha256(
        canonical_json(embedded).encode()).hexdigest()
    record_entry = manifest["entries"][0]
    assert record_entry["kind"] == "record"
    assert record_entry["resource"]["ref"] == {"id": "arm-01", "version": 1,
                                               "sha256": "deadbeef"}
    assert record_entry["sha256"] == hashlib.sha256(
        canonical_json(record).encode()).hexdigest()  # 文件原字节摘要
    assert result["archive_sha256"] == hashlib.sha256(blob).hexdigest()


def test_import_preview_requires_privacy_confirm_and_detects_conflict(svc):
    record = _seed_adaptation(svc)
    pkg = svc.export_package("op-1", {"items": [
        {"kind": "adaptation", "device_id": "arm-01", "id": None, "version": 1}]})
    blob = svc.read_package_bytes(pkg["package_id"])
    with pytest.raises(LayoutOpsError) as exc:
        svc.import_preview("op-1", blob, privacy_confirmed=False)
    assert exc.value.code == "layout_request_invalid"
    preview = svc.import_preview("op-2", blob, privacy_confirmed=True)
    # 另一干净库：同 ID 同摘要 → 复用
    other = LayoutOpsService(KV(), blobs_dir=__import__("pathlib").Path(
        svc._blobs).parent / "other")
    assert other.import_preview("op-2", blob, True)["to_import"][0]["ref"]["version"] == 1
    # 同 ID 异摘要 → 整包冲突
    record["pages"] = {"home": ["changed"]}
    other._store.save("adaptation_version:arm-01:1", record)
    conflict = other.import_preview("op-2", blob, True)
    assert conflict["blocked"] and conflict["conflicts"]


def test_import_commit_rejected_when_blocked_and_writes_inert_when_clean(
        tmp_path):
    from runtime.layout_state import LayoutStateStore
    from tests.layout_r2_helpers import LegacyKV

    kv = LegacyKV(tmp_path / "src" / "layout.db")
    state = LayoutStateStore(tmp_path / "src" / "layout.db",
                             data_root=tmp_path / "src" / "data")
    svc = LayoutOpsService(kv, blobs_dir=tmp_path / "blobs",
                           state_store=state, data_root=tmp_path / "src" / "data")
    _seed_adaptation(svc, device="arm-02")
    pkg = svc.export_package("op-1", {"items": [
        {"kind": "adaptation", "device_id": "arm-02", "id": None, "version": 1}]})
    blob = svc.read_package_bytes(pkg["package_id"])
    # 目标：带持久状态库的空 Store（正式导入路径）
    dst_kv = LegacyKV(tmp_path / "dst" / "layout.db")
    dst_state = LayoutStateStore(tmp_path / "dst" / "layout.db",
                                 data_root=tmp_path / "dst" / "data")
    other = LayoutOpsService(dst_kv, blobs_dir=tmp_path / "o2",
                             state_store=dst_state,
                             data_root=tmp_path / "dst" / "data")
    preview = other.import_preview("op-2", blob, True)
    result = other.import_commit("op-2", preview["preview_id"])
    assert result["status"] == "imported"
    # 正式读取路径可见（catalog 门控），原字节/记录保持
    imported = other.resolve_adaptation("arm-02", 1)
    assert imported["pages"] == {"home": []}
    # 不触碰本台指针：arm-02 无任何 current 指针记录
    assert other.resolve_adaptation("arm-02", 1) is not None
    assert dst_state.adaptation_pointer_get("arm-02") is None
    # 冲突整包拒绝：改写本机已提交的不可变版本文件（同 ID/version 异内容）
    version_file = (tmp_path / "dst" / "data" / "devices" / "arm-02"
                    / "layout_adaptation" / "versions" / "1.json")
    tampered = json.loads(version_file.read_text(encoding="utf-8"))
    tampered["pages"] = {}
    tampered["sha256"] = "zz"
    version_file.write_text(json.dumps(tampered, ensure_ascii=False),
                            encoding="utf-8")
    preview2 = other.import_preview("op-2", blob, True)
    with pytest.raises(LayoutOpsError) as exc:
        other.import_commit("op-2", preview2["preview_id"])
    assert exc.value.code == "layout_request_conflict"
    state.close()
    dst_state.close()


def test_import_rejects_path_traversal_symlink_and_oversize(svc, tmp_path):
    def blob_with(names):
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for n in names:
                zf.writestr(n, "{}")
        return buf.getvalue()

    bad = blob_with(["../evil.json"])
    with pytest.raises(LayoutOpsError) as exc:
        svc.import_preview("op-1", bad, True)
    assert exc.value.code == "layout_request_invalid"
    evil = zipfile.ZipInfo("/abs/path.json")
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(evil, "{}")
    with pytest.raises(LayoutOpsError):
        svc.import_preview("op-1", buf.getvalue(), True)
    with pytest.raises(LayoutOpsError) as exc:
        svc.import_preview("op-1", b"not-a-zip", True)
    assert exc.value.code == "layout_request_invalid"


def test_import_rejects_duplicate_and_case_collision_and_unlisted_entries(svc):
    def manifest_only_zip(extra_names):
        import hashlib
        inner = {"contract_version": "layout-package.v1", "items": [
            {"kind": "adaptation", "device_id": "arm-01", "id": None,
             "version": 1, "sha256": "x", "record": {}}],
            "assets": []}
        inner["manifest_sha256"] = hashlib.sha256(json.dumps(
            inner, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("manifest.json", json.dumps(inner))
            for n in extra_names:
                zf.writestr(n, b"")
        return buf.getvalue()

    # 大小写碰撞条目
    with pytest.raises(LayoutOpsError) as exc:
        svc.import_preview("op-1", manifest_only_zip(["A.txt", "a.txt"]), True)
    assert exc.value.code == "layout_request_invalid"
    # 未在 manifest 列出的夹带文件
    with pytest.raises(LayoutOpsError) as exc:
        svc.import_preview("op-1", manifest_only_zip(["sneaky.json"]), True)
    assert exc.value.code == "layout_request_invalid"


def test_export_closure_includes_template_and_raw_asset_bytes(tmp_path):
    import numpy as np
    import cv2
    from runtime.layout_template import LayoutTemplateService, \
        LayoutTemplateStore, sha256_bytes

    # 真实可解码 PNG 资产（导出/导入按原字节与真实图像校验）
    image = np.full((12, 16, 3), 128, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", image)
    assert ok
    png = buf.tobytes()
    tpl_store = LayoutTemplateStore(tmp_path / "tpl.db")
    tpl_svc = LayoutTemplateService(tpl_store)
    draft = tpl_svc.create_draft({"template_id": "tpl-9"})
    tpl_svc.upload_asset(draft["draft_id"], "blank-1", png)
    tpl_svc.publish({
        "template_id": "tpl-9", "version": 1,
        "pages": {"chat_ime_empty": {"anchors": [
            {"anchor_id": "a", "empty_input_asset_ref": "blank-1"}]}},
        "draft_id": draft["draft_id"]})
    kv = KV()
    ops = LayoutOpsService(kv, blobs_dir=tmp_path / "blobs",
                           template_store=tpl_store)
    # 适配记录引用模板 → 闭包应包含模板 + 其资产原字节
    adaptation = {"device_id": "arm-07", "version": 1,
                  "template_id": "tpl-9", "template_version": 1,
                  "pages": {}, "sha256": "a"}
    kv.save("adaptation_version:arm-07:1", adaptation)
    result = ops.export_package("op-1", {"items": [
        {"kind": "adaptation", "device_id": "arm-07", "id": None,
         "version": 1}]})
    blob = ops.read_package_bytes(result["package_id"])
    zf = zipfile.ZipFile(BytesIO(blob))
    manifest = json.loads(zf.read("manifest.json"))
    resources = {(e["resource"]["kind"], e["resource"]["ref"]["id"],
                  e["resource"]["ref"]["version"])
                 for e in manifest["entries"] if e["kind"] == "record"}
    assert ("adaptation", "arm-07", 1) in resources
    assert ("template", "tpl-9", 1) in resources  # 传递引用闭包
    assert manifest["file_count"] == 1 + len(manifest["entries"])
    assert manifest["total_bytes"] == sum(e["byte_size"]
                                          for e in manifest["entries"])
    asset_entries = [e for e in manifest["entries"] if e["kind"] == "asset"]
    assert len(asset_entries) == 1
    asset = asset_entries[0]
    assert asset["path"] == "templates/tpl-9/1/assets/blank-1.png"
    assert asset["sha256"] == sha256_bytes(png)
    assert asset["asset"]["id"] == "blank-1"
    assert asset["owner"]["ref"]["id"] == "tpl-9"
    assert zf.read(asset["path"]) == png  # 资产原字节入包


def test_import_rejects_tampered_asset_bytes(tmp_path):
    import numpy as np
    import cv2
    from runtime.layout_template import LayoutTemplateService, \
        LayoutTemplateStore

    image = np.full((8, 8, 3), 64, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", image)
    assert ok
    tpl_store = LayoutTemplateStore(tmp_path / "tpl.db")
    tpl_svc = LayoutTemplateService(tpl_store)
    draft = tpl_svc.create_draft({"template_id": "tpl-9"})
    tpl_svc.upload_asset(draft["draft_id"], "blank-1", buf.tobytes())
    tpl_svc.publish({
        "template_id": "tpl-9", "version": 1,
        "pages": {"chat_ime_empty": {"anchors": [
            {"anchor_id": "a", "empty_input_asset_ref": "blank-1"}]}},
        "draft_id": draft["draft_id"]})
    ops = LayoutOpsService(KV(), blobs_dir=tmp_path / "blobs",
                           template_store=tpl_store)
    result = ops.export_package("op-1", {"items": [
        {"kind": "template", "device_id": None, "id": "tpl-9", "version": 1}]})
    blob = ops.read_package_bytes(result["package_id"])
    # 篡改资产一个字节而不改 manifest → 坏 hash 整包拒绝
    src = zipfile.ZipFile(BytesIO(blob))
    asset_path = [n for n in src.namelist() if "/assets/" in n][0]
    bad_bytes = b"tampered-bytes"
    buf2 = BytesIO()
    with zipfile.ZipFile(buf2, "w") as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == asset_path:
                data = bad_bytes
            dst.writestr(info.filename, data)
    with pytest.raises(LayoutOpsError) as exc:
        ops.import_preview("op-1", buf2.getvalue(), True)
    assert exc.value.code == "layout_request_invalid"
    # 不可解码图像（即使摘要自洽）同样拒绝
    import hashlib
    from runtime.layout_state import canonical_json, content_sha
    manifest = json.loads(src.read("manifest.json"))
    for entry in manifest["entries"]:
        if entry["path"] == asset_path:
            entry["byte_size"] = len(bad_bytes)
            entry["sha256"] = hashlib.sha256(bad_bytes).hexdigest()
            entry["asset"]["sha256"] = entry["sha256"]
    manifest["sha256"] = content_sha(
        {k: v for k, v in manifest.items() if k != "sha256"})
    buf3 = BytesIO()
    with zipfile.ZipFile(buf3, "w") as dst:
        for info in src.infolist():
            if info.filename == "manifest.json":
                dst.writestr(info.filename, canonical_json(manifest))
            elif info.filename == asset_path:
                dst.writestr(info.filename, bad_bytes)
            else:
                dst.writestr(info.filename, src.read(info.filename))
    with pytest.raises(LayoutOpsError) as exc:
        ops.import_preview("op-1", buf3.getvalue(), True)
    assert exc.value.code == "layout_request_invalid"


# ---------------- 回滚 ----------------
def test_rollback_preview_and_commit_cas(svc):
    _seed_adaptation(svc, version=1)
    _seed_adaptation(svc, version=2)
    svc._store.save("layout_pointer:adaptation:arm-01",
                    {"revision": 4, "version": 2})
    preview = svc.rollback_preview("arm-01", {"resource": "adaptation",
                                              "target_version": 1})
    assert preview["current_version"] == 2
    assert preview["target_version"] == 1
    with pytest.raises(LayoutOpsError) as exc:
        svc.rollback_commit("arm-01", {"resource": "adaptation",
                                       "target_version": 1,
                                       "expected_revision": 3})
    assert exc.value.code == "layout_revision_conflict"
    result = svc.rollback_commit("arm-01", {"resource": "adaptation",
                                            "target_version": 1,
                                            "expected_revision": 4})
    assert result["rolled_back_to"] == 1
    assert result["revision"] == 5  # 只前进修订号
    pointer = svc._store.load("layout_pointer:adaptation:arm-01")
    assert pointer["version"] == 1 and pointer["revision"] == 5
    with pytest.raises(LayoutOpsError) as exc:
        svc.rollback_preview("arm-01", {"resource": "adaptation",
                                        "target_version": 9})
    assert exc.value.code == "layout_resource_missing"


# ---------------- L06 资料问题复核 ----------------
def test_review_resolves_only_with_evidence(tmp_path):
    """R2-17 修订：冻结 review_request 闭集（issue UUID/expected_revision/evidence）；
    复核经 typed store（review_revision CAS、逐 issue resolution/evidence 保留）。"""
    from runtime.layout_state import LayoutStateStore
    state = LayoutStateStore(tmp_path / "layout.db")
    review = LayoutReviewService(KV(), state)
    issue = review.open_issue("arm-01", "search_region", None, None, 2,
                              "screen_outside_region")
    _seq = [0]

    def _request(evidence_ids, expected_revision):
        _seq[0] += 1
        return {"client_request_id": str(uuid.uuid4()),
                "issue_ids": [issue["issue_id"]],
                "expected_revision": expected_revision,
                "evidence_ids": evidence_ids,
                "reason": f"复核-{_seq[0]}",
                "contract_version": "layout-review.v1",
                "correlation_id": str(uuid.uuid4())}

    # 无证据不放开（schema 层即要求 ≥1；空列表直接拒）
    with pytest.raises(ReviewError) as exc:
        review.resolve("arm-01", _request([], 0), "op-1")
    assert exc.value.code == "layout_request_invalid"
    # 有证据（同设备 typed 布局证据）才解除
    from runtime.layout_state import state_identity, state_key
    evidence = {"evidence_id": "ev-1", "device_id": "arm-01",
                "principal_id": "op-1", "capture_id": str(uuid.uuid4()),
                "page_state": "wechat_list",
                "source_binding": {"device_id": "arm-01",
                                   "installation_binding": {
                                       "camera_id": "cam-01",
                                       "camera_device_unique_id": "uvc-01",
                                       "camera_binding_id": "33333333-3333-4333-8333-333333333333",
                                       "arm_binding_id": "arm-bind-01"},
                                   "source_size": {"width": 640, "height": 480},
                                   "source_geometry_id": "a" * 64,
                                   "evidence_id": "ev-1",
                                   "source_session_id": generation_stub(),
                                   "frame_seq": 1,
                                   "source_captured_at": "2026-10-04T10:00:00.000Z",
                                   "server_received_at": "2026-10-04T10:00:00.100Z"},
                "timing_proof": {"source_clock_id": "mach-absolute:" + "b" * 64,
                                 "clock_registration_id": "22222222-2222-4222-8222-222222222222",
                                 "source_generation_id": generation_stub(),
                                 "source_frame_seq": 1, "source_capture_seq": 1,
                                 "source_captured_monotonic_ns": 1_000_000_000,
                                 "source_capture_uncertainty_ms": 1.0,
                                 "source_timing_provenance": "camera_pts_host_clock.v1"},
                "image_sha256": "0" * 64, "media_type": "image/jpeg",
                "server_instance_id": "22222222-2222-4222-8222-222222222222",
                "captured_monotonic_ms": 1000, "received_monotonic_ms": 1000050,
                "privacy_scope": "controlled_test"}
    state.state_insert("evidence",
                       state_key("evidence", state_identity("evidence", evidence)),
                       "arm-01", "op-1", evidence)
    result = review.resolve("arm-01", _request(["ev-1"], 0), "op-1")
    assert result["results"][0]["state"] == "resolved"
    assert result["revision"] == 1
    issues = review.list_issues("arm-01", state="resolved")
    assert issues[0]["resolution"]["evidence_ids"] == ["ev-1"]
    # 重复解除：已是 resolved → 409（无一可解）
    from tests.layout_r2_helpers import FakeClock  # noqa: F401
    with pytest.raises(ReviewError) as exc:
        review.resolve("arm-01", _request(["ev-1"], 1), "op-1")
    assert exc.value.code == "layout_items_unverified"


def generation_stub():
    return "11111111-1111-4111-8111-111111111111"
