"""坐标桥缺省路径与校准档案共用 data 根目录，支持实际输入法调用。"""
from drivers import calibration_bundle as cb, gesture_program as gp, ime_coordinates
from tests.test_gesture_coordinate_frame import _frame_for
from tests.test_screen_gesture_driver import gesture_bundle


def test_default_coordinate_path_and_missing_file_use_calibration_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "default_data_root", lambda: tmp_path / "default-data")
    expected = cb.bundle_path("arm-01").parent / gp.COORDINATE_FRAME_FILENAME
    assert gp.coordinate_frame_path("arm-01") == expected
    assert gp.load_coordinate_frame("arm-01") is None
    assert gp.coordinate_frame_path("arm-01", tmp_path / "explicit-data") == (
        tmp_path / "explicit-data/devices/arm-01" / gp.COORDINATE_FRAME_FILENAME)


def test_default_coordinate_frame_roundtrip_reaches_live_ime_consumer(tmp_path, monkeypatch):
    monkeypatch.setattr(cb, "default_data_root", lambda: tmp_path)
    bundle = gesture_bundle()
    run = (bundle.get("autoCalibration") or {}).get("runId") or "manual-finish"
    frame = _frame_for(bundle, root_origin=[0, 0], run_id=run)
    gp.save_coordinate_frame("arm-01", frame)
    assert gp.load_coordinate_frame("arm-01") == frame
    display = {"display_id": 0, "width_px": frame["display_width_px"],
               "height_px": frame["display_height_px"], "rotation": frame["rotation"],
               "density": frame["density"]}
    assert ime_coordinates.coordinate_frame("arm-01", bundle, display, None) == frame
