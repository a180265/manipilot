"""Recovery observes physical boundaries across lighting; it never reuses old corners."""
import cv2
import numpy as np
import pytest

from drivers import screen_edge
from runtime.recovery import locate_recovery_screen, verify_phone_unchanged, recovery_anchor


CORNERS = np.array([[156, 104], [1156, 123], [1156, 583], [149, 552]], float)


def frame(offset=(0, 0)):
    image = np.full((720, 1280, 3), 185, np.uint8)
    corners = np.rint(CORNERS + offset).astype(np.int32)
    cv2.fillConvexPoly(image, corners - 6, (15, 15, 15))
    cv2.fillConvexPoly(image, corners, (95, 90, 100))
    return image


def no_contour(*args, **kwargs):
    raise screen_edge.ScreenEdgeError("亮度轮廓不可用")


def test_recovery_uses_observed_gradient_when_luminance_contour_fails(monkeypatch):
    monkeypatch.setattr(screen_edge, "locate_screen_quad", no_contour)
    located = locate_recovery_screen(frame(), CORNERS.tolist())
    np.testing.assert_allclose(located["corners"], CORNERS, atol=3)
    assert located["source"] == "calibrated_prediction"


@pytest.mark.parametrize("image", [np.zeros((720, 1280, 3), np.uint8), frame((100, 0))])
def test_missing_or_distant_phone_cannot_recover(monkeypatch, image):
    monkeypatch.setattr(screen_edge, "locate_screen_quad", no_contour)
    result = verify_phone_unchanged(
        {"screen": {"edgeModel": {"corners": CORNERS.tolist()}}}, lambda: image)
    assert result["ok"] is False


def test_gradient_iteration_does_not_expand_recovery_search_bound(monkeypatch):
    monkeypatch.setattr(screen_edge, "locate_screen_quad", no_contour)
    monkeypatch.setattr(screen_edge, "locate_quad_by_gradient_lines", lambda *a, **k: {
        "corners": (CORNERS + [40, 0]).tolist()})
    with pytest.raises(screen_edge.ScreenEdgeError, match="32px"):
        locate_recovery_screen(frame(), CORNERS.tolist())


def test_gradient_still_requires_near_rigid_geometry(monkeypatch):
    monkeypatch.setattr(screen_edge, "locate_screen_quad", no_contour)
    distorted = CORNERS.copy()
    distorted[0] += [30, 0]
    monkeypatch.setattr(screen_edge, "locate_quad_by_gradient_lines", lambda *a, **k: {
        "corners": distorted.tolist()})
    with pytest.raises(ValueError, match="无法可靠补偿"):
        locate_recovery_screen(frame(), CORNERS.tolist())


def test_auto_calibration_recovers_from_measured_center_sample():
    bundle = {"screen": {"screenWidth": 1000, "screenHeight": 2000}, "xy": {
        "validation": [{"u": .5, "v": .5, "passed": True}],
        "samples": [{"effectiveRobotXY": [30, 20], "actualTouch": [100, 200]},
                    {"effectiveRobotXY": [50, 60], "actualTouch": [500, 1001]}]}}
    assert recovery_anchor(bundle) == ([50., 60.], [500., 1001.])


def test_legacy_measured_validation_pair_remains_supported():
    assert recovery_anchor({"xy": {"validation": [
        {"effectiveRobotXY": [1, 2], "targetScreen": [3, 4]}]}}) == ([1., 2.], [3., 4.])


@pytest.mark.parametrize("robot,touch", [([True, 1], [2, 3]), ([1, float("nan")], [2, 3]),
                                       ([1, 2], None), ([1], [2, 3])])
def test_recovery_never_invents_an_anchor_from_incomplete_samples(robot, touch):
    with pytest.raises(RuntimeError, match="已实测"):
        recovery_anchor({"xy": {"samples": [
            {"effectiveRobotXY": robot, "actualTouch": touch, "u": .5, "v": .5}]}})


def probe_frame(centers=()):
    image = np.full((720, 1280, 3), 115, np.uint8)
    for center in centers:
        cv2.circle(image, center, 37, (240, 240, 240), 3)
        cv2.circle(image, center, 33, (175, 170, 145), -1)
        cv2.circle(image, center, 6, (15, 15, 15), -1)
    return image


def test_probe_is_measured_without_using_expected_anchor():
    from runtime.recovery import observe_recovery_probe
    before = probe_frame()
    after = probe_frame([(620, 310)])
    np.testing.assert_allclose(observe_recovery_probe(before, after, CORNERS),
                               [620, 310], atol=3)


@pytest.mark.parametrize("before,after", [
    (probe_frame([(620, 310)]), probe_frame([(620, 310)])),
    (probe_frame(), probe_frame([(620, 310), (900, 310)])),
    (probe_frame(), probe_frame([(620, 40)])),
    (probe_frame(), probe_frame()),
])
def test_static_content_ambiguous_heads_and_outside_supports_cannot_verify(before, after):
    from runtime.recovery import observe_recovery_probe
    with pytest.raises(RuntimeError, match="无法唯一识别"):
        observe_recovery_probe(before, after, CORNERS)


@pytest.mark.parametrize("park_session,expected", [("old", "new"), ("unrelated", "unrelated")])
def test_only_verified_coordinate_frames_rebind_saved_park(monkeypatch, park_session, expected):
    from drivers import calibration_bundle
    from runtime import recovery, automatic_gestures
    saved = []
    monkeypatch.setattr(calibration_bundle, "save_bundle", lambda b, root: saved.append(b))
    monkeypatch.setattr(automatic_gestures, "restore_certificates", lambda *a, **kw: {})
    bundle = {"deviceId": "arm-01", "daemonSession": "old", "safePark": {
        "confirmed": True, "daemonSession": park_session, "parkX": 0., "parkY": 0., "parkZ": 0.}}
    result = recovery.attempt_recovery("arm-01", bundle=bundle, new_session="new",
                                       arm_anchor_verifier=lambda _: True)
    assert result["recovered"] is True
    assert saved[0]["safePark"]["daemonSession"] == expected
    assert bundle["safePark"]["daemonSession"] == park_session
