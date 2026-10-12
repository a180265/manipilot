"""Isolated gesture UI fixture. No production devices, sockets or data are used."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb
from drivers.daemon_protocol import DOUBLE_TAP_FEATURE, SWIPE_FEATURE
from scripts import motor_debug_server as debug
from tests.test_motor_debug_gesture_plans import SimulatedEntityVerifier
from tests.test_motor_debug_gesture_profiles import FakeDaemon
from tests.test_screen_gesture_driver import gesture_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18943)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="fleet-gesture-ui-") as directory, pytest.MonkeyPatch.context() as patch:
        root = Path(directory)
        daemon = FakeDaemon()
        bundle = gesture_bundle()
        bundle["daemonSession"] = daemon.session
        bundle["status"] = "draft"
        cb.save_bundle(bundle, root)
        context = {
            "context_id": "isolated-gesture-context", "device_id": "arm-01",
            "daemon_session": daemon.session, "camera_id": "cam-01",
            "camera_device_unique_id": "uvc-01", "binding_revision": 7,
            "frame_width": 1280, "frame_height": 720,
        }
        features = [DOUBLE_TAP_FEATURE, SWIPE_FEATURE]
        submissions = []
        previews = []
        saves = []
        outcomes = ["confirmed"]
        frame = {"seq": 100}
        lease = {}
        plan_unloaded = [False]
        public_context = {
            "deviceId": "arm-01", "contextId": context["context_id"],
            "daemonSession": daemon.session, "cameraId": "cam-01",
            "cameraDeviceUniqueId": "uvc-01", "bindingRevision": 7,
            "frameWidth": 1280, "frameHeight": 720, "checks": [],
        }
        patch.setattr(debug, "DATA_DIR", root)
        patch.setattr(debug, "_BASIC_TICKETS", {})
        patch.setattr(debug, "_BASIC_ACTIVE", set())
        patch.setattr(debug, "daemon_for", lambda _: daemon)
        patch.setattr(daemon, "status", lambda: {
            "connected": True, "session": daemon.session, "motion_features": list(features),
            "lease": deepcopy(lease) or None})
        patch.setattr(debug, "camera_status", lambda _: {
            "has_frame": True, "seq": frame["seq"], "width": 1280, "height": 720, "error": None})
        patch.setattr(debug, "_reject_motion_during_auto_run", lambda *args: None)
        patch.setattr(debug, "log_op", lambda *args, **kwargs: None)
        patch.setattr(debug, "CameraMotionVerifier", SimulatedEntityVerifier)

        def require_context(device, context_id, **kwargs):
            if device != "arm-01" or context_id != context["context_id"]:
                raise ValueError("Fixture context mismatch")
            return context

        patch.setattr(debug, "require_context", require_context)

        async def simulate(device, body, gesture):
            submissions.append(deepcopy(body))
            return outcomes[0], {"motionConfirmed": outcomes[0] == "confirmed",
                                 "effectStatus": "manual_confirmation_required",
                                 "simulation": True}

        patch.setattr(debug, "_run_basic_gesture", simulate)
        image = np.full((720, 1280, 3), 30, np.uint8)
        cv2.rectangle(image, (100, 100), (1200, 600), (80, 130, 80), -1)
        cv2.putText(image, "SIMULATION ONLY - NO HARDWARE", (160, 340),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.25, (255, 255, 255), 2)
        png = cv2.imencode(".png", image)[1].tobytes()
        controls = """
        <aside style="padding:12px;background:#fff;color:#111;position:relative;z-index:10">
        <strong>隔离仿真 · 不连接硬件 · 临时校准数据</strong>
        <button onclick="fetch('/_fixture/ready',{method:'POST'})">仿真：校准完成</button>
        <button onclick="fetch('/_fixture/change',{method:'POST'})">仿真：校准版本变化</button>
        <button onclick="fetch('/_fixture/missing',{method:'POST'})">仿真：特性缺失</button>
        <button onclick="fetch('/_fixture/unknown',{method:'POST'})">仿真：下次结果未知</button>
        <button onclick="fetch('/_fixture/unloaded',{method:'POST'})">仿真：接口未加载</button>
        <button onclick="fetch('/_fixture/advance',{method:'POST'})">仿真：画面前进400帧</button>
        <button onclick="fetch('/_fixture/expire',{method:'POST'})">仿真：票据到期</button>
        <button onclick="fetch('/_fixture/lease',{method:'POST'})">仿真：其他任务占用</button>
        </aside>
        """

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, payload, status=200, content_type="application/json; charset=utf-8"):
                content = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                parsed = urlparse(self.path)
                path, query = parsed.path, parse_qs(parsed.query)
                device = query.get("deviceId", query.get("device", ["arm-01"]))[0]
                ctx = query.get("contextId", [""])[0]
                try:
                    if path == "/":
                        page = debug.PAGE_PATH.read_text()
                        self.respond(page.replace("<body>", "<body>" + controls, 1).encode(),
                                     content_type="text/html; charset=utf-8")
                    elif path == "/api/camera/stream":
                        self.respond(png, content_type="image/png")
                    elif path == "/api/devices":
                        self.respond({"devices": [{
                            "deviceId": "arm-01", "cameraId": "cam-01", "driverMode": "simulation",
                            "dynamic": True, "daemonAddr": "NO HARDWARE", "calibrationRoot": "TEMPORARY"}]})
                    elif path == "/api/gestures/plan":
                        if plan_unloaded[0]:
                            self.respond({"error": "HTTP 404"}, 404)
                        else:
                            self.respond(debug.calculated_gesture_plan(device, ctx))
                    elif path == "/api/gestures/state":
                        self.respond(debug.basic_gesture_state(device, ctx, query.get("requestId", [None])[0]))
                    elif path == "/api/gestures/saved":
                        self.respond(debug.saved_basic_gestures(device, ctx))
                    elif path == "/api/wizard/state":
                        current = cb.load_bundle("arm-01", root)
                        self.respond({"bundle": current, "stage": "ready" if current["status"] in {"ready", "motion_ready"} else "idle",
                                      "stageDisplay": "仿真校准", "auto": {}, "checks": [],
                                      "requiredDebugChecks": [], "requiredWechatChecks": []})
                    elif path == "/api/camera/status":
                        frame["seq"] += 1
                        self.respond(debug.camera_status(device))
                    elif path == "/api/status":
                        self.respond({"daemon": {"connected": True, "session": daemon.session},
                                      "mpos": {"x": -52, "y": 49, "z": -8, "state": "Idle"}})
                    elif path == "/_test":
                        self.respond({"submissions": submissions, "previews": previews, "saves": saves,
                                      "commands": daemon.commands, "hardware": "simulated"})
                    else:
                        self.respond({})
                except Exception as exc:
                    self.respond({"error": str(exc)}, 400)

            def do_POST(self):
                path = urlparse(self.path).path
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                body = json.loads(raw or b"{}")
                try:
                    if path == "/api/device-context":
                        self.respond(public_context)
                    elif path == "/api/gestures/preview":
                        result = debug.preview_calculated_gesture(body["deviceId"], body)
                        previews.append(body)
                        self.respond(result)
                    elif path == "/api/gestures/test":
                        body.setdefault("operator", "调试台")
                        self.respond(debug.test_basic_gesture(body["deviceId"], body))
                    elif path == "/api/gestures/review":
                        body.setdefault("operator", "调试台")
                        self.respond(debug.review_basic_gesture(body["deviceId"], body))
                    elif path == "/api/gestures/saved":
                        result = debug.save_basic_gesture(body["deviceId"], body)
                        saves.append({"request": deepcopy(body), "entry": deepcopy(result)})
                        self.respond(result)
                    elif path in {"/_fixture/ready", "/_fixture/change"}:
                        current = cb.load_bundle("arm-01", root)
                        current["status"] = "ready"
                        if path.endswith("change"):
                            current["xy"]["robotToScreen"]["c"] += .01
                        cb.save_bundle(current, root)
                        self.respond({"ok": True})
                    elif path == "/_fixture/missing":
                        features.clear()
                        self.respond({"ok": True})
                    elif path == "/_fixture/unknown":
                        outcomes[0] = "unknown"
                        self.respond({"ok": True})
                    elif path == "/_fixture/unloaded":
                        plan_unloaded[0] = True
                        self.respond({"ok": True})
                    elif path == "/_fixture/advance":
                        frame["seq"] += 400
                        self.respond({"seq": frame["seq"]})
                    elif path == "/_fixture/expire":
                        for ticket in debug._BASIC_TICKETS.values():
                            ticket["expires"] = 0
                        self.respond({"ok": True})
                    elif path == "/_fixture/lease":
                        lease.update(lease_id="fixture-foreign", owner_type="fixture")
                        self.respond({"ok": True})
                    else:
                        self.respond({"error": "Fixture refuses this operation"}, 409)
                except Exception as exc:
                    self.respond({"error": str(exc)}, 400)

            def do_DELETE(self):
                self.respond({"closed": True})

        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        print(f"Isolated gesture fixture: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
