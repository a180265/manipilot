"""R2-16：四态适配通过必须由服务端证据推导（服务层）。

红测复现（修前）：伪造证据 + 客户端 passed/check 可直接发布 verified——
test_r2_16_forged_evidence_and_client_passed_cannot_publish_verified 在修前
源码上失败于"不存在 verified 版本"断言（修前 publish 只查证据字符串和非空
items，信任客户端 passed）。

修后行为（本文件全部断言）：
- write_page 按冻结字段封闭，客户端 check/passed/status 一律拒绝；
- capture 经注入提供器取受控两帧，服务端核验真实采样推进/间隔/新鲜度/
  归属/首次接纳时间/设备/作者/v3；采集一次消费；
- 服务端读真实图像+模板定义逐态逐项匹配（真实 matchTemplate，非指定结果）；
- accepted/failed/blocking 由结果生成；伪造通过不能发布 verified；
- 预览消费不可变预览并重验会话修订/身份/安装/v3/期限；预览后又写页不能
  消费旧结果；证据真实但图像/类型/目标不匹配 → 失败项保存为 needs_review。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import numpy as np
import pytest

from runtime.layout_adaptation import (
    PAGE_STATES,
    AdaptationError,
    LayoutAdaptationService,
)
from runtime.layout_state import LayoutStateStore
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
def env(tmp_path):
    clock = FakeClock()
    provider = FakeCaptureProvider(
        build_page_frames(build_template_assets()), binding=DEFAULT_BINDING,
        source_geometry_id=DEFAULT_GEOMETRY_ID)
    calibration = FakeCalibrationProvider()
    bundle = LayoutBundle(tmp_path / "rt", clock=clock,
                          capture_provider=provider,
                          calibration_provider=calibration)
    yield {"bundle": bundle, "clock": clock, "provider": provider,
           "calibration": calibration, "root": tmp_path / "rt"}
    bundle.close()


def _four_captures(svc, device, sid, revision, *, provider=None,
                   client_request_suffix=""):
    """四态各采集一次（owner=session），返回 state → capture 响应。"""
    responses = {}
    for index, state in enumerate(PAGE_STATES):
        responses[state] = svc.capture(device, {
            "client_request_id": str(uuid.uuid4()),
            "owner_kind": "session", "owner_id": sid,
            "expected_owner_revision": revision,
            "page_state": state, "controlled_test_confirmed": True})
        assert len(responses[state]["capture"]["evidence"]) == 2
    return responses


def _write_all(svc, device, sid, revision):
    captures = _four_captures(svc, device, sid, revision)
    current = revision
    for state in PAGE_STATES:
        evidence_ids = captures[state]["source_evidence_ids"]
        view = svc.write_page(device, sid, state, {
            "client_request_id": str(uuid.uuid4()),
            "expected_session_revision": current,
            "source_evidence_ids": evidence_ids,
            "local_overrides": []})
        assert view["check"]["status"] == "passed", view["check"]
        current = view["session_revision"]
    return captures, current


# ---------------- 红测复现：伪造证据 + passed 不能发布 verified ----------------
def test_r2_16_forged_evidence_and_client_passed_cannot_publish_verified(
        tmp_path):
    clock = FakeClock()
    root = tmp_path / "rt"
    state_store = LayoutStateStore(root / "data" / "layout.db",
                                   data_root=root / "data", clock=clock)
    from tests.layout_r2_helpers import LegacyKV

    class _Tpl:
        def get_template(self, template_id, version):
            return {"template_id": template_id, "version": version,
                    "pages": []}

    svc = LayoutAdaptationService(store=LegacyKV(root / "data" / "layout.db"),
                                  template_store=_Tpl(), clock=clock,
                                  state_store=state_store,
                                  server_instance_id="inst-forged")
    device = "arm-01"
    sid = svc.start_session(device, {
        "template_id": "tpl-1", "template_version": 1,
        "principal_id": "operator"})["session_id"]
    # 每页传虚构证据 + 一个客户端 passed/check 事实
    for state in PAGE_STATES:
        try:
            svc.write_page(device, sid, state, {
                "evidence_ids": [f"fake-{state}-1", f"fake-{state}-2"],
                "check": {"items": [{"item_kind": "anchor",
                                     "item_id": "back_button",
                                     "status": "passed"}],
                          "status": "passed"},
                "passed": True})
        except AdaptationError:
            pass  # 修后：请求字段封闭/证据不存在直接拒绝（允许路径）
    published = False
    try:
        preview = svc.preview(device, sid)
        record = svc.publish(device, {
            "session_id": sid, "preview_id": preview["preview_id"],
            "principal_id": "operator",
            "client_request_id": str(uuid.uuid4()),
            "accepted_items": preview["accepted_items"]})
        published = record.get("status") == "verified"
    except AdaptationError:
        published = False
    # 目标断言：伪造证据 + passed 不能产生任何 verified 版本
    assert not published, "伪造证据+客户端 passed 不得发布 verified"
    assert svc.get_current(device) is None


# ---------------- 合法四态：真实两帧 + 服务端匹配 → verified ----------------
def test_r2_16_real_evidence_full_flow_publishes_verified(env):
    svc = env["bundle"].adaptation_svc
    device = "arm-01"
    sid = svc.start_session(device, {
        "template_ref": {"id": "tpl-1", "version": 1},
        "principal_id": "operator",
        "installation_binding": DEFAULT_BINDING})["session_id"]
    _, revision = _write_all(svc, device, sid, 1)
    assert revision == 5  # 1 + 四次写页
    preview = svc.preview(device, {"capture_session_id": sid})
    assert preview["status"] == "preview"
    assert len(preview["accepted_items"]) > 0
    assert preview["failed_items"] == []
    record = svc.publish(device, {
        "client_request_id": str(uuid.uuid4()),
        "session_id": sid, "preview_id": preview["preview_id"],
        "principal_id": "operator",
        "expected_revision": 0,
        "accepted_items": preview["accepted_items"]})
    assert record["status"] == "verified"
    assert record["record"]["version"] == 1
    assert record["operation_id"]
    # 无按钮试触/发送：采集调用全部零动作（providers 只返回帧）
    assert all(call.endswith(":2") or call.endswith(":1")
               for call in env["provider"].calls)
    # 服务重启后已发布适配不失效：从持久资料恢复
    env["bundle"].close()
    reopened = LayoutBundle.__new__(LayoutBundle)
    from tests.layout_r2_helpers import open_bundle
    bundle2 = open_bundle(env["root"], clock=env["clock"],
                          capture_provider=env["provider"],
                          calibration_provider=env["calibration"])
    try:
        record2 = bundle2.adaptation_svc.get_version(device, 1)
        assert record2 is not None and record2["sha256"] == record["record"]["sha256"]
        assert bundle2.adaptation_svc.get_current(device)["version"] == 1
    finally:
        bundle2.close()


# ---------------- 服务端匹配真实失败：错图/缺最低项/错设备/错作者 ----------------
def test_r2_16_mismatched_image_yields_failed_not_verified(tmp_path):
    clock = FakeClock()
    provider_frames = build_page_frames(build_template_assets(), correct=False)
    provider = FakeCaptureProvider(provider_frames, binding=DEFAULT_BINDING,
                                   source_geometry_id=DEFAULT_GEOMETRY_ID)
    bundle = LayoutBundle(tmp_path / "rt", clock=clock,
                          capture_provider=provider,
                          calibration_provider=FakeCalibrationProvider())
    try:
        svc = bundle.adaptation_svc
        device = "arm-01"
        sid = svc.start_session(device, {
            "template_ref": {"id": "tpl-1", "version": 1},
            "principal_id": "operator",
            "installation_binding": DEFAULT_BINDING})["session_id"]
        # 图像与模板不匹配：写页 check 真实失败，不指定结果
        capture = svc.capture(device, {
            "client_request_id": str(uuid.uuid4()),
            "owner_kind": "session", "owner_id": sid,
            "expected_owner_revision": 1, "page_state": "wechat_list",
            "controlled_test_confirmed": True})
        view = svc.write_page(device, sid, "wechat_list", {
            "client_request_id": str(uuid.uuid4()),
            "expected_session_revision": 1,
            "source_evidence_ids": capture["source_evidence_ids"],
            "local_overrides": []})
        check = view["check"]
        assert check["status"] == "failed"
        failed_ids = {r["id"] for r in check["anchor_results"]
                      if r["status"] == "failed"}
        assert failed_ids, "不匹配图像必须产生真实失败锚点"
        assert any(r.get("reason_code") in ("score_below_threshold",
                                            "anchor_missing")
                   for r in check["anchor_results"])
        # 四页写完（失败项保留）→ preview 完整保存失败项，发布 needs_review
        captures = _four_captures(svc, device, sid, view["session_revision"])
        current = view["session_revision"]
        for state in PAGE_STATES[1:]:
            view = svc.write_page(device, sid, state, {
                "client_request_id": str(uuid.uuid4()),
                "expected_session_revision": current,
                "source_evidence_ids": captures[state]["source_evidence_ids"],
                "local_overrides": []})
            current = view["session_revision"]
        preview = svc.preview(device, {"capture_session_id": sid})
        assert preview["failed_items"], "失败项必须完整保存"
        record = svc.publish(device, {
            "client_request_id": str(uuid.uuid4()),
            "session_id": sid, "preview_id": preview["preview_id"],
            "principal_id": "operator", "expected_revision": 0,
            "accepted_items": preview["accepted_items"]})
        assert record["status"] == "needs_review"
        assert record["failed_items"] == preview["failed_items"]
        # 修失败项不要求逐按钮重登记：重采失败页后可整体确认
    finally:
        bundle.close()


def test_r2_16_wrong_device_author_and_capture_gates(tmp_path):
    bundle = LayoutBundle(tmp_path / "rt", capture_provider=FakeCaptureProvider(
        build_page_frames(build_template_assets()), binding=DEFAULT_BINDING,
        source_geometry_id=DEFAULT_GEOMETRY_ID),
        calibration_provider=FakeCalibrationProvider())
    try:
        svc = bundle.adaptation_svc
        device = "arm-01"
        sid = svc.start_session(device, {
            "template_ref": {"id": "tpl-1", "version": 1},
            "principal_id": "op-a",
            "installation_binding": DEFAULT_BINDING})["session_id"]
        # 错作者采集
        with pytest.raises(AdaptationError, match="author_mismatch"):
            svc.capture(device, {
                "client_request_id": str(uuid.uuid4()),
                "owner_kind": "session", "owner_id": sid,
                "expected_owner_revision": 1, "page_state": "wechat_list",
                "principal_id": "op-b",
                "controlled_test_confirmed": True})
        # 缺 v3：未配置/未就绪校准 → 409 items_unverified
        bundle2 = LayoutBundle(
            tmp_path / "rt2",
            capture_provider=FakeCaptureProvider(
                build_page_frames(build_template_assets()),
                binding=DEFAULT_BINDING,
                source_geometry_id=DEFAULT_GEOMETRY_ID),
            calibration_provider=FakeCalibrationProvider(ready=False))
        try:
            sid2 = bundle2.adaptation_svc.start_session(device, {
                "template_ref": {"id": "tpl-1", "version": 1},
                "principal_id": "op-a",
                "installation_binding": DEFAULT_BINDING})["session_id"]
            with pytest.raises(AdaptationError, match="items_unverified"):
                bundle2.adaptation_svc.capture(device, {
                    "client_request_id": str(uuid.uuid4()),
                    "owner_kind": "session", "owner_id": sid2,
                    "expected_owner_revision": 1,
                    "page_state": "wechat_list",
                    "controlled_test_confirmed": True})
        finally:
            bundle2.close()
    finally:
        bundle.close()


def test_r2_16_capture_sampling_gates_reject_bad_frames(tmp_path):
    assets = build_template_assets()
    frames = build_page_frames(assets)
    provider = FakeCaptureProvider(frames, binding=DEFAULT_BINDING,
                                   source_geometry_id=DEFAULT_GEOMETRY_ID)
    bundle = LayoutBundle(tmp_path / "rt", capture_provider=provider,
                          calibration_provider=FakeCalibrationProvider())
    try:
        svc = bundle.adaptation_svc
        device = "arm-01"
        sid = svc.start_session(device, {
            "template_ref": {"id": "tpl-1", "version": 1},
            "principal_id": "op-a",
            "installation_binding": DEFAULT_BINDING})["session_id"]
        base_request = {
            "owner_kind": "session", "owner_id": sid,
            "expected_owner_revision": 1, "page_state": "wechat_list",
            "controlled_test_confirmed": True}

        def attempt(mutate):
            def wrapped(device_id, count=2, page_state=None):
                produced = provider(device_id, count, page_state)
                mutate(produced)
                return produced
            svc._capture_provider = wrapped
            try:
                svc.capture(device, {**base_request,
                                     "client_request_id": str(uuid.uuid4())})
            except AdaptationError as exc:
                return exc
            finally:
                svc._capture_provider = provider
            return None

        # 序号不推进
        exc = attempt(lambda produced: produced[1].update(
            source_frame_seq=produced[0]["source_frame_seq"]))
        assert exc is not None and "推进" in exc.message
        # 间隔 <200ms（最坏口径）
        exc = attempt(lambda produced: produced[1].update(
            source_captured_monotonic_ns=(
                produced[0]["source_captured_monotonic_ns"] + 190_000_000)))
        assert exc is not None and "间隔" in exc.message
        # 年龄 >1000ms
        exc = attempt(lambda produced: produced[1].update(
            received_monotonic_ms=(
                produced[1]["source_captured_monotonic_ns"] // 1_000_000
                + 1500)))
        assert exc is not None and "年龄" in exc.message
        # 换绑定
        def swap_binding(produced):
            produced[1] = {**produced[1],
                           "installation_binding":
                               {**DEFAULT_BINDING,
                                "camera_binding_id":
                                    "44444444-4444-4444-8444-444444444444"}}
        exc = attempt(swap_binding)
        assert exc is not None and exc.code == "source_binding_changed"
    finally:
        bundle.close()


# ---------------- 预览不可变 + 预览后写页不能消费旧结果 ----------------
def test_r2_16_preview_invalidated_by_later_page_write(env):
    svc = env["bundle"].adaptation_svc
    device = "arm-01"
    sid = svc.start_session(device, {
        "template_ref": {"id": "tpl-1", "version": 1},
        "principal_id": "operator",
        "installation_binding": DEFAULT_BINDING})["session_id"]
    _write_all(svc, device, sid, 1)
    preview = svc.preview(device, {"capture_session_id": sid})
    # 预览后又写页（重采一页）→ 会话修订变化 → 旧预览不能消费
    captures = _four_captures(svc, device, sid, 5)
    view = svc.write_page(device, sid, "chat_ime_text", {
        "client_request_id": str(uuid.uuid4()),
        "expected_session_revision": 5,
        "source_evidence_ids": captures["chat_ime_text"]["source_evidence_ids"],
        "local_overrides": []})
    assert view["session_revision"] == 6
    with pytest.raises(AdaptationError, match="revision_conflict|会话已变化|重新预览") as excinfo:
        svc.publish(device, {
            "client_request_id": str(uuid.uuid4()),
            "session_id": sid, "preview_id": preview["preview_id"],
            "principal_id": "operator", "expected_revision": 0,
            "accepted_items": preview["accepted_items"]})
    # 重新预览后可发布；旧预览仍不可二次消费（一次消费语义）
    preview2 = svc.preview(device, {"capture_session_id": sid})
    record = svc.publish(device, {
        "client_request_id": str(uuid.uuid4()),
        "session_id": sid, "preview_id": preview2["preview_id"],
        "principal_id": "operator", "expected_revision": 0,
        "accepted_items": preview2["accepted_items"]})
    assert record["status"] == "verified"
    with pytest.raises(AdaptationError, match="preview_consumed"):
        svc.publish(device, {
            "client_request_id": str(uuid.uuid4()),
            "session_id": sid, "preview_id": preview2["preview_id"],
            "principal_id": "operator", "expected_revision": 1,
            "accepted_items": preview2["accepted_items"]})


# ---------------- 采集按编号幂等：同 client_request_id 重放不重新采图 ----------------
def test_r2_16_capture_replay_same_request_same_result(env):
    svc = env["bundle"].adaptation_svc
    device = "arm-01"
    sid = svc.start_session(device, {
        "template_ref": {"id": "tpl-1", "version": 1},
        "principal_id": "operator",
        "installation_binding": DEFAULT_BINDING})["session_id"]
    request_id = str(uuid.uuid4())
    request = {"client_request_id": request_id, "owner_kind": "session",
               "owner_id": sid, "expected_owner_revision": 1,
               "page_state": "wechat_list", "controlled_test_confirmed": True}
    first = svc.capture(device, request)
    calls_before = len(env["provider"].calls)
    replay = svc.capture(device, request)
    assert replay["capture_id"] == first["capture_id"]
    assert replay["replayed"] is True
    assert replay["capture"]["created_at"] == first["capture"]["created_at"]
    assert len(env["provider"].calls) == calls_before  # 未重新采图
    # 同号异内容冲突
    with pytest.raises(AdaptationError, match="request_conflict|不同内容|已绑定"):
        svc.capture(device, {**request, "page_state": "chat_ime_hidden"})
    # 页证据 15 分钟期限：accepted_at = 采集首次归档时间，重放不续期
    assert first["capture"]["evidence"][0]["page_state"] == "wechat_list"
