"""Real isolated processes: reloading the Fleet group must not kill its daemon.

The child is a temporary heartbeat writer, never arm_daemon3 or a serial driver.
Only groups created by this test with start_new_session=True receive a signal.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX process-group reload contract')

WORKER = '''import json, os, time
from pathlib import Path
root = Path.cwd()
device = os.environ["ARM_DAEMON_DEVICE_ID"]
(root / (device + ".identity")).write_text(json.dumps({
    "pid": os.getpid(), "pgid": os.getpgrp(), "sid": os.getsid(0)}))
deadline = time.monotonic() + 20
while time.monotonic() < deadline:
    (root / (device + ".heartbeat")).write_text(str(time.monotonic_ns()))
    time.sleep(.02)
'''

FLEET = '''import asyncio, json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import runtime.daemon_supervisor as module
module._port_is_open = lambda *args: False
async def run():
    root = Path.cwd()
    supervisor = module.DaemonSupervisor(
        root, hardware_mode="simulation", script_path=root / "worker.py",
        log_dir=root / "logs", max_restarts=0)
    configs = [{"device_id": device, "driver_mode": "daemon",
                "supervise_in_simulation": True,
                "daemon_addr": "127.0.0.1:19999", "serial_port": "/dev/test-only"}
               for device in ("fixture-one", "fixture-two")]
    await supervisor.start(configs)
    owned = dict(supervisor._processes)
    for _ in range(300):
        if all((root / (device + ".heartbeat")).exists() for device in owned):
            break
        await asyncio.sleep(.01)
    print(json.dumps({"fleet_pid": os.getpid(), "fleet_pgid": os.getpgrp(),
                      "children": {device: json.loads((root / (device + ".identity")).read_text())
                                   for device in owned}}), flush=True)
    while True:
        command = (await asyncio.to_thread(sys.stdin.readline)).strip()
        if command == "detach":
            await supervisor.detach_all()
        elif command == "stop_one":
            await supervisor.stop_device("fixture-one")
        elif command in ("stop_all", ""):
            await supervisor.stop_all()
        print(json.dumps({device: process.poll() for device, process in owned.items()}), flush=True)
        if command in ("stop_all", ""):
            break
asyncio.run(run())
'''


def _response(process):
    readable, _, _ = select.select([process.stdout], [], [], 6)
    assert readable, 'Isolated Fleet fixture did not respond'
    value = process.stdout.readline()
    assert value, 'Isolated Fleet fixture exited unexpectedly'
    return json.loads(value)


def _command(process, value):
    process.stdin.write(value + '\n')
    process.stdin.flush()
    return _response(process)


def _heartbeat_moves(path):
    before = path.read_text()
    until = time.monotonic() + 1
    while time.monotonic() < until:
        after = path.read_text()
        if after and after != before:
            return True
        time.sleep(.02)
    return False


@pytest.fixture
def isolated_fleet(tmp_path):
    (tmp_path / 'worker.py').write_text(WORKER)
    (tmp_path / 'fleet.py').write_text(FLEET)
    repo = Path(__file__).resolve().parents[1]
    with (tmp_path / 'fleet-errors.log').open('w') as errors:
        fleet = subprocess.Popen(
            [sys.executable, str(tmp_path / 'fleet.py'), str(repo)], cwd=tmp_path,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            text=True, start_new_session=True)
        info = None
        try:
            info = _response(fleet)
            assert info['fleet_pid'] == info['fleet_pgid'] == fleet.pid
            assert fleet.pid != os.getpgrp()
            yield fleet, info, tmp_path
        finally:
            if fleet.poll() is None:
                try:
                    _command(fleet, 'stop_all')
                    fleet.wait(timeout=6)
                except (OSError, AssertionError, subprocess.TimeoutExpired):
                    fleet.kill()
                    fleet.wait(timeout=3)
            if info:
                for child in info['children'].values():
                    try:
                        # Every PID came from this fixture's direct child;
                        # check the recorded group before cleaning up an orphan.
                        if os.getpgid(child['pid']) == child['pgid']:
                            os.kill(child['pid'], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            fleet.stdin.close()
            fleet.stdout.close()


@pytest.mark.parametrize('detach_before_reload', [False, True])
def test_fleet_group_reload_preserves_daemon_processes(isolated_fleet, detach_before_reload):
    fleet, info, root = isolated_fleet
    if detach_before_reload:
        assert _command(fleet, 'detach') == {'fixture-one': None, 'fixture-two': None}
    assert os.getpgid(fleet.pid) == fleet.pid
    os.killpg(fleet.pid, signal.SIGTERM)
    fleet.wait(timeout=5)
    for device, child in info['children'].items():
        assert _heartbeat_moves(root / (device + '.heartbeat')), 'Fleet group reload killed daemon'
        assert child['pid'] == child['pgid'] == child['sid']
        assert child['pgid'] != info['fleet_pgid']


def test_explicit_stops_target_only_owned_daemons(isolated_fleet):
    fleet, info, root = isolated_fleet
    stopped = _command(fleet, 'stop_one')
    assert stopped['fixture-one'] is not None
    assert stopped['fixture-two'] is None
    assert fleet.poll() is None
    assert _heartbeat_moves(root / 'fixture-two.heartbeat')
    assert info['children']['fixture-one']['pgid'] != info['children']['fixture-two']['pgid']
    stopped = _command(fleet, 'stop_all')
    assert all(code is not None for code in stopped.values())
    assert fleet.wait(timeout=5) == 0
