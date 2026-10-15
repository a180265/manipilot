"""Auto_CS#20 纯观测通道：screen-observation 解除机械臂会话绑定。

四个面（全部离线合成，无硬件、无串口会话）：
1. 校准档案新状态 observation_ready：观测校验门跳过 daemonSession 绑定，
   deviceId/cameraId/矩阵与四角几何校验保留；ready/motion_ready 原语义零变化；
   运行前运动门禁对 observation_ready 一律拒绝（纯观测档案不能驱动动作）。
2. 相机-only 档案形状（屏幕几何+cameraToScreen 单应+edgeModel，运动字段
   全部未校准）可经向导观测路径保存/晋升，无需串口会话。
3. 向导 goal="observation"：start 不要求机械臂连接 → 手机校准页 open 直接
   进入屏幕几何阶段（屏幕坐标域取本会话校准页 open 上报）→ edge_capture/
   camera_fit 复用 SCREEN_GEOMETRY 阶段既有实现（零机械动作）→ finish 置
   observation_ready；运动域操作在观测档整体禁用（OfflineDaemon 零指令为证）。
4. screen-observation 加入启用前冻结只读豁免（GET/HEAD 放行，写仍拒绝）。
既有运动路径语义由既有套件背书，本文件不重复。
"""
from __future__ import annotations

import hashlib
import time as _time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from adapters.vision.ocr import OcrResult, OcrWord
from api.app import create_app
from drivers import calibration_bundle as cb
from drivers import calibration_wizard as cw
from drivers.calibration_wizard import WizardError
from runtime.screen_observation import _validate_calibration

SCREEN_W, SCREEN_H = 1080, 2400
CAM_W, CAM_H = 960, 540
IME_SCREEN = {"screenWidth": SCREEN_W, "screenHeight": SCREEN_H,
              "orientation": "portrait"}


def cam(sx, sy):
    """测试用相机几何：屏幕画面按比例缩进相机帧（与既有向导测试同族）。"""
    return (sx * CAM_W / SCREEN_W, sy * CAM_H / SCREEN_H)


# ---------- 面一：观测校验门（_validate_calibration 接受/拒绝矩阵） ----------

def _camera_only_bundle(*, status="observation_ready", device_id="arm-01",
                        camera_id="cam-01", daemon_session=None,
                        corners=None, homography=None):
    corners = corners or [[30, 30], [270, 30], [270, 270], [30, 270]]
    homography = homography if homography is not None else [1, 0, 0, 0, 1, 0, 0, 0, 1]
    return {
        "schemaVersion": 2, "status": status,
        "deviceId": device_id, "cameraId": camera_id,
        "daemonSession": daemon_session,
        "cameraDeviceUniqueId": "physical-camera-01", "bindingRevision": 3,
        "frame": {"width": 300, "height": 300},
        "screen": {"orientation": "portrait", "screenWidth": 300,
                   "screenHeight": 300, "edgeModel": {"corners": corners}},
        "camera": {"cameraToScreen": {"homography": homography}},
    }


def _device(**overrides):
    device = {"device_id": "arm-01", "camera_id": "cam-01"}
    device.update(overrides)
    return device


def test_observation_ready_without_daemon_session_passes_gate():
    _validate_calibration(_camera_only_bundle(), "arm-01", _device())


def test_observation_ready_ignores_stale_daemon_session_binding():
    # 观测档不校验会话绑定：设备带着别的残留会话也不影响
    _validate_calibration(_camera_only_bundle(daemon_session="dead-session"),
                          "arm-01", _device(daemon_session="other-session"))


@pytest.mark.parametrize("overrides", [
    {"status": "draft"},
    {"status": "invalidated"},
    {"status": None},
])
def test_non_ready_statuses_still_rejected(overrides):
    with pytest.raises(Exception, match="calibration_invalid|校准"):
        _validate_calibration(_camera_only_bundle(**overrides), "arm-01", _device())


def test_observation_gate_still_requires_device_identity():
    with pytest.raises(Exception):
        _validate_calibration(_camera_only_bundle(), "arm-02", _device())
    with pytest.raises(Exception):
        _validate_calibration(_camera_only_bundle(), "arm-01", _device(camera_id="cam-09"))


def test_observation_gate_still_requires_valid_geometry():
    with pytest.raises(Exception):
        _validate_calibration(
            _camera_only_bundle(homography=[0, 0, 0, 0, 0, 0, 0, 0, 0]),
            "arm-01", _device())
    with pytest.raises(Exception):
        # 非凸四角（自交）必须拒绝：活体定位防线依赖有效几何基准
        _validate_calibration(
            _camera_only_bundle(corners=[[30, 30], [270, 270], [270, 30], [30, 270]]),
            "arm-01", _device())


def test_ready_semantics_unchanged_by_observation_status():
    bundle = _camera_only_bundle(status="ready", daemon_session="sess-1")
    with pytest.raises(Exception):
        _validate_calibration(bundle, "arm-01", _device())  # 无会话：拒绝
    with pytest.raises(Exception):
        _validate_calibration(bundle, "arm-01", _device(daemon_session="sess-2"))
    _validate_calibration(bundle, "arm-01", _device(daemon_session="sess-1"))


def test_motion_ready_semantics_unchanged():
    bundle = _camera_only_bundle(status="motion_ready", daemon_session="sess-1")
    _validate_calibration(bundle, "arm-01", _device(daemon_session="sess-1"))
    with pytest.raises(Exception):
        _validate_calibration(bundle, "arm-01", _device())


def test_motion_precheck_gate_rejects_observation_ready_profile():
    """纯观测档案永远不能驱动动作：运行前统一门禁判 bundle_not_ready。"""
    bundle = _camera_only_bundle()
    bundle["daemonSession"] = None
    checks = cb.validate_calibration_bundle(
        bundle, device_id="arm-01", camera_id="cam-01",
        camera_device_unique_id="physical-camera-01", binding_revision=3,
        daemon_addr="127.0.0.1:8795", frame_width=300, frame_height=300,
        daemon_session="sess-1")
    codes = {check["code"] for check in checks}
    assert "bundle_not_ready" in codes


def test_observation_status_constant_matches_design():
    assert cb.OBSERVATION_READY_STATUS == "observation_ready"


# ---------- 面三：向导相机-only 路径（goal=observation） ----------

class OfflineDaemon:
    """纯观测部署仿真：串口从未连接；任何指令/位置读取都是测试失败。"""

    def __init__(self):
        self.cmds: list[str] = []
        self.status_reads = 0

    def status(self):
        self.status_reads += 1
        return {"connected": False, "session": None, "device_present": False,
                "estop_locked": False, "lease": None, "last_error": None}

    def mpos(self):
        raise AssertionError("纯观测校准不得读取机械位置")

    def cmd(self, line: str):
        self.cmds.append(line)
        raise AssertionError(f"纯观测校准不得下发机械指令：{line}")


class FakeIme:
    """校准会话最小仿真（与 test_calibration_wizard.FakeIme 同族子集）。"""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.completed: list[str] = []

    def _view(self, sid):
        s = self.sessions[sid]
        return {"sessionId": sid, "deviceId": s["deviceId"], "status": s["status"],
                "clientState": s["clientState"],
                "screen": (dict(s["screen"]) if s.get("screen") else None),
                "samples": [dict(x) for x in s["samples"]],
                "armed": dict(s["armed"]) if s["armed"] else None}

    def create_session(self, device_id):
        for s in self.sessions.values():
            if s["deviceId"] == device_id and s["status"] == "active":
                raise cw.ImeError(409, "calibration_active", "active exists")
        sid = f"cal-{len(self.sessions) + 1:03d}"
        self.sessions[sid] = {"deviceId": device_id, "status": "active",
                              "clientState": "unknown", "screen": None,
                              "samples": [], "armed": None}
        return self._view(sid)

    def get_session(self, sid):
        if sid not in self.sessions:
            raise cw.ImeError(404, "calibration_not_found", "not found")
        return self._view(sid)

    def arm(self, sid, sample_id, sequence, target=None):
        s = self.sessions[sid]
        if s["clientState"] != "open":
            raise cw.ImeError(409, "phone_page_closed", "page closed")
        s["armed"] = {"sampleId": sample_id, "sequence": sequence,
                      "target": target, "armed_at": _time.time()}
        return self._view(sid)

    def disarm(self, sid, sample_id, sequence=None):
        s = self.sessions[sid]
        if s["armed"] and s["armed"]["sampleId"] == sample_id:
            s["armed"] = None
        return self._view(sid)

    def client(self, sid, state, screen=None):
        s = self.sessions[sid]
        s["clientState"] = state
        if state == "open" and screen:
            s["screen"] = dict(screen)

    def complete(self, sid):
        self.completed.append(sid)
        self.sessions[sid]["status"] = "completed"

    def cancel(self, sid):
        self.sessions[sid]["status"] = "cancelled"

    def list_devices(self):
        return [{"deviceId": d, "online": True}
                for d in sorted({s["deviceId"] for s in self.sessions.values()})]


class ObsEnv:
    """观测档向导环境：机械臂全程离线，相机/边缘/绿靶为注入检测器。"""

    def __init__(self, tmp_path, *, binding_revision=1, daemon=None):
        self.daemon = daemon or OfflineDaemon()
        self.ime = FakeIme()
        self.frame_seq = iter(range(1, 10 ** 6))
        self.quad = {"corners": [[10.0, 5.0], [950.0, 5.0],
                                 [950.0, 530.0], [10.0, 530.0]],
                     "frameW": CAM_W, "frameH": CAM_H}
        self.wizard = cw.CalibrationWizard(
            device="arm-01", daemon=self.daemon, ime=self.ime,
            data_root=tmp_path,
            camera_fetch=lambda: (b"jpg", next(self.frame_seq)),
            edge_detector=lambda jpg: dict(self.quad),
            target_detector=self._green_from_armed,
            binding_facts=lambda: {
                "cameraId": "cam-01",
                "cameraDeviceUniqueId": "uvc-test-01",
                "bindingRevision": binding_revision,
                "daemonAddr": "127.0.0.1:8795",
                "frameWidth": CAM_W,
                "frameHeight": CAM_H,
            },
            watcher_interval=0.05,
            target_draw_wait_s=0.02,
            target_detect_timeout_s=0.3,
        )
        _ENVS.append(self)

    def close(self):
        try:
            self.wizard.cancel()
        except Exception:
            pass

    def _green_from_armed(self, jpg):
        """动态绿靶检测：按当前布防标靶 (u,v) 返回绿框中心（零机械动作配对）。"""
        for s in self.ime.sessions.values():
            if s["armed"] and s["armed"].get("target"):
                t = s["armed"]["target"]
                return (*cam(t["u"] * SCREEN_W, t["v"] * SCREEN_H), CAM_W, CAM_H)
        return None

    def open_phone(self, screen=IME_SCREEN):
        self.ime.client(self.wizard.state["imeSessionId"], "open", screen=screen)


_ENVS: list[ObsEnv] = []


@pytest.fixture(autouse=True)
def _stop_observation_watchers():
    yield
    while _ENVS:
        env = _ENVS.pop()
        env.close()


def wait_stage(env: ObsEnv, stage: str, timeout: float = 3.0) -> None:
    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if env.wizard.state["stage"] == stage:
            return
        _time.sleep(0.02)
    raise AssertionError(
        f"等待阶段 {stage} 超时（当前 {env.wizard.state['stage']}）")


def drive_to_screen_geometry(env: ObsEnv) -> None:
    env.wizard.start("k40-01", goal="observation")
    assert env.wizard.state["stage"] == cw.STAGE_PRECHECK
    env.open_phone()
    wait_stage(env, cw.STAGE_SCREEN_GEOMETRY)


def test_observation_start_does_not_require_daemon_connection(tmp_path):
    env = ObsEnv(tmp_path)
    view = env.wizard.start("k40-01", goal="observation")
    assert view["stage"] == cw.STAGE_PRECHECK
    assert env.wizard.state["goal"] == "observation"
    assert env.wizard.state["daemonSession"] is None
    assert env.daemon.status_reads == 0  # 从未询问机械臂
    assert env.wizard._bundle()["status"] == "draft"


def test_observation_start_rejects_unknown_goal(tmp_path):
    env = ObsEnv(tmp_path)
    with pytest.raises(WizardError, match="目标档位"):
        env.wizard.start("k40-01", goal="teleport")


def test_motion_goal_start_still_requires_daemon_connection(tmp_path):
    env = ObsEnv(tmp_path)
    with pytest.raises(WizardError, match="未连接"):
        env.wizard.start("k40-01")


def test_phone_open_advances_straight_to_screen_geometry(tmp_path):
    env = ObsEnv(tmp_path)
    drive_to_screen_geometry(env)
    state = env.wizard.state
    assert state["screen"] == {"orientation": "portrait",
                               "screenWidth": SCREEN_W, "screenHeight": SCREEN_H}
    assert env.daemon.cmds == []
    bundle = env.wizard._bundle()
    assert bundle["status"] == "draft"
    assert bundle["screen"]["screenWidth"] == SCREEN_W


def test_phone_open_without_screen_metadata_stays_precheck(tmp_path):
    env = ObsEnv(tmp_path)
    env.wizard.start("k40-01", goal="observation")
    env.open_phone(screen=None)
    wait_stage_contra = _time.time() + 0.6
    while _time.time() < wait_stage_contra:
        assert env.wizard.state["stage"] == cw.STAGE_PRECHECK
        _time.sleep(0.05)


def test_wait_phone_open_observation_mode_advances_and_short_circuits(tmp_path):
    env = ObsEnv(tmp_path)
    env.wizard.start("k40-01", goal="observation")
    env.open_phone()
    result = env.wizard.wait_phone_open(timeout=2.0)
    assert result["open"] is True
    assert env.wizard.state["stage"] == cw.STAGE_SCREEN_GEOMETRY
    # 已推进后再调用：短路返回，不再要求 precheck
    again = env.wizard.wait_phone_open(timeout=0.5)
    assert again["open"] is True and again.get("auto") is True


def test_observation_camera_only_finish_produces_valid_profile(tmp_path):
    env = ObsEnv(tmp_path)
    drive_to_screen_geometry(env)
    edge = env.wizard.edge_capture()
    assert edge["edge"]["corners"] == env.quad["corners"]
    assert env.wizard.state["stage"] == cw.STAGE_SCREEN_GEOMETRY  # 观测档不自动跳验收
    fit = env.wizard.camera_fit()
    assert fit["source"] == "screen_target"
    assert fit["homography"]["inlierCount"] >= 4
    assert env.wizard.state["stage"] == cw.STAGE_SCREEN_GEOMETRY
    result = env.wizard.finish()
    assert result["status"] == "observation_ready"
    assert env.wizard.state["stage"] == cw.STAGE_READY
    # 零机械动作硬证据：串口指令与位置读取均为空
    assert env.daemon.cmds == []
    with pytest.raises(AssertionError):
        env.daemon.mpos()
    # 相机-only 档案形状：屏幕几何+单应齐备，运动字段全部未校准
    bundle = env.wizard._bundle()
    assert bundle["status"] == "observation_ready"
    assert bundle["daemonSession"] is None
    assert bundle["screen"]["edgeModel"]["corners"] == env.quad["corners"]
    assert bundle["camera"]["cameraToScreen"]["homography"]
    assert bundle["camera"]["validated"] is True
    assert bundle["z"]["validated"] is False
    assert bundle["xy"]["robotToScreen"] is None
    assert bundle["safePark"]["confirmed"] is False
    # 保存的档案直接通过观测校验门（设备侧无会话）
    _validate_calibration(bundle, "arm-01",
                          {"device_id": "arm-01", "camera_id": "cam-01"})
    # IME 会话正常收尾
    assert env.ime.completed == [env.wizard.state.get("imeSessionId")] or env.ime.completed


def test_observation_finish_requires_geometry_evidence(tmp_path):
    env = ObsEnv(tmp_path)
    drive_to_screen_geometry(env)
    with pytest.raises(WizardError, match="observation_ready|屏幕"):
        env.wizard.finish()
    env.wizard.edge_capture()
    with pytest.raises(WizardError, match="observation_ready|屏幕"):
        env.wizard.finish()  # 只有边缘、没有单应


def test_motion_operations_blocked_in_observation_goal(tmp_path):
    env = ObsEnv(tmp_path)
    drive_to_screen_geometry(env)
    blocked = [
        lambda: env.wizard.z_tap_test("tap", 3.0, 1500.0, 0),
        lambda: env.wizard.z_save("tap"),
        lambda: env.wizard.capture_hover_z(),
        lambda: env.wizard.xy_finish(),
        lambda: env.wizard.visual_verify(0.5, 0.5),
        lambda: env.wizard.visual_verify_all(),
        lambda: env.wizard.acceptance_execute("debug", "ime_input_tap"),
        lambda: env.wizard.acceptance_record("debug", "ime_input_tap", True),
        lambda: env.wizard.acceptance_advance(),
        lambda: env.wizard.screen_geometry_from_targets(),
        lambda: env.wizard.auto_start(),
    ]
    for action in blocked:
        with pytest.raises(WizardError, match="纯观测"):
            action()
    assert env.daemon.cmds == []  # 拒绝发生在任何机械动作之前


def test_observation_recalibration_replaces_previous_ready_bundle(tmp_path):
    """换新手机场景：旧 ready 运动档案被显式重建为纯观测档案（finish 一次性）。"""
    old_homography = [2, 0, 5, 0, 2, 5, 0, 0, 1]
    ready = cb.new_bundle("arm-01")
    ready.update({
        "status": "ready", "daemonSession": "sess-old",
        "cameraId": "cam-01", "cameraDeviceUniqueId": "uvc-test-01",
        "bindingRevision": 1, "daemonAddr": "127.0.0.1:8795",
        "frame": {"width": CAM_W, "height": CAM_H},
        "screen": {"orientation": "portrait", "screenWidth": SCREEN_W,
                   "screenHeight": SCREEN_H,
                   "edgeModel": {"corners": [[1, 1], [2, 2], [3, 3], [4, 4]]}},
        "camera": {"cameraToScreen": {"homography": old_homography},
                   "validated": True},
    })
    ready["xy"]["robotToScreen"] = {"a": 10.0, "b": 0, "c": 100.0,
                                    "d": 0, "e": 8.0, "f": 200.0}
    ready["xy"]["validated"] = True
    cb.save_bundle(ready, tmp_path)

    env = ObsEnv(tmp_path)
    drive_to_screen_geometry(env)
    # 会话中途：start 沿用既有「新一轮校准回 draft」语义（状态位翻转），
    # 但旧运动档案的参数数据不被观测会话触碰（P01 精神：数据保留到显式重建）
    mid = cb.load_bundle("arm-01", tmp_path)
    assert mid["status"] == "draft"
    assert mid["xy"]["robotToScreen"] is not None
    assert mid["camera"]["cameraToScreen"]["homography"] == old_homography
    env.wizard.edge_capture()
    env.wizard.camera_fit()
    env.wizard.finish()
    bundle = cb.load_bundle("arm-01", tmp_path)
    assert bundle["status"] == "observation_ready"
    assert bundle["daemonSession"] is None
    assert bundle["xy"]["robotToScreen"] is None  # 旧运动映射不并入观测档案
    assert bundle["camera"]["cameraToScreen"]["homography"] != old_homography
    assert bundle["screen"]["edgeModel"]["corners"] == env.quad["corners"]


class SessionDaemon(OfflineDaemon):
    """运动档起点仿真：status 报已连接；任何指令/位置读取仍视为测试失败。"""

    def status(self):
        return {"connected": True, "session": "sess-A", "device_present": True,
                "estop_locked": False, "lease": None, "last_error": None}


def test_observation_finish_after_motion_session_keeps_legacy_motion_fields(tmp_path):
    """评审实测边界钉住：同一 wizard 先跑运动档会话，再 start(goal=observation)。

    observation_rebuild 只隔离磁盘旧档；同会话 preserved state 里的旧
    z/xy/safePark/targets 会复制进 observation_ready 档案（_sync_bundle 尾段
    按 state 回填）。安全性不受影响：observation_ready 被运动门禁双重独立
    拒绝（bundle_not_ready + daemon_session_unbound），档案仅供纯读；本测试
    钉住该现状，防止 docstring 声明与实现再度脱节。
    """
    env = ObsEnv(tmp_path, daemon=SessionDaemon())
    env.wizard.start("k40-01")  # 运动档会话：daemon 会话在手
    assert env.wizard.state["goal"] == "motion"
    # 造出运动档校准产物（直接播种 state，与既有向导测试同族做法）
    with env.wizard._lock:
        env.wizard.state["z"].update({
            "hoverZ": 2.0,
            "autoSafe": {"zSafe": 5.0, "tapDepth": 3.0, "validated": True},
        })
        env.wizard.state["xy"] = {
            "accepted": [], "trials": [],
            "fit": {"a": 10.0, "b": 0.0, "c": 100.0,
                    "d": 0.0, "e": 8.0, "f": 200.0,
                    "residualRmsPx": 0.2, "maxResidualPx": 0.4},
            "validation": [], "validationPassed": True,
        }
        env.wizard.state["safePark"] = {
            "confirmed": True, "parkX": 11.0, "parkY": 12.0, "parkZ": 5.0,
            "source": "test",
        }
        env.wizard.state["targets"] = {
            "ime_delete_key": {"nx": 0.1, "ny": 0.9, "enabled": True},
        }
        env.wizard._sync_bundle()
        env.wizard._save_state()

    # 同一向导实例改跑观测档：start(goal=observation) 保留 preserved state
    env.wizard.start("k40-01", goal="observation")
    assert env.wizard.state["goal"] == "observation"
    assert env.wizard.state["z"]["hoverZ"] == 2.0  # preserved state 未清洗
    env.open_phone()
    wait_stage(env, cw.STAGE_SCREEN_GEOMETRY)
    env.wizard.edge_capture()
    env.wizard.camera_fit()
    result = env.wizard.finish()
    assert result["status"] == "observation_ready"
    # 观测 finish 零机械动作
    assert env.daemon.cmds == []

    bundle = cb.load_bundle("arm-01", tmp_path)
    assert bundle["status"] == "observation_ready"
    assert bundle["daemonSession"] is None
    # 遗留运动字段确实带入观测档案（docstring 已如实声明该边界）
    assert bundle["z"]["hoverZ"] == 2.0
    assert bundle["z"]["zSafe"] == 5.0
    assert bundle["xy"]["robotToScreen"] is not None
    assert bundle["safePark"]["parkX"] == 11.0
    assert bundle["targets"]["ime_delete_key"]["enabled"] is True
    # 双重独立拒绝：运动门禁对 observation_ready 一律不放行
    fails = cb.validate_calibration_bundle(
        bundle, device_id="arm-01", camera_id="cam-01",
        camera_device_unique_id="uvc-test-01", binding_revision=1,
        daemon_addr="127.0.0.1:8795", frame_width=CAM_W, frame_height=CAM_H,
        daemon_session="sess-A")
    codes = {item["code"] for item in fails}
    assert "bundle_not_ready" in codes
    assert "daemon_session_unbound" in codes


def test_state_view_exposes_observation_goal(tmp_path):
    env = ObsEnv(tmp_path)
    env.wizard.start("k40-01", goal="observation")
    view = env.wizard.state_view()
    assert view["goal"] == "observation"


# ---------- 面一（HTTP）+ 面四：观测档案出证据 + 冻结豁免 ----------

class EvidenceHardware:
    """无串口会话的设备仿真：device 字典不含 daemon_session 键。"""

    def __init__(self, data_root):
        self.data_root = data_root
        self.account = "wx-01"
        self.frame_seq = 12
        image = np.full((600, 300, 3), 220, dtype=np.uint8)
        image[30:570, 30:270] = np.random.default_rng(7).integers(
            90, 175, (540, 240, 3), dtype=np.uint8)
        self.image = cv2.imencode(".png", image)[1].tobytes()
        self.ocr_engine = self

    def get(self, device_id):
        if device_id != "arm-01":
            from runtime.errors import ApiError
            raise ApiError("device_not_found")
        return {"device_id": device_id, "camera_id": "cam-01",
                "wechat_account_id": self.account}

    def driver(self, device_id):
        return self

    def camera_broker(self, device_id):
        return self

    def camera(self, device_id):
        return self

    async def capture(self):
        from runtime.util import iso, utc_now
        captured_at = iso(utc_now())
        return {
            "image": self.image, "width": 300, "height": 600,
            "device_id": "arm-01", "camera_id": "cam-01",
            "captured_at": captured_at, "source_captured_at": captured_at,
            "source_received_at": captured_at, "frozen": False,
            "sha256": hashlib.sha256(self.image).hexdigest(),
            "device_unique_id": "physical-camera-01", "binding_revision": 3,
            "source_frame_seq": self.frame_seq,
        }

    def recognize(self, image, *, region):
        return OcrResult(
            text="hello", words=[OcrWord("hello", 50, 100, 50, 20, 0.98)],
            confidence=0.98, truncated=False, engine="fixture-ocr", engine_version="1",
            region=region, request_id="ocr-01", elapsed_ms=1, width=300, height=600,
        )


def _observation_bundle():
    return {
        "schemaVersion": 2, "deviceId": "arm-01", "cameraId": "cam-01",
        "status": "observation_ready", "daemonSession": None,
        "cameraDeviceUniqueId": "physical-camera-01", "bindingRevision": 3,
        "frame": {"width": 300, "height": 600},
        "screen": {"screenWidth": 300, "screenHeight": 600,
                   "edgeModel": {"corners": [[30, 30], [269, 30],
                                             [269, 569], [30, 569]]}},
        "camera": {"cameraToScreen": {"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}},
    }


@pytest.fixture
def observation_api(tmp_path):
    from types import SimpleNamespace
    from drivers.calibration_bundle import save_bundle
    bundle = _observation_bundle()
    save_bundle(bundle, tmp_path)
    hardware = EvidenceHardware(tmp_path)
    runtime = SimpleNamespace(config={"screen_ime_enabled": False},
                              devices=hardware)
    app = create_app(runtime)
    app.state.runtime = runtime
    client = TestClient(app)
    try:
        yield client, hardware, bundle
    finally:
        client.close()


def _contract_schema():
    from pathlib import Path
    return json_load((Path(__file__).resolve().parents[1]
                      / "api/contracts/screen-observation.v1.schema.json"))


def json_load(path):
    import json
    return json.loads(path.read_text(encoding="utf-8"))


def test_http_observation_profile_serves_evidence_without_daemon_session(
        observation_api):
    client, _, _ = observation_api
    response = client.get("/v1/devices/arm-01/screen-observation")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["daemon_session"] is None  # 纯观测档案：无机械臂会话
    Draft202012Validator(_contract_schema(),
                         format_checker=FormatChecker()).validate(body)
    assert body["quality"] == "clear"
    assert body["ocr"]["words"][0]["text"] == "hello"


def test_http_observation_profile_still_rejects_broken_calibration(
        observation_api):
    from drivers.calibration_bundle import save_bundle
    client, hardware, bundle = observation_api
    save_bundle({**bundle, "status": "draft"}, hardware.data_root)
    response = client.get("/v1/devices/arm-01/screen-observation")
    assert response.status_code == 409
    assert response.json()["error"]["key"] == "calibration_invalid"


def _maintenance_app(tmp_path):
    from types import SimpleNamespace
    hardware = EvidenceHardware(tmp_path)
    from drivers.calibration_bundle import save_bundle
    save_bundle(_observation_bundle(), tmp_path)
    runtime = SimpleNamespace(
        config={"screen_ime_enabled": False, "pre_enable_maintenance": True},
        devices=hardware)
    app = create_app(runtime)
    app.state.runtime = runtime
    return TestClient(app)


def test_screen_observation_exempt_from_pre_enable_freeze(tmp_path):
    """冻结语义=仅允许只读检查：screen-observation（GET 无副作用、不领任务、
    不动电机）与 camera-source-frame 同类，进豁免清单。"""
    client = _maintenance_app(tmp_path)
    response = client.get("/v1/devices/arm-01/screen-observation")
    assert response.status_code == 200, response.text


def test_freeze_exemption_does_not_over_match(tmp_path):
    client = _maintenance_app(tmp_path)
    # 写操作仍冻结
    assert client.post(
        "/v1/devices/arm-01/screen-observation", content=b"{}").status_code == 503
    # 前缀相似路径不豁免（fullmatch 语义）
    assert client.get(
        "/v1/devices/arm-01/screen-observation-history").status_code == 503
    assert client.get("/v1/devices/arm-01/screen-observation/extra").status_code == 503
