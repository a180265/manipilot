"""Swipe profile regressions with temporary bundles and a simulated daemon only."""
import asyncio
import json

import pytest

from drivers import calibration_bundle as cb
from tests.test_daemon_arm import CALIB_SESSION, FakeDaemon, make_driver, write_device_files
from tests.test_ui_targets import ready_bundle


def camera_swipe(**changes):
    return {
        "start": {"source": "camera_gesture", "cameraX": 300, "cameraY": 300,
                  "frameW": 1280, "frameH": 720},
        "end": {"source": "camera_gesture", "cameraX": 500, "cameraY": 300,
                "frameW": 1280, "frameH": 720},
        "durationMs": 600,
        **changes,
    }


@pytest.fixture()
def rig(tmp_path):
    write_device_files(tmp_path)
    bundle = ready_bundle(session=CALIB_SESSION)
    bundle["z"].update(hoverZ=0, zSafe=0, tapDepth=8, tapFeed=1200)
    cb.save_bundle(bundle, tmp_path)
    daemon = FakeDaemon(mpos=(20, 12.5, 5)).start()
    driver = make_driver(daemon, tmp_path)
    try:
        asyncio.run(driver.connect())
        yield daemon, driver, bundle, tmp_path
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()


def test_camera_swipe_uses_current_bundle_not_stale_profile_file(rig):
    daemon, driver, _, root = rig
    assert json.loads((root / "devices" / "arm-01" / "arm_profile.json").read_text()) == {
        "hoverZ": 2, "tapDepth": 3, "tapFeed": 1500,
    }
    before = {p: p.read_bytes() for p in root.rglob("*.json")}

    result = asyncio.run(driver.swipe(camera_swipe()))

    assert daemon.commands == [
        "Z -5.000 F1200",
        "SWIPE 20.000 0.000 8.000 F2000 Z1200",
    ]
    assert daemon.mpos == [40, 12.5, 0]
    assert result["swipe_executed"] is True
    assert result["effect_status"] == "unverified"
    assert {p: p.read_bytes() for p in root.rglob("*.json")} == before


@pytest.mark.parametrize("hover, safe, expected_z, expected_command", [
    (0, -4, 0, "Z -5.000 F1200"),
    (-4, 0, -4, "Z -9.000 F1200"),
    (None, 0, 0, "Z -5.000 F1200"),
    ("missing", -4, -4, "Z -9.000 F1200"),
])
def test_camera_swipe_preserves_calibrated_hover_and_safe_fallback(
        rig, hover, safe, expected_z, expected_command):
    daemon, driver, bundle, root = rig
    bundle["z"]["zSafe"] = safe
    if hover == "missing":
        bundle["z"].pop("hoverZ")
    else:
        bundle["z"]["hoverZ"] = hover
    cb.save_bundle(bundle, root)
    before = {p: p.read_bytes() for p in root.rglob("*.json")}

    asyncio.run(driver.swipe(camera_swipe()))

    assert daemon.commands == [
        expected_command,
        "SWIPE 20.000 0.000 8.000 F2000 Z1200",
    ]
    assert daemon.mpos[2] == expected_z
    assert {p: p.read_bytes() for p in root.rglob("*.json")} == before
