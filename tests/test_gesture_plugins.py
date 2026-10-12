"""三个手势插件验收测试（规范第 12/22.5/22.6/22.7/22.10/22.16/22.17 节）。

- gesture-validation-adapter：自检 run -> 发布 -> outcome 映射 -> detail 字段；
- gesture-program-adapter：本地拒绝零动作 / manual_only 转人工 / 合法程序三层归约；
- wx-wechat-adapter wx_auto_navigate：单步导航（search/return_top/back_to_list）
  与旧 wx_list_navigate 零回归。

全部使用 fake 设备与进程内替身；不连接串口、不联网。
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid as uuid_module
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")
pytestmark = [needs_wx_example]


from tests.helpers import make_device_configs, make_runtime, wait_terminal
from tests.test_screen_gesture_driver import gesture_bundle

ROOT = Path(__file__).parents[1]
VALIDATION_SOURCE = ROOT / "examples" / "plugins" / "gesture-validation-adapter"
PROGRAM_SOURCE = ROOT / "examples" / "plugins" / "gesture-program-adapter"
WX_SOURCE = ROOT / "examples" / "plugins" / "wx-wechat-adapter"


def load_module(name: str, source_dir: Path):
    spec = importlib.util.spec_from_file_location(name, source_dir / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_manifest(source_dir: Path) -> dict:
    return json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))


def full_binding(device_id: str = "arm-01", *, profile_version: int = 0) -> dict:
    from drivers.screen_gestures import binding_snapshot
    bundle, frame = calibration_fixture()
    return {
        **binding_snapshot(bundle), "device_id": device_id,
        "coordinate_frame_hash": frame["coordinate_frame_hash"],
        "gesture_profile_version": profile_version,
        "recipe_version": "gesture-rules.v1",
    }


def calibration_fixture():
    from drivers import gesture_program as gp
    from drivers import screen_gestures as sg
    bundle = gesture_bundle()
    frame = gp.build_coordinate_frame(
        binding=sg.binding_snapshot(bundle), calibration_run_id="run-plugin-test",
        root_width_px=bundle["screen"]["screenWidth"],
        root_height_px=bundle["screen"]["screenHeight"],
        display_width_px=1080, display_height_px=2400,
        root_origin_display_px=[0, 80], density=3.0,
        source_client_instance_id="0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001")
    return bundle, frame


def _seed_calibration(tmp_path: Path) -> None:
    """校准 bundle + 坐标桥写入 tmp/data（与 runtime 数据根一致）。"""
    from drivers import calibration_bundle as cb, gesture_program as gp
    bundle, frame = calibration_fixture()
    cb.save_bundle(bundle, tmp_path / "data")
    gp.save_coordinate_frame("arm-01", frame, data_root=tmp_path / "data")


def navigation_program(gesture_id):
    points = {
        "scroll_up": [(0.5, 0.8), (0.5, 0.35)],
        "scroll_down": [(0.5, 0.35), (0.5, 0.8)],
        "system_back_left": [(0.02, 0.5), (0.30, 0.5)],
        "system_back_right": [(0.98, 0.5), (0.70, 0.5)],
        "system_home": [(0.5, 0.98), (0.5, 0.70)],
    }[gesture_id]
    return {
        "program_version": "gesture-program.v1",
        "coordinate_space": "calibration_root_normalized.v1",
        "strokes": [{"points": [dict(x=x, y=y, t_ms=i * 2200)
                                for i, (x, y) in enumerate(points)],
                     "hold_before_ms": 40, "hold_after_ms": 20, "gap_after_ms": 0}],
    }


def seed_navigation(tmp_path):
    from runtime import automatic_gestures as ag
    from drivers.screen_gestures import binding_snapshot
    _seed_calibration(tmp_path)
    bundle, frame = calibration_fixture()
    # 开关已删（2026-09-30 拍板）：可用性由校准与证书决定
    run = stub_run("completed", valid=("scroll_up", "scroll_down"))
    for gesture_id in ("system_back_left", "system_back_right", "system_home"):
        row = deepcopy(run["action_results"][0])
        row.update(gesture_id=gesture_id, program=navigation_program(gesture_id))
        run["action_results"].append(row)
    run.update(input_binding=binding_snapshot(bundle),
               coordinate_frame_hash=frame["coordinate_frame_hash"])
    return ag.publish_run_results("arm-01", run, data_root=tmp_path / "data")


def seed_navigation_template(tmp_path, kind="top"):
    from drivers.screen_gestures import binding_snapshot
    from tests.test_automatic_gesture_api import _template_fixture
    directory = tmp_path / "data" / "devices" / "arm-01" / "navigation_templates"
    directory.mkdir(parents=True, exist_ok=True)
    template = _template_fixture(binding_snapshot(calibration_fixture()[0]), kind,
                                 evidence_root=tmp_path / "evidence")
    (directory / f"{kind}.json").write_text(json.dumps(template), encoding="utf-8")


async def unmatched_template(*args, **kwargs):
    return {"matched": False, "camera_failed": False}


async def drive(runtime, body, *, source="console", reason="插件验收"):
    task = runtime.submit_task(body, source=source,
                               context={"operator_id": "操作员甲", "reason": reason})
    return await wait_terminal(runtime, task["task_id"])


def last_detail(task: dict) -> dict:
    done_rows = [row for row in task.get("step_log", []) if row.get("phase") == "done"]
    assert done_rows, task.get("failure_reason")
    return done_rows[-1].get("detail") or {}


# =====================================================================
# 交付 1：gesture-validation-adapter
# =====================================================================

def self_test_body(actions=("scroll_up", "scroll_down"), *, key=None,
                   profile_version=0) -> dict:
    key = key or f"self-{uuid_module.uuid4().hex[:8]}"
    return {
        "task_id": key, "idempotency_key": key,
        "task_type": "gesture_self_test",
        "contract_version": "gesture-validation.v1",
        "device_id": "arm-01",
        "payload": {"binding": full_binding(profile_version=profile_version),
                    "actions": list(actions),
                    "rules_version": "gesture-rules.v1"},
        "expires_at": "2099-01-01T00:00:00Z",
        "correlation_id": key,
    }


def stub_run(state: str, *, valid=("scroll_up",), run_id="run-stub-1",
             stop_reason=None, physical_count=6) -> dict:
    rows = []
    for gesture_id in ("scroll_up", "scroll_down"):
        is_valid = gesture_id in valid
        rows.append({
            "gesture_id": gesture_id,
            "state": "valid" if is_valid else "unverified",
            "validation_status": "three_passes" if is_valid else "not_tested",
            "trial_ids": ["t1", "t2", "t3"] if is_valid else ["t4"],
            "controller_status": "completed",
            "contact_status": "continuous" if is_valid else "not_observed",
            "effect_status": "verified" if is_valid else "not_checked",
            "reason_code": None if is_valid else "effect_not_observed",
            "evidence_refs": [],
            "program": navigation_program(gesture_id),
        })
    return {
        "run_id": run_id, "device_id": "arm-01", "state": state,
        "input_binding": {k: v for k, v in full_binding().items()
                          if k not in {"coordinate_frame_hash", "gesture_profile_version", "recipe_version"}},
        "coordinate_frame_hash": full_binding()["coordinate_frame_hash"],
        "gesture_rules_version": "gesture-rules.v1",
        "action_results": rows,
        "budget": {"physical_count": physical_count, "physical_limit": 36,
                   "elapsed_ms": 1234, "timeout_ms": 600000},
        "stop_reason": stop_reason,
        "published_profile_version": None,
    }


def install_runner_stub(monkeypatch, run: dict) -> list[dict]:
    from runtime import gesture_validation as gv
    created: list[dict] = []

    class StubRunner:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.actions = None

        async def run(self, actions):
            self.actions = list(actions)
            return deepcopy(run)

    monkeypatch.setattr(gv, "GestureValidationRunner", StubRunner)
    return created


def validation_fixture(tmp_path, monkeypatch):
    module = load_module("gval_adapter_under_test", VALIDATION_SOURCE)
    monkeypatch.setattr(module, "DATA_ROOT", tmp_path / "data")
    return module, load_manifest(VALIDATION_SOURCE)


def test_self_test_passed_publishes_profile_and_succeeds(tmp_path, monkeypatch):
    module, manifest = validation_fixture(tmp_path, monkeypatch)
    _seed_calibration(tmp_path)
    created = install_runner_stub(
        monkeypatch, stub_run("passed", valid=("scroll_up", "scroll_down")))

    async def run():
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            done = await drive(runtime, self_test_body())
            assert done["status"] == "confirmed", (
                done.get("failure_reason"), done.get("step_log"))
            detail = last_detail(done)
            assert detail["contract_version"] == "gesture-validation.v1"
            assert detail["run_id"] == "run-stub-1"
            assert detail["run_state"] == "passed"
            assert detail["published_profile_version"] == 1
            assert detail["budget"] == {"physical_count": 6, "physical_limit": 36,
                                        "elapsed_ms": 1234, "timeout_ms": 600000}
            assert detail["controller_status"] == "completed"
            assert detail["contact_status"] == "continuous"
            assert detail["effect_status"] == "verified"
            assert detail["input_binding"]["device_id"] == "arm-01"
            assert [row["gesture_id"] for row in detail["action_results"]] == \
                ["scroll_up", "scroll_down"]
            assert set(detail["action_results"][0]) == set(module.ACTION_RESULT_KEYS)

            # 证书发布落在 runtime 数据根
            from runtime import automatic_gestures as ag
            profile = ag.load_profile("arm-01", tmp_path / "data")
            assert profile is not None and profile["version"] == 1
            assert profile["actions"]["scroll_up"]["state"] == "valid"
            assert profile["actions"]["scroll_down"]["state"] == "valid"

            # Runner 构造接线：能力执行器、协调器与预算由插件注入
            assert len(created) == 1
            kwargs = created[0]
            assert kwargs["device_id"] == "arm-01"
            assert kwargs["ime_device_id"] == "arm-01"
            assert kwargs["run_id"].startswith("run-")
            assert kwargs["data_root"] == tmp_path / "data"
            assert callable(kwargs["execute_swipe"])
            assert callable(kwargs["execute_double_tap"])
            assert callable(kwargs["capture_frame"])
            assert kwargs["coordinator"] is not None
        finally:
            await runtime.shutdown()

    asyncio.run(run())


def test_self_test_partial_maps_failed_and_keeps_valid_actions(tmp_path, monkeypatch):
    module, manifest = validation_fixture(tmp_path, monkeypatch)
    _seed_calibration(tmp_path)
    install_runner_stub(monkeypatch, stub_run("partial", valid=("scroll_up",)))

    async def run():
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            done = await drive(runtime, self_test_body())
            assert done["status"] == "failed", done.get("failure_reason")
            assert done.get("error_category") == "effect_not_observed"
            detail = last_detail(done)
            assert detail["run_state"] == "partial"
            # partial 不写成 verified：effect 聚合为 unknown，已通过动作保留在明细
            assert detail["effect_status"] == "unknown"
            valid_rows = [row for row in detail["action_results"]
                          if row["state"] == "valid"]
            assert [row["gesture_id"] for row in valid_rows] == ["scroll_up"]
            assert "scroll_up" in done.get("result_message", "") or \
                "scroll_up" in detail["message"]
            from runtime import automatic_gestures as ag
            profile = ag.load_profile("arm-01", tmp_path / "data")
            assert profile["actions"]["scroll_up"]["state"] == "valid"
            assert profile["actions"]["scroll_down"]["state"] == "unverified"
        finally:
            await runtime.shutdown()

    asyncio.run(run())


def test_self_test_vision_only_only_for_scroll_only_sets(tmp_path, monkeypatch):
    """vision_only 只对纯滑动集生效（2026-09-30 OCR 优先拍板）。

    纯滑动 + payload.vision_only=True → runner 收到 vision_only=True
    （页面会话整段跳过）；混合动作集（tap/back/home）无视该标志，强制
    回页面采样路径（vision_only=False），绝不半吊子视觉。
    """
    module, manifest = validation_fixture(tmp_path, monkeypatch)
    _seed_calibration(tmp_path)
    created = install_runner_stub(monkeypatch, stub_run("passed"))

    async def run():
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            body = self_test_body(("scroll_up", "scroll_down"))
            body["payload"]["vision_only"] = True
            done = await drive(runtime, body)
            assert done["status"] == "confirmed", done.get("failure_reason")
            assert created[-1]["vision_only"] is True

            mixed = self_test_body(("scroll_up", "double_tap"), key="self-mixed")
            mixed["payload"]["vision_only"] = True
            done = await drive(runtime, mixed)
            assert done["status"] == "confirmed", done.get("failure_reason")
            # 混合集强制页面采样：vision_only 标志不生效
            assert created[-1]["vision_only"] is False
            assert len(created) == 2
        finally:
            await runtime.shutdown()

    asyncio.run(run())


def test_self_test_unknown_maps_unknown_with_manual_review(tmp_path, monkeypatch):
    module, manifest = validation_fixture(tmp_path, monkeypatch)
    _seed_calibration(tmp_path)
    run_payload = stub_run("unknown", valid=("scroll_up",), stop_reason="trace_incomplete")
    run_payload["action_results"][1]["controller_status"] = "unknown"
    install_runner_stub(monkeypatch, run_payload)

    async def run():
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            done = await drive(runtime, self_test_body())
            assert done["status"] == "unknown"
            detail = last_detail(done)
            assert detail["run_state"] == "unknown"
            assert detail["manual_review_required"] is True
            assert detail["controller_status"] == "unknown"
            assert detail["retry_policy"] == "none"
        finally:
            await runtime.shutdown()

    asyncio.run(run())


def test_self_test_without_coordinate_frame_uses_calibration_frame(tmp_path, monkeypatch):
    module, manifest = validation_fixture(tmp_path, monkeypatch)
    _seed_calibration(tmp_path)
    from drivers import gesture_program as gprog
    gprog.coordinate_frame_path("arm-01", tmp_path / "data").unlink()
    created = install_runner_stub(monkeypatch, stub_run("passed"))

    async def run():
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            done = await drive(runtime, self_test_body())
            assert done["status"] == "confirmed", done.get("failure_reason")
            # WX 屏幕内滑动使用校准包派生的内部屏幕坐标桥；
            # 缺少 display-root 旁档不应阻挡 screen-normalized 自动手势。
            assert created
            frame = created[0]["coordinate_frame"]
            assert frame["frame_kind"] == "calibration_screen"
            assert frame["coordinate_space_version"] == 3
            assert done["action_call_count"] == 0
        finally:
            await runtime.shutdown()

    asyncio.run(run())


# =====================================================================
# 交付 2：gesture-program-adapter
# =====================================================================

def swipe_program() -> dict:
    return {
        "program_version": "gesture-program.v1",
        "coordinate_space": "calibration_root_normalized.v1",
        "strokes": [{
            "points": [{"x": 0.5, "y": 0.75, "t_ms": 0},
                       {"x": 0.5, "y": 0.30, "t_ms": 1600}],
            "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0,
        }],
    }


def program_body(*, effect_kind="manual_only", program=None, extra=None) -> dict:
    key = f"prog-{uuid_module.uuid4().hex[:8]}"
    effect = ({"kind": "manual_only", "reference_id": "", "reference_version": 0}
              if effect_kind == "manual_only" else
              {"kind": effect_kind, "reference_id": "ref-1", "reference_version": 1})
    payload = {"binding": full_binding(profile_version=1),
               "program": program or swipe_program(),
               "expected_effect": effect}
    payload.update(extra or {})
    return {
        "task_id": key, "idempotency_key": key,
        "task_type": "gesture_program",
        "contract_version": "gesture-program.v1",
        "device_id": "arm-01", "payload": payload,
        "expires_at": "2099-01-01T00:00:00Z", "correlation_id": key,
    }


def program_fixture(tmp_path, monkeypatch):
    module = load_module("gprog_adapter_under_test", PROGRAM_SOURCE)
    monkeypatch.setattr(module, "DATA_ROOT", tmp_path / "data")
    return module, load_manifest(PROGRAM_SOURCE)


def install_program_driver(runtime, *, result=None, fail=None):
    """给 fake 机械臂挂 execute_gesture_program（平台分发按 getattr 查找）。"""
    arm = runtime.devices.driver("arm-01")
    calls: list[dict] = []

    async def execute_gesture_program(plan):
        calls.append(deepcopy(plan))
        if fail is not None:
            raise fail
        return deepcopy(result or {
            "ok": True, "motion_verified": True, "controller_status": "completed",
            "plan_id": plan.get("plan_id") or "plan-fake-1",
            "strokes_completed": len(plan.get("strokes") or []),
        })

    arm.execute_gesture_program = execute_gesture_program

    async def swipe_and_track(gesture):
        calls.append(deepcopy(gesture))
        return deepcopy(result or {
            "ok": True, "motion_verified": True, "controller_status": "completed",
            "plan_id": "plan-fake-1", "swipe_executed": True})

    arm.swipe = swipe_and_track
    return calls


def enable_program_switches(tmp_path):
    """开关已删（2026-09-30 拍板）：执行资格由校准门禁承担，保留调用点兼容。"""
    return


def test_invalid_program_rejected_locally_with_zero_capability_calls(tmp_path, monkeypatch):
    module, manifest = program_fixture(tmp_path, monkeypatch)
    bad = swipe_program()
    bad["strokes"][0]["points"][0]["t_ms"] = 50  # 首点必须为 0

    async def run():
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            calls = install_program_driver(runtime)
            done = await drive(runtime, program_body(program=bad))
            assert done["status"] == "failed"
            assert done.get("error_category") == "invalid_request"
            assert done["capability_call_count"] == 0, "非法程序必须本地拒绝，零能力调用"
            assert done["action_call_count"] == 0
            assert calls == []
        finally:
            await runtime.shutdown()

    asyncio.run(run())


def test_manual_only_program_is_unknown_not_checked_manual_review(tmp_path, monkeypatch):
    module, manifest = program_fixture(tmp_path, monkeypatch)
    _seed_calibration(tmp_path)

    async def run():
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            enable_program_switches(tmp_path)
            program = swipe_program()
            driver_calls = install_program_driver(runtime)
            done = await drive(runtime, program_body(effect_kind="manual_only",
                                                     program=program))
            assert done["status"] == "unknown", done.get("failure_reason")
            assert len(driver_calls) == 1
            detail = last_detail(done)
            assert detail["contract_version"] == "gesture-program.v1"
            assert detail["controller_status"] == "completed"
            assert detail["contact_status"] == "not_observed"
            assert detail["effect_status"] == "not_checked"
            assert detail["manual_review_required"] is True
            assert detail["reason_code"] == "manual_review_required"
            assert "人工确认" in detail["message"]
            assert len(detail["stroke_results"]) == len(program["strokes"])
            assert detail["stroke_results"][0]["controller_status"] == "completed"
            assert detail["stroke_results"][0]["effect_status"] == "not_checked"
        finally:
            await runtime.shutdown()

    asyncio.run(run())


def test_scroll_displacement_program_succeeds_with_full_detail(tmp_path, monkeypatch):
    module, manifest = program_fixture(tmp_path, monkeypatch)
    _seed_calibration(tmp_path)
    # 位姿矫正门（F1）：按档案四角做姿态拟合后才测量。fake 噪声帧无可锁定
    # 四角、FakeCameraDriver 默认 64x48 与档案帧 1280x720 不符——本测试只
    # 验证位移测量契约：相机给到档案尺寸（须在 startup 实例化前替换）、
    # 矫正桩取恒等（四角=档案值），位移测量仍走打桩。
    import runtime.devices as _devices_mod
    from drivers.fake_camera import FakeCameraDriver as _RealFakeCam
    import drivers.screen_edge as _se
    import drivers.screen_pose as _sp

    class _SizedFakeCam(_RealFakeCam):
        def __init__(self, device_id, **kw):
            kw.setdefault("width", 1280)
            kw.setdefault("height", 720)
            super().__init__(device_id, **kw)

    monkeypatch.setattr(_devices_mod, "FakeCameraDriver", _SizedFakeCam)
    monkeypatch.setattr(_se, "locate_screen_quad",
                        lambda decoded, expected, **kw: {"corners": expected})
    monkeypatch.setattr(_sp, "fit_pose",
                        lambda saved, live, **kw: {
                            "matrix": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                            "corners": list(saved), "movementPx": 0.0,
                            "fitErrorPx": 0.0})


    async def run():
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            enable_program_switches(tmp_path)
            program = swipe_program()
            from drivers import calibration_bundle as cb
            from tests.test_calibrated_scroll import attach_simulated_source_identity
            attach_simulated_source_identity(
                runtime, cb.load_bundle("arm-01", tmp_path / "data"))
            driver_calls = install_program_driver(
                runtime, result={"ok": True, "motion_verified": True,
                                 "controller_status": "completed",
                                 "plan_id": "plan-swipe-9",
                                 "strokes_completed": 1})
            # 位移测量替身：真实光流对 fake 静态帧不可控，按接口契约打桩
            from drivers import scroll_observation as so
            measure_calls: list[dict] = []
            def fake_measure(before_gray, after_gray, roi, *args, **kwargs):
                measure_calls.append(deepcopy(roi))
                return {"status": "displaced", "dx_px": 0.0, "dy_px": -320.5,
                        "inlier_ratio": 0.92, "valid_points": 40,
                        "matched_ocr_rows": 3, "mad_dy_px": 1.0,
                        "reason_code": None,
                        "gesture_rules_version": "gesture-rules.v1"}

            monkeypatch.setattr(so, "measure_displacement", fake_measure)
            done = await drive(runtime, program_body(effect_kind="scroll_displacement",
                                                     program=program))
            assert done["status"] == "confirmed", done.get("failure_reason")
            assert len(driver_calls) == 1
            # runtime 自动规划：swipe 模式委托给 driver.swipe()（SWIPE 宏通道）
            swipe_call = driver_calls[0]
            assert "start" in swipe_call and "end" in swipe_call
            assert swipe_call["start"]["source"] == "screen_normalized"

            detail = last_detail(done)
            from drivers.gesture_program import program_hash
            assert detail["program_hash"] == program_hash(program)
            assert detail["plan_id"] == "plan-swipe-9"
            assert detail["binding"]["device_id"] == "arm-01"
            assert detail["controller_status"] == "completed"
            assert detail["effect_status"] == "verified"
            assert detail["reason_code"] is None
            assert detail["retry_policy"] == "business_next_step"
            assert detail["manual_review_required"] is False
            assert len(detail["stroke_results"]) == 1
            assert detail["stroke_results"][0] == {
                "index": 0, "controller_status": "completed",
                "contact_status": "not_observed", "effect_status": "verified",
                "reason_code": None, "evidence_refs": []}
            # 前后两帧位移测量证据
            assert len(detail["evidence_refs"]) == 2
            assert detail["evidence_refs"][0]["kind"] == "image"
            assert detail["evidence_refs"][0]["evidence_id"] != \
                detail["evidence_refs"][1]["evidence_id"]
            assert len(measure_calls) == 1
            # P1-3：测量 ROI 必须落在屏幕域（程序坐标域 × 屏幕尺寸 1080×2320），
            # 不得再乘相机帧尺寸（binding.frame_*）混用坐标平面
            roi = measure_calls[0]
            assert 0 <= roi["x"] and roi["x"] + roi["width"] <= 1080
            assert 0 <= roi["y"] and roi["y"] + roi["height"] <= 2320
            assert roi["y"] <= 0.30 * 2320 + 2      # 覆盖滑动走廊（y 0.30→0.75）
            assert roi["y"] + roi["height"] >= 0.75 * 2320 - 2
            assert abs(roi["x"] + roi["width"] / 2 - 0.5 * 1080) < 2
        finally:
            await runtime.shutdown()

    asyncio.run(run())


def test_program_detail_has_exact_contract_fields(tmp_path, monkeypatch):
    module, manifest = program_fixture(tmp_path, monkeypatch)
    _seed_calibration(tmp_path)

    async def run():
        runtime = make_runtime(tmp_path, plugin=module.Plugin(), manifest=manifest)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            enable_program_switches(tmp_path)
            install_program_driver(runtime)
            done = await drive(runtime, program_body(effect_kind="manual_only"))
            detail = last_detail(done)
            assert set(detail) == {
                "contract_version", "program_hash", "plan_id", "binding",
                "stroke_results", "controller_status", "contact_status",
                "effect_status", "reason_code", "message", "retry_policy",
                "evidence_refs", "manual_review_required"}
        finally:
            await runtime.shutdown()

    asyncio.run(run())


# =====================================================================
# 交付 3：wx-wechat-adapter wx_auto_navigate
# =====================================================================

NAVIGATION_ID = "f2f2e08c-3c43-4dca-86b3-6bb46d16c001"


def list_observation() -> dict:
    """微信主界面全页 OCR 框（校准平面像素，wechat-list-layout.v1 正向规则）。"""
    return {
        "frameW": 1280, "frameH": 720,
        "ocr_boxes": [
            {"text": "微信", "x": 600, "y": 40, "w": 80, "h": 40, "confidence": 0.97},
            {"text": "微信", "x": 100, "y": 650, "w": 60, "h": 30, "confidence": 0.96},
            {"text": "通讯录", "x": 400, "y": 650, "w": 90, "h": 30, "confidence": 0.96},
            {"text": "发现", "x": 700, "y": 650, "w": 60, "h": 30, "confidence": 0.96},
            {"text": "我", "x": 1020, "y": 650, "w": 40, "h": 30, "confidence": 0.96},
        ],
    }


def auto_nav_payload(action="search_next", *, navigation_id=NAVIGATION_ID,
                     step_index=1, target=None, session_ref=None) -> dict:
    payload = {
        "navigation_id": navigation_id,
        "binding": full_binding(profile_version=1),
        "navigation_profile": {"profile_id": "wx_chat_list_auto", "version": 1,
                               "contract_version": "automatic-gesture-profile.v1"},
        "action": action, "step_index": step_index,
    }
    if target is not None:
        payload["target"] = target
    if session_ref is not None:
        payload["session_ref"] = session_ref
    return payload


class AutoNavContext:
    """wx_auto_navigate 单步替身上下文（只实现插件用到的 StepContext 面）。"""

    def __init__(self, payload, *, locate=None, measure=None, confirm=None,
                 held=None, execute=None, observe=None):
        self.task = {"task_id": "nav-task-1", "task_type": "wx_auto_navigate",
                     "contract_version": "wx-auto-navigation.v1",
                     "device_id": "arm-01", "payload": payload}
        self.device_id = "arm-01"
        self.device_context = {"camera_id": "cam-01", "binding_mode": "real",
                               "daemon_session": "sess-A",
                               "wechat_account_id": "sim-acc-01"}
        self._state = {"manual_session": held}
        self.calls: list[tuple] = []
        self._locate = locate
        self._observe = observe or list_observation()
        self._measure = measure if measure is not None else {
            "status": "displaced", "dx_px": 0.0, "dy_px": -320.5,
            "inlier_ratio": 0.9, "valid_points": 30, "matched_ocr_rows": 3,
            "mad_dy_px": 1.2, "reason_code": None,
            "gesture_rules_version": "gesture-rules.v1"}
        self._confirm = confirm if confirm is not None else {
            "confirmed": True, "ui_state": "wechat_main"}
        self._execute = execute if execute is not None else {
            "ok": True, "motion_verified": True, "controller_status": "completed",
            "plan_id": "plan-nav-1", "strokes_completed": 1}

    async def call_capability(self, capability_id, params=None):
        import copy
        params = copy.deepcopy(params or {})
        self.calls.append((capability_id, params))
        if capability_id == "device.automatic_gesture_profile":
            from drivers import calibration_bundle as cb, gesture_program as gp
            from runtime import automatic_gestures as ag
            assert params["op"] == "resolve_navigation"
            return ag.resolve_navigation_action(
                self.device_id, params["navigation_profile"], params["binding"], params["action"],
                bundle=cb.load_bundle(self.device_id, self.data_root),
                frame=gp.load_coordinate_frame(self.device_id, self.data_root), data_root=self.data_root,
                evidence_root=self.evidence_root)
        if capability_id == "camera.observe_regions":
            return deepcopy(self._observe)
        if capability_id == "camera.locate_text":
            if self._locate is None:
                return {"found": False, "frameSeq": 9, "frameW": 1280, "frameH": 720}
            return deepcopy(self._locate)
        if capability_id == "camera.measure_scroll":
            if params.get("op") == "grab":
                grabs = sum(1 for name, p in self.calls
                            if name == "camera.measure_scroll"
                            and p.get("op") == "grab")
                return {"evidence_id": f"evd-grab-{grabs}",
                        "sha256": f"sha-grab-{grabs}",
                        "captured_at": "2026-09-15T10:00:00Z"}
            return deepcopy(self._measure)
        if capability_id == "arm.execute_gesture_program":
            if isinstance(self._execute, Exception):
                raise self._execute
            return deepcopy(self._execute)
        if capability_id == "camera.confirm_view":
            return deepcopy(self._confirm)
        raise AssertionError(f"未预期的能力调用：{capability_id}")

    async def capture_evidence(self, label):
        self.calls.append(("evidence", label))
        count = sum(1 for name, _ in self.calls if name == "evidence")
        return {"evidence_id": f"evd-{count}", "sha256": f"sha-{count}",
                "captured_at": "2026-09-15T10:00:00Z"}

    async def update_observed_ui_state(self, *args, **kwargs):
        return None

    def state_get(self, key):
        return self._state.get(key)

    def state_set(self, key, value):
        self._state[key] = value

    def check_action_budget(self):
        return 5

    def names(self):
        return [name for name, _ in self.calls]

    def action_calls(self):
        return [name for name, _ in self.calls if name.startswith("arm.")]


@pytest.fixture(autouse=True)
def navigation_context_root(tmp_path, monkeypatch):
    monkeypatch.setattr(AutoNavContext, "data_root", tmp_path / "data", raising=False)
    monkeypatch.setattr(AutoNavContext, "evidence_root", tmp_path / "evidence", raising=False)


def wx_fixture(tmp_path, monkeypatch):
    module = load_module("wx_adapter_auto_nav_under_test", WX_SOURCE)
    monkeypatch.setattr(module, "AUTO_NAV_DATA_ROOT", tmp_path / "data")
    seed_navigation(tmp_path)
    return module


def run_view(tmp_path, navigation_id=NAVIGATION_ID):
    from adapters.uinav import automatic_navigation as nav
    return nav.NavigationRunManager("arm-01", tmp_path / "data").run_view(navigation_id)


def test_auto_navigate_search_target_hit_does_not_move(tmp_path, monkeypatch):
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    ctx = AutoNavContext(
        auto_nav_payload("search_next",
                         target={"visible_name": "客户A",
                                 "external_conversation_id": "c1"}),
        locate={"found": True, "cameraX": 400, "cameraY": 300,
                "frameW": 1280, "frameH": 720, "confidence": 0.95, "frameSeq": 10})

    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "success", result
    detail = result["detail"]
    assert detail["target_found"] is True
    assert detail["controller_status"] == "not_started"
    assert detail["effect_status"] == "not_checked"
    assert detail["motion_evidence_kind"] == "none"
    assert detail["target_evidence"] is not None
    assert detail["contract_version"] == "wx-auto-navigation.v1"
    assert detail["navigation_id"] == NAVIGATION_ID
    assert detail["step_index"] == 1
    # 命中即观测步骤：不执行任何机械动作，也不做位移测量
    assert ctx.action_calls() == []
    assert "camera.measure_scroll" not in ctx.names()
    view = run_view(tmp_path)
    assert view["state"] == "completed"  # target_found 终态


def test_auto_navigate_search_miss_swipes_once_and_verifies_displacement(tmp_path, monkeypatch):
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    ctx = AutoNavContext(
        auto_nav_payload("search_next",
                         target={"visible_name": "客户A",
                                 "external_conversation_id": "c1"}),
        locate={"found": False, "frameSeq": 9, "frameW": 1280, "frameH": 720})

    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "success", result
    detail = result["detail"]
    assert detail["controller_status"] == "completed"
    assert detail["contact_status"] == "not_observed"
    assert detail["effect_status"] == "verified"
    assert detail["target_found"] is False
    assert detail["motion_evidence_kind"] == "list_displacement"
    assert detail["displacement"]["dy_px"] == -320.5
    assert detail["before_evidence"]["evidence_id"] != detail["after_evidence"]["evidence_id"]
    assert detail["before_evidence"]["kind"] == "image"
    assert detail["before_evidence"]["calibration_fingerprint"] == full_binding()["calibration_fingerprint"]
    assert detail["retry_policy"] == "business_next_step"
    assert detail["plan_id"] == "plan-nav-1"
    assert detail["manual_review_required"] is False
    # 单步单动作：一次程序执行、前后各一次位移帧
    assert ctx.action_calls() == ["arm.execute_gesture_program"]
    grabs = [p for name, p in ctx.calls
             if name == "camera.measure_scroll" and p.get("op") == "grab"]
    assert len(grabs) == 2
    program = ctx.calls[[name for name, _ in ctx.calls]
                        .index("arm.execute_gesture_program")][1]["program"]
    assert program["program_version"] == "gesture-program.v1"
    assert len(program["strokes"]) == 1
    # search 步骤不 close：WX 轮次跨任务（§22.14）
    view = run_view(tmp_path)
    assert view["state"] == "active"
    assert view["physical_count"] == 1


def test_auto_navigate_unknown_displacement_is_unknown(tmp_path, monkeypatch):
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    ctx = AutoNavContext(auto_nav_payload("search_next"), measure={
        "status": "unknown", "dx_px": None, "dy_px": None, "inlier_ratio": 0.0,
        "valid_points": 0, "matched_ocr_rows": 0, "mad_dy_px": None,
        "reason_code": "tracking_failed",
        "gesture_rules_version": "gesture-rules.v1"})

    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "unknown"
    detail = result["detail"]
    assert detail["effect_status"] == "unknown"
    assert detail["reason_code"] == "tracking_failed"
    assert detail["manual_review_required"] is True
    assert ctx.action_calls() == ["arm.execute_gesture_program"]
    assert run_view(tmp_path)["state"] == "unknown"


def test_auto_navigate_stationary_search_is_failed_no_effect(tmp_path, monkeypatch):
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    ctx = AutoNavContext(auto_nav_payload("search_next"), measure={
        "status": "stationary", "dx_px": 0.0, "dy_px": -1.0, "inlier_ratio": 0.95,
        "valid_points": 30, "matched_ocr_rows": 3, "mad_dy_px": 0.5,
        "reason_code": None, "gesture_rules_version": "gesture-rules.v1"})

    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "failed"
    assert result["error_category"] == "effect_not_observed"
    assert result["detail"]["effect_status"] == "no_effect"


def test_auto_navigate_return_top_boundary_probe_pending_is_success(tmp_path, monkeypatch):
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    seed_navigation_template(tmp_path)
    monkeypatch.setattr(plugin, "_auto_nav_verify_template", unmatched_template)
    ctx = AutoNavContext(auto_nav_payload("return_top"), measure={
        "status": "stationary", "dx_px": 0.0, "dy_px": -0.5, "inlier_ratio": 0.95,
        "valid_points": 30, "matched_ocr_rows": 3, "mad_dy_px": 0.4,
        "reason_code": None, "gesture_rules_version": "gesture-rules.v1"})

    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "success", result  # §22.14 例外：探测步骤完成
    detail = result["detail"]
    assert detail["reason_code"] == "boundary_probe_pending"
    assert detail["retry_policy"] == "business_boundary_probe"
    assert detail["top_verified"] is False
    assert detail["effect_status"] == "no_effect"
    view = run_view(tmp_path)
    assert view["state"] == "active"
    assert view["boundary_probe_count"] == 1


def test_auto_navigate_return_top_wrong_direction_is_not_success(tmp_path, monkeypatch):
    """return_top must verify downward screen displacement before probing."""
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    seed_navigation_template(tmp_path)
    monkeypatch.setattr(plugin, "_auto_nav_verify_template", unmatched_template)
    ctx = AutoNavContext(auto_nav_payload("return_top"), measure={
        "status": "displaced", "dx_px": 0.0, "dy_px": -320.5,
        "inlier_ratio": 0.95, "valid_points": 30, "matched_ocr_rows": 3,
        "mad_dy_px": 0.5, "reason_code": None,
        "gesture_rules_version": "gesture-rules.v1"})

    result = asyncio.run(plugin._step_auto_navigate(ctx))

    assert result["outcome"] == "failed", result
    assert result["error_category"] == "direction_mismatch"
    assert result["detail"]["reason_code"] == "direction_mismatch"
    assert result["detail"]["manual_review_required"] is True
    view = run_view(tmp_path)
    assert view["state"] == "unknown"
    assert view["state_reason_code"] == "direction_mismatch"
    assert view["steps"][-1]["result_ref"]["plugin_outcome"] == "failed"
    next_ctx = AutoNavContext(auto_nav_payload("return_top", step_index=2))
    next_result = asyncio.run(plugin._step_auto_navigate(next_ctx))
    assert next_result["outcome"] == "failed"
    assert next_ctx.action_calls() == []


def test_auto_navigate_return_top_matching_direction_can_continue(tmp_path, monkeypatch):
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    seed_navigation_template(tmp_path)
    monkeypatch.setattr(plugin, "_auto_nav_verify_template", unmatched_template)
    ctx = AutoNavContext(auto_nav_payload("return_top"), measure={
        "status": "displaced", "dx_px": 0.0, "dy_px": 320.5,
        "inlier_ratio": 0.95, "matched_ocr_rows": 3})

    result = asyncio.run(plugin._step_auto_navigate(ctx))

    assert result["outcome"] == "success", result
    assert result["detail"]["effect_status"] == "verified"
    assert result["detail"]["top_verified"] is False
    assert run_view(tmp_path)["state"] == "active"


@pytest.mark.parametrize("dy_px", [None, 0.0, float("nan")])
def test_auto_navigate_displaced_without_valid_direction_needs_review(
        tmp_path, monkeypatch, dy_px):
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    ctx = AutoNavContext(auto_nav_payload("search_next"), measure={
        "status": "displaced", "dx_px": 0.0, "dy_px": dy_px,
        "inlier_ratio": 0.95, "matched_ocr_rows": 3})

    result = asyncio.run(plugin._step_auto_navigate(ctx))

    assert result["outcome"] == "unknown", result
    assert result["detail"]["effect_status"] == "unknown"
    assert result["detail"]["manual_review_required"] is True
    expected_dy = None if dy_px is None or dy_px != dy_px else dy_px
    assert result["detail"]["displacement"]["dy_px"] == expected_dy
    json.dumps(result, allow_nan=False)
    assert run_view(tmp_path)["state"] == "unknown"


def test_legacy_camera_verified_scroll_checks_direction(tmp_path, monkeypatch):
    module = wx_fixture(tmp_path, monkeypatch)
    ctx = AutoNavContext(auto_nav_payload("return_top"), measure={
        "status": "displaced", "dx_px": 0.0, "dy_px": -100.0})
    plan = {"roi": {"x": 0.1, "y": 0.2, "width": 0.8, "height": 0.6},
            "roi_frame": "screen_norm",
            "program": navigation_program("scroll_down")}

    result = asyncio.run(module._camera_verified_scroll(
        ctx, {}, action="top", auto_plan=plan, returning_top=True))

    assert result["outcome"] == "failed"
    assert result["error_category"] == "direction_mismatch"
    assert result["detail"]["manual_review_required"] is True


def test_auto_navigate_back_to_list_without_session_is_rejected(tmp_path, monkeypatch):
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    ctx = AutoNavContext(
        auto_nav_payload("back_to_list",
                         session_ref={"session_id": "s1", "control_mode": "manual",
                                      "external_conversation_id": "c1"}))

    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "failed"
    assert result["error_category"] == "conversation_mismatch"
    assert "会话" in result["message"]
    assert ctx.action_calls() == [], "会话不匹配必须零动作"
    assert run_view(tmp_path) is None, "前置失败不得创建导航轮"


def test_auto_navigate_back_to_list_matching_session_confirms_and_closes(tmp_path, monkeypatch):
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    held = {"session_id": "s1", "control_mode": "manual",
            "conversation_id": "c1", "wechat_account_id": "sim-acc-01"}
    ctx = AutoNavContext(
        auto_nav_payload("back_to_list",
                         session_ref={"session_id": "s1", "control_mode": "manual",
                                      "external_conversation_id": "c1"}),
        held=held)

    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "success", result
    detail = result["detail"]
    assert detail["effect_status"] == "verified"
    assert detail["motion_evidence_kind"] == "page_transition"
    assert ctx.action_calls() == ["arm.execute_gesture_program"]
    # back_to_list 是单任务终态动作：任务完成即 close(caller_finished)
    view = run_view(tmp_path)
    assert view["state"] == "completed"


def test_auto_navigate_holds_session_rejects_other_actions(tmp_path, monkeypatch):
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    ctx = AutoNavContext(auto_nav_payload("search_next"),
                         held={"session_id": "s9", "control_mode": "manual",
                               "conversation_id": "c9"})
    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "failed"
    assert result["error_category"] == "conversation_mismatch"
    assert ctx.action_calls() == []


def test_auto_navigate_observation_without_text_boxes_rejected(tmp_path, monkeypatch):
    """observe_regions 不提供文字框：布局不受支持，零动作（§22.8/§9.1）。"""
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    ctx = AutoNavContext(auto_nav_payload("search_next"),
                         observe={"frameW": 1280, "frameH": 720,
                                  "components": []})
    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "failed"
    assert result["error_category"] == "navigation_layout_unsupported"
    assert ctx.action_calls() == []
    assert run_view(tmp_path) is None, "布局失败发生在轮次登记之前"


def test_auto_navigate_home_without_eligibility_is_rejected(tmp_path, monkeypatch):
    """home：缺少自动导航资格/桌面模板时不做无法验证的运动（§22.8）。"""
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()
    ctx = AutoNavContext(auto_nav_payload("home"))
    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "failed"
    assert result["error_category"] == "device_not_eligible"
    assert ctx.action_calls() == []


def test_wx_plugin_manifest_declares_auto_navigate_and_capabilities():
    manifest = load_manifest(WX_SOURCE)
    assert manifest["version"] == "1.8.0"
    entry = next(item for item in manifest["task_types"]
                 if item["task_type"] == "wx_auto_navigate")
    assert entry["capability"] == "wechat.auto_navigate"
    assert entry["context_fields"] == ["wechat_account_id"]
    assert manifest["limits"]["max_actions_per_task"] == 200
    for capability in ("arm.execute_gesture_program", "camera.measure_scroll",
                       "device.automatic_gesture_profile"):
        assert capability in manifest["capabilities"]
    # 旧任务类型保持登记
    assert {item["task_type"] for item in manifest["task_types"]} >= {
        "wx_list_navigate", "chat_send", "wx_session_open", "wx_session_send",
        "wx_session_exit", "wx_session_release", "capture_message"}


def test_old_wx_list_navigate_task_still_confirmed(tmp_path):
    """旧任务零回归：真实插件目录安装后提交 wx_list_navigate 仍确认。"""
    from tests.test_stage_f_wx import _wait_terminal
    from tests.test_wx_list_navigation import navigation_body
    from tests.test_wx_navigation_integration import configured_fleet, profile_reference

    async def run():
        runtime, _wx, client, _page, _entry = await configured_fleet(tmp_path)
        camera = runtime.devices.camera("arm-01")
        original_locate = camera.locate_text
        frame = {"seq": 0}

        async def locate(text, **kwargs):
            frame["seq"] += 1
            return {**await original_locate(text, **kwargs),
                    "frameSeq": frame["seq"], "frameW": 1280, "frameH": 720}

        camera.locate_text = locate
        try:
            ref = await profile_reference(client)
            body = navigation_body("next", navigation_profile=ref)
            task = runtime.submit_task(body, source="wx",
                                       context={"operator_id": "wx", "reason": "旧任务回归"})
            done = await _wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed", (
                done.get("failure_reason"), done.get("step_log"))
            swipes = [row for row in runtime.devices.driver("arm-01").action_log
                      if row["action"] == "swipe"]
            assert len(swipes) == 1
        finally:
            await client.aclose()
            await runtime.shutdown()

    asyncio.run(run())


def test_execute_failure_stops_grab_chain(tmp_path, monkeypatch):
    """P1-12：执行异常的退出路径也必须停止帧链任务（不再每 0.12s 截帧落盘）。"""
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()

    async def run():
        ctx = AutoNavContext(
            auto_nav_payload("search_next"),
            execute=RuntimeError("executor 已取消任务"))
        result = await plugin._step_auto_navigate(ctx)

        def grab_count():
            return sum(1 for name, p in ctx.calls
                       if name == "camera.measure_scroll" and p.get("op") == "grab")
        after_step = grab_count()
        await asyncio.sleep(module.AUTO_NAV_GRAB_INTERVAL_S * 4)
        return result, after_step, grab_count()

    result, after_step, later = asyncio.run(run())
    assert result["outcome"] in ("failed", "unknown")
    assert after_step > 0, "列表动作执行前应有帧链采样"
    assert later == after_step, "执行失败返回后帧链任务必须已停止"


def test_search_step_chain_evidence_reaches_measure(tmp_path, monkeypatch):
    """P1-N0：帧链中途采集的证据 id 必须进入 measure 调用。

    gather(单任务) 返回外层列表套结果列表，对它做 isinstance(str) 过滤
    恒为空——帧链证据全部静默丢弃，测量退化为首末两帧。
    """
    module = wx_fixture(tmp_path, monkeypatch)
    plugin = module.Plugin()

    class YieldingContext(AutoNavContext):
        """替身执行无耗时会让帧链来不及采样：注入真机级执行时长。"""

        async def call_capability(self, capability_id, params=None):
            if capability_id == "arm.execute_gesture_program":
                await asyncio.sleep(0.3)
            return await super().call_capability(capability_id, params)

    ctx = YieldingContext(auto_nav_payload("search_next"))
    result = asyncio.run(plugin._step_auto_navigate(ctx))
    assert result["outcome"] == "success", result
    measure_calls = [params for name, params in ctx.calls
                     if name == "camera.measure_scroll"
                     and params.get("op") == "measure"]
    assert len(measure_calls) == 1
    evidence_ids = list(measure_calls[0]["evidence_ids"])
    grabs = [e for e in evidence_ids if str(e).startswith("evd-grab-")]
    assert len(evidence_ids) >= 3, f"应含首帧+帧链+末帧：{evidence_ids}"
    assert len(grabs) >= 3, f"中途帧链证据不得丢弃：{evidence_ids}"
