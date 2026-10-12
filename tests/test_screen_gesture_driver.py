"""Production gestures use temporary calibration and a simulated daemon."""
import asyncio
from copy import deepcopy

import pytest

from drivers import calibration_bundle as cb, screen_gestures as sg
from drivers.screen_pose import fit_pose
from drivers.arm import DriverError
from tests.test_daemon_arm import CALIB_SESSION, FakeDaemon, make_driver
from tests.test_ui_targets import ready_bundle


def gesture_bundle():
    bundle = ready_bundle(session=CALIB_SESSION)
    bundle["screen"]["screenWidth"] = 1080
    bundle["screen"]["screenHeight"] = 2320
    bundle["screen"]["edgeModel"]["corners"] = [[100, 100], [1200, 100], [1200, 600], [100, 600]]
    bundle["camera"]["cameraToScreen"] = {
        "homography": [[0, 2.16, -216], [-2320 / 1100, 0, 2320 * 1200 / 1100], [0, 0, 1]],
        "maxErrorPx": 1.66,
    }
    bundle["xy"]["robotToScreen"] = {"a": -15.5, "b": 0, "c": 425,
                                      "d": 0, "e": 15.5, "f": 752}
    bundle["z"].update(hoverZ=-8, zSafe=-8, tapDepth=8)
    return bundle


def point(x=.5, y=.5):
    return {"source": "screen_normalized", "x": x, "y": y}


@pytest.fixture()
def rig(tmp_path):
    bundle = gesture_bundle()
    cb.save_bundle(bundle, tmp_path)
    daemon = FakeDaemon(mpos=(-52, 49, -8)).start()
    daemon.motion_features += ["tap2.short-lift.v1"]
    driver = make_driver(daemon, tmp_path)
    asyncio.run(driver.connect())
    try:
        yield daemon, driver, bundle, tmp_path
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()


def test_arbitrary_double_tap_same_contact_depth_and_calibration(rig):
    daemon, driver, bundle, root = rig
    before = cb.bundle_path("arm-01", root).read_bytes()
    result = asyncio.run(driver.double_tap({"point": point(), "binding": sg.binding_snapshot(bundle)}))
    assert daemon.commands[-3:] == [
        "Z 5.000 F1500", "TAP2 3.000 F1500 I0.05", "Z -5.000 F1500"]
    assert result["tap_count"] == 2
    assert result["repeat_lift_mm"] == 3
    assert result["contact_interval_min_ms"] == 290
    assert result["double_tap_feed"] == 1500
    assert result["speed_note"]
    assert result["effect_status"] == "unverified"
    assert daemon.mpos[2] == -8
    assert cb.bundle_path("arm-01", root).read_bytes() == before


def test_bottom_swipe_uses_physical_screen_not_camera_ui_inset(rig):
    daemon, driver, bundle, root = rig
    before = cb.bundle_path("arm-01", root).read_bytes()
    result = asyncio.run(driver.swipe({
        "start": point(.5, .99), "end": point(.5, .8),
        "durationMs": 650, "binding": sg.binding_snapshot(bundle)}))
    assert result["swipe_executed"] is True
    assert daemon.commands[-1].startswith("SWIPE 0.000 -28.439 8.000 ")
    assert daemon.mpos[2] == -8
    assert cb.bundle_path("arm-01", root).read_bytes() == before
    assert bundle["screen"]["edgeModel"]["safeInsetPx"] == 24


def test_long_swipe_chooses_safe_duration_when_unspecified(rig):
    daemon, driver, bundle, _ = rig
    result = asyncio.run(driver.swipe({
        "start": point(.5, .98), "end": point(.5, .02),
        "binding": sg.binding_snapshot(bundle)}))
    assert 2800 <= result["duration_ms"] <= 5000
    assert result["swipe_executed"]


def test_swipe_uses_live_phone_pose_for_both_contact_points(rig):
    daemon, driver, bundle, _ = rig
    corners = bundle["screen"]["edgeModel"]["corners"]
    pose = fit_pose(corners, [[x + 5, y + 3] for x, y in corners])

    async def gate(**_):
        return {"checks": [], "pose": pose}

    driver.live_calibration_gate = gate
    start = sg.resolve_point(point(.5, .75), bundle)
    end = sg.resolve_point(point(.5, .3), bundle)
    result = asyncio.run(driver.swipe({
        "start": point(.5, .75), "end": point(.5, .3),
        "binding": sg.binding_snapshot(bundle)}))
    assert result["swipe_executed"]
    assert result["resolved_start_robot_xy"] != [start["robotX"], start["robotY"]]
    assert result["resolved_end_robot_xy"] != [end["robotX"], end["robotY"]]
    assert daemon.commands[-1].startswith("SWIPE ")


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_phone_moves_after_travel_never_touches_screen(rig, action):
    daemon, driver, bundle, _ = rig
    corners = bundle["screen"]["edgeModel"]["corners"]
    baseline = fit_pose(corners, [[x + 5, y + 3] for x, y in corners])
    moved = fit_pose(corners, [[x + 10, y + 3] for x, y in corners])
    gate_calls = 0

    async def gate(**_):
        nonlocal gate_calls
        gate_calls += 1
        travelled = any(command.startswith("XY ") for command in daemon.commands)
        return {"checks": [], "pose": moved if travelled else baseline}

    driver.live_calibration_gate = gate
    request = ({"point": point(), "binding": sg.binding_snapshot(bundle)}
               if action == "double_tap" else {
                   "start": point(.5, .75), "end": point(.5, .3),
                   "binding": sg.binding_snapshot(bundle)})
    with pytest.raises(DriverError, match="再次移动"):
        asyncio.run(getattr(driver, action)(request))
    assert gate_calls >= 2
    assert any(command.startswith("XY ") for command in daemon.commands)
    assert not any(command.startswith(("TAP", "SWIPE", "PATH1"))
                   for command in daemon.commands)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_phone_pose_remains_stable_at_contact(rig, action):
    daemon, driver, bundle, _ = rig
    corners = bundle["screen"]["edgeModel"]["corners"]
    pose = fit_pose(corners, [[x + 5, y + 3] for x, y in corners])
    gate_calls = 0

    async def gate(**_):
        nonlocal gate_calls
        gate_calls += 1
        return {"checks": [], "pose": pose}

    driver.live_calibration_gate = gate
    request = ({"point": point(), "binding": sg.binding_snapshot(bundle)}
               if action == "double_tap" else {
                   "start": point(.5, .75), "end": point(.5, .3),
                   "binding": sg.binding_snapshot(bundle)})
    result = asyncio.run(getattr(driver, action)(request))
    assert result["controller_completed"]
    assert gate_calls >= 2
    assert any(command.startswith(("TAP2 ", "SWIPE ")) for command in daemon.commands)


@pytest.mark.parametrize("action", ["double_tap", "swipe"])
@pytest.mark.parametrize("change", ["draft", "session", "z", "edge", "camera", "binding", "override"])
def test_stale_binding_or_override_never_moves(rig, action, change):
    daemon, driver, bundle, root = rig
    request = ({"point": point()} if action == "double_tap" else
               {"start": point(), "end": point(.5, .7)})
    request["binding"] = sg.binding_snapshot(bundle)
    changed = deepcopy(bundle)
    if change == "draft":
        changed["status"] = "draft"
    elif change == "session":
        daemon.session = "replacement"
    elif change == "z":
        changed["z"]["tapDepth"] = 7
    elif change == "edge":
        changed["screen"]["edgeModel"]["corners"][0][0] += 1
    elif change == "camera":
        changed["cameraDeviceUniqueId"] = "different-camera"
    elif change == "binding":
        request["binding"]["binding_revision"] = True
    else:
        request["tapDepth"] = 9
    cb.save_bundle(changed, root)
    with pytest.raises(DriverError):
        asyncio.run(getattr(driver, action)(request))
    assert daemon.commands == []


@pytest.mark.parametrize("p", [point(0, .5), point(.5, 1), point(True, .5),
                              point(float("nan"), .5), point(1.1, .5)])
def test_invalid_or_physical_edge_point_is_rejected(rig, p):
    daemon, driver, bundle, _ = rig
    with pytest.raises(DriverError):
        asyncio.run(driver.double_tap({"point": p, "binding": sg.binding_snapshot(bundle)}))
    assert not daemon.commands


def test_double_tap_feature_missing_rejected_before_travel(rig):
    daemon, driver, bundle, _ = rig
    daemon.motion_features = ["tap2.short-lift.v1", "swipe.v1"]
    with pytest.raises(DriverError):
        asyncio.run(driver.double_tap({"point": point(), "binding": sg.binding_snapshot(bundle)}))
    assert not daemon.commands

@pytest.mark.parametrize("action", ["double_tap", "swipe"])
def test_frame_change_only_cannot_authorize_arbitrary_gesture(rig, action):
    from types import SimpleNamespace

    daemon, driver, bundle, _ = rig
    driver.motion_verifier = SimpleNamespace(measurement_kind="frame_change")
    request = ({"point": point()} if action == "double_tap" else
               {"start": point(), "end": point(.5, .7)})
    request["binding"] = sg.binding_snapshot(bundle)
    with pytest.raises(DriverError, match="画面变化"):
        asyncio.run(getattr(driver, action)(request))
    assert daemon.commands == []


def test_feature_query_uses_separate_readonly_connection_without_mpos(rig):
    daemon, driver, _, _ = rig
    before = len(daemon.requests)
    state = asyncio.run(driver.read_gesture_state())
    assert state["connected"]
    assert "tap2.short-lift.v1" in state["motion_features"]
    assert [cmd for _, cmd in daemon.requests[before:]] == ["STATUS"]
    assert not daemon.commands
    assert driver.connected


def test_calibration_changed_during_preflight_is_not_used(rig):
    daemon, driver, bundle, root = rig

    async def gate(**_):
        changed = deepcopy(bundle)
        changed["xy"]["robotToScreen"]["c"] += 15
        cb.save_bundle(changed, root)
        return []

    driver.live_calibration_gate = gate
    with pytest.raises(DriverError):
        asyncio.run(driver.double_tap({"point": point(), "binding": sg.binding_snapshot(bundle)}))
    assert not daemon.commands


def test_failed_travel_cannot_report_swipe_executed(rig):
    _, driver, bundle, _ = rig

    class Verifier:
        async def before(self, _):
            pass

        async def after(self, _):
            return {"verified": False}

    driver.motion_verifier = Verifier()
    result = asyncio.run(driver.swipe({
        "start": point(), "end": point(.5, .7), "binding": sg.binding_snapshot(bundle)}))
    assert result["swipe_executed"] is False
    assert result["outcome_hint"] == "unknown"


def test_calibration_change_after_travel_never_presses(rig):
    daemon, driver, bundle, root = rig

    class Verifier:
        async def before(self, label):
            if label == "double_tap":
                changed = deepcopy(bundle)
                changed["z"]["tapDepth"] = 9
                cb.save_bundle(changed, root)

        async def after(self, _):
            return {"verified": True}

    driver.motion_verifier = Verifier()
    with pytest.raises(DriverError) as error:
        asyncio.run(driver.double_tap({"point": point(), "binding": sg.binding_snapshot(bundle)}))
    assert error.value.category == "driver_disconnected"
    assert "结果未知" in error.value.message
    assert not any(cmd.startswith("TAP") for cmd in daemon.commands)


def test_lost_short_lift_reply_never_replays_or_adds_lift(rig):
    daemon, driver, bundle, _ = rig
    daemon.response_hook = lambda cmd, reply, _: None if cmd.startswith("TAP2 ") else reply
    with pytest.raises(DriverError) as error:
        asyncio.run(driver.double_tap({"point": point(), "binding": sg.binding_snapshot(bundle)}))
    assert error.value.category == "driver_disconnected"
    assert len([cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")]) == 1
    assert daemon.commands[-1].startswith("TAP2 ")

@pytest.mark.parametrize("failed_command", ["END_MOTION", "RELEASE"])
def test_cleanup_rejection_after_motion_remains_unknown(rig, failed_command):
    daemon, driver, bundle, _ = rig
    daemon.response_hook = lambda cmd, reply, _: (
        "ERROR: cleanup rejected" if cmd == failed_command else reply)
    with pytest.raises(DriverError) as error:
        asyncio.run(driver.double_tap({"point": point(), "binding": sg.binding_snapshot(bundle)}))
    assert error.value.category == "driver_disconnected"
    assert "结果未知" in error.value.message
    assert len([cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")]) == 1


def test_calibrated_curve_cannot_leave_screen_between_valid_endpoints():
    bundle = gesture_bundle()
    bundle["screen"]["screenWidth"] = 1000
    bundle["screen"]["screenHeight"] = 1000
    bundle["screen"]["edgeModel"]["corners"] = [[0, 0], [1100, 0], [1100, 700], [0, 700]]
    bundle["camera"]["cameraToScreen"]["homography"] = [[1, 0, 0], [0, 2, 0], [0, 0, 1]]
    # Both endpoints map to y=75.5; the straight robot stroke reaches y=-12.5.
    bundle["xy"]["robotToScreen"] = {
        "a": 10, "b": 0, "c": 0, "d": -5.5, "e": 20, "f": 125,
        "qx2": 0, "qxy": 0, "qy2": 0, "px2": .055, "pxy": 0, "py2": 0}
    start = sg.resolve_point(point(.1, .0755), bundle)
    end = sg.resolve_point(point(.9, .0755), bundle)
    with pytest.raises(ValueError, match="屏幕"):
        sg.validate_swipe_path(start, end, bundle)


def test_g05_nonlinear_model_midpath_outside_rejects_whole_swipe(tmp_path):
    """G05：补偿后端点在手机内但非线性模型让直线中段越界 → 整条滑动拒绝。"""
    bundle = gesture_bundle()
    bundle["screen"]["screenWidth"] = 1000
    bundle["screen"]["screenHeight"] = 1000
    bundle["screen"]["edgeModel"]["corners"] = [[0, 0], [1100, 0], [1100, 700], [0, 700]]
    bundle["camera"]["cameraToScreen"]["homography"] = [[1, 0, 0], [0, 2, 0], [0, 0, 1]]
    # 二次项让 robot 直线在屏幕平面弓出手机边界：两端点 y=75.5（屏内），
    # 直线中段 y=-12.5（屏外，见 test_screen_gestures 的纯函数同源用例）。
    bundle["xy"]["robotToScreen"] = {
        "a": 10, "b": 0, "c": 0, "d": -5.5, "e": 20, "f": 125,
        "qx2": 0, "qxy": 0, "qy2": 0, "px2": .055, "pxy": 0, "py2": 0}
    cb.save_bundle(bundle, tmp_path)
    daemon = FakeDaemon(mpos=(-52, 49, -8)).start()
    daemon.motion_features += ["tap2.short-lift.v1"]
    driver = make_driver(daemon, tmp_path)
    asyncio.run(driver.connect())
    try:
        corners = bundle["screen"]["edgeModel"]["corners"]
        driver.live_calibration_gate = lambda **_: asyncio.sleep(0, {
            "checks": [], "pose": fit_pose(corners, corners)})

        with pytest.raises(DriverError, match="屏幕") as excinfo:
            asyncio.run(driver.swipe({
                "start": point(.1, .0755), "end": point(.9, .0755),
                "binding": sg.binding_snapshot(bundle)}))
        # 归一化路径校验把中段越界点回投为 [0,1] 外坐标拒绝；整条滑动
        # 未开始，且无任何机械指令发出。
        # 整条滑动拒绝：不发出任何 SWIPE/XY/TAP 指令。
        assert not any(command.startswith(("SWIPE", "XY ", "TAP", "PATH1"))
                      for command in daemon.commands)
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()
