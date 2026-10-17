"""Calibration root and Android display must remain separate coordinate domains."""
from copy import deepcopy

import cv2
import numpy as np
import pytest

from drivers import calibration_display as display
from runtime.layout_geometry import LayoutGeometryError, warp_root_from_raw

H = np.array([[-.029021474153464923, 2.339747240397833, -157.15599371632157],
              [-2.3161302664697025, .01715561958433222, 2594.965693057723],
              [-6.257833001499252e-06, 3.839380880357953e-05, 1.]])
ROOT_EDGE = [[119.2,65.8],[1124.4,85.7],[1122.9,551.4],[100.7,534.5]]


def metadata():
    return {"deviceId": "phone", "sessionId": "session", "clientInstanceId": "client",
            "page": {"pageId": "legacy-calibration", "legacyCalibrationSessionId": "session",
                     "rootWidthPx":1080, "rootHeightPx":2320, "displayWidthPx":1080,
                     "displayHeightPx":2400, "rootOriginDisplayPx":[0,80], "rotation":0,
                     "density":2.75, "coordinateSpaceVersion":2}}


def bundle():
    return {"frame":{"width":1280,"height":720},
            "screen":{"screenWidth":1080,"screenHeight":2320,"edgeModel":{"corners":ROOT_EDGE}},
            "camera":{"cameraToScreen":{"homography":H.tolist()}}}


def test_full_display_reference_preserves_original_root_evidence():
    value = bundle()
    evidence = metadata()
    display.attach_display_reference(value, evidence, ime_device_id="phone", session_id="session")
    np.testing.assert_allclose(value["screen"]["edgeModel"]["corners"],
        [[117.7574456,68.6285619],[1155.4,81.5],[1159.5,549.1],[103.0723939,538.8506249]], atol=.1)
    assert value["screen"]["calibrationRootEdgeModel"]["corners"] == ROOT_EDGE
    assert value["screen"]["displayCoordinateMetadata"] == evidence
    assert evidence == metadata()


@pytest.mark.parametrize("change", ["session", "root", "origin", "rotation"])
def test_no_guessed_or_stale_display_origin(change):
    value, evidence = bundle(), metadata()
    if change == "session": evidence["sessionId"] = "old"
    elif change == "root": evidence["page"]["rootHeightPx"] = 2300
    elif change == "origin": evidence["page"].pop("rootOriginDisplayPx")
    else: evidence["page"]["rotation"] = 1
    before = deepcopy(value)
    with pytest.raises(ValueError):
        display.attach_display_reference(value, evidence, ime_device_id="phone", session_id="session")
    assert value == before


def test_continuous_display_coverage_does_not_invent_mask_border_pixels():
    value = bundle()
    display.attach_display_reference(value, metadata(), ime_device_id="phone", session_id="session")
    image = np.full((720,1280,3), 211, np.uint8)
    pose = {"matrix":np.eye(3).tolist(), "corners":value["screen"]["edgeModel"]["corners"]}
    screen = warp_root_from_raw(image, value, pose, {"rect":{"x":0,"y":0,"width":1,"height":1}})
    assert screen.shape == (2320,1080,3)
    assert np.all(screen == 211)
    # Old integer mask fails despite a mathematically contained root footprint.
    mask = np.zeros((720,1280), np.uint8)
    cv2.fillConvexPoly(mask, np.rint(pose["corners"]).astype(np.int32), 255)
    assert np.any(cv2.warpPerspective(mask,H,(1080,2320)) != 255)


def test_real_outside_screen_and_camera_fill_still_rejected():
    value = bundle()
    image = np.full((720,1280,3),211,np.uint8)
    pose = {"matrix":np.eye(3).tolist(),"corners":ROOT_EDGE}
    region = {"rect":{"x":0,"y":0,"width":1,"height":1}}
    with pytest.raises(LayoutGeometryError, match="校准屏幕"):
        warp_root_from_raw(image,value,pose,region)
    display.attach_display_reference(value,metadata(),ime_device_id="phone",session_id="session")
    pose["matrix"][0][2] = 200
    with pytest.raises(LayoutGeometryError, match="原图|粗框"):
        warp_root_from_raw(image,value,pose,region)


def test_auto_run_freezes_same_session_metadata_before_thread_start(tmp_path, monkeypatch):
    from drivers import calibration_bundle as cb
    from tests.test_calibration_wizard import auto_ready_env

    env = auto_ready_env(tmp_path)
    screen = env.wizard.state["screen"]
    session = env.wizard.state["imeSessionId"]
    evidence = metadata()
    evidence.update(deviceId=env.wizard.state["imeDeviceId"], sessionId=session)
    evidence["page"].update(legacyCalibrationSessionId=session,
        rootWidthPx=screen["screenWidth"], rootHeightPx=screen["screenHeight"],
        displayWidthPx=screen["screenWidth"], displayHeightPx=screen["screenHeight"]+24,
        rootOriginDisplayPx=[0,24])
    monkeypatch.setattr(env.ime,"fetch_gesture_frame_metadata",lambda *a:evidence,raising=False)
    monkeypatch.setattr(env.wizard._auto_runner,"_auto_spawn",lambda *a:None)
    started = env.wizard.auto_start(defer_input_acceptance=True)
    candidate = cb.load_candidate("arm-01",started["runId"],tmp_path)
    assert candidate["displayCoordinateMetadata"] == evidence
    evidence["page"]["rootOriginDisplayPx"][1] = 999
    assert env.wizard._auto_runner._auto["run"]["displayCoordinateMetadata"]["page"]["rootOriginDisplayPx"] == [0,24]


def test_sidecar_uses_promoted_metadata_without_querying_closed_page(tmp_path):
    from drivers import calibration_wizard as cw, gesture_program as gp
    from tests.test_gesture_coordinate_frame import _make_wizard, _FakeIme, _legacy_metadata
    from tests.test_screen_gesture_driver import gesture_bundle

    value = gesture_bundle()
    value["autoCalibration"] = {"runId":"run-current", "state":"PROMOTED"}
    value["screen"]["displayCoordinateMetadata"] = _legacy_metadata()
    wizard = _make_wizard(tmp_path,_FakeIme(error=AssertionError("page already closed")))
    wizard._save_gesture_coordinate_frame(value)
    frame = gp.load_coordinate_frame("arm-01",tmp_path)
    assert frame["calibration_run_id"] == "run-current"
    assert frame["root_origin_display_px"] == [0,24]
    gp.validate_coordinate_frame(frame,value)
    assert wizard.ime.calls == []


@pytest.mark.parametrize("corrupt", ["metadata", "version", "corner", "new_missing"])
def test_new_display_layout_cannot_use_missing_or_changed_coordinate_bridge(corrupt):
    from runtime.layout_geometry import matrix_and_size
    value = bundle()
    display.attach_display_reference(value,metadata(),ime_device_id="phone",session_id="session")
    if corrupt == "metadata": value["screen"].pop("displayCoordinateMetadata")
    elif corrupt == "version": value["screen"]["edgeModel"]["referenceVersion"] = 9
    elif corrupt == "corner": value["screen"]["edgeModel"]["corners"][0][0] += 1
    else: value["screen"]["edgeModel"] = {"corners":ROOT_EDGE}
    with pytest.raises(LayoutGeometryError):
        matrix_and_size(value)


def test_pose_and_homography_compose_once_from_raw_frame():
    value = bundle()
    display.attach_display_reference(value,metadata(),ime_device_id="phone",session_id="session")
    image = np.random.default_rng(1).integers(0,256,(720,1280,3),dtype=np.uint8)
    movement = np.array([[1.,0.,7.],[0.,1.,3.],[0.,0.,1.]])
    live = cv2.warpPerspective(image,movement,(1280,720))
    pose = {"matrix":movement.tolist(),"corners":value["screen"]["edgeModel"]["corners"]}
    result = warp_root_from_raw(live,value,pose,{"rect":{"x":0,"y":0,"width":1,"height":1}})
    expected = cv2.warpPerspective(image,H,(1080,2320))
    # OpenCV fixed-point interpolation can differ by one 8-bit rounding step.
    assert np.max(np.abs(result.astype(int)-expected.astype(int))) <= 1
