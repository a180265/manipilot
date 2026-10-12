"""Separate touch cycles, calibrated contact plane, and TAP2-only fast feed."""
import asyncio

import pytest

from drivers import screen_gestures as sg
from drivers.daemon_protocol import check_double_tap_feed, check_feed, check_swipe
from tests.test_screen_gesture_driver import rig, point


@pytest.mark.parametrize("fast", [False, True])
@pytest.mark.parametrize("depth", [1.0, 3.0, 8.0])
def test_double_tap_two_contacts_at_original_plane(rig, fast, depth):
    from drivers import calibration_bundle as cb

    daemon, driver, bundle, root = rig
    bundle["z"]["tapDepth"] = depth
    cb.save_bundle(bundle, root)
    original = cb.bundle_path("arm-01", root).read_bytes()
    if fast:
        daemon.motion_features.append("tap2.feed-3000.v1")
    result = asyncio.run(driver.double_tap({
        "point": point(), "binding": sg.binding_snapshot(bundle)}))
    z = bundle["z"]["hoverZ"]
    contact = z + depth
    presses, releases = [], []
    macro_feed = None
    for command in daemon.commands:
        parts = command.split()
        if parts[0] == "Z":
            z += float(parts[1])
        elif parts[0] == "TAP2":
            lift = float(parts[1])
            macro_feed = float(parts[2][1:])
            assert not any(part.startswith("R") for part in parts)
            for _ in range(2):
                z += lift
                presses.append(z)
                z -= lift
                releases.append(z)
    assert presses == pytest.approx([contact, contact])
    assert releases == pytest.approx([contact - min(depth, 3)] * 2)
    assert z == bundle["z"]["hoverZ"]
    assert macro_feed == (3000 if fast else 1500)
    assert result["controller_completed"] and result["tap_count"] == 2
    assert result["double_tap_feed"] == macro_feed
    assert cb.bundle_path("arm-01", root).read_bytes() == original


def test_f3000_only_allowed_for_double_tap():
    check_double_tap_feed(3000)
    check_double_tap_feed(300)  # 2026-09-18 设备更换解禁；范围内正常放行
    for invalid in [3001, 0, float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            check_double_tap_feed(invalid)
    with pytest.raises(ValueError):
        check_feed(3000, axis="Z")
    with pytest.raises(ValueError):
        check_swipe(10, 0, 8, 1500, 3000)
