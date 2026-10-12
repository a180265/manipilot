"""Wake uses one verified calibration point, never client-supplied coordinates."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb, screen_gestures as sg, screen_pose
from drivers.arm import DriverError
from tests.test_screen_gesture_driver import rig, point


def validated_bundle(bundle):
    bundle = deepcopy(bundle)
    bundle["autoCalibration"] = {"runId": "wake-calibration", "state": "PROMOTED"}
    bundle["validation"] = {
        "passed": True, "runId": "wake-calibration", "tolerancePx": 10,
        "points": [
            {"targetId": "validation-1", "u": .3, "v": .3,
             "passed": True, "touchTargetErrorPx": 3},
            {"targetId": "validation-2", "u": .7, "v": .3,
             "passed": True, "touchTargetErrorPx": 4},
            {"targetId": "validation-3", "u": .3, "v": .7,
             "passed": True, "touchTargetErrorPx": 4},
            {"targetId": "validation-4", "u": .7, "v": .7,
             "passed": True, "touchTargetErrorPx": 4},
            {"targetId": "validation-5", "u": .5, "v": .65,
             "passed": True, "touchTargetErrorPx": 4},
        ],
    }
    bundle["modelLocks"] = {
        "cameraToScreen": {"state": cb.MODEL_LOCK_LOCKED, "version": 1},
        "armAffine": {"state": cb.MODEL_LOCK_LOCKED, "version": 1},
    }
    bundle["safePark"] = {
        "parkX": 0, "parkY": 0, "parkZ": bundle["z"]["zSafe"],
        "confirmed": True, "source": "test", "confirmedAt": "2026-09-29T00:00:00Z",
    }
    return bundle


def wake_frame(bundle, *, lit=False, sequence=1):
    shape = (bundle["frame"]["height"], bundle["frame"]["width"], 3)
    image = np.full(shape, 65, np.uint8)
    corners = np.asarray(bundle["screen"]["edgeModel"]["corners"], np.int32)
    cv2.fillConvexPoly(image, corners, (0, 0, 0))
    if lit:
        noise = np.random.default_rng(7).integers(90, 230, shape, dtype=np.uint8)
        mask = np.zeros(shape[:2], np.uint8)
        cv2.fillConvexPoly(mask, corners, 255)
        image[mask != 0] = noise[mask != 0]
    ok, encoded = cv2.imencode(".png", image)
    assert ok
    return {
        "image_bytes": encoded.tobytes(), "frozen": False,
        "source_frame_seq": sequence,
        "source_captured_at": datetime.now(timezone.utc).isoformat(),
        "source_received_at": datetime.now(timezone.utc).isoformat(),
        "camera_id": bundle["cameraId"], "device_id": bundle["deviceId"],
        "device_unique_id": bundle["cameraDeviceUniqueId"],
        "binding_revision": bundle["bindingRevision"],
        "width": shape[1], "height": shape[0],
        "_screen_pose": screen_pose.fit_pose(
            bundle["screen"]["edgeModel"]["corners"],
            bundle["screen"]["edgeModel"]["corners"]),
    }


def test_wake_quality_classifies_the_bound_phone_not_the_scene():
    from drivers.wake_screen import wake_frame_quality
    from tests.test_live_calibration_gate import ready_bundle

    bundle = ready_bundle()
    corners = bundle["screen"]["edgeModel"]["corners"]
    for lit, background, expected in ((False, 220, "waiting"), (True, 0, "clear")):
        frame = wake_frame(bundle, lit=lit)
        image = cv2.imdecode(np.frombuffer(frame["image_bytes"], np.uint8), cv2.IMREAD_COLOR)
        image[cv2.rectangle(
            np.zeros(image.shape[:2], np.uint8), (4, 4), (59, 43), 255, -1) == 0] = background
        ok, data = cv2.imencode(".png", image)
        assert ok
        frame["image_bytes"] = data.tobytes()
        assert wake_frame_quality(frame, sg.binding_snapshot(bundle),
                                  screen_corners=corners) == expected


def test_wake_quality_waits_on_all_black_frame_without_screen_edges():
    from drivers.wake_screen import wake_frame_quality
    from tests.test_live_calibration_gate import ready_bundle

    bundle = ready_bundle()
    frame = wake_frame(bundle)
    ok, data = cv2.imencode(".png", np.zeros((48, 64, 3), np.uint8))
    assert ok
    frame["image_bytes"] = data.tobytes()
    assert wake_frame_quality(
        frame, sg.binding_snapshot(bundle),
        screen_corners=bundle["screen"]["edgeModel"]["corners"]) == "waiting"


def test_wake_quality_requires_phone_geometry_after_wake():
    from drivers.wake_screen import WakeScreenNotLocated, wake_frame_quality
    from tests.test_live_calibration_gate import ready_bundle

    bundle = ready_bundle()
    frame = wake_frame(bundle, lit=True)
    ok, data = cv2.imencode(
        ".png", np.full((frame["height"], frame["width"], 3), 80, np.uint8))
    assert ok
    frame["image_bytes"] = data.tobytes()
    with pytest.raises(WakeScreenNotLocated):
        wake_frame_quality(
            frame, sg.binding_snapshot(bundle),
            screen_corners=bundle["screen"]["edgeModel"]["corners"],
            require_phone=True)


@pytest.mark.parametrize("fault", ["repeated_sequence", "moved_pose"])
def test_wake_observer_rejects_stale_sequence_or_changed_pose(fault):
    from drivers.wake_screen import WakeScreenObserver, resolve_wake_target
    from tests.test_screen_gesture_driver import gesture_bundle

    bundle = validated_bundle(gesture_bundle())
    count = 0

    async def gate(*, require_pose=False):
        nonlocal count
        count += 1
        frame = wake_frame(
            bundle, lit=require_pose,
            sequence=1 if fault == "repeated_sequence" else count)
        if count == 2 and fault == "moved_pose":
            saved = bundle["screen"]["edgeModel"]["corners"]
            moved = [[x + 3, y] for x, y in saved]
            frame["_screen_pose"] = screen_pose.fit_pose(saved, moved)
        return [], frame, None

    observer = WakeScreenObserver(gate, lambda: bundle, resolve_wake_target(bundle), bundle)
    observer.pressed = True

    async def scenario():
        await observer.sample()
        await observer.sample()

    with pytest.raises(DriverError, match="序号未推进|手机位置变化"):
        asyncio.run(scenario())
    assert len(observer.frames) == 1


def test_wake_observer_uses_calibrated_point_before_wake_and_live_pose_after():
    from drivers.wake_screen import WakeScreenObserver, resolve_wake_target
    from tests.test_screen_gesture_driver import gesture_bundle

    bundle = validated_bundle(gesture_bundle())
    calls = []

    async def gate(*, require_pose=False):
        calls.append(require_pose)
        frame = wake_frame(bundle, lit=require_pose, sequence=len(calls))
        if not require_pose:
            frame.pop("_screen_pose")
        return [], frame, None

    observer = WakeScreenObserver(gate, lambda: bundle, resolve_wake_target(bundle), bundle)

    async def scenario():
        await observer.sample(require_dark=True)
        assert observer.pose is None
        observer.pressed = True
        await observer.sample()

    asyncio.run(scenario())
    assert calls == [False, True]
    assert observer.pose is not None


def test_wake_observer_rejects_bright_post_wake_frame_without_live_pose():
    from drivers.wake_screen import WakeScreenObserver, resolve_wake_target
    from tests.test_screen_gesture_driver import gesture_bundle

    bundle = validated_bundle(gesture_bundle())

    async def gate(*, require_pose=False):
        frame = wake_frame(bundle, lit=True, sequence=1)
        frame.pop("_screen_pose")
        return [], frame, None

    observer = WakeScreenObserver(gate, lambda: bundle, resolve_wake_target(bundle), bundle)
    observer.pressed = True
    with pytest.raises(DriverError, match="实时位置不可确认"):
        asyncio.run(observer.sample())


def prepare_wake(rig, *, lit_after=True):
    daemon, driver, original, root = rig
    bundle = validated_bundle(original)
    cb.save_bundle(bundle, root)
    daemon.motion_features.append("tap2.feed-3000.v1")
    calls = []

    async def gate():
        calls.append(None)
        lit = lit_after and any(cmd.startswith("TAP2 ") for cmd in daemon.commands)
        return [], wake_frame(bundle, lit=lit, sequence=len(calls)), None

    driver.screen_wake_gate = gate
    return daemon, driver, bundle, root, calls


def test_wake_target_comes_from_current_passed_validation_without_writes():
    from drivers.wake_screen import resolve_wake_target
    from tests.test_screen_gesture_driver import gesture_bundle

    bundle = validated_bundle(gesture_bundle())
    before = deepcopy(bundle)
    target = resolve_wake_target(bundle)
    assert target == {
        "validation_point_id": "validation-5",
        "point": {"x": .5, "y": .65},
        "binding": sg.binding_snapshot(bundle),
        "target_source": "validation_point",
    }
    assert bundle == before


def test_wake_target_prefers_current_run_tap_anchor_and_keeps_provenance():
    # W01：本轮“确认点击参数/测试点击”锚点优先于独立验证点，来源可追溯。
    from drivers.wake_screen import resolve_wake_target
    from tests.test_screen_gesture_driver import gesture_bundle

    bundle = validated_bundle(gesture_bundle())
    bundle["wakeTapAnchor"] = {
        "anchorId": "tap-anchor-1", "runId": "wake-calibration", "verified": True,
        "touchTargetErrorPx": 4, "u": .42, "v": .58,
        "deviceId": bundle["deviceId"], "cameraId": bundle["cameraId"],
        "daemonSession": bundle["daemonSession"],
        "sampleId": "sample-77", "source": "z_confirm_safe", "ts": "2026-09-29T00:00:00Z",
    }
    before = deepcopy(bundle)
    target = resolve_wake_target(bundle)
    assert target["validation_point_id"] == "tap-anchor-1"
    assert target["point"] == {"x": .42, "y": .58}
    assert target["target_source"] == "wake_tap_anchor"
    assert target["anchor"]["sampleId"] == "sample-77"
    assert target["anchor"]["source"] == "z_confirm_safe"
    assert target["anchor"]["runId"] == "wake-calibration"
    assert target["binding"] == sg.binding_snapshot(bundle)
    assert bundle == before, "resolve must not write the bundle"


@pytest.mark.parametrize("change", ["old_run", "unverified", "error_exceeded",
                                    "bad_binding", "not_a_dict"])
def test_wake_invalid_tap_anchor_is_rejected_not_silently_ignored(change):
    # W01：锚点存在但溯源无效时明确拒绝，不静默回退、不创建默认中心点。
    from drivers.wake_screen import resolve_wake_target
    from tests.test_screen_gesture_driver import gesture_bundle

    bundle = validated_bundle(gesture_bundle())
    anchor = {
        "anchorId": "tap-anchor-1", "runId": "wake-calibration", "verified": True,
        "touchTargetErrorPx": 4, "u": .42, "v": .58,
    }
    if change == "old_run":
        anchor["runId"] = "previous-calibration"
    elif change == "unverified":
        anchor["verified"] = False
    elif change == "error_exceeded":
        anchor["touchTargetErrorPx"] = 11  # 超过 min(tolerancePx,10px)
    elif change == "bad_binding":
        anchor["deviceId"] = "arm-02"
    else:
        anchor = "corrupted"
    bundle["wakeTapAnchor"] = anchor
    with pytest.raises(ValueError):
        resolve_wake_target(bundle)


@pytest.mark.parametrize("change", ["missing", "old_run", "failed", "nan", "edge"])
def test_wake_target_never_guesses_an_unverified_point(change):
    from drivers.wake_screen import resolve_wake_target
    from tests.test_screen_gesture_driver import gesture_bundle

    bundle = validated_bundle(gesture_bundle())
    if change == "missing":
        bundle.pop("validation")
    elif change == "old_run":
        bundle["validation"]["runId"] = "previous"
    elif change == "failed":
        bundle["validation"]["passed"] = False
    else:
        bundle["validation"]["points"] = [bundle["validation"]["points"][1]]
        bundle["validation"]["points"][0]["u"] = float("nan") if change == "nan" else 0
    with pytest.raises(ValueError):
        resolve_wake_target(bundle)


def test_wake_double_taps_once_and_preserves_calibration_and_hover(rig):
    daemon, driver, bundle, root, calls = prepare_wake(rig)
    before = cb.bundle_path("arm-01", root).read_bytes()
    result = asyncio.run(driver.wake_screen({
        "binding": sg.binding_snapshot(bundle), "confirmed": True,
    }))
    # W03：daemon 上报 tap2.feed-3000.v1 但档案没有与当前校准绑定的双击
    # 速度验收证据——沿用本轮验证的校准速度，接触平面与总下压深度不变。
    assert [cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")] == [
        "TAP2 3.000 F1500 I0.05"]
    assert result["validation_point_id"] == "validation-5"
    assert result["target_source"] == "validation_point"
    assert result["controller_completed"] is True
    assert result["motion_verified"] is False
    assert result["effect_status"] == "awake"
    assert result["tap_count"] == 2
    assert result["double_tap_feed"] == bundle["z"]["tapFeed"]
    assert result["double_tap_feed_source"] == "calibrated_tap_feed"
    assert result["speed_note"]
    assert daemon.mpos[2] == bundle["z"]["hoverZ"]
    assert cb.bundle_path("arm-01", root).read_bytes() == before
    assert len(calls) > 2
    assert not any(cmd.startswith(("SWIPE", "PATH1", "$H")) for cmd in daemon.commands)


def test_wake_uses_fast_double_tap_only_with_current_run_evidence(rig):
    # W03：有与当前校准 run 绑定且验收通过的双击速度证据才允许 F3000。
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    bundle["doubleTapSpeedAcceptance"] = {
        "runId": bundle["autoCalibration"]["runId"], "passed": True, "feed": 3000}
    cb.save_bundle(bundle, root)
    result = asyncio.run(driver.wake_screen({
        "binding": sg.binding_snapshot(bundle), "confirmed": True,
    }))
    assert [cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")] == [
        "TAP2 3.000 F3000 I0.05"]
    assert result["double_tap_feed"] == 3000
    assert result["double_tap_feed_source"] == "calibrated_double_tap_evidence"
    assert result["speed_note"] is None
    # 接触平面不变：抬升仍为 3mm，最终回到校准悬停高度。
    assert daemon.mpos[2] == bundle["z"]["hoverZ"]


def test_wake_stale_speed_evidence_does_not_speed_up_the_tap(rig):
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    bundle["doubleTapSpeedAcceptance"] = {
        "runId": "previous-calibration", "passed": True, "feed": 3000}
    cb.save_bundle(bundle, root)
    result = asyncio.run(driver.wake_screen({
        "binding": sg.binding_snapshot(bundle), "confirmed": True,
    }))
    assert [cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")] == [
        "TAP2 3.000 F1500 I0.05"]
    assert result["double_tap_feed"] == 1500
    assert result["double_tap_feed_source"] == "calibrated_tap_feed"


def test_wake_without_observable_dark_pose_waits_without_motion(rig):
    # W09/§9.3：暗屏且当前姿态不可观测（仅校准 ROI 可判断）→ 等待，
    # 自动唤醒不盲动；没有人工单次现场确认时不执行任何机械指令。
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    normal_gate = driver.screen_wake_gate

    async def gate(*, require_pose=False):
        checks, frame, deviation = await normal_gate()
        if not require_pose:
            frame.pop("_screen_pose", None)
        return checks, frame, deviation

    driver.screen_wake_gate = gate
    with pytest.raises(DriverError) as error:
        asyncio.run(driver.wake_screen({
            "binding": sg.binding_snapshot(bundle), "confirmed": True,
        }))
    assert error.value.category == "driver_camera_waiting"
    assert error.value.motion_not_started is True
    assert not daemon.commands
    assert not any(cmd.startswith("TAP2 ") for cmd in daemon.commands)


def test_wake_unlit_effect_does_not_retry_or_invalidate_calibration(rig):
    # W04/§9.4：双击与必要抬起均有完整控制器确认、之后仍暗屏——机械完成
    # 保留（controller_completed=true、tap_count=2），仅亮屏效果 unverified。
    daemon, driver, bundle, root, _ = prepare_wake(rig, lit_after=False)
    before = cb.bundle_path("arm-01", root).read_bytes()
    result = asyncio.run(driver.wake_screen({
        "binding": sg.binding_snapshot(bundle), "confirmed": True,
    }))
    assert result["controller_completed"] is True
    assert result["tap_count"] == 2
    assert result["outcome_hint"] is None
    assert result["effect_status"] == "unverified"
    assert result["frame_observations"][0]["quality"] == "waiting"
    assert result["frame_observations"][-1]["quality"] == "waiting"
    assert len([cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")]) == 1
    assert daemon.mpos[2] == bundle["z"]["hoverZ"]
    assert cb.bundle_path("arm-01", root).read_bytes() == before
    assert not any(cmd.startswith(("SWIPE", "PATH1", "$H")) for cmd in daemon.commands)


def test_wake_compensates_observable_dark_phone_offset(rig):
    # W08/§9.3：暗屏但手机姿态可观测（偏移在补偿限内）→ 唤醒点补偿到当前
    # 位置，只补偿一次，接触平面不变。
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    saved = bundle["screen"]["edgeModel"]["corners"]
    moved = [[x + 6, y + 4] for x, y in saved]
    offset_pose = screen_pose.fit_pose(saved, moved)
    normal_gate = driver.screen_wake_gate

    async def gate(*, require_pose=False):
        checks, frame, deviation = await normal_gate()
        # 手机整体平移后，暗屏与亮屏帧的姿态都应反映同一偏移。
        frame["_screen_pose"] = offset_pose
        return checks, frame, deviation

    driver.screen_wake_gate = gate
    result = asyncio.run(driver.wake_screen({
        "binding": sg.binding_snapshot(bundle), "confirmed": True,
    }))
    from drivers import screen_gestures as _sg
    from drivers.wake_screen import resolve_wake_target

    target = resolve_wake_target(bundle)
    base = _sg.resolve_point(
        {"source": "screen_normalized", **target["point"]}, bundle)
    compensated = screen_pose.physical_point(
        bundle, offset_pose, base["cameraX"], base["cameraY"])
    # 实际走位终点 = 补偿后的机械坐标（未补偿坐标则不同）。
    assert (result["position"]["x"], result["position"]["y"]) == pytest.approx(
        (compensated[0], compensated[1]))
    assert (compensated[0], compensated[1]) != (base["robotX"], base["robotY"])
    assert result["controller_completed"] is True
    assert result["position_reference"] == "live_pose"
    assert result["manual_position_confirmed"] is False


def test_wake_lit_lock_screen_keeps_awake_effect_without_further_motion(rig):
    # W14/§9.4：亮屏+姿态可信但页面是锁屏/未知页（Fleet 不识别页面内容）——
    # effect_status=awake 保留，机械事实保留，不追加任何业务触屏动作
    # （业务 页面恢复屏障在业务侧，不由 Fleet 改回未知）。
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    counter = [0]

    async def gate(*, require_pose=False):
        # 双击后亮起的“锁屏”：中央大时钟块+零星状态栏文字，姿态仍可拟合。
        counter[0] += 1
        lit = any(cmd.startswith("TAP2 ") for cmd in daemon.commands)
        return [], lock_screen_frame(bundle, lit=lit, sequence=counter[0]), None

    driver.screen_wake_gate = gate
    result = asyncio.run(driver.wake_screen({
        "binding": sg.binding_snapshot(bundle), "confirmed": True,
    }))
    assert result["controller_completed"] is True
    assert result["effect_status"] == "awake"
    assert result["motion_verified"] is False
    commands = list(daemon.commands)
    assert len([cmd for cmd in commands if cmd.startswith("TAP2 ")]) == 1
    assert not any(cmd.startswith(("SWIPE", "PATH1", "TAP ", "$H")) for cmd in commands)
    assert daemon.mpos[2] == bundle["z"]["hoverZ"]


def lock_screen_frame(bundle, *, lit=False, sequence=1):
    """亮起的锁屏：可见壁纸纹理+大号时钟+状态栏，无聊天列表内容。"""
    frame = wake_frame(bundle, lit=lit, sequence=sequence)
    if lit:
        image = cv2.imdecode(
            np.frombuffer(frame["image_bytes"], np.uint8), cv2.IMREAD_COLOR)
        corners = np.asarray(bundle["screen"]["edgeModel"]["corners"], np.int32)
        x0, y0 = corners.min(axis=0)
        x1, y1 = corners.max(axis=0)
        # 大号时钟（锁屏主体）与顶部状态栏；背景保留壁纸纹理以维持画质判定。
        image[(y0 + y1) // 2 - 8:(y0 + y1) // 2 + 8,
              (x0 + x1) // 2 - 30:(x0 + x1) // 2 + 30] = 235
        image[y0 + 4:y0 + 10, (x0 + x1) // 2 - 14:(x0 + x1) // 2 + 14] = 210
        for index in range(3):
            top = y0 + 40 + index * 26
            image[top:top + 14, x0 + 30:x0 + 120] = 120 + index * 20
        ok, encoded = cv2.imencode(".png", image)
        assert ok
        frame["image_bytes"] = encoded.tobytes()
    return frame


@pytest.mark.parametrize("change", ["no_confirmation", "point", "depth", "binding", "session",
                                   "estop", "missing_gate", "already_lit", "gate_failure"])
def test_wake_invalid_context_never_starts_motion(rig, change):
    daemon, driver, bundle, _, _ = prepare_wake(rig)
    params = {"binding": sg.binding_snapshot(bundle), "confirmed": True}
    if change == "no_confirmation":
        params["confirmed"] = 1
    elif change == "point":
        params["point"] = point()
    elif change == "depth":
        params["tapDepth"] = 30
    elif change == "binding":
        params["binding"]["calibration_fingerprint"] = "old"
    elif change == "session":
        daemon.session = "other"
    elif change == "estop":
        driver._estop_latched = True
    elif change == "missing_gate":
        driver.screen_wake_gate = None
    else:
        async def gate():
            checks = ([{"code": "camera_unavailable", "message": "missing frame"}]
                      if change == "gate_failure" else [])
            return checks, wake_frame(bundle, lit=True), None
        driver.screen_wake_gate = gate
    with pytest.raises(DriverError) as error:
        asyncio.run(driver.wake_screen(params))
    assert error.value.motion_not_started is True
    assert not daemon.commands


def test_ordinary_double_tap_still_waits_on_black_screen(rig):
    daemon, driver, bundle, _, _ = prepare_wake(rig)

    async def ordinary_gate(**_):
        return [{"code": "camera_waiting", "message": "waiting"}]

    driver.live_calibration_gate = ordinary_gate
    with pytest.raises(DriverError) as error:
        asyncio.run(driver.double_tap({"point": point(), "binding": sg.binding_snapshot(bundle)}))
    assert error.value.category == "driver_camera_waiting"
    assert not daemon.commands


def test_lost_wake_reply_does_not_replay_or_append_retract(rig):
    daemon, driver, bundle, _, _ = prepare_wake(rig)
    daemon.response_hook = lambda cmd, reply, _: None if cmd.startswith("TAP2 ") else reply
    with pytest.raises(DriverError) as error:
        asyncio.run(driver.wake_screen({"binding": sg.binding_snapshot(bundle), "confirmed": True}))
    assert error.value.category == "driver_disconnected"
    assert daemon.commands[-1].startswith("TAP2 ")
    assert len([cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")]) == 1


def test_validation_change_during_travel_never_presses(rig):
    daemon, driver, bundle, root, _ = prepare_wake(rig)
    gate = driver.screen_wake_gate

    async def change_gate():
        if daemon.commands:
            changed = deepcopy(bundle)
            changed["validation"]["points"][1]["passed"] = False
            cb.save_bundle(changed, root)
        return await gate()

    driver.screen_wake_gate = change_gate
    result = asyncio.run(driver.wake_screen({
        "binding": sg.binding_snapshot(bundle), "confirmed": True}))
    assert result["controller_completed"] is False
    assert result["outcome_hint"] == "unknown"
    assert not any(cmd.startswith("TAP2 ") for cmd in daemon.commands)


@pytest.mark.parametrize("change", ["frozen", "stale", "future", "missing_sequence",
                                   "camera", "device", "resolution", "bad_image"])
def test_wake_frame_faults_are_not_black_screen_exceptions(rig, change):
    daemon, driver, bundle, _, _ = prepare_wake(rig)

    async def gate():
        frame = wake_frame(bundle)
        if change == "frozen":
            frame["frozen"] = True
        elif change == "stale":
            frame["source_captured_at"] = "2020-01-01T00:00:00+00:00"
        elif change == "future":
            frame["source_captured_at"] = "2099-01-01T00:00:00+00:00"
        elif change == "missing_sequence":
            frame.pop("source_frame_seq")
        elif change == "camera":
            frame["device_unique_id"] = "different"
        elif change == "device":
            frame["device_id"] = "arm-02"
        elif change == "resolution":
            frame["width"] += 1
        else:
            frame["image_bytes"] = b"not an image"
        return [], frame, None

    driver.screen_wake_gate = gate
    with pytest.raises(DriverError):
        asyncio.run(driver.wake_screen({"binding": sg.binding_snapshot(bundle), "confirmed": True}))
    assert not daemon.commands
