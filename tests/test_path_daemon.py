"""PATH1 连续路径宏（path.v1）测试：协议验证、daemon 宏序列、仿真与桥驱动。

纯软件 + 仿真测试：不连接真机串口、不依赖真实 arm_daemon3 进程。
覆盖规范第 6.5/22.9/15.1 节的 P3 阶段要求。
"""
from __future__ import annotations

import asyncio
import json

import pytest

import scripts.arm_daemon3 as daemon_module
from scripts.arm_daemon3 import ArmHolder
from drivers.arm import ArmDriver, DriverError
from drivers.daemon_protocol import (
    PATH1_CONTACT_TIME_MAX_MS,
    PATH1_FEATURE,
    PATH1_HOLD_MS_MAX,
    PATH1_LINE_MAX_BYTES,
    PATH1_MAX_SEGMENTS,
    reject_json_constant,
    strict_object_pairs,
    validate_path1_payload,
)
from drivers.fake_arm import FakeArmDriver


# ---------------------------------------------------------------- 载荷构造

def legal_payload(**overrides) -> dict:
    payload = {
        "version": 1,
        "depth": 3.0,
        "z_feed": 1500.0,
        "segments": [
            {"op": "down"},
            {"op": "move", "dx": 5.0, "dy": 0.0, "feed": 600.0},
            {"op": "hold", "ms": 120},
            {"op": "up"},
        ],
    }
    payload.update(overrides)
    return payload


def with_segments(*segments) -> dict:
    return legal_payload(segments=list(segments))


# ---------------------------------------------------------------- 1. 载荷验证

def test_validate_path1_payload_legal_returns_normalized_copy():
    payload = legal_payload()
    normalized = validate_path1_payload(payload)
    assert normalized == payload
    assert normalized is not payload
    assert normalized["segments"] is not payload["segments"]
    assert normalized["segments"][1] is not payload["segments"][1]


def test_validate_path1_payload_normalizes_numbers():
    normalized = validate_path1_payload({
        "version": 1, "depth": 3, "z_feed": 1500,
        "segments": [{"op": "down"},
                     {"op": "move", "dx": 2, "dy": -1, "feed": 500},
                     {"op": "hold", "ms": 20},
                     {"op": "up"}],
    })
    assert normalized["depth"] == 3.0
    assert normalized["z_feed"] == 1500.0
    assert normalized["segments"][1] == {"op": "move", "dx": 2.0, "dy": -1.0, "feed": 500.0}
    assert normalized["segments"][2] == {"op": "hold", "ms": 20}


@pytest.mark.parametrize("payload", [
    # 未知 op
    with_segments({"op": "down"}, {"op": "drag", "dx": 1, "dy": 0, "feed": 500}, {"op": "up"}),
    # down 非首项
    with_segments({"op": "move", "dx": 1, "dy": 0, "feed": 500}, {"op": "down"}, {"op": "up"}),
    # up 非末项
    with_segments({"op": "down"}, {"op": "up"},
                  {"op": "move", "dx": 1, "dy": 0, "feed": 500}),
    # 两个 down
    with_segments({"op": "down"}, {"op": "down"}, {"op": "up"}),
    # 缺 up
    with_segments({"op": "down"}, {"op": "move", "dx": 1, "dy": 0, "feed": 500}),
    # 字符串数字
    with_segments({"op": "down"},
                  {"op": "move", "dx": "3", "dy": 0, "feed": 500}, {"op": "up"}),
    # NaN
    with_segments({"op": "down"},
                  {"op": "move", "dx": float("nan"), "dy": 0, "feed": 500}, {"op": "up"}),
    # hold 超时
    with_segments({"op": "down"}, {"op": "hold", "ms": PATH1_HOLD_MS_MAX + 1000}, {"op": "up"}),
    # 总计划时间 > 5000ms（两段 hold 2000ms + 25mm@F600 ≈ 2500ms）
    with_segments({"op": "down"}, {"op": "hold", "ms": 2000}, {"op": "hold", "ms": 2000},
                  {"op": "move", "dx": 25.0, "dy": 0.0, "feed": 600.0}, {"op": "up"}),
    # 总距离 > 150mm
    with_segments({"op": "down"},
                  {"op": "move", "dx": 151.0, "dy": 0.0, "feed": 3000.0}, {"op": "up"}),
    # move feed > SWIPE_FEED_MAX
    with_segments({"op": "down"},
                  {"op": "move", "dx": 10.0, "dy": 0.0, "feed": 3001.0}, {"op": "up"}),
    # 字符串值含换行注入
    with_segments({"op": "down"}, {"op": "hold\n", "ms": 10}, {"op": "up"}),
    # 未知顶层键
    legal_payload(extra="nope"),
    # version 错误
    legal_payload(version=2),
    # depth 非正
    legal_payload(depth=0),
    legal_payload(depth=-1.0),

    # segments 非数组 / 空数组
    legal_payload(segments={"op": "down"}),
    legal_payload(segments=[]),
    # 未知段字段
    with_segments({"op": "down", "depth": 3},
                  {"op": "move", "dx": 1, "dy": 0, "feed": 500}, {"op": "up"}),
])
def test_validate_path1_payload_rejects(payload):
    with pytest.raises(ValueError):
        validate_path1_payload(payload)


def test_validate_path1_payload_rejects_too_many_segments():
    segments = [{"op": "down"}]
    for _ in range(PATH1_MAX_SEGMENTS + 1):
        segments.append({"op": "move", "dx": 0.001, "dy": 0.0, "feed": 500.0})
    segments.append({"op": "up"})
    with pytest.raises(ValueError):
        validate_path1_payload(legal_payload(segments=segments))


def test_validate_path1_payload_missing_top_level_key():
    for key in ("version", "depth", "z_feed", "segments"):
        payload = legal_payload()
        payload.pop(key)
        with pytest.raises(ValueError):
            validate_path1_payload(payload)


def test_json_strict_hooks_reject_duplicate_keys_and_constants():
    with pytest.raises(ValueError, match="重复键"):
        json.loads('{"version":1,"version":2}', object_pairs_hook=strict_object_pairs)
    with pytest.raises(ValueError, match="NaN"):
        json.loads('{"x": NaN}', parse_constant=reject_json_constant)
    with pytest.raises(ValueError, match="Infinity"):
        json.loads('{"x": Infinity}', parse_constant=reject_json_constant)
    # 合法 JSON 正常解析
    assert json.loads('{"a": 1}', object_pairs_hook=strict_object_pairs,
                      parse_constant=reject_json_constant) == {"a": 1}


# ---------------------------------------------------------------- 2. daemon 宏（handle 层）

class _FakeSerial:
    def __init__(self):
        self.writes: list[bytes] = []

    def write(self, data: bytes):
        self.writes.append(data)
        return len(data)


class _FakeArm:
    def __init__(self):
        self.session = "test-session"
        self.closed = False
        self.serial = _FakeSerial()

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self, reads):
        self.reads = iter(reads)
        self.sent = []
        self.closed = False
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, _mode):
        return self

    def readline(self, _size=-1):
        return next(self.reads)

    def close(self):
        self.closed = True


@pytest.fixture
def holder(monkeypatch):
    monkeypatch.setattr(ArmHolder, "_watch", lambda self: None)
    value = ArmHolder()
    value.arm = _FakeArm()
    monkeypatch.setattr(daemon_module, "HOLDER", value)
    return value


def path1_line(payload: dict) -> str:
    return "PATH1 " + json.dumps(payload, separators=(",", ":"))


def test_path1_macro_emits_ordered_controller_commands(holder):
    calls = []
    holder.arm.send = lambda cmd: calls.append(cmd)
    holder.arm.idle = lambda: calls.append("IDLE") or {
        "x": 5.0, "y": -5.0, "z": -8.0, "state": "Idle"}
    holder.leases.claim(1, "debug", "test")

    result = json.loads(daemon_module.handle(path1_line({
        "version": 1, "depth": 3.0, "z_feed": 1500.0,
        "segments": [
            {"op": "down"},
            {"op": "move", "dx": 5.0, "dy": 0.0, "feed": 600.0},
            {"op": "hold", "ms": 120},
            {"op": "move", "dx": 0.0, "dy": -5.0, "feed": 600.0},
            {"op": "up"},
        ]}), 1))

    assert calls == [
        "G91",
        "G1 Z3.000 F1500",
        "G1 X5.000 Y0.000 F600",
        "G4 P0.120",
        "G1 X0.000 Y-5.000 F600",
        "G1 Z-3.000 F1500",
        "G90",
        "IDLE",
    ]
    assert result["path_completed"] is True
    assert result["segments_completed"] == 5
    assert result["contact_released"] is True
    assert result["state"] == "Idle"


def test_path1_advertised_as_motion_command_and_feature(holder):
    assert "PATH1" in daemon_module.MOTION_COMMANDS
    assert PATH1_FEATURE in holder.status()["motionFeatures"]


@pytest.mark.parametrize("payload", [
    legal_payload(version=2),
    legal_payload(depth=0),
    with_segments({"op": "down"}, {"op": "hold", "ms": 3000}, {"op": "up"}),
    with_segments({"op": "down"},
                  {"op": "move", "dx": "3", "dy": 0, "feed": 500}, {"op": "up"}),
    with_segments({"op": "down"}, {"op": "up"}, {"op": "hold", "ms": 10}),
])
def test_path1_invalid_payload_rejected_before_serial_write(holder, payload):
    holder.leases.claim(1, "debug", "test")
    with pytest.raises(ValueError):
        daemon_module.handle(path1_line(payload), 1)
    assert holder.arm.serial.writes == []


def test_path1_rejects_nan_and_duplicate_keys_via_strict_loads(holder):
    holder.leases.claim(1, "debug", "test")
    with pytest.raises(ValueError):
        daemon_module.handle('PATH1 {"version":1,"depth":NaN,"z_feed":1500,'
                             '"segments":[{"op":"down"},{"op":"up"}]}', 1)
    with pytest.raises(ValueError):
        daemon_module.handle('PATH1 {"version":1,"version":1,"depth":3,"z_feed":1500,'
                             '"segments":[{"op":"down"},{"op":"up"}]}', 1)
    assert holder.arm.serial.writes == []


def test_path1_rejects_line_over_byte_budget(holder):
    holder.leases.claim(1, "debug", "test")
    line = "PATH1 " + " " * (PATH1_LINE_MAX_BYTES + 64) + json.dumps(
        legal_payload(), separators=(",", ":"))
    with pytest.raises(ValueError, match="字节"):
        daemon_module.handle(line, 1)
    assert holder.arm.serial.writes == []


def test_path1_invalid_macro_returns_error_line_to_client(holder):
    holder.leases.claim(1, "debug", "test")
    conn = _FakeConn([(path1_line(legal_payload(depth=0)) + "\n").encode(), b""])
    daemon_module.serve_client(conn, 1)
    assert conn.sent[-1].startswith(b"ERROR:")
    assert holder.arm.serial.writes == []


# ---------------------------------------------------------------- 3. 仿真驱动

def fake_plan(strokes=1) -> dict:
    return {
        "execution_mode": "path1",
        "profile": {"hoverZ": -8.0, "tapDepth": 8.0, "tapFeed": 1500.0},
        "binding": {"device_id": "fake-01"},
        "strokes": [
            {"segments": [{"op": "down"}, {"op": "up"}],
             "mech_points": [{"robotX": 0.0, "robotY": 0.0}]}
            for _ in range(strokes)
        ],
    }


def test_fake_arm_execute_gesture_program_ok():
    arm = FakeArmDriver("fake-01")
    asyncio.run(arm.connect())
    result = asyncio.run(arm.execute_gesture_program(fake_plan()))
    assert result["ok"] is True
    assert result["controller_status"] == "completed"
    assert result["stroke_count"] == 1
    assert result["motion_verified"] is True
    assert result["outcome_hint"] == "success"
    assert result["effect_status"] == "unverified"
    assert arm.action_log[-1]["action"] == "execute_gesture_program"
    assert arm.action_log[-1]["strokes"]
    assert PATH1_FEATURE in asyncio.run(arm.read_state())["motion_features"]


def test_fake_arm_execute_gesture_program_fail_path_fault():
    arm = FakeArmDriver("fake-01")
    asyncio.run(arm.connect())
    arm.faults["fail_path"] = True
    with pytest.raises(DriverError) as exc:
        asyncio.run(arm.execute_gesture_program(fake_plan()))
    assert exc.value.category == "driver_motion_failed"


def test_fake_arm_execute_gesture_program_rejects_other_modes():
    arm = FakeArmDriver("fake-01")
    asyncio.run(arm.connect())
    plan = fake_plan()
    plan["execution_mode"] = "swipe"
    with pytest.raises(DriverError) as exc:
        asyncio.run(arm.execute_gesture_program(plan))
    assert exc.value.category == "driver_gesture_invalid"
    assert arm.action_log[-1]["action"] == "connect"


def test_fake_arm_execute_gesture_program_disconnect_after_is_unknown():
    arm = FakeArmDriver("fake-01")
    asyncio.run(arm.connect())
    arm.faults["disconnect_after"] = True
    result = asyncio.run(arm.execute_gesture_program(fake_plan()))
    assert result["motion_verified"] is False
    assert result["outcome_hint"] == "unknown"


# ---------------------------------------------------------------- 4. daemon 驱动桥

@pytest.fixture()
def path_rig(tmp_path):
    from drivers import calibration_bundle as cb
    from tests.test_daemon_arm import FakeDaemon, make_driver
    from tests.test_screen_gesture_driver import gesture_bundle

    bundle = gesture_bundle()
    cb.save_bundle(bundle, tmp_path)
    daemon = FakeDaemon(mpos=(-10.0, 49.0, -8.0)).start()
    driver = make_driver(daemon, tmp_path)
    try:
        asyncio.run(driver.connect())
        yield daemon, driver, bundle, tmp_path
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()


def daemon_plan(bundle, strokes=None) -> dict:
    from drivers import screen_gestures as sg

    if strokes is None:
        strokes = [{
            "segments": [
                {"op": "down"},
                {"op": "move", "dx": 5.0, "dy": 0.0, "feed": 600.0},
                {"op": "move", "dx": 0.0, "dy": 5.0, "feed": 600.0},
                {"op": "hold", "ms": 50},
                {"op": "up"},
            ],
            "mech_points": [
                {"robotX": -10.0, "robotY": 49.0},
                {"robotX": -5.0, "robotY": 49.0},
                {"robotX": -5.0, "robotY": 54.0},
            ],
        }]
    return {
        "execution_mode": "path1",
        "binding": sg.binding_snapshot(bundle),
        "profile": sg.calibrated_profile(bundle),
        "strokes": strokes,
    }


def test_bridge_path1_single_stroke_compact_single_line(path_rig):
    daemon, driver, bundle, _ = path_rig
    result = asyncio.run(driver.execute_gesture_program(daemon_plan(bundle)))

    assert len(daemon.commands) == 1
    command = daemon.commands[0]
    assert command.startswith("PATH1 {")
    assert "\n" not in command and "\r" not in command
    payload = json.loads(command[6:])
    assert payload["version"] == 1
    assert payload["depth"] == 8.0
    assert payload["z_feed"] == 1500.0
    assert [seg["op"] for seg in payload["segments"]] == ["down", "move", "move", "hold", "up"]

    assert result["ok"] is True
    assert result["controller_completed"] is True
    assert result["controller_status"] == "completed"
    assert result["stroke_count"] == 1
    assert result["stroke_results"][0]["segments_completed"] == 5
    assert result["stroke_results"][0]["path_completed"] is True
    assert result["stroke_results"][0]["contact_released"] is True
    assert result["effect_status"] == "unverified"
    assert result["outcome_hint"] == "unknown"  # 无摄像头核验 → 不得当成功
    assert result["position"] == {"x": -5.0, "y": 54.0, "z": -8.0}
    assert daemon.mpos == [-5.0, 54.0, -8.0]
    assert daemon.lease is None
    assert not daemon.transactions


def test_bridge_path1_multi_stroke_travels_between_strokes(path_rig):
    daemon, driver, bundle, _ = path_rig
    daemon.mpos = [-10.0, 49.0, 5.0]  # 悬停上方 13mm，需要分段回悬停
    strokes = [
        {"segments": [{"op": "down"},
                      {"op": "move", "dx": 5.0, "dy": 0.0, "feed": 600.0},
                      {"op": "up"}],
         "mech_points": [{"robotX": -10.0, "robotY": 49.0},
                         {"robotX": -5.0, "robotY": 49.0}]},
        {"segments": [{"op": "down"},
                      {"op": "move", "dx": 0.0, "dy": 5.0, "feed": 600.0},
                      {"op": "up"}],
         "mech_points": [{"robotX": 2.0, "robotY": 49.0},
                         {"robotX": 2.0, "robotY": 54.0}]},
    ]
    result = asyncio.run(driver.execute_gesture_program(daemon_plan(bundle, strokes)))

    labels = [p["phase"] for p in result["phases"]]
    assert labels == ["path_hover_z", "path_hover_z", "path_stroke_0",
                      "path_to_start", "path_stroke_1"]
    commands = daemon.commands
    assert commands[0] == "Z -6.500 F1500"
    assert commands[1] == "Z -6.500 F1500"
    assert commands[2].startswith("PATH1 {")
    assert commands[3] == "XY 7.000 0.000 F5000"
    assert commands[4].startswith("PATH1 {")
    assert all("\n" not in cmd for cmd in commands)

    assert result["controller_completed"] is True
    assert result["stroke_count"] == 2
    assert [r["segments_completed"] for r in result["stroke_results"]] == [3, 3]
    assert daemon.mpos == [2.0, 54.0, -8.0]
    assert result["position"] == {"x": 2.0, "y": 54.0, "z": -8.0}


def test_bridge_path1_feature_missing_rejects_before_motion(path_rig):
    daemon, driver, bundle, _ = path_rig
    daemon.motion_features = ["tap2.v1", "swipe.v1"]
    with pytest.raises(DriverError, match="连续路径"):
        asyncio.run(driver.execute_gesture_program(daemon_plan(bundle)))
    assert daemon.commands == []
    assert not any(text.startswith("CLAIM") for _, text in daemon.requests)


def test_bridge_path1_invalid_segments_rejected_before_motion(path_rig):
    daemon, driver, bundle, _ = path_rig
    plan = daemon_plan(bundle)
    plan["strokes"][0]["segments"][3]["ms"] = 3000
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.execute_gesture_program(plan))
    assert exc.value.category == "driver_gesture_invalid"
    assert daemon.commands == []


@pytest.mark.parametrize("mode", ["swipe", None])
def test_bridge_path1_rejects_non_path1_plans(path_rig, mode):
    daemon, driver, bundle, _ = path_rig
    plan = daemon_plan(bundle)
    plan["execution_mode"] = mode
    with pytest.raises(DriverError) as exc:
        asyncio.run(driver.execute_gesture_program(plan))
    assert exc.value.category == "driver_gesture_invalid"
    assert daemon.commands == []


def test_bridge_path1_reply_without_completion_is_unknown(path_rig):
    daemon, driver, bundle, _ = path_rig
    daemon.response_hook = lambda line, reply, _conn: (
        json.dumps({"x": -5.0, "y": 54.0, "z": -8.0, "state": "Idle"})
        if line.startswith("PATH1 ") else reply)
    with pytest.raises(DriverError, match="结果未知"):
        asyncio.run(driver.execute_gesture_program(daemon_plan(bundle)))
    assert not driver.connected


def test_bridge_path1_endpoint_mismatch_is_unknown(path_rig):
    daemon, driver, bundle, _ = path_rig
    daemon.response_hook = lambda line, reply, _conn: (
        json.dumps({"x": 2.0, "y": 54.0, "z": -8.0, "state": "Idle",
                    "path_completed": True, "segments_completed": 5,
                    "contact_released": True})
        if line.startswith("PATH1 ") else reply)
    with pytest.raises(DriverError, match="终点|结果未知"):
        asyncio.run(driver.execute_gesture_program(daemon_plan(bundle)))
    assert not driver.connected


# ---------------------------------------------------------------- 5. 基类默认不支持

class _BareDriver(ArmDriver):
    async def connect(self): return {}
    async def disconnect(self): return {}
    async def read_state(self): return {}
    async def go_home(self): return {}
    async def move_preset(self, position_id): return {}
    async def click(self, target): return {}
    async def double_tap(self, target): return {}
    async def swipe(self, gesture): return {}
    async def stop(self): return {}
    async def estop(self): return {}

    @property
    def driver_version(self): return "bare-1"

    def supports(self, capability_id): return False


def test_base_driver_does_not_support_gesture_program():
    with pytest.raises(DriverError) as exc:
        asyncio.run(_BareDriver().execute_gesture_program(fake_plan()))
    assert exc.value.category == "driver_policy_rejected"
    assert "path.v1" in exc.value.message


# ---------------------------------------------------------------- 6. 校准双击自检（22.17 节）

def calibration_params(**overrides) -> dict:
    params = {
        "run_id": "run-0001",
        "trial_id": "trial-01",
        "plan_id": "plan-abc123",
        "binding": {"device_id": "fake-01"},
        "point": {"source": "screen_normalized", "x": 0.5, "y": 0.5},
        "interval_ms": 50,
    }
    params.update(overrides)
    return params


def test_fake_arm_test_calibrated_double_tap_ok_and_fail():
    arm = FakeArmDriver("fake-01")
    asyncio.run(arm.connect())

    result = asyncio.run(arm.test_calibrated_double_tap(calibration_params()))
    assert result["ok"] is True
    assert result["controller_status"] == "completed"
    assert result["final_mpos"] == {"x": 0.0, "y": 0.0, "z": -8.0, "state": "Idle"}
    assert result["contact_released"] is True
    assert result["motion_verified"] is True
    assert result["outcome_hint"] == "success"
    assert result["phases"]
    assert arm.action_log[-1]["action"] == "test_calibrated_double_tap"
    assert arm.action_log[-1]["params"]["plan_id"] == "plan-abc123"

    arm.faults["fail_double_tap"] = True
    with pytest.raises(DriverError) as exc:
        asyncio.run(arm.test_calibrated_double_tap(calibration_params()))
    assert exc.value.category == "driver_motion_failed"


def test_base_driver_does_not_support_calibrated_double_tap():
    with pytest.raises(DriverError) as exc:
        asyncio.run(_BareDriver().test_calibrated_double_tap(calibration_params()))
    assert exc.value.category == "driver_policy_rejected"
    assert "校准双击自检" in exc.value.message


def test_bridge_test_calibrated_double_tap_reuses_double_tap(path_rig):
    from drivers import screen_gestures as sg

    daemon, driver, bundle, _ = path_rig
    result = asyncio.run(driver.test_calibrated_double_tap({
        "run_id": "run-0001",
        "trial_id": "trial-01",
        "plan_id": "plan-abc123",
        "binding": sg.binding_snapshot(bundle),
        "point": {"source": "screen_normalized", "x": 0.5, "y": 0.5},
        "interval_ms": 50,
    }))
    assert result["plan_id"] == "plan-abc123"
    assert result["controller_status"] == "completed"
    assert result["final_mpos"]["z"] == -8.0  # 双击净位移为零，停在悬停高度
    assert result["final_mpos"]["x"] == pytest.approx(-7.419, abs=0.01)
    assert result["final_mpos"]["y"] == pytest.approx(26.323, abs=0.01)
    assert result["contact_released"] is True
    assert result["outcome_hint"] == "unknown"  # 无摄像头核验 → 不得当成功
    assert [p["phase"] for p in result["phases"]][-1] == "double_tap_retract"
    assert any(cmd.startswith("TAP2 3.000 F1500 I0.05") for cmd in daemon.commands)
    assert daemon.lease is None
    assert not daemon.transactions


def test_validate_path1_payload_contact_time_boundary():
    """接触时间上限边界与常量绑定（R1 卫生项：不再靠手算 5000）。

    2000(hold) + 30mm@F600(3000ms) = 恰好 PATH1_CONTACT_TIME_MAX_MS → 通过；
    追加 1ms hold → 超上限拒绝。
    """
    at_limit = legal_payload(segments=[
        {"op": "down"},
        {"op": "hold", "ms": PATH1_HOLD_MS_MAX},
        {"op": "move", "dx": 30.0, "dy": 0.0, "feed": 600.0},
        {"op": "up"}])
    validate_path1_payload(at_limit)
    over = legal_payload(segments=[
        {"op": "down"},
        {"op": "hold", "ms": PATH1_HOLD_MS_MAX},
        {"op": "move", "dx": 30.0, "dy": 0.0, "feed": 600.0},
        {"op": "hold", "ms": 1},
        {"op": "up"}])
    with pytest.raises(ValueError, match="接触时间"):
        validate_path1_payload(over)
