"""calibrated_scroll：校准直驱上下滑动（2026-10-03 规格 §5/§6/§7）。

覆盖：链式归并共享函数、v3 绑定（v2 拒绝）、能力查询（零动作）、
入参拒绝（证书/模板/机械参数覆盖）、经正式任务入口的单次滑动
（机械/效果分离 + 方向核对 + 静止=failed(scroll_no_displacement)）。
"""
from __future__ import annotations

import asyncio
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from drivers import calibration_bundle as cb, scroll_observation as so, screen_gestures
from runtime.calibrated_scroll import (
    CONTRACT_VERSION, DEFAULT_OBSERVATION_ROI, PLANNING_VERSION,
    binding_view, capability_view, plan_action, validate_payload,
)
from tests.helpers import make_device_configs, make_runtime
from tests.test_deferred_input_acceptance import motion_bundle

SCROLL_PLUGIN_SOURCE = Path(__file__).parents[1] / "examples" / "plugins" / "screen-gesture-adapter"


# 相机(1280x720) -> 屏幕(1080x2400) 的缩放单应：屏幕点反投回相机画面内
CAMERA_TO_SCREEN = [1080 / 1280, 0, 0, 0, 2400 / 720, 0, 0, 0, 1]
SCREEN_CORNERS = [[240, 60], [1040, 60], [1040, 660], [240, 660]]


def v3_bundle():
    bundle = motion_bundle()
    bundle["schemaVersion"] = 3
    bundle["cameraBindingId"] = "cam-bind-01"
    bundle["sourceGeometryId"] = "geom-01"
    bundle["camera"]["cameraToScreen"]["homography"] = list(CAMERA_TO_SCREEN)
    bundle["screen"]["edgeModel"]["corners"] = [list(c) for c in SCREEN_CORNERS]
    return bundle


# ---------------------------------------------------------------------------
# aggregate_scroll_chain（§6.3 共享归并）
# ---------------------------------------------------------------------------

def _piece(ok=True, dy=0.0, dx=0.0, ratio=0.9, points=20, mad=0.0, reason=None):
    return {"ok": ok, "reason_code": reason, "dy_px": dy, "dx_px": dx,
            "valid_points": points, "mad_dy_px": mad, "inlier_ratio": ratio}


def test_chain_accumulates_displacement_across_pairs():
    merged = so.aggregate_scroll_chain(
        [_piece(dy=-30.0, dx=1.0), _piece(dy=-25.0, dx=-1.0)], 400, 600)
    assert merged["status"] == "displaced"
    assert merged["dy_px"] == pytest.approx(-55.0)
    assert merged["chain_frames"] == 3
    assert merged["valid_points"] == 20 and merged["mad_dy_px"] == 0.0


def test_chain_failed_pair_is_unknown_not_stationary():
    merged = so.aggregate_scroll_chain(
        [_piece(dy=-30.0), _piece(ok=False, reason="insufficient_inliers")], 400, 600)
    assert merged["status"] == "unknown"
    assert merged["reason_code"] == "insufficient_inliers"


def test_chain_per_pair_lateral_ok_but_cumulative_path_excess_is_unknown():
    # 每对横移都在限内（0.03*400=12px），但累计路径超限
    merged = so.aggregate_scroll_chain(
        [_piece(dy=-20.0, dx=10.0), _piece(dy=-20.0, dx=-10.0),
         _piece(dy=-20.0, dx=10.0), _piece(dy=-20.0, dx=-10.0)], 400, 600)
    assert merged["status"] == "unknown"
    assert merged["reason_code"] == "cumulative_lateral_excess"


def test_chain_round_trip_net_zero_is_not_stationary():
    # 明显上下往返：净位移接近零，但逐对位移大 → 不是静止，是 unknown
    merged = so.aggregate_scroll_chain([_piece(dy=40.0), _piece(dy=-40.0)], 400, 600)
    assert merged["status"] == "unknown"


def test_chain_stationary_requires_mad_threshold():
    # 净位移接近零但离散度超静止 MAD 阈值 → 不是静止
    params = so.default_params()
    stationary_mad = float(params["stationary"]["median_ratio"]) * 600
    merged = so.aggregate_scroll_chain(
        [_piece(dy=2.0, mad=stationary_mad * 3), _piece(dy=-2.0, mad=stationary_mad * 3)],
        400, 600)
    assert merged["status"] == "unknown"


def test_chain_ocr_contradiction_is_unknown_even_when_displaced():
    merged = so.aggregate_scroll_chain(
        [_piece(dy=-40.0)], 400, 600, ocr_shift_px=12.0)  # 光流向上、OCR 向下
    assert merged["status"] == "unknown"
    assert merged["reason_code"] == "ocr_flow_conflict"
    ok = so.aggregate_scroll_chain([_piece(dy=-40.0)], 400, 600, ocr_shift_px=-12.0)
    assert ok["status"] == "displaced"


def test_measure_pair_quality_reports_points_and_mad():
    import numpy as np
    rng = np.random.default_rng(7)
    before = (rng.random((120, 160)) * 255).astype("uint8")
    after = np.roll(before, 8, axis=0)
    roi = {"x": 0, "y": 0, "width": 160, "height": 120}
    piece = so.measure_pair_quality(before, after, roi)
    assert piece["ok"] is True
    assert type(piece["valid_points"]) is int and piece["valid_points"] > 0
    assert piece["mad_dy_px"] is not None and piece["mad_dy_px"] >= 0.0


# ---------------------------------------------------------------------------
# v3 绑定
# ---------------------------------------------------------------------------

def test_binding_v3_accepts_v3_and_rejects_v2(tmp_path):
    bundle = v3_bundle()
    binding = screen_gestures.binding_snapshot_v3(bundle)
    assert binding["camera_binding_id"] == "cam-bind-01"
    assert binding["source_geometry_id"] == "geom-01"
    assert set(binding) == set(screen_gestures.BINDING_V3_KEYS)
    with pytest.raises(ValueError, match="schemaVersion"):
        screen_gestures.binding_snapshot_v3(motion_bundle())  # v2 原样拒绝
    missing = v3_bundle()
    missing.pop("cameraBindingId")
    with pytest.raises(ValueError, match="cameraBindingId"):
        screen_gestures.binding_snapshot_v3(missing)


def test_binding_v3_fingerprint_is_semantically_checked(tmp_path):
    bundle = v3_bundle()
    binding = screen_gestures.binding_snapshot_v3(bundle)
    # 语义比较：十字段全部比较；改任意字段（含 fingerprint）即拒绝
    screen_gestures.require_binding_v3(binding, bundle)
    drifted = deepcopy(binding)
    drifted["daemon_session"] = "sess-other"
    with pytest.raises(ValueError, match="已变化"):
        screen_gestures.require_binding_v3(drifted, bundle)
    # 附件 §2：设备登记修订（binding_revision）不参与 v3 语义比较
    revised = deepcopy(binding)
    revised["binding_revision"] = int(revised["binding_revision"]) + 1
    screen_gestures.require_binding_v3(revised, bundle)  # 不拒绝


# ---------------------------------------------------------------------------
# 能力查询与入参校验（经真实运行时与插件安装）
# ---------------------------------------------------------------------------

async def _scroll_runtime(tmp_path, *, schema=3, with_plugin=True):
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime
    config = PlatformConfig(
        {"db_path": str(tmp_path / "fleet.db"), "evidence_dir": str(tmp_path / "evidence"),
         "plugin_dir": str(tmp_path / "plugins"), "hardware_mode": "simulation"},
        root=tmp_path)
    runtime = ArmFleetRuntime(config)
    if with_plugin:
        runtime.plugins.install(SCROLL_PLUGIN_SOURCE, operator_id="操作员甲", reason="测试安装")
        runtime.plugins.activate("screen-gesture-adapter", operator_id="操作员甲", reason="测试启用")
    await runtime.startup(device_configs=make_device_configs(1))
    bundle = v3_bundle() if schema == 3 else motion_bundle()
    root = tmp_path / "calibration"
    if schema == 3:
        # v3 档案按规格只能来自未来 v3 校准生产者（save_bundle 强制 v2），
        # 测试直接落盘完整 v3 JSON——与"旧 v2 不得加字段升级续用"不冲突：
        # 本 fixture 携带完整 v2 机械证据 + v3 身份字段。
        import json as _json
        path = cb.bundle_path("arm-01", root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    else:
        cb.save_bundle(bundle, root)
    runtime.devices.driver("arm-01").data_root = root
    device = runtime.devices.get("arm-01")
    device.update({
        "camera_id": bundle["cameraId"],
        "camera_device_unique_id": bundle["cameraDeviceUniqueId"],
        "daemon_session": bundle["daemonSession"]})
    camera_driver = runtime.devices.camera("arm-01")
    try:
        camera_driver.configure_simulation_frame(
            bundle["frame"]["width"], bundle["frame"]["height"],
            screen_corners=[tuple(c) for c in SCREEN_CORNERS])
    except Exception:
        pass
    _sync_session(runtime, root)
    attach_simulated_source_identity(runtime, bundle)
    return runtime, root, bundle


def attach_simulated_source_identity(runtime, bundle):
    """Freeze the fake camera's own installation identity, not each current bundle."""
    camera = runtime.devices.camera("arm-01")
    capture = camera.capture_sync
    source = {
        "camera_id": bundle["cameraId"],
        "device_unique_id": bundle["cameraDeviceUniqueId"],
        "camera_binding_id": bundle.get("cameraBindingId"),
        "source_geometry_id": bundle.get("sourceGeometryId"),
        "binding_revision": runtime.devices.get("arm-01").get("binding_revision"),
    }
    camera.capture_sync = lambda: {**capture(), **source}


def _sync_session(runtime, root):
    """仿真驱动会话以运行时为准：把档案 daemonSession 对齐当前设备会话。"""
    import json as _json
    device = runtime.devices.get("arm-01")
    session = device.get("daemon_session")
    if not session:
        return
    path = cb.bundle_path("arm-01", root)
    if not path.exists():
        return
    data = _json.loads(path.read_text(encoding="utf-8"))
    if data.get("daemonSession") != session:
        data["daemonSession"] = session
        path.write_text(_json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_capability_view_requires_v3_calibration(tmp_path):
    async def scenario():
        runtime, root, _ = await _scroll_runtime(tmp_path, schema=2)
        try:
            view = capability_view(runtime, "arm-01")
            assert view["usable"] is False and view["binding"] is None
            codes = {item["code"] for item in view["blocking_reasons"]}
            assert "calibration_v3_unavailable" in codes
            assert view["actions"]["swipe_up"]["usable"] is False
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


def test_capability_view_usable_with_v3_and_plugin(tmp_path):
    async def scenario():
        runtime, root, bundle = await _scroll_runtime(tmp_path, schema=3)
        try:
            view = capability_view(runtime, "arm-01")
            assert view["contract_version"] == CONTRACT_VERSION
            assert view["usable"] is True
            assert view["binding"]["camera_binding_id"] == "cam-bind-01"
            assert all(view["actions"][a]["usable"] for a in ("swipe_up", "swipe_down"))
            assert view["planning_version"] == PLANNING_VERSION
            assert view["round_budget"] == {"max_actions": 8, "window_seconds": 90}
            # 计划由校准计算：方向默认值来自 screen_gestures（§5.3）
            plan = plan_action(runtime, "arm-01", "swipe_up")
            assert plan["start"]["y"] > plan["end"]["y"]  # 上滑：自下而上
            down = plan_action(runtime, "arm-01", "swipe_down")
            assert down["start"]["y"] < down["end"]["y"]
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


def test_capability_view_reports_missing_plugin(tmp_path):
    async def scenario():
        runtime, root, _ = await _scroll_runtime(tmp_path, schema=3, with_plugin=False)
        try:
            view = capability_view(runtime, "arm-01")
            codes = {item["code"] for item in view["blocking_reasons"]}
            assert "plugin_not_installed" in codes
            assert view["usable"] is False
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


def test_validate_payload_rejects_certificates_and_parameter_overrides(tmp_path):
    async def scenario():
        runtime, root, bundle = await _scroll_runtime(tmp_path, schema=3)
        try:
            binding = binding_view(runtime, "arm-01")
            device = runtime.devices.get("arm-01")
            task = {"task_type": "calibrated_scroll", "device_id": "arm-01",
                    "payload": {"action": "swipe_up", "binding": binding}}
            validate_payload(task, device)
            for extra in ({"gesture_certificate_id": "cert-1"},
                          {"template_id": "tpl"}, {"top_proof_available": True},
                          {"start": {"x": 0.5, "y": 0.7}, "end": {"x": 0.5, "y": 0.3}},
                          {"duration_ms": 800}, {"depth_mm": 5}, {"ready": True}):
                bad = {"task_type": "calibrated_scroll", "device_id": "arm-01",
                       "payload": {"action": "swipe_up", "binding": binding, **extra}}
                with pytest.raises(ValueError):
                    validate_payload(bad, device)
            wrong_device = {**binding, "device_id": "arm-02"}
            with pytest.raises(ValueError, match="不一致"):
                validate_payload({"task_type": "calibrated_scroll", "device_id": "arm-01",
                                  "payload": {"action": "swipe_up", "binding": wrong_device}},
                                 device)
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 正式任务入口：提交 → 计划 → 执行 → 机械/效果分离
# ---------------------------------------------------------------------------

def _scroll_payload(binding, action="swipe_up", task_id="scroll-1"):
    return {"task_id": task_id, "task_type": "calibrated_scroll",
            "contract_version": CONTRACT_VERSION, "device_id": "arm-01",
            "idempotency_key": task_id, "correlation_id": "corr-scroll",
            "expires_at": "2099-01-01T00:00:00Z",
            "payload": {"action": action, "binding": deepcopy(binding)}}


def test_submit_rejects_forbidden_fields_and_v2_binding(tmp_path):
    async def scenario():
        from runtime.errors import ApiError
        runtime, root, _ = await _scroll_runtime(tmp_path, schema=3)
        try:
            binding = binding_view(runtime, "arm-01")
            bad = _scroll_payload(binding)
            bad["payload"]["duration_ms"] = 900
            with pytest.raises(ApiError):
                runtime.submit_task(bad, source="console",
                                    context={"operator_id": "测试", "reason": "t"})
            v2 = dict(binding)
            for key in ("camera_binding_id", "source_geometry_id"):
                v2.pop(key)
            bad2 = _scroll_payload(v2, task_id="scroll-2")
            with pytest.raises(ApiError):
                runtime.submit_task(bad2, source="console",
                                    context={"operator_id": "测试", "reason": "t"})
            assert runtime.list_tasks() == []
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


def test_scroll_e2e_mechanical_and_effect_separated(tmp_path, monkeypatch):
    async def scenario():
        runtime, root, bundle = await _scroll_runtime(tmp_path, schema=3)
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            # 仿真：机械滑动本身让内容滚动（FakeCamera 纹理整体位移 40px）
            camera = runtime.devices.camera("arm-01")
            driver = runtime.devices.driver("arm-01")
            original_swipe = driver.swipe

            async def scrolling_swipe(gesture=None, *args, **kwargs):
                # 仿真：机械滑动发生时列表内容滚动（相机像素 120px）
                try:
                    camera.scroll_by(24)
                except Exception:
                    pass
                return await original_swipe(*([gesture] if gesture is not None else []),
                                            *args, **kwargs)
            monkeypatch.setattr(driver, "swipe", scrolling_swipe)

            task = _lease(runtime, _submit(runtime, _scroll_payload(binding, "swipe_up")))
            done = await runtime.executor.execute(task["task_id"])
            stored = runtime.get_task(task["task_id"])
            detail = (stored.get("step_log") or [{}])[-1].get("detail") or {}
            assert done["status"] == "confirmed", (done.get("failure_reason"), detail)
            assert detail.get("mechanical_status") == "confirmed"
            assert detail.get("effect_status") == "displaced"
            assert detail.get("direction_consistent") is True  # 上滑 → dy < 0
            assert detail.get("motion_started") is True
            assert detail.get("retryable") is False
            assert detail.get("before_evidence") and detail.get("after_evidence")
            assert detail.get("observation_roi") == DEFAULT_OBSERVATION_ROI
            assert detail.get("binding", {}).get("camera_binding_id") == "cam-bind-01"
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


def test_scroll_e2e_stationary_maps_to_failed_no_displacement(tmp_path, monkeypatch):
    async def scenario():
        runtime, root, bundle = await _scroll_runtime(tmp_path, schema=3)
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            task = _lease(runtime, _submit(runtime, _scroll_payload(binding, "swipe_down", task_id="scroll-still")))
            done = await runtime.executor.execute(task["task_id"])
            stored = runtime.get_task(task["task_id"])
            detail = (stored.get("step_log") or [{}])[-1].get("detail") or {}
            # 内容纹丝不动：机械确认完成 + 效果静止 = failed(scroll_no_displacement)，
            # 不自动重试、不宣称到顶/到底（§6.4 终态映射）
            assert done["status"] == "failed"
            assert done.get("error_category") == "scroll_no_displacement"
            assert detail.get("mechanical_status") == "confirmed"
            assert detail.get("effect_status") == "stationary"
        finally:
            await runtime.shutdown()
    asyncio.run(scenario())


def _stable_session(runtime, monkeypatch, session):
    """仿真 fake 驱动不回报 daemon 会话：状态刷新判会话变化会撤销租约。

    垫驱动 read_state 使其回报固定会话（真机会话由 daemon 连接提供）。
    """
    driver = runtime.devices.driver("arm-01")
    original = driver.read_state

    async def read_state_with_session():
        state = await original()
        if isinstance(state, dict):
            state = dict(state)
            state.setdefault("session", session)
            return state
        return state
    monkeypatch.setattr(driver, "read_state", read_state_with_session)


# --- 公共小助手 -----------------------------------------------------------

def _submit(runtime, payload):
    return runtime.submit_task(payload, source="console",
                               context={"operator_id": "测试", "reason": "规格测试"})


def _lease(runtime, task):
    lease = runtime.leases.acquire(task["device_id"], task["task_id"])
    task.update(lease_id=lease["lease_id"], status="executing")
    runtime.store.save_task(task)
    return task
