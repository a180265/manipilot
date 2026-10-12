"""R4 评审修复的回归锚点测试（先失败后修复驱动）。

P0：direct-validate 调试端点必须不再存在——帧差兜底把"从未接触"翻成
"通过"并发布手势证书的旁路已按 9-23 清理决定删除（validate 在位；
auto-validate 端点 2026-09-30 随开关删除一并移除）。
P1-2：fixture 边界方向（上滑受阻 bottom / 下滑受阻 top）。效果判定按
OCR 优先拍板（2026-09-30）必须携带相机位移证据，trace-only 一律 unknown。
P2-7：WAIT_START/WAIT_END 会话调试打点残留清理。
P2-8：command expiry 必须把 trial 转 cancelled。
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "api" / "app.py"
GV = Path(__file__).resolve().parents[1] / "runtime" / "gesture_validation.py"


def test_p0_direct_validate_endpoint_removed():
    """P0：旁路端点删除后，源码不得再出现该路由或帧差翻案逻辑。"""
    source = APP.read_text(encoding="utf-8")
    assert "/automatic-gestures/direct-validate" not in source, \
        "direct-validate 旁路端点仍存在（R4 P0：可把未验证接触发证为可用）"
    assert "controller_completed_flag" not in source, \
        "帧差翻案判定残留（controller completed + 帧差 => passed 的旁路）"
    # 规范链路仍在位：validate 端点保留；auto-validate 端点已随
    # 「校准后自动测试手势」开关删除（2026-09-30 拍板），同样不得回归
    assert "/automatic-gestures/auto-validate" not in source
    assert "/automatic-gestures/validate" in source


def test_p1_2_fixture_boundary_direction():
    """上滑(direction=-1)受阻于 bottom；下滑(direction=1)受阻于 top（行为级）。

    写反的两组（-1&top / 1&bottom）恰好是能正常滚动的组合——写反时它们
    被误判 fixture_at_boundary 终止 run，而真正受阻的组合落 no_effect 空转。
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from runtime.gesture_validation import evaluate_scroll_trial

    DEFAULTS = {"scroll_self_test": {"pass_thresholds": {
        "endpoint_tolerance_min_px": 6.0, "endpoint_tolerance_factor": 0.0,
        "net_displacement_min_ratio": 0.3, "reverse_max_ratio": 0.2,
        "scroll_min_ratio": 0.05, "path_deviation_percentile": 0.95}}}
    BUNDLE = {"xy": {"maxResidualPx": 3.0},
              "screen": {"screenWidth": 1080, "screenHeight": 2400}}

    def run_trial(boundary_before, direction):
        # 上滑: y 0.7→0.3（归一化）；下滑: y 0.3→0.7
        p0 = (0.5, 0.7) if direction == -1 else (0.5, 0.3)
        p1 = (0.5, 0.3) if direction == -1 else (0.5, 0.7)
        root_w, root_h = 1080.0, 2400.0
        start_px = (p0[0] * root_w, p0[1] * root_h)
        end_px = (p1[0] * root_w, p1[1] * root_h)
        events = [{"xPx": start_px[0], "yPx": start_px[1], "tMs": 0, "action": "down",
                   "pointerId": 1},
                  {"xPx": (start_px[0] + end_px[0]) / 2,
                   "yPx": (start_px[1] + end_px[1]) / 2, "tMs": 50,
                   "pointerId": 1},
                  {"xPx": end_px[0], "yPx": end_px[1], "tMs": 100, "action": "up",
                   "pointerId": 1}]
        # scrollY 与手指 1:1 反号：上滑→scrollY 增大；下滑→减小
        before_y, after_y = (5000, 5800) if direction == -1 else (5800, 5000)
        row_h = 100
        trace = {"events": events, "endReason": "up", "droppedSampleCount": 0,
                 "scroll": {"boundaryBefore": boundary_before,
                            "resetCounterBefore": 3, "resetCounterAfter": 3,
                            "settled": True, "rowHeightPx": row_h,
                            "beforeY": before_y, "afterY": after_y,
                            "firstVisibleRowBefore": before_y // row_h,
                            "rowOffsetBeforePx": before_y % row_h,
                            "firstVisibleRowAfter": after_y // row_h,
                            "rowOffsetAfterPx": after_y % row_h,
                            "viewportHeightPx": 2000}}
        plan = {"program": {"strokes": [{"points": [
            {"x": p0[0], "y": p0[1]}, {"x": p1[0], "y": p1[1]}]}]}}
        # OCR 优先拍板：效果判定必须携带相机位移证据（与手指同向，
        # 上滑 dy<0 / 下滑 dy>0）；相机 status 不合法一律 unknown。
        camera_ok = {"status": "displaced", "dx_px": 0.0,
                     "dy_px": -40.0 if direction == -1 else 40.0,
                     "inlier_ratio": 0.9, "valid_points": 40}
        return evaluate_scroll_trial(
            controller_result={"controller_status": "completed"},
            trace=trace, plan=plan, bundle=BUNDLE, defaults=DEFAULTS,
            camera_result=camera_ok)

    up_bottom = run_trial("bottom", -1)
    down_top = run_trial("top", 1)
    up_top = run_trial("top", -1)
    down_bottom = run_trial("bottom", 1)
    assert up_bottom.get("reason_code") == "fixture_at_boundary", up_bottom
    assert down_top.get("reason_code") == "fixture_at_boundary", down_top
    assert up_top.get("reason_code") is None or up_top.get("reason_code") == "trial_passed", up_top
    assert down_bottom.get("reason_code") is None or down_bottom.get("reason_code") == "trial_passed", down_bottom


def test_p2_7_wait_debug_logging_removed():
    """WAIT_START/WAIT_END 调试打点（局部 import logging/time + 英文 warning）清理。"""
    source = GV.read_text(encoding="utf-8")
    assert "WAIT_START" not in source and "WAIT_END" not in source, \
        "会话调试打点残留（9-23 清理决定同类漏网）"


def test_p2_8_command_expiry_cancels_trial():
    """P2-8 锚点：命令到期把 trial 置 cancelled 的断言已在协议测试中强化
    （test_gesture_capture_protocol.py::TestProtocol::test_command_expiry_cancels_trial）。
    此处钉住协调器的取消语义源锚，防回归时找不到入口。"""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    source = (Path(__file__).resolve().parents[1]
              / "adapters" / "screen_ime" / "gesture_capture.py").read_text(encoding="utf-8")
    assert 'trial.state = "cancelled"' in source, "到期取消 trial 的语义必须保留"
    assert "test_command_expiry_cancels_trial" in (
        Path(__file__).resolve().parent / "test_gesture_capture_protocol.py"
    ).read_text(encoding="utf-8"), "强化的到期取消断言测试必须存在"


def test_p1_3_path1_completes_under_observation_mode(tmp_path):
    """P1-3：观察模式（verifier.observation_only=True）下 PATH1 必须完整执行。

    修前：execute_gesture_program 未传 allow_gesture_observation 且
    validate_gesture_replies=False → observe_only 恒 False → 首段
    verification 未验证即 break，controller 恒 unknown"禁止重放"。
    """
    import asyncio
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.test_daemon_arm import (FakeDaemon, make_driver, write_device_files,
                                       write_ready_bundle)
    from tests.test_daemon_arm import _path1_plan, _write_path1_bundle
    _write_path1_bundle(tmp_path)
    daemon = FakeDaemon().start()
    driver = make_driver(daemon, tmp_path)
    try:
        asyncio.run(driver.connect())

        class ObserveOnlyVerifier:
            observation_only = True
            calls = []

            async def before(self, label):
                self.calls.append(("before", label))

            async def after(self, label):
                self.calls.append(("after", label))
                return {"verified": False, "reason": "观察模式：探头已移除，仅记录"}

        driver.gesture_motion_verifier = ObserveOnlyVerifier()
        result = asyncio.run(driver.execute_gesture_program(_path1_plan(tmp_path)))
        assert result["controller_completed"] is True, result.get("message")
        assert result["controller_status"] == "completed"
        # 证据保留保守语义：观察模式不冒充已验证
        assert result["motion_verified"] is False
        assert result["effect_status"] == "unverified"
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()


def test_delete_test_action_has_motion_gate():
    """P1-2（第五轮复审）：delete-test（真实走位+两秒长按）是物理动作，
    必须过 motion 门——此前 motion=action=="test" 把它漏成 motion=False，
    平台 paused/quarantined/画面过期门全被跳过。"""
    server = Path(__file__).resolve().parents[1] / "scripts" / "motor_debug_server.py"
    source = server.read_text(encoding="utf-8")
    assert 'motion=action in ("test", "delete-test")' in source
    assert 'motion=action == "test"' not in source
