"""R3-13：导入完整提交后才可见（统一可见性判断 + 意图先行）。

缺陷（修前）：`layout_ops.py::import_commit` 资源先落盘（模板进
LayoutTemplateStore、区域进 region store、适配版本文件落盘），catalog
最后才统一提交——中间任何失败/重启，模板/资产/适配对正式 GET、列表、
资产下载、适配选模板**已经可读可选**（半包窗口）；且操作意图在写入
之后才持久。

断言（缺陷卡 R3-13 A/B/C）：
- A：合法包导入后正式 GET 模板/资产可见（旧 R2-20 回归钉住，此处复核）；
- B1：资源已写、catalog 未提交的中间态（真实调用 _apply_import）——
  解析器（GET/资产/适配选模板）全部不可见；
- B2：catalog 提交时刻真实中断（异常不被本地 abort 吸收）→ 整包不可见；
  重启对账 aborted；同号重放 409 引导对账；新编号重试幂等完成后可见；
- B3：catalog 已提交、响应前故障 → 已提交资源可见（不误伤），
  对账按原意图幂等完成后同号重放原结果；
- C：同号重试保留原结果；并发同 ID 异内容无半包（恰好一个整包胜出）；
  重复导入同包不破坏已提交且被复用的资源（catalog 原行保留）。
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import pytest

from runtime.layout_ops import LayoutOpsError, LayoutOpsService
from runtime.layout_state import LayoutStateError, LayoutStateStore
from tests.layout_r2_helpers import (
    DEFAULT_BINDING,
    FakeCalibrationProvider,
    FakeCaptureProvider,
    FakeClock,
    LayoutBundle,
    build_page_frames,
    build_template_assets,
)


def _bundle(root, seed=True):
    return LayoutBundle(root, clock=FakeClock(),
                        capture_provider=FakeCaptureProvider(
                            build_page_frames(build_template_assets()),
                            binding=DEFAULT_BINDING,
                            source_geometry_id="a" * 64),
                        calibration_provider=FakeCalibrationProvider(),
                        template_seed=seed)


def _publish_and_export(bundle):
    """第一台真实四态发布 + 闭合导出（模板+资产+区域+适配）。"""
    svc = bundle.adaptation_svc
    sid = svc.start_session("arm-01", {
        "template_ref": {"id": "tpl-1", "version": 1},
        "principal_id": "operator",
        "installation_binding": DEFAULT_BINDING})["session_id"]
    revision = 1
    for state in ("wechat_list", "chat_ime_hidden", "chat_ime_empty",
                  "chat_ime_text"):
        capture = svc.capture("arm-01", {
            "client_request_id": str(uuid.uuid4()),
            "owner_kind": "session", "owner_id": sid,
            "expected_owner_revision": revision, "page_state": state,
            "controlled_test_confirmed": True})
        view = svc.write_page("arm-01", sid, state, {
            "client_request_id": str(uuid.uuid4()),
            "expected_session_revision": revision,
            "source_evidence_ids": capture["source_evidence_ids"],
            "local_overrides": []})
        revision = view["session_revision"]
    preview = svc.preview("arm-01", {"capture_session_id": sid})
    svc.publish("arm-01", {
        "client_request_id": str(uuid.uuid4()), "session_id": sid,
        "preview_id": preview["preview_id"], "principal_id": "operator",
        "expected_revision": 0, "accepted_items": preview["accepted_items"]})
    export = bundle.ops_svc.export_package("op-src", {"items": [
        {"kind": "adaptation", "device_id": "arm-01", "id": "arm-01",
         "version": 1},
        {"kind": "region", "device_id": "arm-01", "id": "arm-01",
         "version": 1}]})
    return bundle.ops_svc.read_package_bytes(export["package_id"])


def assert_fully_invisible(ops) -> None:
    """未提交资源：解析器（GET/选模板）/资产/适配版本全部不可见。

    统一可见性新接口以 getattr 探测（红测在修前代码上先落
    resolve_template 目标断言；修后全断言生效）。"""
    assert ops.resolve_template("tpl-1", 1) is None, \
        "未提交模板不得经统一解析器可见（半包）"
    assert ops.resolve_adaptation("arm-01", 1) is None, \
        "未提交适配版本不得读取"
    list_fn = getattr(ops, "list_visible_templates", None)
    if list_fn is not None:
        assert list_fn() == [], "未提交模板不得进入正式列表"
    asset_fn = getattr(ops, "resolve_template_asset", None)
    if asset_fn is not None:
        assert asset_fn("tpl-1", 1, "asset-back_button") is None, \
            "未提交模板资产不得下载"


def assert_invisible_for_adaptation(bundle) -> None:
    from runtime.layout_adaptation import AdaptationError
    with pytest.raises(AdaptationError) as exc:
        bundle.adaptation_svc.start_session("arm-09", {
            "template_ref": {"id": "tpl-1", "version": 1},
            "principal_id": "operator",
            "installation_binding": DEFAULT_BINDING})
    assert exc.value.code in ("template_not_found", "invalid_request"), \
        "适配选模板不得选中未提交模板"


# ---------------- A：合法包导入后正式可见（复核锚） ----------------
def test_r3_13_a_committed_import_visible_and_selectable(tmp_path):
    src = _bundle(tmp_path / "src")
    try:
        blob = _publish_and_export(src)
        dst = _bundle(tmp_path / "dst", seed=False)
        try:
            preview = dst.ops_svc.import_preview("op-dst", blob, True)
            commit = dst.ops_svc.import_commit(
                "op-dst", preview["preview_id"],
                client_request_id=str(uuid.uuid4()),
                archive_sha256=preview["archive_sha256"])
            assert commit["status"] == "imported"
            assert dst.ops_svc.resolve_template("tpl-1", 1) is not None
            assert dst.ops_svc.resolve_adaptation("arm-01", 1) is not None
            session = dst.adaptation_svc.start_session("arm-09", {
                "template_ref": {"id": "tpl-1", "version": 1},
                "principal_id": "operator",
                "installation_binding": DEFAULT_BINDING})
            assert dst.adaptation_svc.get_session(
                "arm-09", session["session_id"]) is not None
        finally:
            dst.close()
    finally:
        src.close()


# ---------------- B1：资源已写、catalog 未提交 → 全部不可见 ----------------
def test_r3_13_b1_uncommitted_store_writes_are_invisible(tmp_path):
    src = _bundle(tmp_path / "src")
    try:
        blob = _publish_and_export(src)
        dst = _bundle(tmp_path / "dst", seed=False)
        try:
            preview = dst.ops_svc.import_preview("op-dst", blob, True)
            assert preview["can_import"] is True
            # 真实中间态：资源写入（_apply_import）后、catalog 提交前
            zf = zipfile.ZipFile(io.BytesIO(blob))
            dst.ops_svc._apply_import(preview["manifest"], zf)
            assert_fully_invisible(dst.ops_svc)
            assert_invisible_for_adaptation(dst)
        finally:
            dst.close()
    finally:
        src.close()


# ---------------- B2：catalog 提交时刻中断 → 不可见 + 重启对账 ----------------
def test_r3_13_b2_catalog_crash_invisible_then_reconcile(tmp_path):
    src = _bundle(tmp_path / "src")
    try:
        blob = _publish_and_export(src)
        dst = _bundle(tmp_path / "dst", seed=False)
        request_id = str(uuid.uuid4())
        try:
            preview = dst.ops_svc.import_preview("op-dst", blob, True)
            original = LayoutOpsService._commit_catalog

            def crash(self, rows, op_id):
                raise RuntimeError("注入：catalog 提交进程中断（无 abort）")

            LayoutOpsService._commit_catalog = crash
            try:
                with pytest.raises(RuntimeError):
                    dst.ops_svc.import_commit(
                        "op-dst", preview["preview_id"],
                        client_request_id=request_id,
                        archive_sha256=preview["archive_sha256"])
            finally:
                LayoutOpsService._commit_catalog = original
            # 中断 → 未提交资源不可读不可选
            assert_fully_invisible(dst.ops_svc)
            assert_invisible_for_adaptation(dst)
        finally:
            dst.close()

        # 重启（同一临时根重新装配）：对账 aborted；同号重放 409 引导对账；
        # 新编号重试幂等完成后可见
        dst2 = _bundle(tmp_path / "dst", seed=False)
        try:
            report = dst2.state.reconcile()
            assert any(entry.get("settled") == "aborted"
                       for entry in report), report
            assert_fully_invisible(dst2.ops_svc)
            with pytest.raises(LayoutOpsError) as exc:
                dst2.ops_svc.import_commit(
                    "op-dst", str(uuid.uuid4()),
                    client_request_id=request_id,
                    archive_sha256=preview["archive_sha256"])
            assert exc.value.code == "layout_request_conflict"
            # 新编号重试：幂等完成（资源复用）→ 可见
            preview2 = dst2.ops_svc.import_preview("op-dst", blob, True)
            commit = dst2.ops_svc.import_commit(
                "op-dst", preview2["preview_id"],
                client_request_id=str(uuid.uuid4()),
                archive_sha256=preview2["archive_sha256"])
            assert commit["status"] == "imported"
            assert dst2.ops_svc.resolve_template("tpl-1", 1) is not None
            asset_fn = getattr(dst2.ops_svc, "resolve_template_asset", None)
            if asset_fn is not None:
                assert asset_fn("tpl-1", 1, "asset-back_button") is not None
            assert dst2.ops_svc.resolve_adaptation("arm-01", 1) is not None
        finally:
            dst2.close()
    finally:
        src.close()


# ---------------- B3：catalog 已提交、响应前故障 → 已提交可见 ----------------
def test_r3_13_b3_failure_before_response_keeps_committed_visible(tmp_path):
    src = _bundle(tmp_path / "src")
    try:
        blob = _publish_and_export(src)
        dst = _bundle(tmp_path / "dst", seed=False)
        request_id = str(uuid.uuid4())
        try:
            preview = dst.ops_svc.import_preview("op-dst", blob, True)
            original = LayoutStateStore.operation_commit

            def crash(self, operation_id, **kwargs):
                raise RuntimeError("注入：catalog 已提交、响应前进程中断")

            LayoutStateStore.operation_commit = crash
            try:
                with pytest.raises(RuntimeError):
                    dst.ops_svc.import_commit(
                        "op-dst", preview["preview_id"],
                        client_request_id=request_id,
                        archive_sha256=preview["archive_sha256"])
            finally:
                LayoutStateStore.operation_commit = original
            # catalog 已提交：已提交资源可见（不得误伤已提交资料）
            assert dst.ops_svc.resolve_template("tpl-1", 1) is not None, \
                "已提交 catalog 的资源必须可见"
            # 台账未终结：同号重放引导对账（409），不执行第二次
            operation = dst.ops_svc.get_operation_by_request(request_id)
            assert operation is not None
            assert operation["phase"] in ("prepared", "unknown", "aborted")
            with pytest.raises(LayoutOpsError) as exc:
                dst.ops_svc.import_commit(
                    "op-dst", preview["preview_id"],
                    client_request_id=request_id,
                    archive_sha256=preview["archive_sha256"])
            assert exc.value.code == "layout_request_conflict"
        finally:
            dst.close()

        # 重启对账：catalog 变更已全部落库 → 按原意图幂等完成（不重建资源）
        dst2 = _bundle(tmp_path / "dst", seed=False)
        try:
            report = dst2.state.reconcile()
            entry = next(e for e in report
                         if e.get("resource_kind") == "package")
            assert entry["settled"] == "committed", report
            replay = dst2.ops_svc.import_commit(
                "op-dst", preview["preview_id"],
                client_request_id=request_id,
                archive_sha256=preview["archive_sha256"])
            assert replay["status"] == "imported"
            assert replay["replayed"] is True, "对账完成后同号重放原结果"
            assert dst2.ops_svc.resolve_template("tpl-1", 1) is not None
        finally:
            dst2.close()
    finally:
        src.close()


# ---------------- C：同号重试 / 复用不破坏 / 并发异内容 ----------------
def test_r3_13_c_same_id_retry_and_reuse_preserves_catalog(tmp_path):
    src = _bundle(tmp_path / "src")
    try:
        blob = _publish_and_export(src)
        dst = _bundle(tmp_path / "dst", seed=False)
        try:
            preview = dst.ops_svc.import_preview("op-dst", blob, True)
            request_id = str(uuid.uuid4())
            first = dst.ops_svc.import_commit(
                "op-dst", preview["preview_id"],
                client_request_id=request_id,
                archive_sha256=preview["archive_sha256"])
            assert first["status"] == "imported"
            replay = dst.ops_svc.import_commit(
                "op-dst", preview["preview_id"],
                client_request_id=request_id,
                archive_sha256=preview["archive_sha256"])
            assert replay["replayed"] is True
            assert replay["operation_id"] == first["operation_id"]

            def catalog_rows(store):
                return {(r["value"]["resource"]["kind"],
                         r["value"]["resource"]["ref"]["id"],
                         r["value"]["resource"]["ref"]["version"]):
                        (r["value"]["operation_id"],
                         r["value"]["created_at"])
                        for r in store.state.state_scan("catalog")}

            rows_before = catalog_rows(dst)
            # 重复导入同包（新编号）：全部复用，catalog 原行不重建
            preview2 = dst.ops_svc.import_preview("op-dst", blob, True)
            assert preview2["to_import"] == []
            second = dst.ops_svc.import_commit(
                "op-dst", preview2["preview_id"],
                client_request_id=str(uuid.uuid4()),
                archive_sha256=preview2["archive_sha256"])
            assert second["status"] == "imported"
            assert catalog_rows(dst) == rows_before, \
                "复用不得重建/改写已提交 catalog 行（operation_id/created_at 保留）"
        finally:
            dst.close()
    finally:
        src.close()


def test_r3_13_c_concurrent_same_id_conflicting_content_no_half_package(
        tmp_path):
    """并发同 ID 异内容：恰好一个整包胜出，另一个整包拒绝；无半包可见。"""
    from runtime.layout_ops import canonical_digest
    from runtime.layout_state import canonical_json
    src = _bundle(tmp_path / "src")
    try:
        blob = _publish_and_export(src)
        manifest = json.loads(
            zipfile.ZipFile(io.BytesIO(blob)).read("manifest.json"))
        src_zip = zipfile.ZipFile(io.BytesIO(blob))
        entries = []
        for entry in manifest["entries"]:
            data = src_zip.read(entry["path"])
            if entry["path"].endswith("templates/tpl-1/1/record.json"):
                record = json.loads(data)
                record["pages"] = {}
                data = canonical_json(record).encode("utf-8")
            entry["byte_size"] = len(data)
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            entries.append((entry, data))
        manifest["entries"] = [entry for entry, _ in entries]
        manifest["total_bytes"] = sum(e["byte_size"] for e, _ in entries)
        manifest.pop("sha256")
        manifest["sha256"] = canonical_digest(manifest)
        buf2 = io.BytesIO()
        with zipfile.ZipFile(buf2, "w") as out:
            out.writestr("manifest.json", canonical_json(manifest))
            for entry, data in entries:
                out.writestr(entry["path"], data)
        conflicting = buf2.getvalue()

        dst = _bundle(tmp_path / "dst", seed=False)
        try:
            p1 = dst.ops_svc.import_preview("op-a", blob, True)
            p2 = dst.ops_svc.import_preview("op-b", conflicting, True)
            results = {}

            def run(tag, preview, package):
                try:
                    results[tag] = dst.ops_svc.import_commit(
                        f"op-{tag}", preview["preview_id"],
                        client_request_id=str(uuid.uuid4()),
                        archive_sha256=hashlib.sha256(package).hexdigest())
                except LayoutOpsError as exc:
                    results[tag] = exc

            t1 = threading.Thread(target=run, args=("a", p1, blob))
            t2 = threading.Thread(target=run, args=("b", p2, conflicting))
            t1.start()
            t2.start()
            t1.join()
            t2.join()
            def outcome(value):
                if isinstance(value, LayoutOpsError):
                    return value.code
                if isinstance(value, dict):
                    return value.get("status") or value.get("code")
                return type(value).__name__

            outcomes = {tag: outcome(value)
                        for tag, value in results.items()}
            statuses = sorted(outcomes[tag] for tag in ("a", "b"))
            assert statuses[0] == "imported", outcomes
            assert statuses[1] in ("layout_request_conflict",
                                   "layout_revision_conflict"), \
                f"败者必须整包 409 拒绝：{outcomes}"
            # 胜出内容整包提交可见（记录/资产/适配一致），绝无半包：
            # 模板恰为两包之一的完整内容；胜出包的适配记录必须可见；
            # catalog 无重复/残留行。
            template = dst.ops_svc.resolve_template("tpl-1", 1)
            assert template is not None
            pages = template.get("pages") or {}
            assert "wechat_list" in pages or pages == {}, \
                "模板必须恰为两包之一的完整内容（不得混合/半包）"
            adaptation = dst.ops_svc.resolve_adaptation("arm-01", 1)
            assert adaptation is not None, \
                "胜出包的适配记录必须整包提交可见"
            catalog_keys = [(r["value"]["resource"]["kind"],
                             r["value"]["resource"]["ref"]["id"],
                             r["value"]["resource"]["ref"]["version"])
                            for r in dst.state.state_scan("catalog")]
            assert len(catalog_keys) == len(set(catalog_keys)), catalog_keys
        finally:
            dst.close()
    finally:
        src.close()
