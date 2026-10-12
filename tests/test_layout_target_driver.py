"""Actual daemon driver with in-process transport, tmp stores and synthetic frames."""
from __future__ import annotations

import asyncio
import copy
import json
import time

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb, screen_pose, ui_targets
from drivers.arm import DriverError
from drivers.daemon_arm import DaemonArmDriver
from drivers.daemon_protocol import DOUBLE_TAP_FEATURE
from runtime.layout_geometry import mapping_error_bound
from runtime.layout_matching import compute_page_check
from tests.test_layout_observation import LayoutRig, offline_only, png  # noqa: F401


class Transport:
    """No socket, subprocess or device access; retains the final gate ordering."""
    def __init__(self, rig):
        self.rig, self.commands, self.opened = rig, [], True
        self.position = dict(x=0.0, y=0.0, z=2.0)
        self.before_send = None
        self.after_send = None

    def is_open(self):
        return self.opened

    def close(self):
        self.opened = False

    def cmd(self, line, *, timeout=None, input_claim=None):
        if self.before_send:
            self.before_send(line)
        if input_claim:
            for key in ("geometry_gate", "callback"):
                if callable(input_claim.get(key)):
                    input_claim[key]()
        self.commands.append(line)
        parts = line.split()
        if parts[0] == "STATUS":
            result = {"deviceId": "arm-01", "connected": True, "session": "sess-A",
                      "motionFeatures": [DOUBLE_TAP_FEATURE]}
        elif parts[0] == "CLAIM":
            result = {"claimed": True, "lease_id": "offline"}
        elif parts[0] == "BEGIN_MOTION":
            result = {"tx": "offline"}
        elif parts[0] == "END_MOTION":
            result = {"ended": "offline"}
        elif parts[0] == "RELEASE":
            result = {"released": True}
        else:
            if parts[0] == "XY":
                self.position["x"] += float(parts[1])
                self.position["y"] += float(parts[2])
            elif parts[0] == "Z":
                self.position["z"] += float(parts[1])
            result = {**self.position, "state": "Idle"}
            if parts[0] == "TAP2":
                result.update(tap_count=2)
        if self.after_send:
            self.after_send(line)
        return json.dumps(result)


class Verifier:
    async def before(self, label):
        pass

    async def after(self, label):
        return {"verified": True}


@pytest.fixture
def target_rig(tmp_path, monkeypatch):
    rig = LayoutRig(tmp_path, monkeypatch)
    template = copy.deepcopy(rig.template)
    template["version"] = 2
    for page in template["pages"]:
        target_id = "wechat_chats_tab" if page["state"] == "wechat_list" else "back_button"
        page["targets"] = [{
            "id": target_id, "anchor_id": page["anchors"][0]["id"], "mode": "tap",
            "relative_rect": {"dx": 0, "dy": 0, "width": .1, "height": .05},
        }, {
            "id": "hover_spot", "anchor_id": page["anchors"][0]["id"], "mode": "hover_only",
            "relative_rect": {"dx": .1, "dy": .1, "width": .1, "height": .05},
        }]
        if page["state"] == "chat_ime_text":
            page["targets"].append({
                "id": "send_button", "anchor_id": "send_button_feature", "mode": "tap",
                "relative_rect": {"dx": 0, "dy": 0, "width": .1, "height": .05},
            })
    rig.adaptation["page_checks"] = [
        compute_page_check(page, png(rig.screens[page["state"]]),
            {a["id"]: rig.assets[a["asset_id"]] for a in page["anchors"]},
            ["ev-1", "ev-2"], mapping_error_bound_px=mapping_error_bound(rig.bundle))
        for page in template["pages"]]
    rig.template_version(template)
    rig.source.image = rig.frame("wechat_list")
    rig.arm = DaemonArmDriver("arm-01", data_root=rig.data)
    rig.transport = Transport(rig)
    rig.arm._transport, rig.arm._connected = rig.transport, True
    rig.arm.motion_verifier = Verifier()

    async def live_gate(**kwargs):
        return {"checks": [], "pose": copy.deepcopy(rig.pose)}

    rig.pose = screen_pose.fit_pose(rig.bundle["screen"]["edgeModel"]["corners"],
                                   rig.bundle["screen"]["edgeModel"]["corners"])
    rig.arm.live_calibration_gate = live_gate
    rig.runtime.devices._arms["arm-01"] = rig.arm
    rig.runtime.devices.set_layout_observation_provider(rig.runtime.layout_observation.resolve)

    def forbidden(*args, **kwargs):
        pytest.fail("Selected shared targets must not read the legacy archive")

    monkeypatch.setattr(ui_targets, "load_entry", forbidden)
    yield rig
    rig.close()


def motion(rig):
    return [cmd for cmd in rig.transport.commands if cmd.split()[0] in {"Z", "XY", "TAP", "TAP2"}]


def test_actual_driver_click_uses_shared_crop_without_legacy_registration(target_rig):
    rig = target_rig
    result = asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert result["target_source"] == "shared_layout"
    # Screen centre (32,32), inverse XY mapping; not source camera centre (132,82).
    assert result["resolved_robot_xy"] == pytest.approx([-6.8, -21])
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1
    assert not (rig.data / "devices/arm-01/ui_targets.json").exists()


def test_shared_navigation_accepts_motion_ready_without_reintroducing_input_acceptance(target_rig):
    rig = target_rig
    rig.bundle["status"] = cb.MOTION_READY_STATUS
    rig.bundle["acceptance"]["wechat"] = False
    cb.save_bundle(rig.bundle, rig.data)
    result = asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert result["target_source"] == "shared_layout"


def test_shared_fixed_target_ignores_external_ocr_coordinates(target_rig):
    result = asyncio.run(target_rig.arm.click({
        "target_id": "wechat_chats_tab", "target_ref": {
            "source": "camera_ocr", "cameraX": 999, "cameraY": 999}}))
    assert result["resolved_robot_xy"] == pytest.approx([-6.8, -21])


def test_phone_shift_is_compensated_exactly_once(target_rig):
    rig = target_rig
    matrix = np.float32([[1, 0, 7], [0, 1, 5]])
    rig.source.image = cv2.warpAffine(rig.source.image, matrix, (1280, 720),
                                     borderValue=(20, 20, 20))
    saved = rig.bundle["screen"]["edgeModel"]["corners"]
    rig.pose = screen_pose.fit_pose(saved, (np.asarray(saved) + [7, 5]).tolist())
    result = asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert result["resolved_robot_xy"] == pytest.approx([-6.1, -20.375])


def test_actual_driver_atomic_double_tap_uses_shared_target(target_rig):
    rig = target_rig
    result = asyncio.run(rig.arm.double_tap({"target_id": "wechat_chats_tab"}))
    assert result["tap_count"] == 2
    assert sum(cmd.startswith("TAP2 ") for cmd in motion(rig)) == 1
    assert not any(cmd.startswith("TAP ") for cmd in motion(rig))


def test_actual_driver_hover_has_no_contact(target_rig):
    rig = target_rig
    result = asyncio.run(rig.arm.move_target("hover_spot"))
    assert result["target_mode"] == "hover_only" and result["tap_executed"] is False
    assert motion(rig) and not any(cmd.startswith("TAP") for cmd in motion(rig))
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click({"target_id": "hover_spot"}))


@pytest.mark.parametrize("target_id", ["missing", "back_button", "send_button"])
def test_wrong_page_or_missing_target_never_falls_back(target_rig, target_id):
    with pytest.raises(DriverError):
        asyncio.run(target_rig.arm.click({"target_id": target_id}))
    assert motion(target_rig) == []


@pytest.mark.parametrize("ref", [
    {"template_version": 1}, {"adaptation_revision": 99},
    {"adaptation_revision": True},
    {"template_ref": {"id": "different", "version": 2, "sha256": "a" * 64}},
])
def test_old_request_version_never_retargets(target_rig, ref):
    with pytest.raises(DriverError):
        asyncio.run(target_rig.arm.click({"target_id": "wechat_chats_tab", "target_ref": ref}))
    assert motion(target_rig) == []


@pytest.mark.parametrize("fault", ["page", "binding", "calibration", "selection", "target"])
def test_change_after_travel_has_zero_contact_and_preserves_travel(target_rig, fault):
    rig = target_rig

    def after(line):
        if not line.startswith("XY "):
            return
        if fault == "page":
            rig.source.image = rig.frame("chat_ime_hidden")
        elif fault == "binding":
            rig.runtime.devices.get("arm-01")["binding_revision"] += 1
        elif fault == "calibration":
            changed = copy.deepcopy(rig.bundle)
            changed["xy"]["robotToScreen"]["a"] = 11
            cb.save_bundle(changed, rig.data)
        elif fault == "selection":
            updated = copy.deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 2))
            updated["version"] = 3
            rig.select(updated)
        else:
            rig.source.image[66:98, 116:148] = 240

    rig.transport.after_send = after
    with pytest.raises(DriverError) as caught:
        asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))
    assert getattr(caught.value, "motion_not_started", False) is False


def test_final_transport_lock_rechecks_binding_before_input_claim(target_rig):
    rig = target_rig
    claims = []

    def before(line):
        if line.startswith("TAP "):
            rig.runtime.devices.get("arm-01")["binding_revision"] += 1

    rig.transport.before_send = before
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab",
                                  "input_claim": {"callback": lambda: claims.append(True)}}))
    assert claims == []
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


def test_first_command_final_gate_failure_does_not_invent_travel(target_rig):
    rig = target_rig

    def before(line):
        if line.startswith("XY "):
            rig.runtime.devices.get("arm-01")["binding_revision"] += 1

    rig.transport.before_send = before
    with pytest.raises(DriverError) as caught:
        asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert motion(rig) == []
    assert caught.value.motion_not_started is True


def test_missing_live_pose_gate_never_allows_shared_motion(target_rig):
    rig = target_rig
    rig.arm.live_calibration_gate = None
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert motion(rig) == []


def test_fresh_recheck_after_slow_travel_does_not_use_expired_initial_frame(target_rig):
    rig = target_rig
    rig.transport.after_send = lambda line: time.sleep(1.05) if line.startswith("XY ") else None
    assert asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))["ok"] is True


@pytest.mark.parametrize("method", ["click", "double_tap", "move_target"])
def test_task_selection_changed_before_resolution_never_moves(target_rig, method):
    rig = target_rig
    expected = rig.runtime.layout_observation.execution_binding("arm-01")
    updated = copy.deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 2))
    updated["version"] = 3
    rig.select(updated)
    with pytest.raises(DriverError, match="版本"):
        if method == "move_target":
            asyncio.run(rig.arm.move_target("hover_spot", expected_layout=expected))
        else:
            asyncio.run(getattr(rig.arm, method)(
                {"target_id": "wechat_chats_tab", "_layout_binding": expected}))
    assert motion(rig) == []


def test_task_binding_persists_and_mutable_task_cannot_replace_it(target_rig):
    rig = target_rig
    task = {"task_id": "offline-layout-task", "device_id": "arm-01", "status": "running"}
    rig.runtime.store.save_task(task)
    expected = rig.runtime._pin_task_layout(rig.runtime.store.get_task(task["task_id"]), task)
    assert rig.runtime.store.get_task(task["task_id"])["layout_execution_binding"] == expected
    task["layout_execution_binding"] = None
    rig.runtime._save_task_progress(task)
    assert rig.runtime.store.get_task(task["task_id"])["layout_execution_binding"] == expected
    updated = copy.deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 2))
    updated["version"] = 3
    rig.select(updated)
    with pytest.raises(DriverError):
        rig.runtime._pin_task_layout(rig.runtime.store.get_task(task["task_id"]), task)
    assert motion(rig) == []


def test_unselected_task_cannot_acquire_new_template_mid_task(target_rig):
    with pytest.raises(DriverError):
        asyncio.run(target_rig.arm.click({"target_id": "wechat_chats_tab", "_layout_binding": None}))
    assert motion(target_rig) == []


def test_failed_target_does_not_block_another_target_in_same_page(target_rig):
    rig = target_rig
    template = copy.deepcopy(rig.parts["ops_svc"].resolve_template("offline-template", 2))
    template["version"] = 3
    page = template["pages"][0]
    page["targets"].append({
        "id": "too_small", "anchor_id": "wechat_chats_tab", "mode": "tap",
        "relative_rect": {"dx": .3, "dy": 0, "width": 4 / 320, "height": 4 / 640}})
    rig.adaptation["page_checks"][0] = compute_page_check(
        page, png(rig.screens["wechat_list"]),
        {a["id"]: rig.assets[a["asset_id"]] for a in page["anchors"]},
        ["ev-1", "ev-2"], mapping_error_bound_px=mapping_error_bound(rig.bundle))
    rig.template_version(template)
    result = asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert result["ok"] is True
    count = len(motion(rig))
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click({"target_id": "too_small"}))
    assert len(motion(rig)) == count


def test_safe_box_check_rejects_a_plan_outside_the_current_target(target_rig):
    rig = target_rig
    frame = asyncio.run(rig.camera._selected_layout_frame())
    with pytest.raises(ValueError, match="安全区"):
        frame.verify_target_point("wechat_chats_tab", {"robotX": 0, "robotY": 0}, rig.bundle)


def test_current_send_target_still_requires_input_switch(target_rig):
    rig = target_rig
    rig.screens["chat_ime_text"][532:548, 40:80] = 30
    rig.source.image = rig.frame("chat_ime_text")
    with pytest.raises(DriverError) as caught:
        asyncio.run(rig.arm.click({"target_id": "send_button"}))
    assert caught.value.category == "input_switch_disabled"
    assert motion(rig) == []


def test_invalid_selected_archive_prevents_legacy_target_fallback(target_rig):
    rig = target_rig
    (rig.data / "devices/arm-01/layout_adaptation/versions/2.json").unlink()
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert motion(rig) == []


def test_reported_position_outside_live_safety_box_never_contacts(target_rig):
    rig = target_rig

    def after(line):
        if line.startswith("XY "):
            rig.transport.position["x"] += 3

    rig.transport.after_send = after
    with pytest.raises(DriverError, match="安全区"):
        asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


def test_skipped_tiny_xy_still_checks_actual_position(target_rig):
    rig = target_rig
    template = copy.deepcopy(rig.parts["ops_svc"].resolve_template("offline-template", 2))
    template["version"] = 3
    page = template["pages"][0]
    page["targets"][0]["relative_rect"].update(width=5 / 320, height=5 / 640)
    rig.bundle["xy"]["robotToScreen"]["a"] = 20
    cb.save_bundle(rig.bundle, rig.data)
    rig.adaptation["page_checks"][0] = compute_page_check(
        page, png(rig.screens["wechat_list"]),
        {a["id"]: rig.assets[a["asset_id"]] for a in page["anchors"]},
        ["ev-1", "ev-2"], mapping_error_bound_px=mapping_error_bound(rig.bundle))
    rig.template_version(template)
    rig.transport.position.update(x=(19.3 - 100) / 20, y=(18.5 - 200) / 8)
    with pytest.raises(DriverError, match="安全区"):
        asyncio.run(rig.arm.click({"target_id": "wechat_chats_tab"}))
    assert motion(rig) == []


@pytest.mark.parametrize("method", ["click", "double_tap"])
def test_switch_off_at_transport_boundary_prevents_send_contact(target_rig, monkeypatch, method):
    rig = target_rig
    rig.screens["chat_ime_text"][532:548, 40:80] = 30
    rig.source.image = rig.frame("chat_ime_text")
    enabled = [True]
    monkeypatch.setattr(rig.arm, "_input_switch_allowed", lambda bundle: enabled[0])

    def before(line):
        if line.startswith("TAP"):
            enabled[0] = False

    rig.transport.before_send = before
    with pytest.raises(DriverError) as caught:
        asyncio.run(getattr(rig.arm, method)({"target_id": "send_button"}))
    assert caught.value.category == "input_switch_disabled"
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))
