"""Automatic promotion must preserve only this run's Z confirmation evidence."""
from copy import deepcopy
import socket

import pytest

from drivers import calibration_bundle as cb, calibration_wizard as cw
from tests.test_calibration_wizard import (
    auto_ready_env, finish_e_acceptance, wait_auto,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Z provenance tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)


@pytest.fixture
def env(tmp_path):
    rig = auto_ready_env(tmp_path)
    base = cb.load_bundle("arm-01", tmp_path)
    base["z"].update({
        "zSafe": 2.0, "hoverZ": 2.0, "tapDepth": 1.0, "tapFeed": 1000.0,
        "source": "old_confirmation", "daemonSession": "previous-session",
        "confirmedAt": "2000-01-01T00:00:00",
        "validatedAt": "2000-01-01T00:00:01",
        "mposAtConfirm": {"x": 99.0, "y": 98.0, "z": 2.0},
        "report": {"sampleId": "old-touch", "screenX": 99.0, "screenY": 98.0},
        "validationError": "obsolete confirmation error",
        "validated": True, "deleteValidated": True,
    })
    cb.save_bundle(base, tmp_path)
    try:
        yield rig
    finally:
        rig.wizard.auto_stop()
        thread = rig.wizard._auto_runner._auto["thread"]
        if thread is not None:
            thread.join(timeout=2)
            assert not thread.is_alive()
        rig.wizard._watcher_gen += 1


def promote(env, *, deferred=True):
    env.wizard.auto_start(defer_input_acceptance=deferred)
    if not deferred:
        view = wait_auto(env, lambda value: value["phase"] == "E_ACCEPTANCE"
                         or value["state"] not in cw.AUTO_ACTIVE_STATES)
        assert view["phase"] == "E_ACCEPTANCE", view
        finish_e_acceptance(env, view["runId"])
    view = wait_auto(env, lambda value: value["state"] not in cw.AUTO_ACTIVE_STATES)
    assert view["state"] == cw.AUTO_STATE_PROMOTED, view
    return view


def gate(env, bundle, **scope):
    binding = env.wizard.state["binding"]
    return cb.validate_calibration_bundle(
        bundle, device_id="arm-01", daemon_session=env.daemon.session,
        camera_id=binding["cameraId"],
        camera_device_unique_id=binding["cameraDeviceUniqueId"],
        binding_revision=binding["bindingRevision"],
        daemon_addr=binding["daemonAddr"],
        frame_width=binding["frameWidth"], frame_height=binding["frameHeight"],
        **scope)


@pytest.mark.parametrize("deferred", [True, False], ids=["motion-ready", "ready"])
def test_promotion_replaces_old_z_confirmation_evidence(env, tmp_path, deferred):
    confirmation = deepcopy(env.wizard.state["z"]["autoSafe"])
    view = promote(env, deferred=deferred)
    promoted = cb.load_bundle("arm-01", tmp_path)
    run = cb.load_candidate("arm-01", view["runId"], tmp_path)
    assert run["z"] == confirmation
    for key, value in confirmation.items():
        assert promoted["z"][key] == value, key
    assert promoted["z"]["hoverZ"] == confirmation["zSafe"]
    assert promoted["z"]["tapDwellMs"] == 0
    assert promoted["z"]["deleteHoldMs"] == 2000
    assert promoted["z"]["deleteDepth"] == confirmation["tapDepth"]
    assert promoted["z"]["deleteFeed"] == confirmation["tapFeed"]
    assert promoted["z"]["deleteValidated"] is (not deferred)
    assert promoted["status"] == (cb.MOTION_READY_STATUS if deferred else "ready")
    assert gate(env, promoted) == []


def test_old_run_z_session_cannot_promote_or_overwrite_active_bundle(env, tmp_path):
    env.wizard.state["z"]["autoSafe"]["daemonSession"] = "previous-session"
    path = cb.bundle_path("arm-01", tmp_path)
    before = path.read_bytes()
    env.wizard.auto_start(defer_input_acceptance=True)
    view = wait_auto(env, lambda value: value["state"] not in cw.AUTO_ACTIVE_STATES)
    assert view["state"] == cw.AUTO_STATE_FAILED, view
    assert view["lastFailure"]["code"] == "promote_motion_invalid"
    run = cb.load_candidate("arm-01", view["runId"], tmp_path)
    assert run["validation"]["passed"] is True
    assert run["z"]["daemonSession"] != run["daemonSession"]
    assert path.read_bytes() == before
    assert env.ime.tasks_created == []
    assert not (path.parent / "gesture_coordinate_frame.json").exists()


def test_fresh_z_provenance_does_not_grant_input_acceptance(env, tmp_path):
    promote(env)
    promoted = cb.load_bundle("arm-01", tmp_path)
    assert promoted["status"] == cb.MOTION_READY_STATUS
    assert promoted["acceptance"]["debug"] is False
    assert promoted["acceptance"]["deferred"]["debug"] is True
    assert promoted["z"]["deleteValidated"] is False
    assert env.ime.tasks_created == []
    assert gate(env, promoted) == []
    # §4.6（2026-10-03）：输入/发送 scope 不再由 E 验收拒绝——新鲜 Z 溯源
    # 提供的是机械证据；输入资格由「允许输入与发送」开关层负责。
    for scope in ("require_input_acceptance", "wechat_send"):
        codes = {check["code"] for check in gate(env, promoted, **{scope: True})}
        assert "input_acceptance_deferred" not in codes
        assert "input_acceptance_missing" not in codes
        assert "delete_parameters_not_validated" not in codes
    with pytest.raises(cw.WizardError, match="未完成"):
        env.wizard.finish()


def test_absent_run_metadata_does_not_fall_back_to_old_confirmation(env, tmp_path):
    env.wizard.state["z"]["autoSafe"].pop("validationError")
    promote(env)
    promoted = cb.load_bundle("arm-01", tmp_path)
    assert "validationError" not in promoted["z"]
    assert promoted["z"]["daemonSession"] == env.daemon.session
    assert gate(env, promoted) == []
