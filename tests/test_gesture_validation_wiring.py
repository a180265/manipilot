"""NAV07: real Plugin -> Runner -> capability publish -> eligibility/resolver.

Only phone, arm and camera boundaries are faked. All files live in tmp_path;
the plugin is loaded from a separate installation, never patched DATA_ROOT.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import cv2
import numpy as np
import pytest

from adapters.screen_ime import gesture_capture
from drivers import calibration_bundle as cb, gesture_program as gp
from drivers.fake_arm import FakeArmDriver
from drivers.fake_camera import FakeCameraDriver
from runtime import automatic_gestures as ag
from runtime.util import iso, utc_now
from tests.helpers import make_device_configs, make_runtime, wait_terminal
from tests.test_gesture_plugins import (
    VALIDATION_SOURCE, calibration_fixture, last_detail, load_manifest,
    load_module, self_test_body,
)
from tests.test_gesture_validation_run import (
    FakeCoordinator as BaseCoordinator, good_trace_for,
)


class FakeCoordinator(BaseCoordinator):
    def __init__(self, defaults):
        super().__init__(lambda _: good_trace_for(
            defaults["scroll_self_test"]["candidates"][0]))
        self.ready_payload["systemGestureInsets"] = {
            "left": 20, "top": 0, "right": 20, "bottom": 30}
        self.ready_trials = []
        self.ready_limit = None
        self.active_trial = None
        self.camera = None

    async def wait_ready(self, session_id, trial_id, timeout_ms):
        if self.ready_limit is not None and len(self.ready_trials) >= self.ready_limit:
            return {"ready": False}
        assert trial_id not in self.ready_trials
        self.ready_trials.append(trial_id)
        self.camera.png = self.camera.fixture_png
        return await super().wait_ready(session_id, trial_id, timeout_ms)

    async def mark_executing(self, session_id, trial_id):
        assert trial_id in self.ready_trials
        self.active_trial = trial_id
        await super().mark_executing(session_id, trial_id)


class FakeDriver(FakeArmDriver):
    def __init__(self, device_id, *, root, coordinator, session):
        super().__init__(device_id, move_seconds=0)
        self.data_root = root
        self.coordinator = coordinator
        self.session = session
        self.swipes = []
        self.after_swipe = None

    async def connect(self):
        return {**await super().connect(), "session": self.session}

    async def read_state(self):
        return {**await super().read_state(), "session": self.session}

    async def swipe(self, gesture):
        trial = self.coordinator.active_trial
        assert trial in self.coordinator.ready_trials
        assert trial not in [item[0] for item in self.swipes]
        self.swipes.append((trial, deepcopy(gesture)))
        if self.coordinator.prepared[-1]["kind"] == "prepare_home":
            self.coordinator.camera.png = self.coordinator.camera.launcher_png
        elif self.coordinator.prepared[-1]["kind"] == "reset_list":
            self.coordinator.camera.png = self.coordinator.camera.shifted_fixture_png
        if self.after_swipe:
            self.after_swipe()
        return await super().swipe(gesture)


class FakeCamera(FakeCameraDriver):
    # 校准档案（gesture_bundle）的手机四角：相机域 (100,100)-(1200,600)。
    # 手机内部才是可追踪内容；外部为静态暗背景，保证定位/去姿态链路能
    # 找到手机边界（与生产 measure_scroll 同语义）。
    PHONE = (100, 100, 1200, 600)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        rng = np.random.default_rng(7)
        launcher = self._phone_content(rng)
        self.launcher_png = cv2.imencode(".png", launcher)[1].tobytes()
        rng = np.random.default_rng(8)
        coarse = cv2.resize(
            rng.integers(0, 256, (16, 27), np.uint8), (1280, 720),
            interpolation=cv2.INTER_CUBIC)
        fine = cv2.resize(
            rng.integers(0, 256, (45, 80), np.uint8), (1280, 720),
            interpolation=cv2.INTER_CUBIC)
        fixture = self._compose(cv2.addWeighted(coarse, 0.8, fine, 0.2, 0))
        # In the calibrated rig, camera +X maps to screen -Y. Move the same
        # visual features by 40 camera pixels to exercise real optical flow —
        # 内容在手机内部移动，手机边界与背景不动（不把整机位移算进滚动）。
        x0, y0, x1, y1 = self.PHONE
        interior = cv2.warpAffine(
            fixture[y0:y1, x0:x1], np.float32([[1, 0, 40], [0, 1, 0]]),
            (x1 - x0, y1 - y0), borderMode=cv2.BORDER_REFLECT)
        shifted = fixture.copy()
        shifted[y0:y1, x0:x1] = interior
        self.fixture_png = cv2.imencode(".png", fixture)[1].tobytes()
        self.shifted_fixture_png = cv2.imencode(".png", shifted)[1].tobytes()
        self.png = self.launcher_png
        self.seq = 0
        self.timestamp = None

    def _phone_content(self, rng):
        return self._compose(rng.integers(0, 256, (720, 1280), np.uint8))

    def _compose(self, content):
        frame = np.full((720, 1280), 38, np.uint8)
        x0, y0, x1, y1 = self.PHONE
        frame[y0:y1, x0:x1] = content[y0:y1, x0:x1]
        return frame

    def capture_sync(self):
        self.seq += 1
        return {"image": self.png, "sha256": hashlib.sha256(self.png).hexdigest(),
                "width": 1280, "height": 720,
                "captured_at": self.timestamp or iso(utc_now()),
                "frame_seq": self.seq}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # Relocate exactly the shipped plugin, keeping its installation data empty.
    # The unfixed plugin therefore fails safely without touching field data.
    install = tmp_path / "installation"
    source = install / "examples/plugins/gesture-validation-adapter"
    source.mkdir(parents=True)
    shutil.copy2(VALIDATION_SOURCE / "plugin.py", source / "plugin.py")
    (install / "configs").mkdir()
    shutil.copy2(VALIDATION_SOURCE.parents[2] / "configs/gesture_defaults.json",
                 install / "configs/gesture_defaults.json")
    module = load_module("nav07_installed_selftest", source)
    root = tmp_path / "device-runtime-data"
    bundle, frame = calibration_fixture()
    cb.save_bundle(bundle, root)
    gp.save_coordinate_frame("arm-01", frame, data_root=root)
    # 开关已删（2026-09-30 拍板）：可用性由校准与证书决定
    coordinator = FakeCoordinator(module.load_defaults())
    monkeypatch.setattr(gesture_capture, "_GLOBAL_COORDINATOR", coordinator)
    from runtime import devices
    monkeypatch.setattr(devices, "FakeArmDriver", lambda device_id, **kw: FakeDriver(
        device_id, root=root, coordinator=coordinator, session=bundle["daemonSession"]))
    monkeypatch.setattr(devices, "FakeCameraDriver", FakeCamera)
    runtime = make_runtime(tmp_path, plugin=module.Plugin(),
                           manifest=load_manifest(VALIDATION_SOURCE))
    return runtime, coordinator, root, bundle, frame, install


async def start(rig):
    runtime, coordinator, _, bundle, _, _ = rig
    from drivers.screen_gestures import binding_snapshot
    binding = binding_snapshot(bundle)
    await runtime.startup(device_configs=make_device_configs(
        1, camera_device_unique_id=binding["camera_device_unique_id"],
        binding_revision=binding["binding_revision"]))
    coordinator.camera = runtime.devices.camera("arm-01")


async def submit(rig, actions):
    runtime = rig[0]
    task = runtime.submit_task(self_test_body(actions), source="console",
                               context={"operator_id": "测试", "reason": "NAV07隔离装配"})
    return await wait_terminal(runtime, task["task_id"])


def assert_published(rig, done, gesture_id, navigation_action):
    runtime, coordinator, root, bundle, frame, install = rig
    detail = last_detail(done)
    assert done["status"] == "confirmed", (done.get("failure_reason"),
                                            done.get("uncertainty_reason"), detail)
    assert detail["run_state"] == "passed"
    count = 3 * len(done["payload"]["actions"])
    assert detail["budget"]["physical_count"] == count
    assert len(coordinator.ready_trials) == count
    driver = runtime.devices.driver("arm-01")
    assert len(driver.swipes) == count
    run_path = root / "devices/arm-01/gesture_validation_runs" / f'{detail["run_id"]}.json'
    run = json.loads(run_path.read_text())
    certificate = ag.load_profile("arm-01", root)["actions"][gesture_id]
    action = next(row for row in run["action_results"] if row["gesture_id"] == gesture_id)
    if gesture_id == "scroll_up":
        observations = [row for row in run["scroll_observations"]
                        if row["action"] == gesture_id]
        assert len(observations) == 3
        for row in observations:
            assert row["before_evidence_id"] != row["after_evidence_id"]
            assert row["classification"] == "trial_passed", row
            measured = row["camera_observation"]
            assert measured["status"] == "displaced", measured
            assert measured["dy_px"] < 0, measured
    assert certificate["program"] == action["program"]
    assert certificate["program_hash"] == gp.program_hash(certificate["program"])
    points = certificate["program"]["strokes"][0]["points"]
    swipes = [gesture for trial, gesture in driver.swipes if trial in action["trial_ids"]]
    assert len(swipes) == 3
    for gesture in swipes:
        assert gesture["start"]["x"] == points[0]["x"]
        assert gesture["start"]["y"] == points[0]["y"]
        assert gesture["end"]["x"] == points[-1]["x"]
        assert gesture["end"]["y"] == points[-1]["y"]
    view = ag.gestures_view("arm-01", bundle=bundle, frame=frame, data_root=root)
    assert view["actions"][gesture_id]["usable"], view
    resolved = ag.resolve_navigation_action(
        "arm-01", {"profile_id": ag.NAVIGATION_PROFILE_ID, "version": 1,
                   "contract_version": ag.CONTRACT_VERSION}, view["binding"],
        navigation_action, bundle=bundle, frame=frame, data_root=root,
        evidence_root=runtime.config.evidence_dir, evidence_get=runtime.evidence.get)
    assert resolved["program"] == certificate["program"]
    assert not (install / "data/devices/arm-01/gesture_validation_runs").exists()
    assert not (runtime.config.root / "data/devices/arm-01/gesture_validation_runs").exists()


def test_scroll_uses_context_root_and_publishes_executed_program(rig):
    async def run():
        await start(rig)
        try:
            done = await submit(rig, ["scroll_up"])
            assert_published(rig, done, "scroll_up", "search_next")
        finally:
            await rig[0].shutdown()
    asyncio.run(run())


def test_scroll_measures_frames_captured_during_motion(rig, monkeypatch):
    async def run():
        await start(rig)
        runtime = rig[0]
        driver = runtime.devices.driver("arm-01")
        original_swipe = driver.swipe
        original_invoke = runtime._invoke_gesture_capability
        measurements = []

        async def delayed_swipe(gesture):
            result = await original_swipe(gesture)
            await asyncio.sleep(0.30)
            return result

        async def observe(plugin_id, task, capability_id, params, record):
            if capability_id == "camera.measure_scroll" and params.get("op") == "measure":
                measurements.append(deepcopy(params))
            return await original_invoke(plugin_id, task, capability_id, params, record)

        monkeypatch.setattr(driver, "swipe", delayed_swipe)
        monkeypatch.setattr(runtime, "_invoke_gesture_capability", observe)
        try:
            done = await submit(rig, ["scroll_up"])
            assert_published(rig, done, "scroll_up", "search_next")
            assert len(measurements) == 3
            for params in measurements:
                ids = params.get("evidence_ids", [])
                assert len(ids) > 2, "Only endpoint frames cannot track full-page scrolling"
                assert ids[0] == params["before_evidence_id"]
                assert ids[-1] == params["after_evidence_id"]
                assert len(ids) == len(set(ids))
                records = [runtime.evidence.get(evidence_id) for evidence_id in ids]
                assert all(record["task_id"] == done["task_id"] for record in records)
                assert any(record["label"] == "自检滑动期间取证" for record in records)
        finally:
            await runtime.shutdown()
    asyncio.run(run())


def test_scroll_capture_failure_preserves_one_motion_and_no_certificate(rig, monkeypatch):
    async def run():
        await start(rig)
        runtime = rig[0]
        driver = runtime.devices.driver("arm-01")
        original_swipe = driver.swipe
        original_capture = runtime.plugin_capture_evidence

        async def delayed_swipe(gesture):
            result = await original_swipe(gesture)
            await asyncio.sleep(0.15)
            return result

        async def capture(plugin_id, task, label):
            if label == "自检滑动期间取证":
                raise OSError("camera disconnected during motion")
            return await original_capture(plugin_id, task, label)

        monkeypatch.setattr(driver, "swipe", delayed_swipe)
        monkeypatch.setattr(runtime, "plugin_capture_evidence", capture)
        try:
            done = await submit(rig, ["scroll_up", "scroll_down"])
            assert done["status"] == "unknown"
            assert len(driver.swipes) == 1
            detail = last_detail(done)
            assert detail["budget"]["physical_count"] == 1
            assert detail["contact_status"] == "continuous"
            assert detail["effect_status"] == "unknown"
            profile = ag.load_profile("arm-01", rig[2])
            assert profile["actions"]["scroll_up"]["state"] == "unverified"
        finally:
            await runtime.shutdown()
    asyncio.run(run())


async def register_launcher(rig):
    from adapters.uinav.navigation_templates import NavigationTemplates
    templates = NavigationTemplates(rig[0], "arm-01")
    preview = await templates.preview({"kind": "launcher"}, "测试")
    return templates.register("launcher", {
        "preview_id": preview["preview_id"], "expected_version": 0,
        "confirmed": True, "restore_target": None,
        "anchor_regions": [{"left_px": 100 + index * 250, "top_px": 400,
                            "width_px": 100, "height_px": 100}
                           for index in range(3)],
    })


@pytest.mark.parametrize("fault", ["source_owner", "crop_owner", "crop_path", "source_missing"])
@pytest.mark.parametrize("when", ["before_submit", "after_ready", "after_capture"])
def test_home_checks_authoritative_evidence_before_motion(rig, monkeypatch, fault, when):
    async def run():
        runtime, coordinator, root, *_ = rig
        await start(rig)
        try:
            template = await register_launcher(rig)
            anchor = template["anchor_evidence"][0]
            evidence_id = (anchor["frame_evidence"][1]["evidence_id"]
                           if fault.startswith("source") else anchor["evidence_id"])
            image_files = list((runtime.config.evidence_dir / "navtpl-launcher").glob("*.png"))
            original_images = {path: path.read_bytes() for path in image_files}

            def revoke():
                if fault == "source_missing":
                    runtime.store._write("DELETE FROM evidence WHERE evidence_id=?", (evidence_id,))
                else:
                    record = runtime.evidence.get(evidence_id)
                    key, value = (("file_path", "navtpl-launcher/foreign.png")
                                  if fault == "crop_path" else ("device_id", "arm-02"))
                    record[key] = value
                    runtime.store.save_evidence(record)

            if when == "before_submit":
                revoke()
            elif when == "after_ready":
                original_ready = coordinator.wait_ready

                async def ready(*args, **kwargs):
                    result = await original_ready(*args, **kwargs)
                    revoke()
                    return result

                monkeypatch.setattr(coordinator, "wait_ready", ready)
            else:
                original_capture = runtime.plugin_capture_evidence

                async def capture(*args, **kwargs):
                    evidence = await original_capture(*args, **kwargs)
                    await asyncio.sleep(0)
                    revoke()
                    return evidence

                monkeypatch.setattr(runtime, "plugin_capture_evidence", capture)
            done = await submit(rig, ["system_home"])
            assert done["status"] == "failed", done
            assert runtime.devices.driver("arm-01").swipes == []
            profile = ag.load_profile("arm-01", root) or {}
            assert profile.get("actions", {}).get("system_home", {}).get("state") != "valid"
            assert original_images == {path: path.read_bytes() for path in image_files}
        finally:
            await runtime.shutdown()
    asyncio.run(run())


def test_context_evidence_lookup_is_device_scoped_and_returns_a_copy():
    from types import SimpleNamespace
    from tasks.plugin_api import StepContext

    records = {"local": {"device_id": "arm-01", "detail": {"checked": True}},
               "foreign": {"device_id": "arm-02"}}
    runtime = SimpleNamespace(evidence=SimpleNamespace(get=records.get))
    ctx = StepContext(runtime, {"device_id": "arm-01"}, {"plugin_id": "test"})
    result = ctx.get_evidence("local")
    result["detail"]["checked"] = False
    assert records["local"]["detail"]["checked"] is True
    assert ctx.get_evidence("foreign") is None
    assert ctx.get_evidence("missing") is None


def test_home_registered_server_crops_reach_runner_publish_and_resolver(rig):
    async def run():
        await start(rig)
        try:
            template = await register_launcher(rig)
            assert all(anchor["crop_sha256"] for anchor in template["anchor_evidence"])
            # Navigation profiles bootstrap from a genuinely validated scroll.
            done = await submit(rig, ["scroll_up", "system_home"])
            assert_published(rig, done, "scroll_up", "search_next")
            assert_published(rig, done, "system_home", "home")
            assert [item["kind"] for item in rig[1].prepared] == ["reset_list"] * 3 + ["prepare_home"] * 3
            screenshots = [record for record in rig[0].evidence.for_task(done["task_id"])
                           if record["label"] == "自检取证"]
            screenshots.sort(key=lambda record: record["captured_at"])
            home_frames = screenshots[-6:]
            assert len(home_frames) == 6
            for before, after in zip(home_frames[::2], home_frames[1::2]):
                assert before["sha256"] != after["sha256"]
        finally:
            await rig[0].shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("fault,reason", [
    ("missing", "launcher_template_missing"),
    ("binding", "navigation_template_binding_changed"),
    ("frame_binding", "navigation_template_binding_changed"),
    ("incomplete_binding", "navigation_template_binding_changed"),
    ("crop_missing", "navigation_template_invalid"),
    ("crop_hash", "navigation_template_invalid"),
    ("crop_path", "navigation_template_invalid"),
    ("source_missing", "navigation_template_invalid"),
    ("source_hash", "navigation_template_invalid"),
    ("wrong_evidence_root", "navigation_template_invalid"),
    ("identity", "navigation_template_invalid"),
    ("json", "navigation_template_invalid"),
])
def test_home_bad_template_blocks_all_actions_before_motion(rig, fault, reason):
    async def run():
        runtime, coordinator, root, *_ = rig
        await start(rig)
        try:
            path = root / "devices/arm-01/navigation_templates/launcher.json"
            if fault != "missing":
                template = await register_launcher(rig)
                if fault == "binding":
                    template["binding"]["daemon_session"] = "old-session"
                elif fault == "frame_binding":
                    template["binding"]["coordinate_frame_hash"] = "a" * 64
                elif fault == "incomplete_binding":
                    del template["binding"]["coordinate_frame_hash"]
                elif fault == "identity":
                    template["template_id"] = "top"
                elif fault == "crop_path":
                    template["anchor_evidence"][0]["evidence_id"] = "../outside"
                elif fault in ("source_missing", "source_hash"):
                    source = runtime.config.evidence_dir / "navtpl-launcher" / (
                        template["anchor_evidence"][0]["frame_evidence"][0]["evidence_id"] + ".png")
                    if fault == "source_missing":
                        source.unlink()
                    else:
                        source.write_bytes(b"tampered source")
                elif fault in ("crop_missing", "crop_hash"):
                    crop = runtime.config.evidence_dir / "navtpl-launcher" / (
                        template["anchor_evidence"][0]["evidence_id"] + ".png")
                    if fault == "crop_missing":
                        crop.unlink()
                    else:
                        crop.write_bytes(b"tampered crop")
                elif fault == "wrong_evidence_root":
                    (runtime.config.evidence_dir / "navtpl-launcher").rename(
                        rig[-1] / "navtpl-launcher")
                path.write_text("{" if fault == "json" else json.dumps(template))
            done = await submit(rig, ["scroll_up", "system_home"])
            assert done["status"] == "failed", done
            detail = last_detail(done)
            assert detail["reason_code"] == reason, detail
            assert detail["budget"]["physical_count"] == 0
            assert detail["controller_status"] == "not_started"
            assert runtime.devices.driver("arm-01").swipes == []
            assert coordinator.executing == []
            assert ag.load_profile("arm-01", root) is None
        finally:
            await runtime.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("fault", [
    "unmatched", "undecodable", "template_changed", "stale", "wrong_dimensions",
])
def test_home_uncertain_current_evidence_is_unknown_without_retry(rig, fault):
    async def run():
        runtime, coordinator, root, bundle, frame, _ = rig
        await start(rig)
        try:
            template = await register_launcher(rig)
            def change_after_motion():
                camera = runtime.devices.camera("arm-01")
                if fault == "unmatched":
                    camera.png = cv2.imencode(".png", np.zeros((720, 1280), np.uint8))[1].tobytes()
                elif fault == "undecodable":
                    camera.png = b"not an image"
                elif fault == "stale":
                    camera.timestamp = "2000-01-01T00:00:00+00:00"
                elif fault == "wrong_dimensions":
                    camera.png = cv2.imencode(".png", np.zeros((48, 64), np.uint8))[1].tobytes()
                else:
                    template["binding"]["daemon_session"] = "replaced"
                    (root / "devices/arm-01/navigation_templates/launcher.json").write_text(json.dumps(template))
            runtime.devices.driver("arm-01").after_swipe = change_after_motion
            done = await submit(rig, ["system_home"])
            assert done["status"] == "unknown", done
            detail = last_detail(done)
            assert detail["run_state"] == "unknown"
            assert detail["manual_review_required"] is True
            assert detail["retry_policy"] == "none"
            assert detail["budget"]["physical_count"] == 1
            assert len(coordinator.prepared) == len(coordinator.executing) == 1
            assert len(runtime.devices.driver("arm-01").swipes) == 1
            view = ag.gestures_view("arm-01", bundle=bundle, frame=frame, data_root=root)
            assert not view["actions"]["system_home"]["usable"]
        finally:
            await runtime.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("ready_limit", [0, 1, 2])
def test_home_requires_new_phone_ready_after_each_success(rig, ready_limit):
    async def run():
        runtime, coordinator, root, bundle, frame, _ = rig
        await start(rig)
        try:
            await register_launcher(rig)
            coordinator.ready_limit = ready_limit
            done = await submit(rig, ["system_home"])
            detail = last_detail(done)
            assert done["status"] == "failed", detail
            assert detail["reason_code"] == "trial_not_ready"
            assert detail["run_state"] == "failed"
            assert detail["retry_policy"] == "none"
            assert len(coordinator.prepared) == ready_limit + 1
            assert len(coordinator.ready_trials) == len(coordinator.executing) == ready_limit
            assert len(runtime.devices.driver("arm-01").swipes) == ready_limit
            assert not ag.gestures_view("arm-01", bundle=bundle, frame=frame,
                                        data_root=root)["actions"]["system_home"]["usable"]
        finally:
            await runtime.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("with_home", [False, True])
def test_back_preserves_phone_commit_channel_without_parent_verifier(rig, with_home):
    async def run():
        runtime, coordinator, *_ = rig
        await start(rig)
        try:
            if with_home:
                await register_launcher(rig)
            coordinator.page_events = [{"kind": "back_committed", "committed": True,
                                         "pageId": "gesture-child"}]
            actions = ["scroll_up", "system_back_left"]
            if with_home:
                actions.append("system_home")
            done = await submit(rig, actions)
            assert_published(rig, done, "system_back_left", "back_to_list")
        finally:
            await runtime.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize("runtime_calibration", [True, False])
def test_populated_installation_never_substitutes_for_context_root(rig, runtime_calibration):
    async def run():
        runtime, coordinator, root, bundle, frame, install = rig
        decoy_root = install / "data"
        cb.save_bundle(bundle, decoy_root)
        gp.save_coordinate_frame("arm-01", frame, data_root=decoy_root)
        before = {p.relative_to(decoy_root): p.read_bytes()
                  for p in decoy_root.rglob("*") if p.is_file()}
        if not runtime_calibration:
            (root / "devices/arm-01/gesture_coordinate_frame.json").unlink()
        await start(rig)
        try:
            done = await submit(rig, ["scroll_up"])
            if runtime_calibration:
                assert_published(rig, done, "scroll_up", "search_next")
            else:
                assert done["status"] == "confirmed", last_detail(done)
                derived = gp.build_calibration_coordinate_frame(bundle)
                assert derived["coordinate_frame_hash"] != frame["coordinate_frame_hash"]
                certificate = ag.load_profile("arm-01", root)["actions"]["scroll_up"]
                assert certificate["state"] == "valid"
                assert certificate["coordinate_frame_hash"] == derived["coordinate_frame_hash"]
                assert len(runtime.devices.driver("arm-01").swipes) == 3
                assert len(coordinator.prepared) == 3
            assert {p.relative_to(decoy_root): p.read_bytes()
                    for p in decoy_root.rglob("*") if p.is_file()} == before
        finally:
            await runtime.shutdown()
    asyncio.run(run())
