"""Isolated debugger UI fixture; never connects to production services or hardware."""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse

from tests.test_calibration_wizard import (
    Env, _run_xy_stage, auto_report, drive_to_z_ready,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18942)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="fleet-e-ui-") as directory:
        env = Env(Path(directory))
        drive_to_z_ready(env)
        env.ime.script = auto_report(env.ime, env.daemon)
        env.daemon.z = 2.0
        env.wizard.capture_hover_z()
        env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
        env.wizard.z_save("tap")
        env.wizard.z_tap_test("delete", 3.0, 1500.0, 2200)
        env.wizard.z_save("delete")
        _run_xy_stage(env)
        env.wizard.edge_capture()
        env.wizard.camera_fit()
        actions = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, payload, status=200):
                content = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/":
                    page = Path(__file__).parents[1] / "scripts/motor_debug_page.html"
                    content = page.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                elif path == "/api/wizard/state":
                    self.respond(env.wizard.state_view())
                elif path == "/api/devices":
                    self.respond({"devices": [{
                        "deviceId": "arm-01", "cameraId": "fake-camera",
                        "driverMode": "simulation", "dynamic": True,
                    }]})
                elif path == "/_test":
                    self.respond({"actions": actions, "stage": env.wizard.state["stage"],
                                  "hardware": "simulated", "database": "temporary"})
                else:
                    self.respond({})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/device-context":
                    self.respond({"contextId": "isolated-context", "deviceId": "arm-01"})
                    return
                if self.path != "/api/wizard":
                    self.respond({"error": "Fixture rejects this action"}, 409)
                    return
                action = body.get("action")
                try:
                    if action == "acceptance-record":
                        result = env.wizard.acceptance_record(
                            body["kind"], body["check"], body["ok"],
                            body.get("detail", ""))
                    elif action == "acceptance-advance":
                        result = env.wizard.acceptance_advance()
                    else:
                        raise ValueError("Fixture rejects physical or unrelated actions")
                    actions.append(action)
                    self.respond(result)
                except Exception as exc:
                    self.respond({"error": str(exc)}, 409)

            def do_DELETE(self):
                self.respond({"closed": True})

        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        print(f"Isolated debugger fixture: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
