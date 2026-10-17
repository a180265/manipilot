"""Real geometry nonce → real driver → fake daemon with a nonzero root origin."""
import asyncio
import json

import pytest

from drivers import calibration_bundle as cb, gesture_program as gp
from drivers.arm import DriverError
from runtime.ime_geometry import ImeGeometryError
from tests.test_r2_live_geometry_driver import (ARM_DEVICE, live_env, seed_coordinate_frame,
                                               LiveGeometryEnv, IME_DEVICE,
                                               CANDIDATE_CENTER, DELETE_CENTER)  # noqa: F401
from tests.test_region_qualified_clicks import region_driver


def offset_frame(env):
    bundle = cb.load_bundle(ARM_DEVICE, env.driver.data_root)
    bundle["screen"]["screenHeight"] = 2320
    cb.save_bundle(bundle, env.driver.data_root)
    return seed_coordinate_frame(env.driver.data_root, origin=(0, 80))


@pytest.mark.parametrize("target,center", [("ime_candidate_bar", CANDIDATE_CENTER),
                                            ("ime_delete_key", DELETE_CENTER)])
@pytest.mark.parametrize("region", [False, True])
def test_display_origin_is_subtracted_exactly_once(live_env, monkeypatch, target, center, region):
    env = live_env
    offset_frame(env)
    if region:
        region_driver(env.driver, monkeypatch)
    async def scenario():
        env.start_phone()
        ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, target)
        original = json.loads(json.dumps(ref))
        gate = env.runtime._geometry_gates[ref["nonce"]]
        result = await env.driver.click({"target_id": target, "target_ref": ref,
                                         "input_claim": {"geometry_gate": gate}})
        assert result["resolved_robot_xy"] == pytest.approx(
            [round((center[0]-2)/18, 3), round((center[1]-80-3)/18, 3)])
        assert result["click_point"]["screen"] == [center[0], center[1]-80]
        assert ref == original
        assert len(env.tap_commands()) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["missing", "binding", "hash", "run", "internal",
                                  "display", "rotation", "density", "density_nan", "density_zero"])
def test_missing_or_incompatible_frame_never_moves(live_env, fault):
    env = live_env
    frame = offset_frame(env)
    path = gp.coordinate_frame_path(ARM_DEVICE, env.driver.data_root)
    if fault == "missing":
        path.unlink()
    else:
        if fault == "binding": frame["binding"]["daemon_session"] = "other"
        if fault == "run": frame["calibration_run_id"] = "another-run"
        if fault == "internal": frame = gp.build_calibration_coordinate_frame(cb.load_bundle(ARM_DEVICE, env.driver.data_root))
        if fault == "display": frame["display_height_px"] = 2500
        if fault == "rotation": frame["rotation"] = 90
        if fault == "density": frame["density"] = 3.0
        if fault == "density_nan": frame["density"] = float("nan")
        if fault == "density_zero": frame["density"] = 0
        frame["coordinate_frame_hash"] = ("a"*64 if fault in ("hash", "density_nan")
                                           else gp.coordinate_frame_hash(frame))
        path.write_text(json.dumps(frame))
    async def scenario():
        env.start_phone()
        ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, "ime_candidate_bar")
        with pytest.raises(DriverError) as caught:
            await env.driver.click({"target_id": "ime_candidate_bar", "target_ref": ref})
        assert caught.value.motion_not_started is True
        assert not any(command.startswith(("XY ", "TAP ")) for command in env.daemon.commands)
    asyncio.run(scenario())


@pytest.mark.parametrize("key,value", [("display_id", 1), ("rotation", 180), ("density", 3.0)])
def test_final_gate_rejects_same_size_other_display_identity(live_env, key, value):
    env = live_env
    async def scenario():
        env.start_phone()
        ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, "ime_candidate_bar")
        expected = dict(ref["display"], **{key: value})
        with pytest.raises(ImeGeometryError, match="display"):
            env.geometry.consume_for_click(
                IME_DEVICE, ref["nonce"], target_id="ime_candidate_bar",
                expected_task="t-r2-10-ime", expected_session=ref["runtimeSessionId"],
                expected_display=expected)
        # A rejected identity check must leave the nonce available for its
        # original authenticated snapshot, which still has the same dimensions.
        env.geometry.consume_for_click(
            IME_DEVICE, ref["nonce"], target_id="ime_candidate_bar",
            expected_task="t-r2-10-ime", expected_session=ref["runtimeSessionId"],
            expected_display=ref["display"])
    asyncio.run(scenario())


def test_frame_changes_after_travel_block_contact_before_nonce(live_env, monkeypatch):
    env = live_env
    offset_frame(env)
    consumed = []
    original_consume = env.geometry.consume_for_click
    def consume(*args, **kwargs):
        consumed.append(args[1])
        return original_consume(*args, **kwargs)
    monkeypatch.setattr(env.geometry, "consume_for_click", consume)
    def after_move(command, reply, _conn):
        if command.startswith("XY "):
            path = gp.coordinate_frame_path(ARM_DEVICE, env.driver.data_root)
            frame = json.loads(path.read_text())
            frame["source_client_instance_id"] = "changed"
            frame["coordinate_frame_hash"] = gp.coordinate_frame_hash(frame)
            path.write_text(json.dumps(frame))
        return reply
    env.daemon.response_hook = after_move
    async def scenario():
        env.start_phone()
        ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, "ime_candidate_bar")
        gate = env.runtime._geometry_gates[ref["nonce"]]
        with pytest.raises(DriverError, match="坐标桥") as caught:
            await env.driver.click({"target_id": "ime_candidate_bar", "target_ref": ref,
                                     "input_claim": {"geometry_gate": gate}})
        assert getattr(caught.value, "motion_not_started", False) is not True
        assert any(command.startswith("XY ") for command in env.daemon.commands)
        assert env.tap_commands() == []
        assert consumed == []
    asyncio.run(scenario())


@pytest.mark.parametrize("binding_changes", [False, True])
def test_ip_deployment_live_geometry_to_contact_without_token(tmp_path, monkeypatch, binding_changes):
    env = LiveGeometryEnv(tmp_path, paired_deployment=False)
    try:
        offset_frame(env)
        if binding_changes:
            def after_move(command, reply, _conn):
                if command.startswith("XY "):
                    monkeypatch.setattr(env.runtime.devices, "input_binding",
                                        lambda _: {"ime_device_id": "another-phone"})
                return reply
            env.daemon.response_hook = after_move

        async def scenario():
            env.start_phone()
            ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, "ime_candidate_bar")
            gate = env.runtime._geometry_gates[ref["nonce"]]
            target = {"target_id": "ime_candidate_bar", "target_ref": ref,
                      "input_claim": {"geometry_gate": gate}}
            if binding_changes:
                with pytest.raises(DriverError, match="绑定"):
                    await env.driver.click(target)
                assert env.tap_commands() == []
            else:
                result = await env.driver.click(target)
                assert result["click_point"]["screen"] == [540, 2140]
                assert len(env.tap_commands()) == 1
        asyncio.run(scenario())
    finally:
        env.close()
