"""Explicit safe parking stays independent of page targets and sends no contact."""
from __future__ import annotations

import asyncio

import pytest

from drivers import calibration_bundle as cb
from drivers.arm import DriverError
from drivers.daemon_arm import DaemonArmDriver
from drivers.fake_arm import FakeArmDriver
from runtime.capabilities import BASE_CAPABILITIES, DEFAULT_DEVICE_CAPABILITIES
from runtime.parking import CALIBRATION_SAFE_PARK
from tests.test_layout_target_driver import target_rig, motion  # noqa: F401


def save_park(rig, **overrides):
    rig.bundle["safePark"] = {
        "confirmed": True, "parkX": 1.0, "parkY": 2.0, "parkZ": 2.0,
        "daemonSession": "sess-A", **overrides,
    }
    cb.save_bundle(rig.bundle, rig.data)


def test_safe_park_is_an_explicit_capability():
    assert "arm.park" in DEFAULT_DEVICE_CAPABILITIES
    assert any(x["capability_id"] == "arm.park" for x in BASE_CAPABILITIES)
    assert DaemonArmDriver("arm-01").supports("arm.park")
    assert FakeArmDriver("arm-01").supports("arm.park")


def test_safe_park_does_not_require_current_page_or_old_targets(target_rig):
    rig = target_rig
    save_park(rig)

    async def forbidden():
        pytest.fail("Safe parking must not resolve a page/keyboard target")

    rig.arm.attach_layout_provider(forbidden)
    result = asyncio.run(rig.arm.park())
    assert result["ok"] is True and result["arrived"] is True
    assert result["role"] == CALIBRATION_SAFE_PARK
    assert result["tap_executed"] is False
    assert result["position"] == dict(x=1.0, y=2.0, z=2.0)
    assert motion(rig) and not any(x.startswith(("TAP ", "TAP2 ")) for x in motion(rig))
    assert any(x.startswith("CLAIM ") for x in rig.transport.commands)
    assert any(x.split()[0] == "BEGIN_MOTION" for x in rig.transport.commands)


@pytest.mark.parametrize("change", [{"confirmed": False}, {"daemonSession": "old-session"}])
def test_safe_park_rejects_unconfirmed_or_stale_park_before_motion(target_rig, change):
    save_park(target_rig, **change)
    with pytest.raises(DriverError):
        asyncio.run(target_rig.arm.park())
    assert not motion(target_rig)


def test_safe_park_keeps_live_calibration_gate(target_rig):
    save_park(target_rig)

    async def failed(**kwargs):
        raise DriverError("driver_policy_rejected", "live geometry changed")

    target_rig.arm.live_calibration_gate = failed
    with pytest.raises(DriverError, match="live geometry changed"):
        asyncio.run(target_rig.arm.park())
    assert not motion(target_rig)


def test_safe_park_reports_unreached_endpoint(target_rig):
    rig = target_rig
    save_park(rig, parkZ=3.0)

    def lost_step(line):
        if line.startswith("XY "):
            rig.transport.position["x"] -= 1

    rig.transport.after_send = lost_step
    result = asyncio.run(rig.arm.park())
    assert result["ok"] is False and result["arrived"] is False
    assert result["tap_executed"] is False


@pytest.mark.parametrize("change", [{"parkX": None}, {"parkY": float("nan")},
                                    {"parkZ": float("inf")}])
def test_safe_park_rejects_missing_or_nonfinite_position_before_motion(target_rig, change):
    save_park(target_rig, **change)
    with pytest.raises(DriverError) as exc:
        asyncio.run(target_rig.arm.park())
    assert exc.value.motion_not_started is True
    assert not motion(target_rig)


def test_device_registration_refreshes_stale_saved_capability_list(tmp_path):
    from tests.test_stage_a_devices import make_manager

    manager = make_manager(tmp_path)
    config = {"device_id": "arm-01"}
    device = manager.register_device(config)
    device["capabilities"] = ["arm.home"]
    manager._store.save_device(device)
    refreshed = manager.register_device(config, replacing=True)
    assert "arm.park" in refreshed["capabilities"]
    assert "arm.park" in manager._store.get_device("arm-01")["capabilities"]
