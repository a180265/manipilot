import asyncio
from contextlib import nullcontext
import json
import threading
from unittest.mock import AsyncMock

import pytest

from drivers import calibration_bundle as cb, gesture_profiles as gp
from scripts import motor_debug_server as debug
from tests.test_gesture_profiles import draft, evidence, navigation
from tests.test_ui_targets import ready_bundle


class FakeDaemon:
    def __init__(self):
        self._lock = threading.RLock()
        self.motion_active = False
        self.lease_id = None
        self.lease_expires = 0
        self.lease = None
        self.session = "sess-A"
        self.commands = []

    def status(self):
        return {"session": self.session, "lease": self.lease, "connected": True}

    def cmd(self, command, **kwargs):
        self.commands.append(command)
        if command != "RELEASE":
            raise AssertionError("Unexpected hardware command")
        self.lease = None
        return json.dumps({"released": True})

    def motion_scope(self):
        return nullcontext()


@pytest.fixture()
def environment(monkeypatch, tmp_path):
    daemon = FakeDaemon()
    cb.save_bundle(ready_bundle(), tmp_path)
    draft(tmp_path)
    context = {
        "camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
        "frame_width": 1280, "frame_height": 720,
    }
    monkeypatch.setattr(debug, "DATA_DIR", tmp_path)
    monkeypatch.setattr(debug, "daemon_for", lambda device: daemon)
    monkeypatch.setattr(debug, "require_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(debug, "_reject_motion_during_auto_run", lambda *args: None)
    monkeypatch.setattr(debug, "log_op", lambda *args, **kwargs: None)
    monkeypatch.setattr(debug, "device_config", lambda device: {"daemon_addr": "127.0.0.1:8795"})
    return daemon, tmp_path


def body(revision=1, **kwargs):
    return {
        "profileId": "wx_chat_list", "contextId": "ctx", "operator": "tester",
        "expectedRevision": revision, "direction": "search", "confirmed": True,
        "mainViewConfirmed": True, **kwargs,
    }


def test_explicit_confirmation_required(environment, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(debug, "_run_gesture_test", run)
    for missing in ("confirmed", "mainViewConfirmed"):
        with pytest.raises(ValueError):
            debug.test_gesture_profile("arm-01", body(**{missing: False}))
    run.assert_not_called()


def test_owned_idle_debug_lease_released_but_no_connect_command(environment, monkeypatch):
    daemon, root = environment
    daemon.lease_id = "own"
    daemon.lease = {"lease_id": "own", "owner_type": "debug"}
    run = AsyncMock(return_value=("confirmed", evidence("search")))
    monkeypatch.setattr(debug, "_run_gesture_test", run)
    result = debug.test_gesture_profile("arm-01", body())
    assert result["outcome"] == "confirmed"
    assert daemon.commands == ["RELEASE"]
    run.assert_awaited_once()
    assert result["entry"]["state"] == "draft"


@pytest.mark.parametrize("lease", [
    {"lease_id": "business", "owner_type": "business"},
    {"lease_id": "other-debug", "owner_type": "debug"},
])
def test_never_preempt_foreign_lease(environment, monkeypatch, lease):
    daemon, root = environment
    daemon.lease_id = "own"
    daemon.lease = lease
    run = AsyncMock()
    monkeypatch.setattr(debug, "_run_gesture_test", run)
    with pytest.raises(ValueError):
        debug.test_gesture_profile("arm-01", body())
    assert not daemon.commands
    assert gp.load_profile("arm-01", "wx_chat_list", root)["revision"] == 1
    run.assert_not_called()


def test_motion_busy_or_stale_revision_does_not_release_lease(environment, monkeypatch):
    daemon, root = environment
    daemon.lease_id = "own"
    daemon.lease = {"lease_id": "own", "owner_type": "debug"}
    daemon.motion_active = True
    with pytest.raises(ValueError):
        debug.test_gesture_profile("arm-01", body())
    daemon.motion_active = False
    with pytest.raises(ValueError):
        debug.test_gesture_profile("arm-01", body(revision=9))
    assert daemon.commands == []


def test_unknown_proof_blocks_repeat_and_enable(environment, monkeypatch):
    daemon, root = environment
    run = AsyncMock(return_value=("unknown", {"reason": "lost reply"}))
    monkeypatch.setattr(debug, "_run_gesture_test", run)
    result = debug.test_gesture_profile("arm-01", body())
    assert result["outcome"] == "unknown" and result["entry"]["test_blocked"]
    with pytest.raises(ValueError):
        debug.test_gesture_profile("arm-01", body(revision=result["entry"]["revision"]))
    with pytest.raises(ValueError):
        gp.set_enabled("arm-01", "wx_chat_list", enabled=True, human_confirmed=True,
                       expected_revision=result["entry"]["revision"], operator="tester",
                       current_session="sess-A", data_root=root)
    run.assert_awaited_once()


def test_revision_changed_during_test_does_not_certify_new_draft(environment, monkeypatch):
    daemon, root = environment

    async def run(*args):
        draft(root)
        return "confirmed", evidence("search")

    monkeypatch.setattr(debug, "_run_gesture_test", run)
    with pytest.raises(ValueError):
        debug.test_gesture_profile("arm-01", body())
    assert gp.load_profile("arm-01", "wx_chat_list", root)["tests"] == {}


def test_session_changed_during_test_remains_pending_and_unusable(environment, monkeypatch):
    daemon, root = environment

    async def run(*args):
        daemon.session = "new-session"
        return "confirmed", evidence("search")

    monkeypatch.setattr(debug, "_run_gesture_test", run)
    with pytest.raises(ValueError):
        debug.test_gesture_profile("arm-01", body())
    entry = gp.load_profile("arm-01", "wx_chat_list", root)
    assert entry["state"] == "testing"
    assert not gp.view_profile(entry, cb.load_bundle("arm-01", root), daemon.session)["usable"]


class FakeCamera:
    def __init__(self, *, ui="gesture_main"):
        self.ui = ui
        self.references = []
        self.closed = False

    def register_reference(self, name, image):
        self.references.append(name)

    async def identify_ui_state(self):
        return {"ui_state": self.ui, "confidence": 1.0}

    async def close(self):
        self.closed = True


class FakeEntityVerifier:
    """Profile orchestration uses simulated evidence, not a live probe template."""
    measurement_kind = "simulated_entity_motion"

    def __init__(self, *args, **kwargs):
        pass

    async def before(self, label):
        return {"baseline": True}

    async def after(self, label):
        return {"verified": True}


@pytest.mark.parametrize("fault,expected", [
    (None, "confirmed"), ("ack_only", "unknown"), ("stale_after", "unknown"),
    ("unknown_after_ui", "unknown"), ("connection", "failed"), ("swipe", "unknown"),
    ("missing_top_before", "failed"), ("missing_top_transition", "unknown"),
])
def test_production_swipe_and_before_after_within_lease(environment, monkeypatch, fault, expected):
    daemon, root = environment
    entry = gp.begin_test("arm-01", "wx_chat_list", "search", expected_revision=1,
                          operator="tester", current_session="sess-A", data_root=root)
    camera = FakeCamera()
    arms = []
    monkeypatch.setattr(debug, "_gesture_camera", lambda *args: camera)
    monkeypatch.setattr(debug, "CameraMotionVerifier", FakeEntityVerifier)
    monkeypatch.setattr(debug, "check_live_calibration", AsyncMock(return_value=([], {}, None)))

    class Arm:
        def __init__(self, *args, **kwargs):
            self.daemon_addr = kwargs["daemon_addr"]
            self.lease = False
            self.swipes = []
            self.closed = False
            self.connected = False
            self.options = kwargs
            arms.append(self)

        async def connect(self):
            if fault == "connection":
                raise RuntimeError("No serial session")
            self.connected = True

        async def read_state(self):
            return {"session": "sess-A"}

        async def swipe(self, gesture):
            self.swipes.append(gesture)
            assert (await self.live_calibration_gate())["checks"] == []
            self.lease = True
            await self.motion_verifier.before("swipe_to_start")
            if fault == "swipe":
                raise RuntimeError("motion reply lost")
            await self.motion_verifier.before("swipe_release")
            if fault == "unknown_after_ui":
                camera.ui = "other"
            after = await self.motion_verifier.after("swipe_release")
            self.lease = False
            return {"ok": True, "motion_verified": fault != "ack_only" and after["verified"],
                    "swipe_executed": True,
                    "outcome_hint": "unknown" if fault == "ack_only" else "success"}

        async def disconnect(self):
            self.closed = True

    async def capture(camera, config, path):
        assert arms[0].lease, "Evidence must stay inside the production motion lease"
        phase = path.stem
        path.write_bytes(b"fake-png")
        return {
            "path": str(path), "sha256": phase,
            "source_frame_seq": 1 if phase == "before" or fault == "stale_after" else 2,
            "top_anchor_found": (phase == "before" and fault != "missing_top_before") or
                                (phase == "after" and fault == "missing_top_transition"),
        }

    monkeypatch.setattr(debug, "DaemonArmDriver", Arm)
    monkeypatch.setattr(debug, "_gesture_evidence", capture)
    result, proof = asyncio.run(debug._run_gesture_test("arm-01", "ctx", entry))
    assert result == expected
    assert len(arms) == 1 and arms[0].closed and camera.closed
    assert len(arms[0].swipes) <= 1
    assert arms[0].options["owner_type"] == "debug"
    assert proof["production_ready"] is False


def test_current_binding_camera_frame_provider_only_no_direct_uvc(environment, monkeypatch):
    camera = debug._gesture_camera("arm-01", {
        "camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
        "frame_width": 1280, "frame_height": 720,
    })
    source = camera._frame_provider
    assert source.expected_device_id == "arm-01"
    assert source.expected_camera_id == "cam-01"
    assert source.expected_device_unique_id == "uvc-01"
    assert source.allow_legacy is False
    assert camera._capture is None


@pytest.mark.parametrize("texts,found", [
    (["Pinned contact"], True), (["Pinned contact suffix"], False),
    (["Pinned contact", "Pinned contact"], None),
])
def test_top_evidence_matches_exact_text_and_rejects_ambiguity(tmp_path, texts, found):
    from types import SimpleNamespace
    import cv2
    import numpy as np
    from adapters.vision.ocr import OcrResult, OcrWord, OcrEngineError

    png = cv2.imencode(".png", np.full((720, 1280, 3), 120, np.uint8))[1].tobytes()
    frame = {"width": 1280, "height": 720, "frozen": False, "source_frame_seq": 1,
             "image_bytes": png, "sha256": "test-frame", "captured_at": "now"}
    ocr = OcrResult(" ".join(texts), [
        OcrWord(text, 10, index * 30, 200, 20, .99) for index, text in enumerate(texts)
    ], .99, False, "fake", "1", width=450, height=60)
    camera = SimpleNamespace(capture=AsyncMock(return_value=frame),
                             ocr_engine=SimpleNamespace(recognize=lambda *args, **kwargs: ocr))
    config = gp.validate_navigation(navigation())
    if found is None:
        with pytest.raises(OcrEngineError):
            asyncio.run(debug._gesture_evidence(camera, config, tmp_path / "test.png"))
    else:
        result = asyncio.run(debug._gesture_evidence(camera, config, tmp_path / "test.png"))
        assert result["top_anchor_found"] is found


def serve_visual_fixture():
    """Isolated browser fixture. No route proxies to runtime, cameras or daemon."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import tempfile
    import time
    from urllib.parse import parse_qs, urlsplit
    import cv2
    import numpy as np

    root = Path(tempfile.mkdtemp(prefix="gesture-browser-"))
    cb.save_bundle(ready_bundle(), root)
    image = np.full((720, 1280, 3), 210, dtype=np.uint8)
    cv2.rectangle(image, (290, 30), (870, 690), (245, 245, 245), -1)
    cv2.putText(image, "SIMULATION ONLY", (350, 85), cv2.FONT_HERSHEY_SIMPLEX, .8, (30, 80, 40), 2)
    for index in range(6):
        y = 140 + index * 82
        cv2.rectangle(image, (315, y), (840, y + 70), (220, 230, 220), -1)
        cv2.putText(image, f"Test conversation {index + 1}", (350, y + 42),
                    cv2.FONT_HERSHEY_SIMPLEX, .7, (40, 40, 40), 2)
    png = cv2.imencode(".png", image)[1].tobytes()
    calls = []
    daemon = FakeDaemon()
    daemon.mpos = lambda: {"x": 20, "y": 20, "z": 2, "state": "Idle"}
    context = {"context_id": "fixture-context", "device_id": "arm-01",
               "camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
               "binding_revision": 7, "daemon_session": "sess-A",
               "frame_width": 1280, "frame_height": 720}
    patches = pytest.MonkeyPatch()
    patches.setattr(debug, "DATA_DIR", root)
    patches.setattr(debug, "_BASIC_TICKETS", {})
    patches.setattr(debug, "_BASIC_ACTIVE", set())
    patches.setattr(debug, "daemon_for", lambda device: daemon)
    patches.setattr(debug, "camera_status", lambda device: {"has_frame": True, "seq": int(time.time() * 10)})
    patches.setattr(debug, "_reject_motion_during_auto_run", lambda *args: None)
    patches.setattr(debug, "log_op", lambda *args, **kwargs: None)
    patches.setattr(debug, "device_config", lambda device: {"daemon_addr": "SIMULATION"})

    def fixture_context(device, context_id, **kwargs):
        if device != "arm-01" or context_id != "fixture-context":
            raise ValueError("Isolated fixture device/context mismatch")
        return context

    async def simulate_basic(device, body, gesture):
        directory = root / "devices" / device / "basic_gesture_evidence"
        directory.mkdir(parents=True, exist_ok=True)
        for phase in ("before", "after"):
            (directory / f"{phase}.png").write_bytes(png)
        # Swipe deliberately records uncertainty then drops the HTTP reply below.
        outcome = "confirmed" if body["action"] == "double_tap" else "unknown"
        return outcome, {"motionConfirmed": outcome == "confirmed",
                         "effectStatus": "manual_confirmation_required",
                         "before": {"source_frame_seq": body["frameSeq"]},
                         "after": {"source_frame_seq": body["frameSeq"] + 1},
                         "reason": "隔离仿真结果，不代表任何真实机械动作"}

    patches.setattr(debug, "require_context", fixture_context)
    patches.setattr(debug, "_run_basic_gesture", simulate_basic)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, payload, content="application/json", code=200):
            data = json.dumps(payload, ensure_ascii=False).encode() if content == "application/json" else payload
            self.send_response(code)
            self.send_header("Content-Type", content)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def view(self, profile_id):
            entry = gp.load_profile("arm-01", profile_id, root)
            return {"entry": gp.view_profile(entry, cb.load_bundle("arm-01", root), "sess-A") if entry else None}

        def do_GET(self):
            parsed = urlsplit(self.path)
            path, query = parsed.path, parse_qs(parsed.query)
            if path == "/":
                html = debug.PAGE_PATH.read_text(encoding="utf-8")
                return self.send(html.encode(), "text/html; charset=utf-8")
            if path == "/api/gestures/state":
                return self.send(debug.basic_gesture_state(
                    query.get("deviceId", ["arm-01"])[0], query.get("contextId", [""])[0],
                    query.get("requestId", [None])[0]))
            if path == "/api/gestures/evidence":
                return self.send(png, "image/png")
            if path.startswith("/api/camera/") and path != "/api/camera/status":
                return self.send(png, "image/png")
            if path == "/api/camera/status":
                return self.send({"has_frame": True, "seq": int(time.time() * 10)})
            if path == "/api/devices":
                return self.send({"devices": [{"deviceId": device_id, "driverMode": "daemon",
                    "cameraId": "cam-01", "calibrationStatus": "validated", "dynamic": True,
                    "daemonAddr": "SIMULATION", "calibrationRoot": str(root)}
                    for device_id in ("arm-01", "arm-02")]})
            if path == "/api/status":
                return self.send({"daemon": {"connected": True, "device_present": True, "session": "sess-A"},
                                  "anchorMatch": True, "usable": True, "calibration": None})
            if path == "/api/gesture-profiles":
                if "evidencePhase" in query:
                    return self.send(png, "image/png")
                return self.send(self.view(query.get("profileId", ["wx_chat_list"])[0]))
            if path == "/fixture/calls":
                return self.send(calls)
            return self.send({})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or "{}")
            calls.append({"path": self.path, "body": body})
            if self.path == "/api/device-context":
                return self.send({"contextId": "fixture-context", "deviceId": body["deviceId"],
                                  "cameraId": "cam-01", "cameraDeviceUniqueId": "uvc-01",
                                  "daemonSession": "sess-A", "bindingRevision": 7,
                                  "frameWidth": 1280, "frameHeight": 720, "checks": []})
            profile_id = body.get("profileId", "")
            try:
                if self.path == "/api/gestures/test":
                    result = debug.test_basic_gesture(body["deviceId"], body)
                    if body["action"] == "swipe":
                        self.close_connection = True
                        return
                    return self.send(result)
                elif self.path == "/api/gestures/review":
                    return self.send(debug.review_basic_gesture(body["deviceId"], body))
                elif self.path == "/api/gesture-profiles/draft":
                    gp.save_draft("arm-01", profile_id, body["config"], operator=body["operator"],
                                  current_session="sess-A", data_root=root)
                elif self.path == "/api/gesture-profiles/test":
                    entry = gp.begin_test("arm-01", profile_id, body["direction"],
                        expected_revision=body["expectedRevision"], operator=body["operator"],
                        current_session="sess-A", data_root=root)
                    gp.finish_test("arm-01", profile_id, expected_revision=entry["revision"],
                        test_id=entry["pending_test"]["id"], outcome="confirmed", evidence=evidence(body["direction"]),
                        operator=body["operator"], current_session="sess-A", data_root=root)
                elif self.path in {"/api/gesture-profiles/enable", "/api/gesture-profiles/disable"}:
                    gp.set_enabled("arm-01", profile_id, enabled=self.path.endswith("/enable"),
                        expected_revision=body["expectedRevision"], operator=body["operator"],
                        current_session="sess-A", human_confirmed=body.get("confirmed"), data_root=root)
                else:
                    raise ValueError("Only isolated gesture fixture routes are allowed")
                return self.send(self.view(profile_id))
            except ValueError as exc:
                return self.send({"error": {"message": str(exc)}}, code=400)

        def do_DELETE(self):
            return self.send({"closed": True})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    print(f"ISOLATED_GESTURE_FIXTURE=http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        patches.undo()


if __name__ == "__main__":
    serve_visual_fixture()
