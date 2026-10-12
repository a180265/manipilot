"""L03 验收：适配会话/受控采集/逐页服务端核验/预览/CAS 发布/版本（R2-16 后契约）。

原 FIX-13 用例按第二轮 R2-16 修订（更正理由：页面通过事实改由服务端
受控采集 + 真实匹配推导，客户端 check/passed 不再是输入；断言意图保留）：
session 固定设备/作者/模板、四态固定顺序、每态服务端两帧、逐项结果、
preview 不可变逐项结果、accepted_items 恰为通过项、缺页/空页拒绝、
部分通过保留、30min/15min/120s 期限（fake clock）、重放不续期、单次消费。
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import pytest

from runtime.layout_adaptation import (
    PAGE_STATES,
    AdaptationError,
    LayoutAdaptationService,
)
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


@pytest.fixture()
def bundle(tmp_path):
    b = LayoutBundle(
        tmp_path / "rt", clock=FakeClock(),
        capture_provider=FakeCaptureProvider(
            build_page_frames(build_template_assets()),
            binding=DEFAULT_BINDING, source_geometry_id=DEFAULT_GEOMETRY_ID),
        calibration_provider=FakeCalibrationProvider())
    yield b
    b.close()


def _contract_page(client_request_id=None, evidence=("ev-a", "ev-b")):
    return {"client_request_id": client_request_id or str(uuid.uuid4()),
            "expected_session_revision": 1,
            "source_evidence_ids": list(evidence),
            "local_overrides": []}


def _capture(svc, device, sid, revision, state):
    return svc.capture(device, {
        "client_request_id": str(uuid.uuid4()), "owner_kind": "session",
        "owner_id": sid, "expected_owner_revision": revision,
        "page_state": state, "controlled_test_confirmed": True})


def _write(svc, device, sid, revision, state, capture=None):
    capture = capture or _capture(svc, device, sid, revision, state)
    return svc.write_page(device, sid, state, {
        "client_request_id": str(uuid.uuid4()),
        "expected_session_revision": revision,
        "source_evidence_ids": capture["source_evidence_ids"],
        "local_overrides": []})


def _fill(svc, device, sid, revision=1, states=None):
    current = revision
    for state in (states or PAGE_STATES):
        view = _write(svc, device, sid, current, state)
        current = view["session_revision"]
    return current


def _start(svc, device="arm-01", principal="operator"):
    return svc.start_session(device, {
        "template_ref": {"id": "tpl-1", "version": 1},
        "principal_id": principal,
        "installation_binding": DEFAULT_BINDING})


# ---------------- 会话 ----------------
def test_start_session_fixes_device_author_template(bundle):
    svc = bundle.adaptation_svc
    rec = _start(svc)
    assert rec["status"] == "active"
    assert rec["device_id"] == "arm-01"
    assert rec["principal_id"] == "operator"
    assert rec["template_id"] == "tpl-1" and rec["template_version"] == 1
    assert rec["revision"] == 1
    assert rec["expires_at"] > rec["created_at"]
    # 缺模板引用拒绝
    with pytest.raises(AdaptationError, match="template|invalid_request"):
        svc.start_session("arm-01", {"principal_id": "operator"})
    # 缺作者身份拒绝（作者由服务端身份提供，不接受缺省 operator）
    with pytest.raises(AdaptationError, match="author_missing|principal"):
        svc.start_session("arm-01", {
            "template_ref": {"id": "tpl-1", "version": 1}})
    # 未知模板拒绝
    bundle2 = LayoutBundle(
        Path(bundle.root).parent / "rt-empty", clock=bundle.clock,
        capture_provider=None, calibration_provider=None, template_seed=False)
    try:
        with pytest.raises(AdaptationError, match="template_not_found"):
            bundle2.adaptation_svc.start_session("arm-01", {
                "template_ref": {"id": "tpl-x", "version": 1},
                "principal_id": "operator"})
    finally:
        bundle2.close()


def test_session_not_found(bundle):
    svc = bundle.adaptation_svc
    with pytest.raises(AdaptationError, match="session_not_found"):
        svc.write_page("arm-01", "nonexistent", "wechat_list",
                       _contract_page())


# ---------------- 逐页写入（服务端核验，客户端事实字段封闭） ----------------
def test_write_page_requires_fixed_states_two_evidence_and_closed_fields(
        bundle):
    svc = bundle.adaptation_svc
    sid = _start(svc)["session_id"]
    with pytest.raises(AdaptationError, match="page_state"):
        svc.write_page("arm-01", sid, "home", _contract_page())
    with pytest.raises(AdaptationError, match="evidence|session_revision|UUID"):
        svc.write_page("arm-01", sid, "wechat_list",
                       {"client_request_id": str(uuid.uuid4()),
                        "expected_session_revision": 1,
                        "source_evidence_ids": ["only-one"],
                        "local_overrides": []})
    # 客户端 check/passed 事实字段一律不接受
    with pytest.raises(AdaptationError, match="不接受字段|passed|check"):
        svc.write_page("arm-01", sid, "wechat_list", {
            "client_request_id": str(uuid.uuid4()),
            "expected_session_revision": 1,
            "source_evidence_ids": ["a", "b"], "local_overrides": [],
            "check": {"items": []}, "passed": True})
    # 错设备写页拒绝
    with pytest.raises(AdaptationError, match="device_mismatch"):
        svc.write_page("arm-02", sid, "wechat_list", _contract_page())


def test_write_page_replay_keeps_first_acceptance_and_revision(bundle):
    svc = bundle.adaptation_svc
    clock = bundle.clock
    sid = _start(svc)["session_id"]
    capture = _capture(svc, "arm-01", sid, 1, "wechat_list")
    request_id = str(uuid.uuid4())
    body = {"client_request_id": request_id,
            "expected_session_revision": 1,
            "source_evidence_ids": capture["source_evidence_ids"],
            "local_overrides": []}
    first = svc.write_page("arm-01", sid, "wechat_list", dict(body))
    accepted_at = first["page"]["capture"]["accepted_at"]
    revision = first["session_revision"]
    clock.advance(600)
    replay = svc.write_page("arm-01", sid, "wechat_list", dict(body))
    # 同编号重放：首次接纳时间与 revision 不变
    assert replay["replayed"] is True
    assert replay["page"]["capture"]["accepted_at"] == accepted_at
    assert replay["session_revision"] == revision
    # 新证据（新采集）→ revision 前进
    current = svc.write_page("arm-01", sid, "wechat_list", {
        "client_request_id": str(uuid.uuid4()),
        "expected_session_revision": revision,
        "source_evidence_ids": _capture(svc, "arm-01", sid, revision,
                                        "wechat_list")["source_evidence_ids"],
        "local_overrides": []})
    assert current["page"]["capture"]["accepted_at"] != accepted_at
    assert current["session_revision"] == revision + 1


# ---------------- 预览 / 发布 ----------------
def test_full_flow_publish_verified_with_exact_accepted_items(bundle):
    svc = bundle.adaptation_svc
    sid = _start(svc)["session_id"]
    _fill(svc, "arm-01", sid)
    preview = svc.preview("arm-01", {"capture_session_id": sid})
    # accepted_items 按冻结状态序（wechat_list→…）+ (item_kind,item_id) 排序
    order = {state: index for index, state in enumerate(PAGE_STATES)}
    keys = [(i["page_state"], i["item_kind"], i["item_id"])
            for i in preview["accepted_items"]]
    assert keys == sorted(keys, key=lambda k: (order.get(k[0], 99), k[1],
                                               k[2]))
    assert len(preview["accepted_items"]) >= 4 * 2  # 每页最低锚点均通过
    record = svc.publish("arm-01", {
        "client_request_id": str(uuid.uuid4()), "session_id": sid,
        "preview_id": preview["preview_id"], "principal_id": "operator",
        "expected_revision": 0,
        "accepted_items": preview["accepted_items"]})
    assert record["status"] == "verified"
    # 持久记录 = 封闭 layout:adaptation（R2-17）；返回体携带派生投影
    assert record["record"]["version"] == 1
    assert record["accepted_items"] == preview["accepted_items"]
    assert record["failed_items"] == []
    assert len(record["record"]["page_checks"]) == 4
    assert record["record"]["sha256"]
    # 同 preview 只能消费一次
    with pytest.raises(AdaptationError, match="preview_consumed"):
        svc.publish("arm-01", {
            "client_request_id": str(uuid.uuid4()), "session_id": sid,
            "preview_id": preview["preview_id"],
            "principal_id": "operator", "expected_revision": 1,
            "accepted_items": preview["accepted_items"]})


def test_empty_pages_and_missing_page_rejected(bundle):
    svc = bundle.adaptation_svc
    sid = _start(svc)["session_id"]
    with pytest.raises(AdaptationError, match="pages_incomplete"):
        svc.preview("arm-01", {"capture_session_id": sid})
    with pytest.raises(AdaptationError, match="invalid_request"):
        svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                               "session_id": sid,
                               "principal_id": "operator"})
    # 缺一页：三页也拒绝
    _fill(svc, "arm-01", sid, states=PAGE_STATES[:3])
    with pytest.raises(AdaptationError, match="pages_incomplete"):
        svc.preview("arm-01", {"capture_session_id": sid})


def test_wrong_device_author_template_and_forged_accepted_items(bundle):
    svc = bundle.adaptation_svc
    sid = _start(svc, principal="op-A")["session_id"]
    _fill(svc, "arm-01", sid)
    # 错设备
    with pytest.raises(AdaptationError, match="device_mismatch"):
        svc.publish("arm-02", {"client_request_id": str(uuid.uuid4()),
                               "session_id": sid, "preview_id": "x",
                               "principal_id": "op-A"})
    # 错作者
    preview = svc.preview("arm-01", {"capture_session_id": sid})
    with pytest.raises(AdaptationError, match="author_mismatch"):
        svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                               "session_id": sid,
                               "preview_id": preview["preview_id"],
                               "principal_id": "op-B", "expected_revision": 0,
                               "accepted_items": preview["accepted_items"]})
    base = {"session_id": sid, "preview_id": preview["preview_id"],
            "principal_id": "op-A"}
    # 伪造 accepted_items（多报/少报均拒绝）
    with pytest.raises(AdaptationError, match="accepted_items_mismatch"):
        svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                               **base, "expected_revision": 0,
                               "accepted_items": [
                                   {"page_state": "wechat_list",
                                    "item_kind": "anchor",
                                    "item_id": "ghost_item"}]})
    # 请求模板与会话固定值不符
    preview2 = svc.preview("arm-01", {"capture_session_id": sid})
    with pytest.raises(AdaptationError, match="template_mismatch"):
        svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                               **base, "preview_id": preview2["preview_id"],
                               "template_version": 2, "expected_revision": 0,
                               "accepted_items": preview2["accepted_items"]})
    # 正确作者 + 精确 accepted_items → 成功
    preview3 = svc.preview("arm-01", {"capture_session_id": sid})
    record = svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                                    **base,
                                    "preview_id": preview3["preview_id"],
                                    "expected_revision": 0,
                                    "accepted_items":
                                        preview3["accepted_items"]})
    assert record["status"] == "verified"


def test_failed_item_keeps_partial_results_not_verified(tmp_path):
    # chat_ime_text 页换成不匹配图像（真实匹配失败，非指定结果）
    assets = build_template_assets()
    frames = build_page_frames(assets)
    wrong = build_page_frames(assets, correct=False)
    frames["chat_ime_text"] = wrong["chat_ime_text"]
    b = LayoutBundle(
        tmp_path / "rt", clock=FakeClock(),
        capture_provider=FakeCaptureProvider(
            frames, binding=DEFAULT_BINDING,
            source_geometry_id=DEFAULT_GEOMETRY_ID),
        calibration_provider=FakeCalibrationProvider())
    try:
        svc = b.adaptation_svc
        sid = _start(svc)["session_id"]
        revision = 1
        for state in PAGE_STATES:
            view = _write(svc, "arm-01", sid, revision, state)
            revision = view["session_revision"]
        preview = svc.preview("arm-01", {"capture_session_id": sid})
        record = svc.publish("arm-01", {
            "client_request_id": str(uuid.uuid4()), "session_id": sid,
            "preview_id": preview["preview_id"], "principal_id": "operator",
            "expected_revision": 0,
            "accepted_items": preview["accepted_items"]})
        # 有失败的完整检查只保留部分通过，不伪装 verified
        assert record["status"] == "needs_review"
        assert record["failed_items"]
        failed_states = {item["page_state"]
                         for item in record["failed_items"]}
        assert "chat_ime_text" in failed_states
        assert {item["page_state"] for item in record["blocking_reasons"]} \
            == failed_states
        # 其余三页通过项完整保留
        passed_states = {item["page_state"]
                         for item in record["accepted_items"]}
        assert {"wechat_list", "chat_ime_hidden", "chat_ime_empty"} \
            <= passed_states
    finally:
        b.close()


def test_time_limits_with_fake_clock_and_no_replay_extension(bundle):
    svc = bundle.adaptation_svc
    clock = bundle.clock
    sid = _start(svc)["session_id"]
    _fill(svc, "arm-01", sid)
    preview = svc.preview("arm-01", {"capture_session_id": sid})
    clock.advance(121)
    with pytest.raises(AdaptationError, match="preview_expired"):
        svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                               "session_id": sid,
                               "preview_id": preview["preview_id"],
                               "principal_id": "operator",
                               "expected_revision": 0,
                               "accepted_items": preview["accepted_items"]})
    # 页证据 15 分钟边界：自首次接纳（采集归档时刻）900 秒
    clock.advance(600)  # 距首次接纳 ~721s
    svc.preview("arm-01", {"capture_session_id": sid})  # 仍在期限内
    clock.advance(400)  # >900s：页证据过期
    with pytest.raises(AdaptationError, match="evidence_expired"):
        svc.preview("arm-01", {"capture_session_id": sid})
    # 重新采集受影响页（四页全部重采）后可预览
    current = svc.get_session("arm-01", sid)["revision"]
    _fill(svc, "arm-01", sid, revision=current)
    svc.preview("arm-01", {"capture_session_id": sid})
    # session 30 分钟边界
    clock.advance(1900)
    with pytest.raises(AdaptationError, match="session_expired"):
        svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                               "session_id": sid, "preview_id": "p",
                               "principal_id": "operator"})
    with pytest.raises(AdaptationError, match="session_expired"):
        svc.write_page("arm-01", sid, "wechat_list", _contract_page())


def test_publish_without_preview_or_bogus_preview_rejected(bundle):
    svc = bundle.adaptation_svc
    sid = _start(svc)["session_id"]
    _fill(svc, "arm-01", sid)
    with pytest.raises(AdaptationError, match="preview_not_found"):
        svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                               "session_id": sid, "preview_id": "bogus",
                               "principal_id": "operator"})


def test_version_read_back(bundle):
    svc = bundle.adaptation_svc
    sid = _start(svc)["session_id"]
    _fill(svc, "arm-01", sid)
    preview = svc.preview("arm-01", {"capture_session_id": sid})
    record = svc.publish("arm-01", {
        "client_request_id": str(uuid.uuid4()), "session_id": sid,
        "preview_id": preview["preview_id"], "principal_id": "operator",
        "expected_revision": 0, "accepted_items": preview["accepted_items"]})
    assert svc.get_version("arm-01", 1)["sha256"] \
        == record["record"]["sha256"]
    assert svc.get_version("arm-01", 2) is None
    assert svc.get_current("arm-01")["version"] == 1
