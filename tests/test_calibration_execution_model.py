"""Acceptance and target execution preserve the calibrated motion model."""
from copy import deepcopy
from itertools import count
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from drivers import calibration_wizard as cw
from tests.test_calibration_wizard import (
    CAM_H, CAM_W, SCREEN_H, SCREEN_W, Env, FakeOcr, FakeOcrWord,
    auto_ready_env, wait_auto,
)
from tests.test_motor_debug_transport import transport  # noqa: F401


QUADRATIC_MODEL = {
    "a": 10.0, "b": 0.5, "c": 100.0,
    "d": 0.3, "e": 8.0, "f": 1300.0,
    "qx2": 0.02, "qxy": -0.01, "qy2": 0.005,
    "px2": -0.004, "pxy": 0.008, "py2": 0.015,
}
SCREEN_TARGET = (424.0, 1476.2)
ROBOT_TARGET = (30.0, 20.0)


def execution_env(tmp_path):
    env = Env(tmp_path)
    state = env.wizard.state
    state["stage"] = cw.STAGE_DEBUG_ACCEPTANCE
    state["imeDeviceId"] = "k40-model-test"
    state["screen"] = {
        "orientation": "portrait", "screenWidth": SCREEN_W, "screenHeight": SCREEN_H,
    }
    state["geometry"]["edge"] = {**deepcopy(env.quad), "frameSeq": 0}
    state["geometry"]["homography"] = {
        "homography": [
            [SCREEN_W / CAM_W, 0, 0],
            [0, SCREEN_H / CAM_H, 0],
            [0, 0, 1],
        ],
        "frameW": CAM_W, "frameH": CAM_H,
    }
    state["xy"]["fit"] = deepcopy(QUADRATIC_MODEL)
    state["z"].update({
        "hoverZ": 5.0,
        "tap": {"depth": 3.0, "feed": 1500.0, "dwellMs": 0},
        "delete": {"depth": 3.0, "feed": 1500.0, "holdMs": 2200},
    })
    camera_x = SCREEN_TARGET[0] * CAM_W / SCREEN_W
    camera_y = SCREEN_TARGET[1] * CAM_H / SCREEN_H
    env.wizard._ocr_engine = FakeOcr([
        FakeOcrWord("统一校准测试", camera_x - 40, camera_y - 12),
    ])
    return env


def test_manual_acceptance_click_preserves_quadratic_calibration(tmp_path):
    env = execution_env(tmp_path)

    result = env.wizard.acceptance_execute("debug", "ime_input_tap")

    assert result["targetSource"] == "camera_ocr"
    assert result["screenTarget"] == pytest.approx(SCREEN_TARGET)
    assert result["robotTarget"] == pytest.approx(ROBOT_TARGET, abs=0.01)
    assert (env.daemon.x, env.daemon.y) == pytest.approx(ROBOT_TARGET, abs=0.01)
    assert env.daemon.z == 5.0
    assert env.daemon.tap_count == 1
    assert env.wizard.state["xy"]["fit"] == QUADRATIC_MODEL


def test_auto_acceptance_click_uses_complete_candidate_model(tmp_path):
    env = execution_env(tmp_path)
    run = {
        "runId": "quadratic-e-test", "deviceId": "arm-01",
        "phase": "E_ACCEPTANCE", "state": cw.AUTO_STATE_RUNNING,
        "edge": deepcopy(env.wizard.state["geometry"]["edge"]),
        "h": {"homography": deepcopy(
            env.wizard.state["geometry"]["homography"]["homography"])},
        "binding": {"frameWidth": CAM_W, "frameHeight": CAM_H},
        "arm": {"robotToScreen": deepcopy(QUADRATIC_MODEL)},
        "z": {"zSafe": -5.0},
        "acceptance": {},
    }
    env.wizard._auto["run"] = run
    env.wizard.state["xy"]["fit"]["c"] = 900.0

    result = env.wizard.auto_acceptance_execute(run["runId"], "ime_input_tap")

    assert result["screenTarget"] == pytest.approx(SCREEN_TARGET)
    assert result["robotTarget"] == pytest.approx(ROBOT_TARGET, abs=0.01)
    assert (env.daemon.x, env.daemon.y) == pytest.approx(ROBOT_TARGET, abs=0.01)
    assert env.daemon.z == -5.0
    assert env.daemon.tap_count == 1
    assert run["arm"]["robotToScreen"] == QUADRATIC_MODEL


def test_calibration_view_requires_only_debugger_ime_checks(tmp_path):
    env = execution_env(tmp_path)

    for view in (env.wizard.state_view(), env.wizard.auto_view()):
        assert view["requiredDebugChecks"] == ["ime_input_tap", "ime_delete_clear"]
        assert view["requiredWechatChecks"] == []
    env.wizard.state["stage"] = cw.STAGE_READY
    assert env.wizard.state_view()["stageDisplay"] == "校准已保存"


@pytest.mark.parametrize("operation", ["registered_target", "visual_verify"])
def test_target_checks_use_the_same_quadratic_model(tmp_path, operation):
    env = execution_env(tmp_path)
    session = env.ime.create_session("k40-model-test")["sessionId"]
    env.ime.client(session, "open")
    env.wizard.state.update(stage=cw.STAGE_TARGET, imeSessionId=session,
                            daemonSession=env.daemon.session)
    env.wizard.target_save("send_button", SCREEN_TARGET[0] / SCREEN_W,
                           SCREEN_TARGET[1] / SCREEN_H)
    env.daemon.on_tap = lambda _line: env.ime.report(
        session, env.ime.sessions[session]["armed"]["sampleId"], *SCREEN_TARGET)

    if operation == "registered_target":
        result = env.wizard.target_test("send_button")["result"]
        assert result["withinTolerance"] is True
    else:
        env.targets.append((SCREEN_TARGET[0] * CAM_W / SCREEN_W,
                            SCREEN_TARGET[1] * CAM_H / SCREEN_H, CAM_W, CAM_H))
        result = env.wizard.visual_verify(SCREEN_TARGET[0] / SCREEN_W,
                                          SCREEN_TARGET[1] / SCREEN_H)["result"]
        assert result["contactVsNominalPx"] == 0
    # Stored normalized positions and camera pixels are rounded.
    assert (env.daemon.x, env.daemon.y) == pytest.approx(ROBOT_TARGET, abs=0.05)


def test_acceptance_motion_failure_cancels_displayed_ime_task(tmp_path, monkeypatch):
    env = execution_env(tmp_path)
    original = env.daemon.cmd

    def disconnected(line):
        if line.startswith("TAP"):
            raise RuntimeError("模拟点击时通信中断：结果未知")
        return original(line)

    monkeypatch.setattr(env.daemon, "cmd", disconnected)
    with pytest.raises(Exception, match="结果未知"):
        env.wizard.acceptance_execute("debug", "ime_input_tap")

    assert len(env.ime.tasks_created) == 1
    assert env.ime.cancelled_tasks == [env.ime.tasks_created[0]["taskId"]]


def test_auto_acceptance_waits_for_input_confirmation_before_deleting(tmp_path):
    from drivers import target_archive
    from tests.test_calibration_wizard import auto_ready_env, wait_auto

    env = auto_ready_env(tmp_path, ocr_words=[
        FakeOcrWord("统一校准测试", 300, 400),
    ])
    target_archive.save_target(
        "arm-01", "ime_delete_key",
        {"nx": 0.1, "ny": 0.9, "targetMode": "hover_only"}, tmp_path,
    )
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = env.wizard.auto_view()["runId"]
    try:
        wait_auto(env, lambda view: bool(
            ((view.get("acceptance") or {}).get("debug") or {})
            .get("ime_input_tap", {}).get("executedAt")), desc="input executed")
        # Leave enough time for the former eager deletion to run.
        threading.Event().wait(0.8)
        view = env.wizard.auto_view()
        assert not (view["acceptance"]["debug"].get("ime_delete_clear") or {}).get(
            "executedAt"), "Input must remain visible until the operator confirms it"

        env.wizard.auto_acceptance_record(run_id, "debug", "ime_input_tap", True)
        wait_auto(env, lambda view: bool(
            ((view.get("acceptance") or {}).get("debug") or {})
            .get("ime_delete_clear", {}).get("executedAt")), desc="delete executed")
        assert env.wizard.auto_view()["state"] != "PROMOTED"
        env.wizard.auto_acceptance_record(run_id, "debug", "ime_delete_clear", True)
        wait_auto(env, lambda view: view["state"] == "PROMOTED", desc="saved")
        assert env.wizard.finish()["status"] == "ready"
    finally:
        env.wizard.auto_stop()


@pytest.mark.parametrize("outcome", ["recover", "busy", "cancel", "e_busy", "e_cancel"])
def test_auto_runner_position_contention_preserves_calibration(
        tmp_path, monkeypatch, transport, outcome):
    from scripts import motor_debug_server as debug
    from drivers import calibration_bundle as cb

    env = auto_ready_env(
        tmp_path,
        ocr_words=[FakeOcrWord("统一校准测试", 300, 400)],
    )
    mode = outcome.removeprefix("e_")
    client, _sockets, opened = transport
    fake = env.daemon
    monkeypatch.setattr(client, "status", fake.status)
    monkeypatch.setattr(client, "ensure_debug_lease", fake.ensure_debug_lease)
    monkeypatch.setattr(client, "_status_cmd",
                        lambda line, **kwargs: json.dumps(fake.mpos()))
    monkeypatch.setattr(client, "MPOS_WAIT_TIMEOUT", 1.0 if mode == "recover" else 0.2)

    def motion(line):
        with client.motion_scope():
            return fake.cmd(line)

    monkeypatch.setattr(client, "cmd", motion)
    env.wizard.daemon = client
    park_before = deepcopy(env.wizard.state["safePark"])
    bundle_path = tmp_path / "devices" / "arm-01" / "calibration_bundle.json"
    bundle_before = bundle_path.read_bytes()
    entered, release, reading = threading.Event(), threading.Event(), threading.Event()
    phase_method = "_auto_e_acceptance" if outcome.startswith("e_") else "_auto_phase2_validate"
    original_phase = getattr(env.wizard, phase_method)
    timer = None

    def read(**kwargs):
        if entered.is_set():
            reading.set()
        return client.mpos(**kwargs)

    env.wizard._position_read = read

    def held_query():
        with client._motion_state_lock:
            entered.set()
            assert release.wait(12)

    with ThreadPoolExecutor(max_workers=1) as pool:
        def phase2(run, *args, **kwargs):
            nonlocal timer
            pool.submit(held_query)
            assert entered.wait(1)
            if mode == "recover":
                timer = threading.Timer(0.35, release.set)
                timer.start()
            return original_phase(run, *args, **kwargs)

        monkeypatch.setattr(env.wizard, phase_method, phase2)
        env.wizard.auto_start(defer_input_acceptance=False)
        run_id = env.wizard.auto_view()["runId"]
        try:
            assert entered.wait(10)
            assert reading.wait(1)
            snapshot = debug.status_snapshot("arm-01")
            assert snapshot["status_stale"] and snapshot["mpos"] is None
            if mode == "recover":
                view = wait_auto(env, lambda view: view["phase"] == "E_ACCEPTANCE"
                                 or view["state"] == "FAILED", timeout=12)
                assert view["phase"] == "E_ACCEPTANCE", view["lastFailure"]
                assert view["lastFailure"] is None
                assert view["validation"]["passed"]
                assert len([s for s in env.wizard._auto["run"]["samples"]
                            if s["phase"] == "PHASE2_VALIDATE"]) == 16
            elif mode == "busy":
                view = wait_auto(env, lambda view: view["state"] == "FAILED", timeout=2)
                assert view["lastFailure"]["code"] == "status_read_busy"
                candidate = cb.load_candidate("arm-01", run_id, tmp_path)
                assert candidate["lastPark"] is None
                failure = [e for e in candidate["events"] if e["event"] == "safe_park_failed"][-1]
                assert failure["detail"]["code"] == "status_read_busy"
            else:
                started = time.monotonic()
                view = env.wizard.auto_stop(run_id)
                assert time.monotonic() - started < 1.0
                assert view["state"] == "STOPPED"
                assert view["lastFailure"]["code"] == "operator_stopped"
            assert bundle_path.read_bytes() == bundle_before
            assert env.wizard.state["safePark"] == park_before
            assert not opened
        finally:
            release.set()
            if timer is not None:
                timer.join(timeout=1)
            env.wizard.auto_stop(run_id)
            env.wizard._auto["thread"].join(timeout=2)


def test_auto_run_does_not_continue_after_initial_park_failure(tmp_path, monkeypatch):
    from scripts import motor_debug_server as debug

    env = auto_ready_env(tmp_path)
    commands_before = list(env.daemon.cmds)
    phase0 = []
    monkeypatch.setattr(env.wizard, "_auto_phase0_camera", lambda run: phase0.append(True))

    def busy(**kwargs):
        raise debug.DaemonCommandError("status_read_busy", "位置只读查询持续忙")

    env.wizard._position_read = busy
    env.wizard.auto_start()
    view = wait_auto(env, lambda view: view["state"] == "FAILED", timeout=2)

    assert view["lastFailure"]["code"] == "status_read_busy"
    assert not phase0
    assert env.daemon.cmds == commands_before
    assert env.wizard.state["safePark"]["confirmed"]


@pytest.mark.parametrize("code", ["daemon_unreachable", "daemon_command_timeout",
                                "device_context_mismatch"])
def test_auto_position_transport_failure_does_not_attempt_recovery(tmp_path, code):
    from scripts import motor_debug_server as debug

    env = auto_ready_env(tmp_path)
    commands_before = list(env.daemon.cmds)
    reads = []

    def unavailable(**kwargs):
        reads.append(True)
        raise debug.DaemonCommandError(code, "当前位置未确认，禁止自动恢复")

    env.wizard._position_read = unavailable
    env.wizard.auto_start()
    view = wait_auto(env, lambda view: view["state"] == "FAILED", timeout=2)

    assert view["lastFailure"]["code"] == code
    assert len(reads) == 1
    assert env.daemon.cmds == commands_before


@pytest.fixture(scope="module")
def failed_e_snapshot(tmp_path_factory):
    from drivers import calibration_bundle as cb

    root = tmp_path_factory.mktemp("failed-e-source")
    env = auto_ready_env(
        root,
        auto_config={"acceptance_timeout_s": 0.05},
        ocr_words=[FakeOcrWord("统一校准测试", 300, 400)],
    )
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda view: view["state"] == "FAILED", timeout=15)
    env.wizard._auto["thread"].join(timeout=2)
    assert view["phase"] == "E_ACCEPTANCE"
    assert view["lastFailure"]["code"] == "acceptance_timeout"
    run = cb.load_candidate("arm-01", view["runId"], root)
    assert run["validation"]["passed"]
    return deepcopy((env.wizard.state, cb.load_bundle("arm-01", root), run))


@pytest.fixture
def resumable_e(tmp_path, failed_e_snapshot):
    from drivers import calibration_bundle as cb

    env = auto_ready_env(tmp_path, ocr_words=[FakeOcrWord("统一校准测试", 300, 400)])
    state, bundle, run = deepcopy(failed_e_snapshot)
    env.wizard.state = state
    env.frame_seq = count(run["edge"]["frameSeq"] + 1)
    run["config"]["acceptance_timeout_s"] = 8.0
    env.wizard._save_state()
    cb.save_bundle(bundle, tmp_path)
    cb.save_candidate(run, tmp_path)
    return env, run


def test_e_resume_http_entry_points_removed():
    """2026-10-03 规格 §4.6：E 验收制度退役——auto-acceptance-resume/execute/record
    与手动 acceptance-* 经调试台入口一律 410（input_acceptance_removed），
    不得再借旧入口重跑定位或机械采样；输入资格由「允许输入与发送」开关管理。"""
    from scripts import motor_debug_server as debug

    class StubWizard:
        def __getattr__(self, name):
            raise AssertionError(f"退役入口不得触碰向导状态：{name}")

    for action in ("auto-acceptance-resume", "auto-acceptance-execute",
                   "auto-acceptance-record", "acceptance-execute",
                   "acceptance-record", "acceptance-advance"):
        with pytest.raises(ValueError) as caught:
            debug.wizard_http(StubWizard(), action, {"runId": "r"})
        assert getattr(caught.value, "code", None) == "input_acceptance_removed"


@pytest.mark.parametrize("position_issue", [
    "running", "holding", "missing_state", "away_xy", "away_z", "nonfinite",
    "unavailable", "session_changed",
])
def test_e_resume_requires_fresh_idle_park_after_unknown_cleanup(
        resumable_e, tmp_path, monkeypatch, position_issue):
    from drivers import calibration_bundle as cb
    from scripts import motor_debug_server as debug

    env, run = resumable_e
    run["events"].append({
        "event": "safe_park_failed", "phase": "E_ACCEPTANCE",
        "detail": {"error": "Broken pipe"},
    })
    cb.save_candidate(run, tmp_path)
    before = cb.candidate_path("arm-01", run["runId"], tmp_path).read_bytes()
    commands = list(env.daemon.cmds)
    spawned = []
    monkeypatch.setattr(env.wizard, "_auto_spawn", lambda *args, **kwargs: spawned.append(True))

    def read(**kwargs):
        pos = env.daemon.mpos()
        if position_issue == "running":
            pos["state"] = "Run"
        elif position_issue == "holding":
            pos["state"] = "Hold:0"
        elif position_issue == "missing_state":
            pos.pop("state")
        elif position_issue == "away_xy":
            pos["x"] += 1
        elif position_issue == "away_z":
            pos["z"] -= 1
        elif position_issue == "nonfinite":
            pos["x"] = float("nan")
        elif position_issue == "unavailable":
            raise debug.DaemonCommandError("daemon_unreachable", "Broken pipe")
        elif position_issue == "session_changed":
            env.daemon.session = "changed-during-mpos"
        return pos

    env.wizard._position_read = read
    with pytest.raises(cw.AutoRunError) as error:
        env.wizard.auto_resume_acceptance(run["runId"])
    assert error.value.park is False
    assert not spawned and env.daemon.cmds == commands
    if position_issue == "session_changed":
        # P06：读取期间换会话 → 恢复拒绝且候选执行资格撤销（留痕不删档）。
        revoked = cb.load_candidate("arm-01", run["runId"], tmp_path)
        assert revoked["executionEligibility"]["eligible"] is False
    else:
        assert cb.candidate_path("arm-01", run["runId"], tmp_path).read_bytes() == before
    assert not env.ime.tasks_created


def test_e_resume_preserves_unknown_cleanup_and_records_new_position(
        resumable_e, tmp_path, monkeypatch):
    from drivers import calibration_bundle as cb

    env, run = resumable_e
    failure = {"event": "safe_park_failed", "detail": {"error": "Broken pipe"}}
    run["events"].append(failure)
    cb.save_candidate(run, tmp_path)
    reads = []

    def read(**kwargs):
        reads.append(True)
        return env.daemon.mpos()

    env.wizard._position_read = read
    monkeypatch.setattr(env.wizard, "_auto_spawn", lambda *args, **kwargs: None)
    commands = list(env.daemon.cmds)
    env.wizard.auto_resume_acceptance(run["runId"])

    assert reads == [True]
    saved = cb.load_candidate("arm-01", run["runId"], tmp_path)
    assert failure in saved["events"]
    assert saved["acceptanceResumePosition"]["mpos"] == env.daemon.mpos()
    assert saved["acceptanceResumePosition"]["daemonSession"] == run["daemonSession"]
    assert env.daemon.cmds == commands
    assert saved["safePark"] == run["safePark"]


def test_e_resume_rechecks_position_in_runner_before_any_action(resumable_e):
    env, run = resumable_e
    reads = []
    commands = list(env.daemon.cmds)

    def read(**kwargs):
        reads.append(True)
        return {**env.daemon.mpos(), "state": "Idle" if len(reads) == 1 else "Run"}

    env.wizard._position_read = read
    env.wizard.auto_resume_acceptance(run["runId"])
    try:
        view = wait_auto(env, lambda view: view["state"] == "FAILED", timeout=2)
        assert view["lastFailure"]["code"] == "acceptance_resume_position_unconfirmed"
        assert env.daemon.cmds == commands
        assert not env.ime.tasks_created
        assert len(reads) == 2
    finally:
        env.wizard.auto_stop()
        env.wizard._auto["thread"].join(timeout=2)


def test_e_resume_manual_execution_cannot_bypass_position_gate(resumable_e, monkeypatch):
    env, run = resumable_e
    monkeypatch.setattr(env.wizard, "_auto_spawn", lambda *args, **kwargs: None)
    env.wizard.auto_resume_acceptance(run["runId"])
    commands = list(env.daemon.cmds)
    env.wizard._position_read = lambda **kwargs: {**env.daemon.mpos(), "state": "Run"}

    with pytest.raises(cw.AutoRunError) as error:
        env.wizard.auto_acceptance_execute(run["runId"], "ime_input_tap")

    assert error.value.code == "acceptance_resume_position_unconfirmed"
    assert error.value.park is False
    assert env.daemon.cmds == commands
    assert not env.ime.tasks_created


def test_e_resume_position_wait_cancellation_never_parks(resumable_e, tmp_path):
    from drivers import calibration_bundle as cb
    from scripts import motor_debug_server as debug

    env, run = resumable_e
    reading = threading.Event()
    reads = []
    commands = list(env.daemon.cmds)

    def read(**kwargs):
        reads.append(True)
        if len(reads) == 1:
            return env.daemon.mpos()
        reading.set()
        assert kwargs["cancel_event"].wait(2)
        raise debug.DaemonCommandError("status_read_cancelled", "只读等待已取消")

    env.wizard._position_read = read
    env.wizard.auto_resume_acceptance(run["runId"])
    try:
        assert reading.wait(2)
        started = time.monotonic()
        view = env.wizard.auto_stop(run["runId"])
        assert time.monotonic() - started < 1
        assert view["state"] == "STOPPED"
        assert env.daemon.cmds == commands
        assert not env.ime.tasks_created
        assert len(reads) == 2
        saved = cb.load_candidate("arm-01", run["runId"], tmp_path)
        assert saved["lastPark"] is None
        assert saved["events"][-1]["event"] == "safe_park_skipped"
    finally:
        env.wizard.auto_stop()
        env.wizard._auto["thread"].join(timeout=2)
