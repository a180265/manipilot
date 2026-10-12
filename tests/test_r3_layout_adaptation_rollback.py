"""R3-16：适配历史版本正式回滚（kind 显式路由 + 新四态核验 + 指针前进）。

缺陷（修前）：`api/app.py` 回滚路由只接 _region_svc（kind=phone_search_region）；
`LayoutOpsService.rollback_preview/rollback_commit` 虽支持 adaptation/region
形状，但**无路由调用者**，且语义不满足 §8/缺陷卡：
- 无 kind 显式路由（适配回滚无入口语义）；
- 不要求新四态会话核验目标历史版本；
- 不保存复核事实（review 记录）与操作台账指针协议；
- 无同号幂等重放（重复提交重复推进 revision）；
- 不核设备空闲 / 存储 unknown / 校准一致性。

断言（缺陷卡 R3-16 A/B/C）：
- A：适配 v2 正式回滚到 v1——指针 selected=v1、revision 前进；v1 字节/摘要
  不变；新核验事实（review 记录）与新指针修订可查；不产生新版本文件
  （不以重新发布冒充回滚）；同号重放原结果、revision 不再推进；
- B：无新会话、错设备会话、旧（过期）预览、校准变化、设备忙、存储
  unknown 逐项拒绝；legacy resource 形状（粗框/旧测试链）不回归；
- C：提交后同号重放（丢响应）不重复推进 revision、不改历史。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import pytest

from runtime.layout_ops import LayoutOpsError, LayoutOpsService
from tests.layout_r2_helpers import (
    DEFAULT_BINDING,
    FakeCalibrationProvider,
    FakeCaptureProvider,
    FakeClock,
    LayoutBundle,
    build_page_frames,
    build_template_assets,
)

FOUR_STATES = ("wechat_list", "chat_ime_hidden", "chat_ime_empty",
               "chat_ime_text")


def _make_bundle(tmp_path):
    return LayoutBundle(tmp_path, clock=FakeClock(),
                        capture_provider=FakeCaptureProvider(
                            build_page_frames(build_template_assets()),
                            binding=DEFAULT_BINDING,
                            source_geometry_id="a" * 64),
                        calibration_provider=FakeCalibrationProvider())


def _four_state_session(bundle, pointer_revision):
    """真实四态会话（采集→服务端匹配四页通过→预览→发布 pointer 版本）。"""
    svc = bundle.adaptation_svc
    sid = svc.start_session("arm-01", {
        "template_ref": {"id": "tpl-1", "version": 1},
        "principal_id": "operator",
        "installation_binding": DEFAULT_BINDING})["session_id"]
    revision = 1
    for state in FOUR_STATES:
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
        assert view["check"]["status"] == "passed", (state, view["check"])
        revision = view["session_revision"]
    preview = svc.preview("arm-01", {"capture_session_id": sid})
    record = svc.publish("arm-01", {
        "client_request_id": str(uuid.uuid4()), "session_id": sid,
        "preview_id": preview["preview_id"], "principal_id": "operator",
        "expected_revision": pointer_revision,
        "accepted_items": preview["accepted_items"]})
    assert record["status"] == "verified", record.get("failed_items")
    return sid, record


def _rollback_session(bundle):
    """新四态核验会话（四页全通过；不发布——回滚不是重新发布）。"""
    svc = bundle.adaptation_svc
    sid = svc.start_session("arm-01", {
        "template_ref": {"id": "tpl-1", "version": 1},
        "principal_id": "operator",
        "installation_binding": DEFAULT_BINDING})["session_id"]
    revision = 1
    for state in FOUR_STATES:
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
        assert view["check"]["status"] == "passed", (state, view["check"])
        revision = view["session_revision"]
    return sid


@pytest.fixture()
def env(tmp_path):
    bundle = _make_bundle(tmp_path / "rt")
    idle = {"ok": True}
    attach = getattr(bundle.ops_svc, "attach_adaptation_rollback_env", None)
    if attach is not None:  # 修前无该装配接口（红测落目标断言）
        attach(session_loader=bundle.adaptation_svc._load_session,
               capture_provider=bundle.capture_provider,
               calibration_provider=bundle.calibration_provider,
               binding_provider=bundle.binding_provider,
               device_idle=lambda device: idle["ok"])
    bundle.idle = idle
    bundle.ops_svc._clock = bundle.clock  # 与适配服务同一受控时钟（bundle 未注入）
    v1_file = (bundle.root / "data" / "devices" / "arm-01" /
               "layout_adaptation" / "versions" / "1.json")
    _four_state_session(bundle, 0)
    _four_state_session(bundle, 1)
    yield {"bundle": bundle, "idle": idle, "v1_file": v1_file}
    bundle.close()


# ================= A：适配 v2 → v1 正式回滚 =================
def test_r3_16_a_adaptation_rollback_v2_to_v1(env):
    bundle = env["bundle"]
    ops = bundle.ops_svc
    state = bundle.state
    v1_bytes = env["v1_file"].read_bytes()
    sid_rb = _rollback_session(bundle)
    # A1 kind 显式路由的适配回滚预览（新四态会话核验目标版本）
    preview = ops.rollback_preview("arm-01", {
        "kind": "layout_adaptation", "target_version": 1,
        "rollback_session_id": sid_rb, "expected_revision": 2})
    assert preview["kind"] == "layout_adaptation", \
        "必须按 kind 显式路由到适配回滚（修前无该语义）"
    assert preview["status"] == "verified"
    assert preview["target_ref"]["version"] == 1
    assert preview["expected_revision"] == 2
    assert preview.get("rollback_session", {}).get("chat_ime_text", {}).get(
        "evidence_ids"), "预览必须保留新四态核验证据摘要"
    # A2 提交：指针前进 + 复核事实 + 台账
    request_id = str(uuid.uuid4())
    commit = ops.rollback_commit("arm-01", {
        "kind": "layout_adaptation", "preview_id": preview["preview_id"]},
        client_request_id=request_id)
    assert commit["rolled_back_to"] == 1
    assert commit["revision"] == 3, "只前进 current revision"
    pointer = state.adaptation_pointer_get("arm-01")
    assert pointer["version"] == 1 and pointer["revision"] == 3
    assert pointer["sha256"] == commit["pointer"]["sha256"]
    # A3 v1 字节/摘要不变（不改历史）；不产生新版本文件（不以重新发布冒充）
    assert env["v1_file"].read_bytes() == v1_bytes
    assert state.read_adaptation_version("arm-01", 1)["sha256"] == \
        state.read_adaptation_version("arm-01", 1)["sha256"]
    assert state.read_adaptation_version("arm-01", 3) is None
    # A4 新复核事实（review 记录）与操作台账可查
    review = state.review_record_get(commit["review_id"])
    assert review is not None and review["kind"] == "adaptation_rollback"
    assert review["target_ref"]["version"] == 1
    assert review["rollback_session"]["chat_ime_text"]["evidence_ids"]
    operation = state.operation_get(commit["operation_id"])
    assert operation["phase"] == "committed"
    # A5 同号重放：原结果、revision 不再推进
    replay = ops.rollback_commit("arm-01", {
        "kind": "layout_adaptation", "preview_id": preview["preview_id"]},
        client_request_id=request_id)
    assert replay["replayed"] is True
    assert replay["revision"] == 3, "重放不得重复推进 revision"
    assert state.adaptation_pointer_get("arm-01")["revision"] == 3


# ================= B：逐项拒绝 + legacy 不回归 =================
def test_r3_16_b_missing_session_rejected(env):
    with pytest.raises(LayoutOpsError) as exc:
        env["bundle"].ops_svc.rollback_preview("arm-01", {
            "kind": "layout_adaptation", "target_version": 1,
            "expected_revision": 2})
    assert exc.value.status == 409, "无新四态会话必须拒绝适配回滚预览"


def test_r3_16_b_wrong_device_session_rejected(env):
    bundle = env["bundle"]
    sid = _rollback_session(bundle)
    with pytest.raises(LayoutOpsError) as exc:
        bundle.ops_svc.rollback_preview("arm-02", {
            "kind": "layout_adaptation", "target_version": 1,
            "rollback_session_id": sid, "expected_revision": 2})
    assert exc.value.status in (403, 404)


def test_r3_16_b_stale_preview_rejected(env):
    bundle = env["bundle"]
    sid = _rollback_session(bundle)
    preview = bundle.ops_svc.rollback_preview("arm-01", {
        "kind": "layout_adaptation", "target_version": 1,
        "rollback_session_id": sid, "expected_revision": 2})
    bundle.clock.advance(121)  # 预览 120 秒期限
    with pytest.raises(LayoutOpsError) as exc:
        bundle.ops_svc.rollback_commit("arm-01", {
            "kind": "layout_adaptation", "preview_id": preview["preview_id"]},
            client_request_id=str(uuid.uuid4()))
    assert exc.value.status == 409


def test_r3_16_b_calibration_changed_rejected(env):
    bundle = env["bundle"]
    changed = {"camera_binding_id": DEFAULT_BINDING["camera_binding_id"],
               "arm_binding_id": DEFAULT_BINDING["arm_binding_id"],
               "camera_device_unique_id":
                   DEFAULT_BINDING["camera_device_unique_id"],
               "ime_device_id": "k40-1",
               "screen_descriptor": {
                   "width_px": 360, "height_px": 640,
                   "orientation": "portrait",
                   "system_insets": {"left": 0, "top": 0, "right": 0,
                                     "bottom": 0},
                   "calibration_geometry_hash": "c" * 64}}
    bundle.ops_svc.attach_adaptation_rollback_env(
        session_loader=bundle.adaptation_svc._load_session,
        capture_provider=bundle.capture_provider,
        calibration_provider=bundle.calibration_provider,
        binding_provider=lambda device: changed,
        device_idle=lambda device: True) if hasattr(
        bundle.ops_svc, "attach_adaptation_rollback_env") else None
    sid = _rollback_session(bundle)
    with pytest.raises(LayoutOpsError) as exc:
        bundle.ops_svc.rollback_preview("arm-01", {
            "kind": "layout_adaptation", "target_version": 1,
            "rollback_session_id": sid, "expected_revision": 2})
    assert exc.value.status in (409, 422), "校准变化必须拒绝回滚核验"


def test_r3_16_b_busy_device_rejected_at_commit(env):
    bundle = env["bundle"]
    sid = _rollback_session(bundle)
    preview = bundle.ops_svc.rollback_preview("arm-01", {
        "kind": "layout_adaptation", "target_version": 1,
        "rollback_session_id": sid, "expected_revision": 2})
    env["idle"]["ok"] = False
    with pytest.raises(LayoutOpsError) as exc:
        bundle.ops_svc.rollback_commit("arm-01", {
            "kind": "layout_adaptation", "preview_id": preview["preview_id"]},
            client_request_id=str(uuid.uuid4()))
    assert exc.value.status == 409
    assert bundle.state.adaptation_pointer_get("arm-01")["revision"] == 2, \
        "忙设备不得推进指针"


def test_r3_16_b_unknown_storage_blocked(env):
    bundle = env["bundle"]
    sid = _rollback_session(bundle)
    preview = bundle.ops_svc.rollback_preview("arm-01", {
        "kind": "layout_adaptation", "target_version": 1,
        "rollback_session_id": sid, "expected_revision": 2})
    bundle.state._blocked_reasons.append("注入：存储完整性未恢复")
    with pytest.raises(LayoutOpsError) as exc:
        bundle.ops_svc.rollback_commit("arm-01", {
            "kind": "layout_adaptation", "preview_id": preview["preview_id"]},
            client_request_id=str(uuid.uuid4()))
    assert exc.value.status == 503
    bundle.state._blocked_reasons.clear()


def test_r3_16_b_legacy_resource_shape_still_works(env):
    """legacy resource 形状（旧 KV 指针链/粗框）不回归：region 形状可预览。"""
    preview = env["bundle"].ops_svc.rollback_preview("arm-01", {
        "resource": "region", "target_version": 1})
    assert preview["resource"] == "region"
    assert preview["target_version"] == 1


# ================= C：丢响应重启同号重放 =================
def test_r3_16_c_replay_after_restart_no_double_revision(tmp_path):
    bundle = _make_bundle(tmp_path / "rt")
    try:
        attach = getattr(bundle.ops_svc,
                         "attach_adaptation_rollback_env", None)
        if attach is not None:
            attach(session_loader=bundle.adaptation_svc._load_session,
                   capture_provider=bundle.capture_provider,
                   calibration_provider=bundle.calibration_provider,
                   binding_provider=bundle.binding_provider,
                   device_idle=lambda device: True)
        bundle.ops_svc._clock = bundle.clock  # 与适配服务同一受控时钟
        _four_state_session(bundle, 0)
        _four_state_session(bundle, 1)
        sid_rb = _rollback_session(bundle)
        preview = bundle.ops_svc.rollback_preview("arm-01", {
            "kind": "layout_adaptation", "target_version": 1,
            "rollback_session_id": sid_rb, "expected_revision": 2})
        request_id = str(uuid.uuid4())
        commit = bundle.ops_svc.rollback_commit("arm-01", {
            "kind": "layout_adaptation", "preview_id": preview["preview_id"]},
            client_request_id=request_id)
        assert commit["revision"] == 3
        bundle.close()
        # 重启（同根重装配）：同号重放不重复推进 revision、不改历史
        bundle2 = _make_bundle(tmp_path / "rt")
        try:
            attach2 = getattr(bundle2.ops_svc,
                              "attach_adaptation_rollback_env", None)
            if attach2 is not None:
                attach2(session_loader=bundle2.adaptation_svc._load_session,
                        capture_provider=bundle2.capture_provider,
                        calibration_provider=bundle2.calibration_provider,
                        binding_provider=bundle2.binding_provider,
                        device_idle=lambda device: True)
            replay = bundle2.ops_svc.rollback_commit("arm-01", {
                "kind": "layout_adaptation",
                "preview_id": preview["preview_id"]},
                client_request_id=request_id)
            assert replay["replayed"] is True
            assert replay["revision"] == 3
            pointer = bundle2.state.adaptation_pointer_get("arm-01")
            assert pointer["version"] == 1 and pointer["revision"] == 3
            assert bundle2.state.read_adaptation_version("arm-01", 3) is None
        finally:
            bundle2.close()
    finally:
        try:
            bundle.close()
        except Exception:
            pass
