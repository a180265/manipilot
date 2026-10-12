"""R2-19：布局发布可持久幂等恢复（§7.1 typed state + §9 崩溃协议）。

红测复现（修前）：发布不落持久台账、preview 内存消费、丢回包后同号重试
报 preview_consumed、无 request_hash 冲突检测、无 typed-state 表——
test_r2_19_lost_response_same_request_replays_original 与
test_r2_19_typed_state_table_matches_frozen_ddl 在修前源码上失败。

修后断言：
- 布局库真实建立冻结 `layout_typed_state`（六列/15 kind/主键/归属索引，
  与 typed-state.sql 冻结 DDL 逐列一致）；
- 成功落盘后丢回包、同号重试、同根重启都返回原响应（replayed），不重复
  登记、不覆盖 v1；同号异内容 409 request_conflict；
- intent/文件/指针/最终 Store 提交各中间点故障注入后，启动对账按 §9 矩阵
  落 aborted/committed/unknown；不自动重发发布；
- 版本由持久预约分配（prepared 后崩溃占用版本号，不重复使用）；
- 并发 CAS 只成功一次；旧自由 KV 只经离线迁移（逐记录源摘要+键映射、
  缺身份/旧自报 verified/半提交/未知键原样归档、可转换旧定义只生成草稿、
  中断后同根重跑幂等）。
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import uuid
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import pytest

from runtime.layout_adaptation import (
    PAGE_STATES,
    AdaptationError,
    LayoutAdaptationService,
)
from runtime.layout_state import (
    STATE_KINDS,
    LayoutStateError,
    LayoutStateStore,
    canonical_json,
    state_identity,
    state_key,
)
from tests.layout_r2_helpers import (
    DEFAULT_BINDING,
    DEFAULT_GEOMETRY_ID,
    FakeCalibrationProvider,
    FakeCaptureProvider,
    FakeClock,
    LayoutBundle,
    LegacyKV,
    build_page_frames,
    build_template_assets,
)

DOCS_DDL = Path("docs/specs/layout-reuse/typed-state.sql")


def _make_bundle(root, clock=None, provider_mutator=None):
    provider = FakeCaptureProvider(
        build_page_frames(build_template_assets()), binding=DEFAULT_BINDING,
        source_geometry_id=DEFAULT_GEOMETRY_ID)
    return LayoutBundle(root, clock=clock or FakeClock(),
                        capture_provider=provider,
                        calibration_provider=FakeCalibrationProvider())


def _full_publish(bundle, device="arm-01", principal="operator",
                  expected_revision=0, request_id=None, _return_request=False):
    svc = bundle.adaptation_svc
    sid = svc.start_session(device, {
        "template_ref": {"id": "tpl-1", "version": 1},
        "principal_id": principal,
        "installation_binding": DEFAULT_BINDING})["session_id"]
    revision = 1
    for state in PAGE_STATES:
        capture = svc.capture(device, {
            "client_request_id": str(uuid.uuid4()),
            "owner_kind": "session", "owner_id": sid,
            "expected_owner_revision": revision,
            "page_state": state, "controlled_test_confirmed": True})
        view = svc.write_page(device, sid, state, {
            "client_request_id": str(uuid.uuid4()),
            "expected_session_revision": revision,
            "source_evidence_ids": capture["source_evidence_ids"],
            "local_overrides": []})
        revision = view["session_revision"]
    preview = svc.preview(device, {"capture_session_id": sid})
    request = {
        "client_request_id": request_id or str(uuid.uuid4()),
        "session_id": sid, "preview_id": preview["preview_id"],
        "principal_id": principal, "expected_revision": expected_revision,
        "accepted_items": preview["accepted_items"]}
    record = svc.publish(device, request)
    if _return_request:
        return record, request
    return record


# ---------------- 冻结 DDL：真实表结构与附件逐列一致 ----------------
def test_r2_19_typed_state_table_matches_frozen_ddl(tmp_path):
    store = LayoutStateStore(tmp_path / "layout.db",
                             data_root=tmp_path / "data")
    store.close()
    conn = sqlite3.connect(str(tmp_path / "layout.db"))

    def schema_of(conn):
        columns = conn.execute(
            "PRAGMA table_info(layout_typed_state)").fetchall()
        indexes = conn.execute(
            "PRAGMA index_list(layout_typed_state)").fetchall()
        index_columns = {}
        for index in indexes:
            cols = [r[2] for r in conn.execute(
                f"PRAGMA index_info({index[1]!r})".replace("'", '"')
                if False else f'PRAGMA index_info("{index[1]}")')]
            index_columns[index[1]] = cols
        return columns, index_columns

    frozen = sqlite3.connect(":memory:")
    frozen.executescript(DOCS_DDL.read_text(encoding="utf-8"))
    frozen_columns, frozen_indexes = schema_of(frozen)
    runtime_columns, runtime_indexes = schema_of(conn)
    assert [c[1:6] for c in frozen_columns] == [c[1:6] for c in runtime_columns]
    # 15 kind 闭集：插入未知 kind 被拒
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO layout_typed_state (kind, state_key, device_id, "
            "principal_id, revision, json) VALUES ('other_kind', ?, NULL, "
            "NULL, 1, '{}')", ("a" * 64,))
    # 归属索引存在且列序一致
    owner_index = [v for k, v in runtime_indexes.items()
                   if k == "layout_typed_state_owner"]
    assert owner_index and owner_index[0] == ["device_id", "principal_id",
                                              "kind"]
    conn.close()


def test_r2_19_state_cas_and_one_shot_consumption(tmp_path):
    store = LayoutStateStore(tmp_path / "layout.db",
                             data_root=tmp_path / "data")
    try:
        value = {"preview_id": "pv1", "device_id": "arm-01",
                 "principal_id": "op", "consumed_by": None}
        key = state_key("adaptation_preview",
                        state_identity("adaptation_preview", value))
        assert store.state_insert("adaptation_preview", key, "arm-01", "op",
                                  value) == "committed"
        # 同键同完整内容复用原行（幂等）
        assert store.state_insert("adaptation_preview", key, "arm-01", "op",
                                  dict(value)) == "reused"
        # 异内容 409 冲突（不覆盖、不加 revision 掩盖）
        with pytest.raises(LayoutStateError, match="layout_request_conflict"):
            store.state_insert("adaptation_preview", key, "arm-01", "op",
                               {**value, "preview_id": "pv2"})
        # 一次消费：revision 1→2，仅 consumed_by 变
        assert store.state_consume("adaptation_preview", key,
                                   "req-1") == "consumed"
        row = store.state_row("adaptation_preview", key)
        assert row["revision"] == 2
        assert row["value"]["consumed_by"] == "req-1"
        assert set(row["value"].keys()) == set(value.keys())
        # 同编号重放不再执行（already），他人消费被拒
        assert store.state_consume("adaptation_preview", key,
                                   "req-1") == "already"
        with pytest.raises(LayoutStateError, match="layout_request_conflict"):
            store.state_consume("adaptation_preview", key, "req-2")
        # CAS：期望旧 revision 更新被整体拒绝
        with pytest.raises(LayoutStateError, match="layout_revision_conflict"):
            store.state_update("adaptation_preview", key, "arm-01", "op", 1,
                               value)
    finally:
        store.close()


# ---------------- 丢回包 / 同号重试 / 同根重启：原响应重放 ----------------
def test_r2_19_lost_response_same_request_replays_original(tmp_path):
    bundle = _make_bundle(tmp_path / "rt")
    try:
        request_id = str(uuid.uuid4())
        record, publish_request = _full_publish(
            bundle, request_id=request_id, _return_request=True)
        assert record["record"]["version"] == 1
        # 丢回包：调用方不知道结果，用原请求同号同内容重试（body 逐字段一致）
        replay = bundle.adaptation_svc.publish("arm-01", dict(publish_request))
        assert replay["replayed"] is True
        assert replay["adaptation_ref"]["version"] == 1
        assert replay["adaptation_ref"]["sha256"] == record["record"]["sha256"]
        assert replay["operation_id"] == record["operation_id"]
        assert replay["revision"] == record["revision"]
        # operation 台账始终返回原 replayed=false 快照
        operation = bundle.state.operation_get_by_request(request_id)
        assert operation["response"]["replayed"] is False
        assert operation["phase"] == "committed"
        # 同号异内容冲突（改期望修订 = 不同内容）
        with pytest.raises(AdaptationError, match="不同内容"):
            bundle.adaptation_svc.publish("arm-01", {
                **publish_request, "expected_revision": 999})
        # v1 不被覆盖：仍只有版本 1 的持久文件
        assert bundle.state.list_adaptation_versions("arm-01") == {
            1: record["record"]["sha256"]}
        # 同根重启后原响应仍可按编号查询（持久台账）
        bundle.close()
        reopened = _make_bundle(tmp_path / "rt", clock=bundle.clock)
        try:
            operation2 = reopened.state.operation_get_by_request(request_id)
            assert operation2["operation_id"] == record["operation_id"]
            assert reopened.adaptation_svc.get_version("arm-01", 1)["sha256"] \
                == record["record"]["sha256"]
            # 重启后同号重试仍返回原响应（不再执行发布）
            replay2 = reopened.adaptation_svc.publish("arm-01",
                                                      dict(publish_request))
            assert replay2["replayed"] is True
            assert replay2["adaptation_ref"]["version"] == 1
            assert reopened.state.list_adaptation_versions("arm-01") == {
                1: record["record"]["sha256"]}
        finally:
            reopened.close()
    finally:
        try:
            bundle.close()
        except Exception:
            pass


# ---------------- 中间故障注入 → 启动对账（§9 矩阵） ----------------
def _publish_with_fault(root, fault_point, clock=None):
    bundle = _make_bundle(root, clock=clock or FakeClock())
    bundle.state.arm_faults({fault_point})
    result = {}
    try:
        try:
            record = _full_publish(bundle)
            result["record"] = record
        except (LayoutStateError, AdaptationError) as exc:
            result["error"] = exc
    finally:
        bundle.close()
    return result


def test_r2_19_fault_after_intent_settles_aborted(tmp_path):
    root = tmp_path / "rt"
    outcome = _publish_with_fault(root, "intent")
    assert isinstance(outcome.get("error"), Exception)
    bundle = _make_bundle(root, clock=FakeClock())
    try:
        report = bundle.state.reconcile()
        settled = [entry["settled"] for entry in report]
        assert "aborted" in settled
        # 旧指针保留：没有版本文件、没有指针
        assert bundle.state.list_adaptation_versions("arm-01") == {}
        assert bundle.state.adaptation_pointer_get("arm-01") is None
        # 对账后可以重新发布（新编号），版本号不被半成品占用？——
        # prepared 未写文件即中止：版本未落盘，但也未产生可用发布
        operation = bundle.state.operation_get(outcome and
                                               report[0]["operation_id"])
        assert operation["phase"] == "aborted"
    finally:
        bundle.close()


def test_r2_19_fault_after_files_aborted_keeps_orphan_and_reservation(
        tmp_path):
    root = tmp_path / "rt"
    outcome = _publish_with_fault(root, "pointer")  # 文件已写、指针故障前
    bundle = _make_bundle(root, clock=FakeClock())
    try:
        report = bundle.state.reconcile()
        assert [entry["settled"] for entry in report] == ["aborted"]
        # 完整孤档保留并占用版本号，不补发发布
        versions = bundle.state.list_adaptation_versions("arm-01")
        assert len(versions) == 1
        assert bundle.state.adaptation_pointer_get("arm-01") is None
        assert bundle.state.next_adaptation_version("arm-01") == 2
        # 指针仍为空：读取路径看不到孤档（catalog 未提交）
        assert bundle.adaptation_svc.get_version("arm-01", 1) is None
    finally:
        bundle.close()


def test_r2_19_fault_after_pointer_reconciles_committed(tmp_path):
    root = tmp_path / "rt"
    outcome = _publish_with_fault(root, "final_tx")  # 指针已切、Store 未提交
    bundle = _make_bundle(root, clock=FakeClock())
    try:
        pointer = bundle.state.adaptation_pointer_get("arm-01")
        assert pointer is not None and pointer["version"] == 1
        report = bundle.state.reconcile()
        assert [entry["settled"] for entry in report] == ["committed"]
        # 按原 intent 补齐 Store 事务并 committed：catalog 可见、原响应持久
        operation = bundle.state.operation_get(report[0]["operation_id"])
        assert operation["phase"] == "committed"
        assert operation["response"]["replayed"] is False
        assert bundle.adaptation_svc.get_version("arm-01", 1) is not None
        # 不再次换指针/增加 revision：pointer revision 仍为 1
        assert bundle.state.adaptation_pointer_get("arm-01")["revision"] == 1
    finally:
        bundle.close()


def test_r2_19_pointer_contradiction_blocks_new_writes_until_repair(
        tmp_path):
    root = tmp_path / "rt"
    bundle = _make_bundle(root)
    try:
        record = _full_publish(bundle)  # v1 正常（指针 revision=1）
        # 预备一次发布：prepared+文件完整、指针仍指 before（注入 pointer 故障）
        bundle.state.arm_faults({"pointer"})
        with pytest.raises((AdaptationError, LayoutStateError)):
            _full_publish(bundle, expected_revision=1)
        bundle.state.arm_faults(set())
        # 人为制造指针矛盾（既非该 intent 的 before 也非 after）
        with bundle.state.transaction() as conn:
            conn.execute(
                "UPDATE layout_adaptation_pointer SET revision=42, "
                "selected_version=99 WHERE device_id='arm-01'")
        bundle2 = _make_bundle(root, clock=bundle.clock)
        try:
            # 启动对账落 unknown 并阻断新写（不猜成功、不重放）
            report = bundle2.state.reconcile()
            assert any(entry["settled"] == "unknown" for entry in report)
            assert bundle2.state.blocked_reasons
            with pytest.raises(AdaptationError, match="archive_invalid|阻断"):
                _full_publish(bundle2, expected_revision=42)
            # 现场保留：prepared 操作与文件不清理
            assert bundle2.state.operations_non_terminal()
            # 修复存储完整性（恢复指针到 intent before）→ 重新对账解除阻断
            v1 = bundle2.state.read_adaptation_version("arm-01", 1)
            with bundle2.state.transaction() as conn:
                conn.execute(
                    "UPDATE layout_adaptation_pointer SET revision=1, "
                    "selected_version=1, selected_sha256=? "
                    "WHERE device_id='arm-01'", (v1["sha256"],))
            report2 = bundle2.state.reconcile()
            assert any(entry["settled"] == "aborted" for entry in report2)
            assert not bundle2.state.blocked_reasons
            # 解除后可正常发布（孤档保留占用版本号 → 新发布为 v3；
            # 指针 revision 前进到 2）
            fixed = _full_publish(bundle2, expected_revision=1)
            assert fixed["record"]["version"] == 3
            assert fixed["revision"] == 2
        finally:
            bundle2.close()
    finally:
        bundle.close()


# ---------------- 并发 CAS：只成功一次，冲突不覆盖 ----------------
def test_r2_19_concurrent_publish_cas_single_winner(tmp_path):
    bundle = _make_bundle(tmp_path / "rt")
    try:
        requests = []
        for _ in range(2):
            svc = bundle.adaptation_svc
            sid = svc.start_session("arm-01", {
                "template_ref": {"id": "tpl-1", "version": 1},
                "principal_id": "operator",
                "installation_binding": DEFAULT_BINDING})["session_id"]
            revision = 1
            for state in PAGE_STATES:
                capture = svc.capture("arm-01", {
                    "client_request_id": str(uuid.uuid4()),
                    "owner_kind": "session", "owner_id": sid,
                    "expected_owner_revision": revision,
                    "page_state": state,
                    "controlled_test_confirmed": True})
                view = svc.write_page("arm-01", sid, state, {
                    "client_request_id": str(uuid.uuid4()),
                    "expected_session_revision": revision,
                    "source_evidence_ids": capture["source_evidence_ids"],
                    "local_overrides": []})
                revision = view["session_revision"]
            preview = svc.preview("arm-01", {"capture_session_id": sid})
            requests.append({"session_id": sid,
                             "preview_id": preview["preview_id"],
                             "principal_id": "operator",
                             "expected_revision": 0,
                             "accepted_items": preview["accepted_items"],
                             "client_request_id": str(uuid.uuid4())})
        results, errors = [], []

        def _try(request):
            try:
                results.append(bundle.adaptation_svc.publish("arm-01",
                                                             request))
            except AdaptationError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_try, args=(r,))
                   for r in requests]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 1 and len(errors) == 1
        assert errors[0].code == "revision_conflict"
        assert results[0]["revision"] == 1
        assert bundle.state.adaptation_pointer_get("arm-01")["revision"] == 1
    finally:
        bundle.close()


# ---------------- 版本持久预约：prepared 后崩溃不丢号 ----------------
def test_r2_19_version_reserved_by_persistent_preparation(tmp_path):
    root = tmp_path / "rt"
    outcome = _publish_with_fault(root, "pointer")  # prepared+文件、无指针
    bundle = _make_bundle(root, clock=FakeClock())
    try:
        bundle.state.reconcile()  # aborted；孤档保留占用版本 1
        # 下一次发布获得版本 2（不覆盖/复用 1 号）
        record = _full_publish(bundle)
        assert record["record"]["version"] == 2
        assert bundle.state.adaptation_pointer_get("arm-01")["version"] == 2
        assert bundle.adaptation_svc.get_version("arm-01", 1) is None
        assert bundle.adaptation_svc.get_version("arm-01", 2)["sha256"] \
            == record["record"]["sha256"]
    finally:
        bundle.close()


# ---------------- 旧自由 KV 离线迁移 ----------------
def test_r2_19_legacy_kv_migration_classes_and_resume(tmp_path):
    root = tmp_path / "rt"
    legacy = LegacyKV(root / "data" / "layout.db")
    # 覆盖各类旧记录
    legacy.save("adaptation_version:arm-01:1", {
        "device_id": "arm-01", "version": 1,
        "pages": {"wechat_list": {"anchors": []}},
        "status": "needs_review", "sha256": "f" * 64})  # 摘要不符→归档
    legacy.save("adaptation_version:arm-01:2", {
        "device_id": "arm-01", "version": 2,
        "pages": {"wechat_list": {"anchors": []}},
        "status": "verified", "sha256": canonical_json(
            {"device_id": "arm-01", "version": 2,
             "pages": {"wechat_list": {"anchors": []}}})})  # 旧自报 verified
    good = {"device_id": "arm-01", "version": 3,
            "pages": {"wechat_list": {"anchors": []}}}
    legacy.save("adaptation_version:arm-01:3", good)  # 可转换 → 草稿
    legacy.save("adaptation_version:arm-02:1", {
        "version": 1, "pages": {}})                     # 缺身份
    legacy.save("layout_op:x:1", {"phase": "prepared"})  # 半提交
    legacy.save("layout_op:y:2", {"phase": "committed"})  # 历史台账
    legacy.save("totally:unknown:key", {"any": "thing"})  # 未知键
    legacy.save("adaptation_pointer:arm-01", {"revision": 5, "version": 2})

    bundle = _make_bundle(root)
    try:
        report = bundle.state.migrate_legacy_kv(legacy)
        entries = {e["source_key"]: e for e in report["entries"]}
        assert entries["adaptation_version:arm-01:1"]["disposition"] == \
            "archived"
        assert entries["adaptation_version:arm-01:1"]["reason"] == \
            "digest_mismatch"
        assert entries["adaptation_version:arm-01:2"]["reason"] == \
            "legacy_self_reported_verified"
        assert entries["adaptation_version:arm-01:2"]["disposition"] == \
            "archived"
        converted = entries["adaptation_version:arm-01:3"]
        assert converted["disposition"] == "draft"
        # 可转换旧定义只生成草稿：draft 行存在，无任何发布版本/指针
        assert converted["target_kind"] == "draft"
        draft_row = bundle.state.state_get("draft",
                                           converted["target_state_key"])
        assert draft_row is not None and draft_row["revision"] == 1
        assert draft_row["metadata"]["migrated_from"] == \
            "adaptation_version:arm-01:3"
        assert draft_row["states"]["wechat_list"]["check"] is None
        assert entries["adaptation_version:arm-02:1"]["reason"] == \
            "identity_missing"
        assert entries["layout_op:x:1"]["reason"] == "half_committed_operation"
        assert entries["layout_op:y:2"]["disposition"] == "archived"
        assert entries["totally:unknown:key"]["reason"] == "unknown_key"
        assert entries["adaptation_pointer:arm-01"]["disposition"] == \
            "archived"
        # 旧自报 verified 不获得执行资格：无适配版本/指针/ catalog 产生
        assert bundle.state.list_adaptation_versions("arm-01") == {}
        assert bundle.state.adaptation_pointer_get("arm-01") is None
        # 旧 KV 只读：原记录未删未改
        assert legacy.load("adaptation_version:arm-01:2")["status"] == \
            "verified"
        # 同根重跑：幂等续跑（已处理键 skipped，不重复归档）
        report2 = bundle.state.migrate_legacy_kv(legacy)
        assert report2["skipped"] == len(report["entries"])
        assert all(e["disposition"] == "skipped"
                   for e in report2["entries"])
        # 同键内容变化：新内容归档待复核，原档字节保留
        legacy.save("totally:unknown:key", {"changed": True})
        report3 = bundle.state.migrate_legacy_kv(legacy)
        changed = [e for e in report3["entries"]
                   if e["source_key"] == "totally:unknown:key"]
        assert changed and changed[0]["disposition"] == "archived"
    finally:
        bundle.close()
