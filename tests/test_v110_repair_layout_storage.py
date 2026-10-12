"""v1.10 实现审查修复回归（第一轮 F 编号 → 第二轮 R2 修订后断言）。

F 编号到测试函数映射（FIX-13/FIX-14 运行时部分；第二轮按 R2-16/19/20 修订：
通过事实由服务端受控采集+真实匹配推导，发布走持久 typed-state 协议，
包为冻结 package:manifest.v1；断言意图保留，更正理由见 R2-D1 报告）：
- F14-1 → test_f14_1_version_persists_across_restart_bytes_immutable
          test_f14_1_concurrent_publish_cas_conflict_never_overwrites
- F13-1 → test_f13_1_empty_pages_missing_page_and_wrong_device_rejected
          test_f13_1_wrong_author_wrong_template_and_forged_accepted_items
- F14-3 → test_f14_3_import_idempotent_and_conflicting_digest_whole_package
          test_f14_3_import_rejects_any_corrupted_resource_without_partial_state
"""
from __future__ import annotations

import json
import sys
import threading
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import pytest

from runtime.layout_adaptation import (
    PAGE_STATES,
    AdaptationError,
)
from runtime.layout_ops import LayoutOpsError
from tests.layout_r2_helpers import (
    DEFAULT_BINDING,
    DEFAULT_GEOMETRY_ID,
    FakeCalibrationProvider,
    FakeCaptureProvider,
    FakeClock,
    LayoutBundle,
    build_page_frames,
    build_template_assets,
)


def _bundle(tmp_path, name="rt", correct=True):
    return LayoutBundle(
        tmp_path / name, clock=FakeClock(),
        capture_provider=FakeCaptureProvider(
            build_page_frames(build_template_assets(), correct=correct),
            binding=DEFAULT_BINDING, source_geometry_id=DEFAULT_GEOMETRY_ID),
        calibration_provider=FakeCalibrationProvider())


def _fill_pages(bundle, device="arm-01", principal="operator",
                states=None):
    svc = bundle.adaptation_svc
    sid = svc.start_session(device, {
        "template_ref": {"id": "tpl-1", "version": 1},
        "principal_id": principal,
        "installation_binding": DEFAULT_BINDING})["session_id"]
    revision = 1
    for state in (states or PAGE_STATES):
        capture = svc.capture(device, {
            "client_request_id": str(uuid.uuid4()), "owner_kind": "session",
            "owner_id": sid, "expected_owner_revision": revision,
            "page_state": state, "controlled_test_confirmed": True})
        view = svc.write_page(device, sid, state, {
            "client_request_id": str(uuid.uuid4()),
            "expected_session_revision": revision,
            "source_evidence_ids": capture["source_evidence_ids"],
            "local_overrides": []})
        revision = view["session_revision"]
    return sid, revision


def _publish(bundle, sid, principal="operator", expected_revision=0,
             device="arm-01", request_id=None):
    svc = bundle.adaptation_svc
    preview = svc.preview(device, {"capture_session_id": sid})
    return svc.publish(device, {
        "client_request_id": request_id or str(uuid.uuid4()),
        "session_id": sid, "preview_id": preview["preview_id"],
        "principal_id": principal, "expected_revision": expected_revision,
        "accepted_items": preview["accepted_items"]})


# ---------------- F14-1：版本持久、重启续排、旧版本字节不变 ----------------
def test_f14_1_version_persists_across_restart_bytes_immutable(tmp_path):
    bundle = _bundle(tmp_path, "rt1")
    clock = bundle.clock
    try:
        sid, _rev = _fill_pages(bundle)
        v1 = _publish(bundle, sid)
        assert v1["record"]["version"] == 1
        version_file = (bundle.root / "data" / "devices" / "arm-01"
                        / "layout_adaptation" / "versions" / "1.json")
        v1_bytes = version_file.read_bytes()
        assert v1_bytes  # 版本 1 已持久落盘（不可变文件）

        # 进程重启：同一临时根重新装配（内存计数清零）
        bundle.close()
        clock.advance(60)
        bundle2 = _bundle(tmp_path, "rt1")
        try:
            # 重启后可读回版本 1（历史不失效）
            assert bundle2.adaptation_svc.get_version("arm-01", 1)["version"] == 1
            sid2, _ = _fill_pages(bundle2)
            v2 = _publish(bundle2, sid2, expected_revision=1)
            assert v2["record"]["version"] == 2  # 不重置为 1
            # v1 字节与摘要逐位不变
            assert version_file.read_bytes() == v1_bytes
            assert bundle2.adaptation_svc.get_version("arm-01", 1)["sha256"] \
                == v1["record"]["sha256"]
        finally:
            bundle2.close()
    finally:
        try:
            bundle.close()
        except Exception:
            pass


def test_f14_1_concurrent_publish_cas_conflict_never_overwrites(tmp_path):
    bundle = _bundle(tmp_path, "rt2")
    try:
        svc = bundle.adaptation_svc
        requests = []
        for _ in range(2):
            sid, _ = _fill_pages(bundle)
            preview = svc.preview("arm-01", {"capture_session_id": sid})
            requests.append({"client_request_id": str(uuid.uuid4()),
                             "session_id": sid,
                             "preview_id": preview["preview_id"],
                             "principal_id": "operator",
                             "expected_revision": 0,
                             "accepted_items": preview["accepted_items"]})
        results, errors = [], []

        def _try(request):
            try:
                results.append(svc.publish("arm-01", request))
            except AdaptationError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_try, args=(r,)) for r in requests]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 并发两次 CAS（同 expected_revision）只成功一次，另一次 409 冲突
        assert len(results) == 1 and len(errors) == 1
        assert errors[0].code == "revision_conflict"
        assert results[0]["record"]["version"] == 1
        # 指针恰好前进一次
        pointer = bundle.state.adaptation_pointer_get("arm-01")
        assert pointer["revision"] == 1 and pointer["version"] == 1
        # 携带正确 expected_revision 的重试成功得到版本 2（不覆盖 v1）
        sid2, _ = _fill_pages(bundle)
        retry = _publish(bundle, sid2, expected_revision=1)
        assert retry["record"]["version"] == 2
        assert (bundle.root / "data" / "devices" / "arm-01"
                / "layout_adaptation" / "versions" / "1.json").exists()
    finally:
        bundle.close()


# ---------------- F13-1：publish 逐项真实核验 ----------------
def test_f13_1_empty_pages_missing_page_and_wrong_device_rejected(tmp_path):
    bundle = _bundle(tmp_path, "rt3")
    try:
        svc = bundle.adaptation_svc
        sid = svc.start_session("arm-01", {
            "template_ref": {"id": "tpl-1", "version": 1},
            "principal_id": "operator",
            "installation_binding": DEFAULT_BINDING})["session_id"]
        # 空 pages：无任何页 → 预览/发布链路拒绝，不能凭空得到 ready
        with pytest.raises(AdaptationError, match="pages_incomplete"):
            svc.preview("arm-01", {"capture_session_id": sid})
        with pytest.raises(AdaptationError, match="invalid_request"):
            svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                                   "session_id": sid,
                                   "principal_id": "operator"})
        # 缺一页（仅三态）→ 拒绝
        revision = 1
        for state in PAGE_STATES[:3]:
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
        with pytest.raises(AdaptationError, match="pages_incomplete"):
            svc.preview("arm-01", {"capture_session_id": sid})
        # 错设备 session：为 arm-01 建的会话用 arm-02 发布 → 拒绝且不落任何版本
        sid_full, _ = _fill_pages(bundle)
        preview = svc.preview("arm-01", {"capture_session_id": sid_full})
        with pytest.raises(AdaptationError, match="device_mismatch"):
            svc.publish("arm-02", {"client_request_id": str(uuid.uuid4()),
                                   "session_id": sid_full,
                                   "preview_id": preview["preview_id"],
                                   "principal_id": "operator"})
        # 错设备读取也拿不到（scope 隔离）
        assert svc.get_session("arm-02", sid_full) is None
        assert svc.get_version("arm-02", 1) is None
        assert bundle.state.adaptation_pointer_get("arm-02") is None
    finally:
        bundle.close()


def test_f13_1_wrong_author_wrong_template_and_forged_accepted_items(tmp_path):
    bundle = _bundle(tmp_path, "rt4")
    try:
        svc = bundle.adaptation_svc
        sid, _ = _fill_pages(bundle, principal="op-A")
        preview = svc.preview("arm-01", {"capture_session_id": sid})
        base = {"session_id": sid, "preview_id": preview["preview_id"],
                "principal_id": "op-A", "expected_revision": 0,
                "accepted_items": preview["accepted_items"]}
        # 其他作者
        with pytest.raises(AdaptationError, match="author_mismatch"):
            svc.publish("arm-01", {**base, "principal_id": "op-B",
                                   "client_request_id": str(uuid.uuid4())})
        # 错模板（请求与会话固定值不符）
        with pytest.raises(AdaptationError, match="template_mismatch"):
            svc.publish("arm-01", {**base, "template_id": "tpl-9",
                                   "client_request_id": str(uuid.uuid4())})
        # 伪造 accepted_items（凭空多报通过项）
        forged = [{"page_state": s, "item_kind": "anchor",
                   "item_id": "ghost"} for s in PAGE_STATES]
        with pytest.raises(AdaptationError, match="accepted_items_mismatch"):
            svc.publish("arm-01", {**base, "accepted_items": forged,
                                   "client_request_id": str(uuid.uuid4())})
        # 少报同样拒绝
        with pytest.raises(AdaptationError, match="accepted_items_mismatch"):
            svc.publish("arm-01", {**base, "accepted_items": [],
                                   "client_request_id": str(uuid.uuid4())})
        # 服务端核验结果不受客户端申报影响：真实发布以预览为准
        record = svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                                        **base})
        assert record["status"] == "verified"
        assert record["accepted_items"] == preview["accepted_items"]
        assert record["per_item_status"] is not None
        # 不存在的模板在会话创建即拒绝
        bundle2 = LayoutBundle(Path(tmp_path) / "rt4b", clock=bundle.clock,
                               capture_provider=None,
                               calibration_provider=None, template_seed=False)
        try:
            with pytest.raises(AdaptationError, match="template_not_found"):
                bundle2.adaptation_svc.start_session("arm-01", {
                    "template_ref": {"id": "tpl-1", "version": 1},
                    "principal_id": "op-A"})
        finally:
            bundle2.close()
    finally:
        bundle.close()


# ---------------- F14-3：导入幂等 / 异摘要整包拒 ----------------
def _export_from_first_device(tmp_path, tag):
    """第一台真实四态发布 + 模板/资产 → 冻结 manifest 闭包导出。"""
    bundle = _bundle(tmp_path, f"src-{tag}")
    sid, _ = _fill_pages(bundle)
    record = _publish(bundle, sid)
    result = bundle.ops_svc.export_package("op-src", {"items": [
        {"kind": "adaptation", "device_id": "arm-01", "id": "arm-01",
         "version": record["record"]["version"]}]})
    blob = bundle.ops_svc.read_package_bytes(result["package_id"])
    return bundle, blob, record


def test_f14_3_import_idempotent_and_conflicting_digest_whole_package(
        tmp_path):
    src, blob, record = _export_from_first_device(tmp_path, "a")
    dst = LayoutBundle(tmp_path / "dst1", clock=src.clock,
                       capture_provider=None, calibration_provider=None,
                       template_seed=False)
    try:
        # 第一次导入：完整落盘（catalog 门控，正式读取路径可见）
        preview = dst.ops_svc.import_preview("op-dst", blob, True)
        assert preview["blocked"] is False
        commit = dst.ops_svc.import_commit(
            "op-dst", preview["preview_id"],
            client_request_id=str(uuid.uuid4()),
            archive_sha256=preview["archive_sha256"])
        assert commit["status"] == "imported"
        imported = dst.ops_svc.resolve_adaptation("arm-01", 1)
        assert imported["sha256"] == record["record"]["sha256"]
        template = dst.ops_svc.resolve_template("tpl-1", 1)
        assert template is not None
        # 第二次导入同包：幂等——全部复用，不造新版本
        preview2 = dst.ops_svc.import_preview("op-dst", blob, True)
        assert preview2["to_import"] == []
        assert len(preview2["reused"]) == len(preview2["items"])
        commit2 = dst.ops_svc.import_commit(
            "op-dst", preview2["preview_id"],
            client_request_id=str(uuid.uuid4()),
            archive_sha256=preview2["archive_sha256"])
        assert commit2["status"] == "imported"
        assert dst.ops_svc.resolve_adaptation("arm-01", 1) == imported

        # 同 ID+version 异摘要：整包冲突拒绝，不留半包
        dst2 = LayoutBundle(tmp_path / "dst2", clock=src.clock,
                            capture_provider=None, calibration_provider=None,
                            template_seed=False)
        try:
            version_file = (dst2.root / "data" / "devices" / "arm-01"
                            / "layout_adaptation" / "versions" / "1.json")
            version_file.parent.mkdir(parents=True, exist_ok=True)
            version_file.write_text(json.dumps(
                {"device_id": "arm-01", "version": 1, "sha256": "zz"}))
            preview_c = dst2.ops_svc.import_preview("op-c", blob, True)
            assert preview_c["blocked"] is True and preview_c["conflicts"]
            with pytest.raises(LayoutOpsError) as exc:
                dst2.ops_svc.import_commit(
                    "op-c", preview_c["preview_id"],
                    client_request_id=str(uuid.uuid4()))
            assert exc.value.code == "layout_request_conflict"
            # 整包拒绝：模板资源也没有可见半包
            assert dst2.ops_svc.resolve_template("tpl-1", 1) is None
        finally:
            dst2.close()
    finally:
        src.close()
        dst.close()


def test_f14_3_import_rejects_any_corrupted_resource_without_partial_state(
        tmp_path):
    src, blob, _record = _export_from_first_device(tmp_path, "b")
    dst = LayoutBundle(tmp_path / "dst3", clock=src.clock,
                       capture_provider=None, calibration_provider=None,
                       template_seed=False)
    try:
        # 篡改资产字节（manifest 不改）→ 坏 hash，预览即拒
        src_zip = zipfile.ZipFile(BytesIO(blob))
        buf = BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for info in src_zip.infolist():
                data = src_zip.read(info.filename)
                if "/assets/" in info.filename:
                    data = b"corrupted-bytes"
                out.writestr(info.filename, data)
        with pytest.raises(LayoutOpsError, match="layout_request_invalid"):
            dst.ops_svc.import_preview("op-dst", buf.getvalue(), True)
        assert dst.ops_svc.resolve_template("tpl-1", 1) is None
        assert dst.ops_svc.resolve_adaptation("arm-01", 1) is None
        # 篡改记录内容 → manifest sha256 校验失败，同样整包拒绝
        src_zip = zipfile.ZipFile(BytesIO(blob))
        buf2 = BytesIO()
        with zipfile.ZipFile(buf2, "w") as out:
            for info in src_zip.infolist():
                data = src_zip.read(info.filename)
                if info.filename == "manifest.json":
                    continue  # 丢 manifest
                out.writestr(info.filename, data)
        with pytest.raises(LayoutOpsError, match="layout_request_invalid"):
            dst.ops_svc.import_preview("op-dst", buf2.getvalue(), True)
        assert dst.ops_svc.resolve_adaptation("arm-01", 1) is None
    finally:
        src.close()
        dst.close()
