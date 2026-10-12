"""R2-10（B 包第二批）：实时键盘坐标穿过真实驱动（docs/2026-10-04-v1.10第二轮修复缺陷卡.md）。

缺陷：`drivers/daemon_arm.py::_resolve_business_target` 构建 live_ime_geometry
entry 时丢掉 frameW/frameH（手机 display 像素域尺寸），`_resolve_live_geometry_target`
却直接读 entry["frameW"]/entry["frameH"] → KeyError；且目标中心会被相机帧尺寸域
混算（nx 按 display 归一化后又乘相机帧宽做姿态补偿投影）。

红测走正式链：真实 Runtime 几何门（register_phone_credential+credentials 登记
每机独立凭据 → handshake → 手机长轮询领取 nonce → 回快照）注入 target_ref，
真实 DaemonArmDriver（FakeDaemon TCP transport 替换外部串口）消费 target_ref
完成 click。手机 display 1080x2400 与相机帧 640x480 明显不同；候选与删除均为
非中心点；机械坐标用独立预期值（robotToScreen 仿射逆映射手算）核对。

全部使用临时 data_root / fake transport；不接触生产端口（8788/8790/8792/8795）、
真实相机或真实机械臂。
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from drivers import calibration_bundle as cb
from drivers.arm import DriverError
from runtime.ime_geometry import CONTRACT_VERSION
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs
from tests.test_daemon_arm import CALIB_SESSION, FakeDaemon
from tests.test_deferred_input_acceptance import motion_bundle
from tests.test_v110_repair_ime_geometry import GEOM_TOKEN

IME_DEVICE = "k40-1"
ARM_DEVICE = "arm-01"

# robotToScreen 仿射（屏幕像素 → 机械 mm）：rx=(sx-2)/18, ry=(sy-3)/18。
# 独立预期值由此手算，与被测代码无共享实现。
ROBOT_TO_SCREEN = {"a": 18.0, "b": 0.0, "c": 2.0, "d": 0.0, "e": 18.0, "f": 3.0}
CANDIDATE_CENTER = ((54 + 1026) / 2.0, (2160 + 2280) / 2.0)  # (540, 2220)
DELETE_CENTER = ((960 + 1050) / 2.0, (1960 + 2050) / 2.0)    # (1005, 2005)
DISPLAY = {"display_id": 0, "width_px": 1080, "height_px": 2400,
           "rotation": 0, "density": 2.75}


def _expected_robot(screen_xy: tuple[float, float]) -> tuple[float, float]:
    sx, sy = screen_xy
    return (round((sx - ROBOT_TO_SCREEN["c"]) / ROBOT_TO_SCREEN["a"], 3),
            round((sy - ROBOT_TO_SCREEN["f"]) / ROBOT_TO_SCREEN["e"], 3))


def _v3_bundle() -> dict:
    bundle = motion_bundle()
    bundle["schemaVersion"] = cb.BUNDLE_SCHEMA_VERSION_V3
    bundle["cameraBindingId"] = str(uuid.uuid4())
    bundle["sourceGeometryId"] = "d" * 64
    bundle["daemonSession"] = CALIB_SESSION
    bundle["frame"] = {"width": 640, "height": 480}  # 相机帧尺寸 ≠ 手机 display
    bundle["screen"]["screenWidth"] = 1080
    bundle["screen"]["screenHeight"] = 2400
    bundle["xy"]["robotToScreen"] = dict(ROBOT_TO_SCREEN)
    bundle["bindingFingerprint"] = cb.binding_fingerprint(
        device_id=bundle["deviceId"], camera_id=bundle["cameraId"],
        camera_device_unique_id=bundle["cameraDeviceUniqueId"],
        daemon_addr=bundle["daemonAddr"], frame_width=640, frame_height=480)
    return bundle


async def _pose_evidence(*, wechat_send: bool = False,
                         screen_edge_advisory: bool = False) -> dict:
    """实时姿态门（外部相机 IO 替换）：姿态稳定（位移≤2px）。"""
    return {"checks": [],
            "pose": {"corners": [[80.0, 60.0], [560.0, 60.0],
                                 [560.0, 420.0], [80.0, 420.0]],
                     "matrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                     "movementPx": 0.4}}


def _snapshot_body(session_id: str, seq: int, nonce: str | None, *,
                   view_instance_id: str, display: dict | None = DISPLAY,
                   candidate_visible: bool = True,
                   candidate_bounds=(54, 2160, 1026, 2280),
                   delete_bounds=(960, 1960, 1050, 2050),
                   current_task_id: str | None = "t-r2-10-ime") -> dict:
    """本文件自带的手机快照（与 8788 几何协议同字段；支持本卡反例注入）。

    测试更正（§4.5，2026-10-03）：R3-02-1 要求快照携带原 IME 任务引用且候选
    栏规划必须绑定非空任务——current_task_id 由固定 None 改为默认携带任务号
    （删除键场景由 R3-02 测试显式传 None 验证原契约）。"""
    return {
        "contract_version": CONTRACT_VERSION,
        "runtime_session_id": session_id,
        "view_instance_id": view_instance_id,
        "seq": seq,
        "captured_uptime_ms": 123456,
        "display": display,
        "layout_version": "k40-candidate-1",
        "keyboard_active": True,
        "geometry_valid": True,
        "keyboard_bounds": {"left": 0, "top": 1560, "right": 1080, "bottom": 2400},
        "current_task_id": current_task_id,
        "reason": "snapshot" if nonce else "layout_ready",
        "request_nonce": nonce,
        "targets": [
            {"id": "ime_candidate_bar", "visible": candidate_visible,
             "enabled": candidate_visible, "clickable": candidate_visible,
             "bounds": {"left": candidate_bounds[0], "top": candidate_bounds[1],
                        "right": candidate_bounds[2], "bottom": candidate_bounds[3]}},
            {"id": "ime_delete_key", "visible": True, "enabled": True,
             "clickable": True,
             "bounds": {"left": delete_bounds[0], "top": delete_bounds[1],
                        "right": delete_bounds[2], "bottom": delete_bounds[3]}},
        ],
    }


class LiveGeometryEnv:
    """真实 Runtime + 真实 DaemonArmDriver（FakeDaemon 传输）+ 几何凭据。"""

    def __init__(self, tmp_path: Path, *, enable_input: bool = True):
        self.tmp_path = tmp_path
        self.daemon = FakeDaemon().start()
        root = tmp_path / "calibration"
        cb.save_bundle(_v3_bundle(), root)
        from runtime.config import PlatformConfig

        config = PlatformConfig(
            {"db_path": str(tmp_path / "fleet.db"),
             "evidence_dir": str(tmp_path / "evidence"),
             "plugin_dir": str(tmp_path / "plugins"),
             "hardware_mode": "simulation",
             "daemon_supervisor_enabled": False,
             "hotplug_discovery_enabled": False},
            root=tmp_path)
        self.runtime = ArmFleetRuntime(config)
        asyncio.run(self.runtime.devices.register_all(make_device_configs(1)))
        self.runtime.devices._devices[ARM_DEVICE]["ime_device_id"] = IME_DEVICE
        self.runtime.register_phone_credential(IME_DEVICE, GEOM_TOKEN)
        # 真实 DaemonArmDriver 替换仿真机械臂驱动（外部 IO=socket 由 FakeDaemon 替换）
        from drivers.daemon_arm import DaemonArmDriver

        self.driver = DaemonArmDriver(
            ARM_DEVICE, daemon_addr=f"127.0.0.1:{self.daemon.port}",
            data_root=root)
        self.runtime.devices._arms[ARM_DEVICE] = self.driver
        self.runtime.devices._devices[ARM_DEVICE].update({
            "daemon_session": CALIB_SESSION, "calibration_status": "validated"})
        asyncio.run(self.driver.connect())
        self.driver.live_calibration_gate = _pose_evidence
        if enable_input:
            # A-开关经真实设置写入口开启（R2-02 统一历史核对链路），不是改恒真
            outcome = self.runtime.input_control.write_settings(
                ARM_DEVICE, data_root=root,
                request={"enabled": True, "ime_device_id": IME_DEVICE,
                         "expected_revision": 0, "operator_id": "操作员",
                         "reason": "R2-10", "client_request_id": "r2-10-enable-1",
                         "correlation_id": "r2-10"},
                calibration_ready=True, binding_state=None,
                call_context={"origin": "human_put", "operator_id": "操作员",
                              "role": "operator",
                              "operation_id": "input_settings_update"})
            assert outcome["outcome"] == "applied", outcome
        self.geometry = self.runtime.ime_geometry
        self.session = self.geometry.handshake(IME_DEVICE, {
            "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
            "client_instance_id": str(uuid.uuid4())}, credentials=GEOM_TOKEN)
        self._seq = 0
        self._view = str(uuid.uuid4())
        self._phone_task: asyncio.Task | None = None
        self._snapshot_kwargs: dict = {}

    def close(self):
        if self._phone_task is not None:
            self._phone_task.cancel()
        try:
            asyncio.run(self.driver.disconnect())
        except Exception:
            pass
        self.daemon.stop()
        try:
            asyncio.run(self.runtime.shutdown())
        except Exception:
            pass

    # ---- 手机侧：领取 nonce → 回快照（真实验证器/会话协议） ----
    def start_phone(self):
        async def phone():
            while True:
                claimed = await asyncio.to_thread(
                    self.geometry.poll_next, IME_DEVICE,
                    self.session["runtime_session_id"], 1, GEOM_TOKEN)
                if claimed is None:
                    await asyncio.sleep(0.005)
                    continue
                self._seq += 1
                body = _snapshot_body(
                    self.session["runtime_session_id"], self._seq,
                    claimed["request_nonce"], view_instance_id=self._view,
                    **self._snapshot_kwargs)
                await asyncio.to_thread(
                    self.geometry.report, IME_DEVICE, body, GEOM_TOKEN)

        self._phone_task = asyncio.create_task(phone())

    def tap_commands(self):
        return [line for line in self.daemon.commands if line.startswith("TAP")]


@pytest.fixture()
def live_env(tmp_path):
    env = LiveGeometryEnv(tmp_path)
    try:
        yield env
    finally:
        env.close()


@pytest.fixture()
def live_env_switch_off(tmp_path):
    env = LiveGeometryEnv(tmp_path, enable_input=False)
    try:
        yield env
    finally:
        env.close()


def _click_params(target_ref: dict, target_id: str) -> dict:
    # 与 runtime._plugin_invoke_capability 的正式接线同形（几何门产出 target_ref
    # 原样进入驱动 arm.click 参数，runtime.py 1573-1576 → 1625-1626）。
    return {"target_id": target_id, "target_ref": target_ref}


async def _gate_and_click(env: LiveGeometryEnv, target_id: str) -> dict:
    target_ref = await env.runtime._ime_live_geometry_target(ARM_DEVICE, target_id)
    assert target_ref["source"] == "live_ime_geometry"
    return await env.driver.click(_click_params(target_ref, target_id))


def test_r2_10_candidate_and_delete_reach_mechanics_with_display_pixels(live_env):
    """候选与删除（均非中心点）：无 KeyError；机械坐标=display 像素经
    robotToScreen 逆映射的独立预期值；合法输入恰一次接触。"""

    async def scenario():
        env = live_env
        env.start_phone()
        result = await _gate_and_click(env, "ime_candidate_bar")
        expected = _expected_robot(CANDIDATE_CENTER)
        # 独立预期值核对（仿射逆映射手算），不是只检查门返回对象
        assert result["resolved_robot_xy"] == pytest.approx(list(expected)), \
            (result["resolved_robot_xy"], expected)
        assert env.daemon.mpos[0] == pytest.approx(expected[0], abs=1e-2)
        assert env.daemon.mpos[1] == pytest.approx(expected[1], abs=1e-2)
        assert len(env.tap_commands()) == 1, "合法候选点击恰一次接触"

        # 删除键（非中心点，第二次独立目标）
        result2 = await _gate_and_click(env, "ime_delete_key")
        expected2 = _expected_robot(DELETE_CENTER)
        assert result2["resolved_robot_xy"] == pytest.approx(list(expected2)), \
            (result2["resolved_robot_xy"], expected2)
        assert len(env.tap_commands()) == 2, "删除键恰一次新接触"
        # 相机帧尺寸域（640x480 换算）绝不能冒充 display 域结果
        camera_domain = _expected_robot(
            (CANDIDATE_CENTER[0] / 1080 * 640, CANDIDATE_CENTER[1] / 2400 * 480))
        assert result["resolved_robot_xy"] != pytest.approx(list(camera_domain)), \
            "坐标被相机帧尺寸域污染（屏幕像素被相机投影）"

    asyncio.run(scenario())


def test_r2_10_missing_or_illegal_display_never_falls_back_to_camera_size(live_env):
    """display 尺寸缺失/非法：驱动明确拒绝、零接触；尺寸绝不回退相机默认 640x480。"""

    async def scenario():
        env = live_env
        env.start_phone()
        # 经真实几何门取得合法 ref 后，按反例改坏 display 域字段直接驱动驱动
        base_ref = await env.runtime._ime_live_geometry_target(
            ARM_DEVICE, "ime_candidate_bar")
        for broken, desc in (
                ({k: v for k, v in base_ref.items()
                  if k not in ("frameW", "frameH")}, "missing"),
                (dict(base_ref, frameW=0), "zero"),
                (dict(base_ref, frameW=-1080), "negative"),
                (dict(base_ref, frameW=float("nan")), "nan"),
                (dict(base_ref, frameH=None), "null-height")):
            with pytest.raises(DriverError) as caught:
                await env.driver.click(
                    _click_params(dict(broken), "ime_candidate_bar"))
            assert caught.value.category in (
                "driver_policy_rejected", "driver_limit_exceeded"), (desc, caught.value)
            message = str(caught.value.message)
            assert "640" not in message and "480" not in message, \
                f"拒绝信息不得按相机默认尺寸回退解释（{desc}）：{message}"
        assert len(env.tap_commands()) == 0, "display 缺失/非法 → 零接触"
        assert env.daemon.mpos[0] == pytest.approx(2.0), "机械臂未走位"

    asyncio.run(scenario())


def test_r2_10_snapshot_without_display_rejected_at_runtime_gate(live_env):
    """快照缺 display/目标隐藏 → Runtime 几何门拒绝，零接触；不落旧固定坐标。"""

    async def scenario():
        env = live_env
        # 手机快照不带 display（Android 布局异常场景）
        env._snapshot_kwargs = {"display": None}
        env.start_phone()
        with pytest.raises(DriverError) as caught:
            await env.runtime._ime_live_geometry_target(ARM_DEVICE,
                                                        "ime_candidate_bar")
        assert caught.value.motion_not_started is True
        assert "不回退旧固定坐标" in str(caught.value)
        assert len(env.tap_commands()) == 0

        # 快照隐藏候选目标（visible=False）→ 同样拒绝、零接触
        env._snapshot_kwargs = {"candidate_visible": False}
        with pytest.raises(DriverError):
            await env.runtime._ime_live_geometry_target(ARM_DEVICE,
                                                        "ime_candidate_bar")
        assert len(env.tap_commands()) == 0
        assert env.daemon.mpos[0] == pytest.approx(2.0)

    asyncio.run(scenario())


def test_r2_10_stale_or_superseded_snapshot_zero_contact(live_env):
    """快照过期（nonce 超时未领取）/被更大 seq 超越 → 拒绝且零接触。"""

    async def scenario():
        env = live_env
        # 手机不领取 nonce：门等超时
        with pytest.raises(DriverError) as caught:
            await env.runtime._ime_live_geometry_target(ARM_DEVICE,
                                                        "ime_delete_key")
        assert caught.value.motion_not_started is True
        assert len(env.tap_commands()) == 0

        # 手机领取门的 nonce → 回快照 → 立即用更大 seq 覆盖（快照被超越）
        sid = env.session["runtime_session_id"]

        async def phone_answer_then_supersede():
            while True:
                claimed = await asyncio.to_thread(
                    env.geometry.poll_next, IME_DEVICE, sid, 1, GEOM_TOKEN)
                if claimed is None:
                    await asyncio.sleep(0.005)
                    continue
                env._seq += 1
                await asyncio.to_thread(
                    env.geometry.report, IME_DEVICE, _snapshot_body(
                        sid, env._seq, claimed["request_nonce"],
                        view_instance_id=env._view), GEOM_TOKEN)
                env._seq += 1
                await asyncio.to_thread(
                    env.geometry.report, IME_DEVICE, _snapshot_body(
                        sid, env._seq, None, view_instance_id=env._view),
                    GEOM_TOKEN)
                return

        superseder = asyncio.create_task(phone_answer_then_supersede())
        try:
            with pytest.raises(DriverError) as caught:
                await env.runtime._ime_live_geometry_target(ARM_DEVICE,
                                                            "ime_delete_key")
        finally:
            superseder.cancel()
        assert caught.value.motion_not_started is True
        assert len(env.tap_commands()) == 0, "快照被超越 → 零接触"
        assert env.daemon.mpos[0] == pytest.approx(2.0)

    asyncio.run(scenario())


def test_r2_10_switch_off_blocks_contact_real_gate(live_env_switch_off):
    """输入开关关闭（真实 is_enabled：未经真实写入口开启=关）：接触命令 0。

    本文件不 monkeypatch _input_switch_allowed（保留真实门禁）。"""

    async def scenario():
        env = live_env_switch_off
        env.start_phone()
        with pytest.raises(DriverError) as caught:
            await _gate_and_click(env, "ime_candidate_bar")
        assert caught.value.category == "input_switch_disabled"
        assert len(env.tap_commands()) == 0, "开关关闭 → 零接触"
        assert env.daemon.mpos[0] == pytest.approx(2.0)

    asyncio.run(scenario())


def test_r2_10_phone_move_guard_blocks_live_contact(live_env):
    """姿态守卫：手机已偏移（>2px）时 live 目标拒绝接触（不做相机投影补偿）。"""

    async def scenario():
        env = live_env
        async def moved_gate(*, wechat_send=False, screen_edge_advisory=False):
            return {"checks": [],
                    "pose": {"corners": [[95.0, 75.0], [575.0, 75.0],
                                         [575.0, 435.0], [95.0, 435.0]],
                             "matrix": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
                             "movementPx": 21.7}}
        env.driver.live_calibration_gate = moved_gate
        env.start_phone()
        with pytest.raises(DriverError) as caught:
            await _gate_and_click(env, "ime_candidate_bar")
        assert caught.value.motion_not_started is True
        assert len(env.tap_commands()) == 0, "手机偏移 → 零接触"

    asyncio.run(scenario())
