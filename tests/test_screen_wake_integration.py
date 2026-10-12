"""Real resolver, driver and calibration gate behind an isolated local fake daemon."""
import asyncio
from copy import deepcopy
import hashlib

import pytest

from drivers import calibration_bundle as cb, screen_gestures as sg
from drivers.daemon_arm import DaemonArmDriver
from drivers.wake_screen import resolve_wake_target
from runtime.calibration_gate import check_live_calibration
from tests.helpers import make_device_configs, make_runtime, wait_terminal
from tests.test_daemon_arm import FakeDaemon
from tests.test_screen_gesture_driver import gesture_bundle
from tests.test_screen_wake_contract import wake_request, validator
from tests.test_screen_wake_driver import validated_bundle, wake_frame
from tests.test_screen_wake_runtime import PLUGIN_DIR


@pytest.mark.parametrize("scenario,status,effect", [
    ("wake", "confirmed", "awake"),
    # W04/§9.4：双击有完整控制器确认、之后仍暗屏——Fleet 确认机械双击已完成，
    # 仅亮屏效果 unverified；WX 侧保留等待屏障（不重试、不重放）。
    ("still_dark", "confirmed", "unverified"),
    ("already_lit", "failed", "unverified"),
    ("frozen", "failed", "unverified"),
    ("lost_reply", "unknown", "unverified"),
])
def test_fleet_wake_end_to_end_keeps_calibration_and_never_replays(
        tmp_path, scenario, status, effect):
    async def run():
        runtime = make_runtime(tmp_path)
        runtime.capabilities.bootstrap()
        await runtime.devices.register_all(make_device_configs())
        daemon = FakeDaemon(mpos=(-52, 49, -8)).start()
        root = tmp_path / "data"
        driver = DaemonArmDriver("arm-01", daemon_addr=f"127.0.0.1:{daemon.port}", data_root=root)
        bundle = validated_bundle(gesture_bundle())
        bundle["daemonAddr"] = driver.daemon_addr
        bundle["bindingFingerprint"] = cb.binding_fingerprint(
            device_id="arm-01", camera_id=bundle["cameraId"],
            camera_device_unique_id=bundle["cameraDeviceUniqueId"],
            daemon_addr=driver.daemon_addr,
            frame_width=bundle["frame"]["width"], frame_height=bundle["frame"]["height"])
        cb.save_bundle(bundle, root)
        binding = resolve_wake_target(bundle)["binding"]

        class Frames:
            sequence = 0

            def capture_sync(self):
                self.sequence += 1
                lit = scenario == "already_lit" or (
                    scenario == "wake" and any(cmd.startswith("TAP2 ") for cmd in daemon.commands))
                frame = wake_frame(bundle, lit=lit, sequence=self.sequence)
                frame.update(image=frame["image_bytes"], captured_at=frame["source_captured_at"],
                             sha256=hashlib.sha256(frame["image_bytes"]).hexdigest())
                frame["frozen"] = scenario == "frozen"
                return frame

        try:
            await driver.connect()
            runtime.devices._arms["arm-01"] = driver
            runtime.devices._cameras["arm-01"] = Frames()
            device = runtime.devices.get("arm-01")
            device.update(binding_mode="real", daemon_addr=driver.daemon_addr,
                          daemon_session=binding["daemon_session"],
                          camera_id=binding["camera_id"],
                          camera_device_unique_id=binding["camera_device_unique_id"],
                          camera_binding_revision=binding["binding_revision"],
                          expected_frame_width=binding["frame_width"],
                          expected_frame_height=binding["frame_height"],
                          camera_quality="waiting", camera_frame_age_ms=0)

            async def gate():
                state = await driver.read_state()
                return await check_live_calibration(
                    device_id="arm-01", camera_id=binding["camera_id"],
                    daemon_addr=driver.daemon_addr, daemon_session=state["session"],
                    estop_locked=bool(state.get("estop_latched") or state.get("daemon_estop_locked")),
                    camera=runtime.devices.camera_broker("arm-01"),
                    bundle=cb.load_bundle("arm-01", root), screen_wake=True)

            driver.screen_wake_gate = gate
            runtime.plugins.install(PLUGIN_DIR, "tester", "isolated integration")
            runtime.plugins.activate("screen-wake-adapter", "tester", "isolated integration")
            if scenario == "lost_reply":
                daemon.response_hook = lambda cmd, reply, _: None if cmd.startswith("TAP2 ") else reply
            baseline = cb.bundle_path("arm-01", root).read_bytes()
            request = wake_request(payload={"binding": binding, "confirmed": True})
            task = runtime.submit_task(request, source="console", context={"operator_id": "tester"})
            await runtime._dispatch_once()
            result = await wait_terminal(runtime, task["task_id"], timeout=15)
            assert result["status"] == status, result
            assert result["effect_status"] == effect
            done = [entry for entry in result["step_log"] if entry["phase"] == "done"]
            assert len(done) == 1
            validator("resultDetail").validate(done[0]["detail"])
            assert done[0]["detail"]["binding"] == binding
            assert done[0]["detail"]["before_evidence"]["evidence_id"] in result["evidence_ids"]
            if scenario in {"wake", "still_dark"}:
                assert done[0]["detail"]["after_evidence"]["evidence_id"] in result["evidence_ids"]
                assert done[0]["detail"]["frame_observations"]
                assert daemon.mpos[2] == bundle["z"]["hoverZ"]
            if status == "failed":
                assert result["motion_not_started"] is True
                assert daemon.commands == []
            else:
                assert len([cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")]) == 1
            assert device["status"] == ("paused" if status == "unknown" else "ready")
            assert device["calibration_status"] == "validated"
            assert device["home_ok"] is (status != "unknown")
            assert runtime.leases.get("arm-01") is None
            assert cb.bundle_path("arm-01", root).read_bytes() == baseline
            before = deepcopy(daemon.commands)
            runtime.submit_task(request, source="console", context={})
            await runtime._dispatch_once()
            assert daemon.commands == before
            assert not any(cmd.startswith(("SWIPE", "PATH1", "$H")) for cmd in daemon.commands)
            if scenario == "lost_reply":
                assert daemon.commands[-1].startswith("TAP2 ")
        finally:
            await driver.disconnect()
            daemon.stop()

    asyncio.run(run())
