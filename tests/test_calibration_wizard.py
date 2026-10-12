"""统一校准向导测试：状态机全流程 + 协议语义（实施方案 §5/§6/§7/§8/§13/§16）。

FakeIme 按 K40TaskServer CalibrationCoordinator 的错误码同步复刻
（calibration_active / phone_page_closed / calibration_armed / sample_duplicate /
sequence_duplicate / sample_not_armed / calibration_not_found），
FakeDaemon 模拟 arm_daemon3 的会话锚点与 TAP/Z/XY 指令。
"""
from __future__ import annotations

import json
from pathlib import Path

import threading
import time as _time

import pytest

from drivers import calibration_wizard as cw
from drivers.calibration_wizard import (
    ImeError,
    TouchReportTimeout,
    WizardError,
)

SCREEN_W, SCREEN_H = 1080, 2400
CAM_W, CAM_H = 960, 540


def cam(sx, sy):
    """测试用摄像头帧：屏幕画面按比例缩进相机帧（相机 960x540 覆盖整屏）。"""
    return (sx * CAM_W / SCREEN_W, sy * CAM_H / SCREEN_H)


def cam_norm(sx, sy):
    """屏幕点的相机归一化坐标。"""
    return (sx / SCREEN_W, sy / SCREEN_H)


# ---------- fakes ----------

class FakeDaemon:
    """arm_daemon3 的最小仿真：会话锚点 + 相对运动指令。

    on_tap 钩子模拟"探头接触屏幕"这一物理事件（TAP 指令触发），
    手机模型据此决定是否上报触点。
    """

    def __init__(self, session="sess-A"):
        self.session = session
        self.x = self.y = 0.0
        self.z = 5.0
        self.cmds: list[str] = []
        self.estop_locked = False
        self.on_tap = None  # 回调 (cmd_line)：探头触屏
        self.xy_loss_ratio = 0.0  # 模拟丢步：XY 实际位移比例损耗（MPos 与指令残差）
        self.tap_count = 0

    def status(self):
        return {"connected": True, "session": self.session, "device_present": True,
                "estop_locked": self.estop_locked, "lease": None, "last_error": None}

    def mpos(self):
        return {"x": self.x, "y": self.y, "z": self.z, "state": "Idle"}

    def ensure_debug_lease(self):
        self.cmds.append("CLAIM debug")

    def cmd(self, line: str):
        self.cmds.append(line)
        parts = line.split()
        if parts[0] == "Z":
            self.z += float(parts[1])
        elif parts[0] == "XY":
            self.x += float(parts[1]) * (1.0 - self.xy_loss_ratio)
            self.y += float(parts[2]) * (1.0 - self.xy_loss_ratio)
        # TAP 只动 Z 且自动回抬：位置不变；触屏事件通知手机模型
        if parts[0] == "TAP":
            self.tap_count += 1
            if self.on_tap:
                self.on_tap(line)
        return dict(self.mpos())


class FakeIme:
    """校准会话语义同步复刻（错误码与 K40TaskServer 一致）。

    phone_lag 模拟手机轮询延迟：armed 只有在布防后 lag 秒才"被手机看到"，
    在此之前到达的物理点击会被静默忽略（与 CalibrationActivity 行为一致）。
    """

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.script = None  # get_session 时注入回调（模拟手机回报时机）
        self.phone_lag = 0.2  # 手机 750ms 轮询间隔的缩小模型
        self.arm_calls: list[tuple] = []  # 布防调用记录（验证预布防复用）
        self.tasks_created: list[dict] = []  # 验收下发的文字任务
        self.ime_task_reads = 0        # task_detail 读取计数（模拟回报时机）
        self.ime_display_after_reads = 1  # 第 N 次读取后转为 displayed；0=立即
        self.ime_stuck_pending = False  # 模拟手机一直不回报
        self.cancelled_tasks: list[str] = []

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
                raise ImeError(409, "calibration_active", "Device already has an active calibration")
        sid = f"cal-{len(self.sessions) + 1:03d}"
        self.sessions[sid] = {"deviceId": device_id, "status": "active",
                              "clientState": "unknown", "screen": None,
                              "samples": [], "armed": None}
        return self._view(sid)

    def get_session(self, sid):
        if sid not in self.sessions:
            raise ImeError(404, "calibration_not_found", "Calibration session not found")
        return self._view(sid)

    def arm(self, sid, sample_id, sequence, target=None):
        s = self.sessions.get(sid)
        if s is None:
            raise ImeError(404, "calibration_not_found", "not found")
        if s["status"] != "active":
            raise ImeError(409, "calibration_closed", "not active")
        if s["clientState"] != "open":
            raise ImeError(409, "phone_page_closed", "page closed")
        if s["armed"] is not None:
            raise ImeError(409, "calibration_armed", "already armed")
        if any(x["sampleId"] == sample_id for x in s["samples"]):
            raise ImeError(409, "sample_duplicate", "sampleId reported")
        if any(x["sequence"] == sequence for x in s["samples"]):
            raise ImeError(409, "sequence_duplicate", "sequence reported")
        self.arm_calls.append((sid, sample_id, sequence))
        s["armed"] = {"sampleId": sample_id, "sequence": sequence, "target": target,
                      "armed_at": _time.time()}
        return self._view(sid)

    def disarm(self, sid, sample_id, sequence=None):
        s = self.sessions[sid]
        if (s["armed"] and s["armed"]["sampleId"] == sample_id
                and (sequence is None or s["armed"]["sequence"] == sequence)):
            s["armed"] = None
        return self._view(sid)

    def report(self, sid, sample_id, screen_x, screen_y, *, sequence=None,
               w=SCREEN_W, h=SCREEN_H, orientation="portrait"):
        """模拟手机校准页回报触点。"""
        s = self.sessions[sid]
        armed = s["armed"]
        if armed is None or armed["sampleId"] != sample_id:
            raise ImeError(409, "sample_not_armed", "No matching sample is armed")
        seq = armed["sequence"] if sequence is None else sequence
        if seq != armed["sequence"]:
            raise ImeError(409, "sample_not_armed", "sequence mismatch")
        s["samples"].append({"sampleId": sample_id, "sequence": seq,
                             "screenX": float(screen_x), "screenY": float(screen_y),
                             "screenWidth": w, "screenHeight": h,
                             "orientation": orientation})
        s["armed"] = None

    def client(self, sid, state, screen=None):
        """页面回报状态；open 时可附带只读屏幕元数据（与 8788 协议一致）。"""
        s = self.sessions[sid]
        s["clientState"] = state
        if state == "open" and screen:
            s["screen"] = dict(screen)

    def create_task(self, payload: dict) -> dict:
        self.tasks_created.append(payload)
        return {"taskId": payload["taskId"], "status": "pending"}

    def task_detail(self, task_id: str) -> dict:
        self.ime_task_reads += 1
        status = "pending"
        if not self.ime_stuck_pending \
                and self.ime_task_reads >= self.ime_display_after_reads:
            status = "displayed"
        return {"task": {"taskId": task_id, "status": status}}

    def cancel_task(self, task_id: str) -> dict:
        self.cancelled_tasks.append(task_id)
        return {"status": "cancelled"}

    def wait_task_displayed(self, task_id: str, *, timeout: float = 8.0) -> dict:
        if self.ime_stuck_pending:
            from drivers.calibration_wizard import ImeError
            raise ImeError(408, "task_not_displayed", "输入法任务未回报已显示（最后状态：pending）")
        return {"status": "displayed", "waitedS": 0.4}

    def list_devices(self) -> list[dict]:
        return [{"deviceId": d, "online": True, "keyboardActive": False,
                 "lastSeenAt": "2026-09-01T00:00:00Z"}
                for d in sorted({s["deviceId"] for s in self.sessions.values()})]

    def complete(self, sid):
        self.sessions[sid]["status"] = "completed"

    def cancel(self, sid):
        if sid not in self.sessions:
            raise ImeError(404, "calibration_not_found", "not found")
        self.sessions[sid]["status"] = "cancelled"


class FakeOcrResult:
    engine = "fake-ocr"
    width = CAM_W
    height = CAM_H

    def __init__(self, words):
        self.words = words


class FakeOcrWord:
    def __init__(self, text, x, y, width=80, height=24, confidence=0.97):
        self.text = text
        self.x = x
        self.y = y
        self.width = width
        self.height = height
        self.confidence = confidence


class FakeOcr:
    def __init__(self, words):
        self.words = words
        self.calls = 0

    def recognize(self, image_bytes, *, region, request_id=""):
        assert region == "input"
        self.calls += 1
        return FakeOcrResult(self.words)


# ---------- 真实映射与脚手架 ----------

def truth(rx, ry):
    """测试用"物理真实"机械→屏幕映射：screen = A·robot + b。"""
    return (10.0 * rx + 100.0, 8.0 * ry + 200.0)


def truth_inverse(sx, sy):
    return ((sx - 100.0) / 10.0, (sy - 200.0) / 8.0)


def auto_report(ime: FakeIme, daemon: FakeDaemon, *, noise=(0.0, 0.0),
                size=(SCREEN_W, SCREEN_H), orientation="portrait"):
    """安装"手机校准页"行为模型（挂在 daemon.on_tap = 探头触屏事件上）。

    物理模型：触点事件只在 TAP 触屏那一刻发生一次；手机只有在
    armed_at+phone_lag 之后（自己的轮询已看到布防/标靶）才会处理该触点，
    否则静默忽略；TAP 停留 >1s 视为长按，被校准页拒收（MAX_TOUCH_DURATION）。
    """
    def on_tap(cmd_line: str):
        dwell_s = 0.0
        for part in cmd_line.split()[2:]:
            if part[:1] in ("P", "p"):
                try:
                    dwell_s = float(part[1:])
                except ValueError:
                    pass
        if dwell_s > 1.0:
            return  # 长按：校准页拒收（>1s 触点不上报）
        for sid, s in ime.sessions.items():
            armed = s["armed"]
            if not armed:
                continue
            if _time.time() - armed.get("armed_at", 0) < ime.phone_lag:
                return  # 手机还没轮询到布防：触点被静默忽略
            sx, sy = truth(daemon.x, daemon.y)
            ime.report(sid, armed["sampleId"], sx + noise[0], sy + noise[1],
                       w=size[0], h=size[1], orientation=orientation)

    daemon.on_tap = on_tap
    return on_tap


# 活跃 Env 登记：conftest autouse 夹具逐用例清场（见 Env.close）
WIZARD_ENVS: list["Env"] = []


class Env:
    def __init__(self, tmp_path, wizard_extra: dict | None = None):
        self.daemon = FakeDaemon()
        self.ime = FakeIme()
        self.tips: list = []
        self.targets: list = []  # 绿靶检测脚本：每次调用弹出一个 (x, y, w, h)
        self.frame_seq = iter(range(1, 10 ** 6))
        self.quad = {"corners": [[10.0, 5.0], [950.0, 5.0], [950.0, 530.0], [10.0, 530.0]],
                     "frameW": CAM_W, "frameH": CAM_H}
        wizard_kwargs = dict(
            device="arm-01", daemon=self.daemon, ime=self.ime,
            data_root=tmp_path,
            camera_fetch=lambda: (b"jpg", next(self.frame_seq)),
            tip_detector=lambda jpg: (self.tips.pop(0) if self.tips else None),
            edge_detector=lambda jpg: dict(self.quad),
            target_detector=lambda jpg: (self.targets.pop(0) if self.targets else None),
            binding_facts=lambda: {
                "cameraId": "cam-01",
                "cameraDeviceUniqueId": "uvc-test-01",
                "bindingRevision": 1,
                "daemonAddr": "127.0.0.1:8795",
                "frameWidth": CAM_W,
                "frameHeight": CAM_H,
            },
            touch_timeout=1.0,
            watcher_interval=0.1,  # 测试里自动检测要快
            phone_arm_lead_s=0.25,  # 覆盖 FakeIme 默认 phone_lag=0.2 的轮询延迟
            target_draw_wait_s=0.02,
            target_detect_timeout_s=0.15,
            allow_probe_fallback=True,
        )
        wizard_kwargs.update(wizard_extra or {})
        self.wizard = cw.CalibrationWizard(**wizard_kwargs)
        # 登记：conftest 的 autouse 夹具在每个用例后 close，防止
        # wizard-phone-watch 轮询线程跨用例泄漏（17 线程×0.1s 轮询曾把
        # 后续用例的 cv2/GIL 饿死成 livelock，全量串行跑挂死）。
        WIZARD_ENVS.append(self)

    def close(self):
        try:
            self.wizard.cancel()
        except Exception:
            pass

    def green_from_armed(self):
        """动态绿靶检测：按当前布防标靶的 (u,v) 返回绿框中心（免疫布防顺序）。"""
        def detect(jpg):
            for s in self.ime.sessions.values():
                if s["armed"] and s["armed"].get("target"):
                    t = s["armed"]["target"]
                    return (*cam(t["u"] * SCREEN_W, t["v"] * SCREEN_H), CAM_W, CAM_H)
            return None
        return detect

    def open_phone(self, screen=None):
        self.ime.client(self.wizard.state["imeSessionId"], "open", screen=screen)

    def jog_to_screen(self, sx, sy):
        rx, ry = truth_inverse(sx, sy)
        self.daemon.x, self.daemon.y = rx, ry
        return rx, ry


def wait_stage(env: Env, stage: str, timeout: float = 3.0) -> None:
    """等待向导自动推进到指定阶段（手机页自动检测走后台 watcher）。"""
    import time as _time

    deadline = _time.time() + timeout
    while _time.time() < deadline:
        if env.wizard.state["stage"] == stage:
            return
        _time.sleep(0.05)
    raise AssertionError(
        f"等待阶段 {stage} 超时（当前 {env.wizard.state['stage']}）")


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def drive_to_z_ready(env: Env):
    """从零推进到 z_ready（会话创建 + 预检 + 手机页 open 自动检测）。"""
    env.wizard.start("k40-01")
    env.wizard.precheck()
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)  # 后台 watcher 自动推进，无需手动等待按钮


# ---------- 生命周期与阶段门（§5） ----------

def test_start_requires_connected_daemon(env, tmp_path):
    env.daemon.session = None
    with pytest.raises(WizardError, match="未连接"):
        env.wizard.start("k40-01")


def test_start_creates_ime_session_and_binds_anchor(env):
    env.wizard.start("k40-01")
    st = env.wizard.state
    assert st["stage"] == cw.STAGE_PRECHECK
    assert st["imeSessionId"] in env.ime.sessions
    assert st["daemonSession"] == "sess-A"
    bundle = env.wizard._bundle()
    assert bundle["daemonSession"] == "sess-A"
    assert bundle["status"] == "draft"


def test_duplicate_active_calibration_rejected(env):
    env.wizard.start("k40-01")
    with pytest.raises(ImeError) as exc:
        env.ime.create_session("k40-01")
    assert exc.value.code == "calibration_active"
    # 向导层：precheck 阶段重复 start = 重新开始（旧会话自动取消，不产生并存会话）
    first_sid = env.wizard.state["imeSessionId"]
    env.wizard.start("k40-01")
    assert env.ime.sessions[first_sid]["status"] == "cancelled"
    assert env.wizard.state["imeSessionId"] != first_sid


def test_wait_phone_open_timeout(env):
    env.wizard.start("k40-01")
    env.wizard.precheck()
    result = env.wizard.wait_phone_open(timeout=0.6)
    assert result["open"] is False
    assert env.wizard.state["stage"] == cw.STAGE_PRECHECK  # 不推进


def test_wait_phone_diagnosis_points_to_device_id_mismatch(env):
    """手机页未回报时的诊断：指向"调试台选对输入法设备"（手机端 ID 不可改）与 8788 不可达。"""
    env.wizard.start("k40-01")
    result = env.wizard.wait_phone_open(timeout=0.6)
    assert result["open"] is False
    assert result["clientState"] == "unknown"
    assert result["imeDeviceId"] == "k40-01"
    hint = result["hint"]
    assert "不可在手机端修改" in hint and "k40-01" in hint  # 配对方向：调试台选定
    assert "10.0.2.2:18080" in result["hint"]  # K40 App 默认服务器地址提示
    assert "k40-01" in result["knownDevices"]  # 已知设备清单（来自会话归属）
    ime_devices = result["imeDevices"]
    assert ime_devices and ime_devices[0]["deviceId"] == "k40-01"
    assert ime_devices[0]["online"] is True  # 带在线状态，供人工核对


def test_wait_phone_diagnosis_closed_page(env):
    """手机页打开过但退出（closed）：诊断单独提示重新进入。"""
    env.wizard.start("k40-01")
    env.ime.client(env.wizard.state["imeSessionId"], "closed")
    result = env.wizard.wait_phone_open(timeout=0.6)
    assert result["open"] is False
    assert result["clientState"] == "closed"
    assert "已退出" in result["hint"] and "重新进入" in result["hint"]


def test_phone_opened_before_or_after_auto_advances(env):
    """手机校准页先开/后开都能被自动检测：无需手动按③即进入 Z 阶段。"""
    env.wizard.start("k40-01")
    # 手机页"已开着"：会话一出现就回报 open（模拟先打开校准页的场景）
    env.ime.client(env.wizard.state["imeSessionId"], "open")
    wait_stage(env, cw.STAGE_Z_READY)
    # 再调 wait-phone：直接短路返回 open（自动检测已推进）
    result = env.wizard.wait_phone_open(timeout=0.5)
    assert result["open"] is True and result.get("auto") is True
    assert env.wizard.state.get("phoneOpenedAt")


def test_restart_from_precheck_allowed_and_bundle_resets_to_draft(env):
    """precheck 阶段可直接重新开始（不逼用户先取消）；档案状态回 draft。"""
    env.wizard.start("k40-01")  # 停在 precheck
    with env.wizard._lock:
        env.wizard._bundle()["status"] = "invalidated"  # 模拟历史失效
        from drivers import calibration_bundle as _cb
        _cb.save_bundle(env.wizard._bundle(), env.wizard.data_root)
    result = env.wizard.start("k40-01")  # 不抛"进行中"
    assert result["stage"] == cw.STAGE_PRECHECK
    assert env.wizard._bundle()["status"] == "draft"
    # 老会话被取消，绑定的是新会话
    old_sessions = [s for s in env.ime.sessions.values() if s["status"] == "cancelled"]
    assert old_sessions
    assert env.wizard.state["imeSessionId"] in env.ime.sessions


def test_tap_race_tap_before_phone_sees_arm_is_dropped(env):
    """回归（现场 7 中丢 6 的竞态）：布防后立即点击，手机还没轮询到布防 →

    触点被校准页静默忽略 → 超时；等手机看到布防后再点击 → 稳定回报。
    """
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    # 复现旧竞态：布防后不等手机轮询（lead 0.05 < lag 0.4）→ 触点被吞
    env.wizard._phone_arm_lead_s = 0.05
    env.ime.phone_lag = 0.4
    with pytest.raises(TouchReportTimeout):
        env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    # 修复后的默认行为：lead 覆盖轮询延迟 → 回报稳定到达
    env.wizard._phone_arm_lead_s = 0.6
    result = env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    assert result["trial"]["ok"] is True
    assert result["trial"]["report"]["screenWidth"] == SCREEN_W


def test_watcher_invalidates_when_session_gone(env):
    """8788 重启丢会话：保留资料并转入会话复核，而不是清空校准结果。"""
    env.wizard.start("k40-01")
    sid = env.wizard.state["imeSessionId"]
    del env.ime.sessions[sid]  # 模拟服务重启后会话消失
    import time as _time

    deadline = _time.time() + 3
    while _time.time() < deadline:
        if (env.wizard.state.get("sessionRebind") or {}).get("required"):
            break
        _time.sleep(0.05)
    assert env.wizard.state["stage"] == cw.STAGE_PRECHECK
    assert env.wizard.state["sessionRebind"]["reason"] == "ime_session_lost"
    assert env.wizard.state["invalidReason"] is None
    # 丢失后仍可直接建立新会话
    env.wizard.start("k40-01")
    assert env.wizard.state["stage"] == cw.STAGE_PRECHECK


def test_watcher_resumes_after_server_restart(tmp_path):
    """服务重启（向导从磁盘恢复到 precheck）后自动检测依然有效。"""
    env1 = Env(tmp_path)
    env1.wizard.start("k40-01")  # 停在 precheck（手机页未开）
    sid = env1.wizard.state["imeSessionId"]

    env2 = Env(tmp_path)  # 新进程：加载磁盘状态 + 恢复 watcher
    env2.ime.sessions = env1.ime.sessions  # 共享同一个（真实）服务端会话
    assert env2.wizard.state["stage"] == cw.STAGE_PRECHECK
    env2.ime.client(sid, "open")
    wait_stage(env2, cw.STAGE_Z_READY)


def test_state_view_fallback_advances_when_watcher_dead(tmp_path):
    """watcher 线程失效时，页面状态轮询（state_view）也能推进阶段（兜底）。"""
    env1 = Env(tmp_path)
    env1.wizard.start("k40-01")
    sid = env1.wizard.state["imeSessionId"]

    # 手工构造 watcher 永不触发的实例（interval 极大），只用 state_view 兜底
    env2 = Env(tmp_path)
    env2.wizard._watcher_gen += 1  # 杀掉构造时恢复的 watcher
    env2.ime.sessions = env1.ime.sessions
    env2.ime.client(sid, "open")
    view = env2.wizard.state_view()
    assert view["stage"] == cw.STAGE_Z_READY
    assert (view["phone"] or {}).get("clientState") == "open"


def test_precheck_returns_checklist(env):
    """预检 = 本机条件逐项清单（daemon/锚点/急停/摄像头/IME 会话），不查手机。"""
    env.wizard.start("k40-01")
    result = env.wizard.precheck()
    assert result["ok"] is True
    names = {c["name"] for c in result["checks"]}
    assert {"daemon", "daemon_session_match", "estop", "camera", "ime_session"} <= names
    # 摄像头不可用时逐项标红但不抛错
    env2 = Env(env.wizard.data_root / "fresh")  # 独立状态目录，避免读到进行中的会话
    env2.wizard.start("k40-01")
    env2.wizard._camera_fetch = None  # 摄像头不可用
    result2 = env2.wizard.precheck()
    assert result2["ok"] is False
    failed = {c["name"] for c in result2["checks"] if not c["ok"]}
    assert "camera" in failed


def test_start_requires_connected_daemon(env, tmp_path):
    env.daemon.session = None
    with pytest.raises(WizardError, match="未连接"):
        env.wizard.start("k40-01")


def test_arm_before_phone_open_rejected(env):
    drive_to_z_ready(env)
    env.ime.client(env.wizard.state["imeSessionId"], "closed")
    with pytest.raises(WizardError, match="手机校准页未打开"):
        env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)


def test_out_of_order_touch_rejected_by_protocol(env):
    """乱序/冒名触点回报：无匹配布防点时协议拒绝（§16 协议测试）。"""
    env.wizard.start("k40-01")
    env.open_phone()
    sid = env.wizard.state["imeSessionId"]
    env.ime.arm(sid, "s1", 1)
    with pytest.raises(ImeError) as exc:
        env.ime.report(sid, "other-sample", 1, 2)
    assert exc.value.code == "sample_not_armed"
    # 二次布防拒绝
    with pytest.raises(ImeError) as exc:
        env.ime.arm(sid, "s2", 2)
    assert exc.value.code == "calibration_armed"


def test_sequence_duplicate_rejected_by_protocol(env):
    env.wizard.start("k40-01")
    env.open_phone()
    sid = env.wizard.state["imeSessionId"]
    env.ime.arm(sid, "s1", 1)
    env.ime.report(sid, "s1", 100, 200)
    with pytest.raises(ImeError) as exc:
        env.ime.arm(sid, "s2", 1)  # 序号 1 已被回报占用
    assert exc.value.code == "sequence_duplicate"


@pytest.mark.parametrize("sample_id,sequence,cleared", [
    ("s1", 2, True),
    ("s1", 1, False),
    ("other", 2, False),
    ("other", 1, False),
    ("s1", None, True),
    ("other", None, False),
])
def test_fake_ime_disarm_matches_sample_and_optional_sequence(sample_id, sequence, cleared):
    ime = FakeIme()
    sid = ime.create_session("k40-test")["sessionId"]
    ime.client(sid, "open")
    armed = ime.arm(sid, "s1", 2)["armed"]

    if sequence is None:
        view = ime.disarm(sid, sample_id)
    else:
        view = ime.disarm(sid, sample_id, sequence)

    assert view["armed"] == (None if cleared else armed)
    assert ime.get_session(sid)["armed"] == view["armed"]


def test_cancel_closes_session_and_resets(env):
    env.wizard.start("k40-01")
    sid = env.wizard.state["imeSessionId"]
    env.wizard.cancel()
    assert env.wizard.state["stage"] == cw.STAGE_IDLE
    assert env.ime.sessions[sid]["status"] == "cancelled"


# ---------- Z 阶段（§6） ----------

def test_hover_capture_records_evidence(env):
    drive_to_z_ready(env)
    env.daemon.z = 2.0
    result = env.wizard.capture_hover_z()
    assert result["hoverZ"] == 2.0
    ev = result["evidence"]
    assert ev["daemonSession"] == "sess-A"
    assert ev["mpos"]["z"] == 2.0
    assert ev["cameraFrameSeq"] >= 1
    assert "x" in ev["screenEdgeDigest"]  # 边缘摘要


def test_tap_encoding_and_delete_params_independent(env):
    """TAP 参数编码正确；普通点击与长按参数互不覆盖（§16 单元测试）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    assert "TAP 3.000 F1500" in env.daemon.cmds
    env.wizard.z_save("tap")
    assert env.wizard.state["stage"] == cw.STAGE_XY

    # 长按在 XY 阶段补测：停留编码为 P 秒
    env.wizard.z_tap_test("delete", 2.5, 1200.0, 2200)
    assert "TAP 2.500 F1200 P2.2" in env.daemon.cmds
    env.wizard.z_save("delete")

    z = env.wizard._bundle()["z"]
    assert (z["tapDepth"], z["tapFeed"], z["tapDwellMs"]) == (3.0, 1500.0, 0)
    assert (z["deleteHoldMs"], z["deleteDepth"], z["deleteFeed"]) == (2200, 2.5, 1200.0)
    assert z["validated"] is True
    assert z["deleteValidated"] is False  # 长按待验收


def test_z_feed_out_of_range_rejected(env):
    """F300 历史失步禁令已解除（2026-09-18 设备更换）；范围下限仍强制。"""
    drive_to_z_ready(env)
    with pytest.raises(ValueError, match="安全范围"):
        env.wizard.z_tap_test("tap", 3.0, 10.0, 0)


def test_tap_without_report_records_timeout(env):
    """机械动作完成但无回报：超时留痕、解除布防、不自动加深（§14）。"""
    drive_to_z_ready(env)
    env.ime.script = lambda sid: None  # 手机页永不回报
    with pytest.raises(TouchReportTimeout, match="touch_report_timeout"):
        env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    trials = env.wizard.state["z"]["trials"]
    assert len(trials) == 1 and trials[0]["ok"] is False
    assert "touch_report_timeout" in trials[0]["error"]
    assert env.ime.sessions[env.wizard.state["imeSessionId"]]["armed"] is None
    # 没有成功回报前不允许保存
    env.wizard.capture_hover_z()
    with pytest.raises(WizardError, match="合法触点回报"):
        env.wizard.z_save("tap")


def test_z_save_requires_hover(env):
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    with pytest.raises(WizardError, match="悬停高度"):
        env.wizard.z_save("tap")


# ---------- 失效路径（§5/§14） ----------

def test_daemon_session_change_invalidates(env):
    drive_to_z_ready(env)
    env.daemon.session = "sess-B"  # 重连换锚点
    with pytest.raises(WizardError, match="会话已变化"):
        env.wizard.capture_hover_z()
    assert env.wizard.state["stage"] == cw.STAGE_PRECHECK
    assert env.wizard.state["sessionRebind"]["reason"] == "daemon_session_changed"
    assert env.wizard.state["invalidReason"] is None
    assert env.wizard._bundle()["status"] == "draft"


def test_screen_size_change_invalidates(env):
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)  # 建立基线
    env.ime.script = auto_report(env.ime, env.daemon, size=(1170, 2532))
    with pytest.raises(WizardError, match="尺寸变化"):
        env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    assert env.wizard.state["invalidReason"]["code"] == "screen_size_changed"


def test_screen_orientation_change_invalidates(env):
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.ime.script = auto_report(env.ime, env.daemon, orientation="landscape")
    with pytest.raises(WizardError, match="方向变化"):
        env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    assert env.wizard.state["invalidReason"]["code"] == "screen_orientation_changed"


def test_ime_session_lost_invalidates(env):
    drive_to_z_ready(env)
    env.ime.cancel(env.wizard.state["imeSessionId"])
    with pytest.raises(WizardError, match="已结束"):
        env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    assert env.wizard.state["stage"] == cw.STAGE_PRECHECK
    assert env.wizard.state["sessionRebind"]["reason"] == "ime_session_lost"
    assert env.wizard.state["invalidReason"] is None


def test_restart_after_invalidation(env):
    drive_to_z_ready(env)
    env.daemon.session = "sess-B"
    with pytest.raises(WizardError):
        env.wizard.capture_hover_z()
    env.daemon.session = "sess-C"
    env.daemon.status = lambda: {"connected": True, "session": "sess-C",
                                 "device_present": True, "estop_locked": False,
                                 "lease": None, "last_error": None}
    env.wizard.start("k40-01")  # 失效后允许重新开始
    assert env.wizard.state["stage"] == cw.STAGE_PRECHECK
    assert env.wizard.state["daemonSession"] == "sess-C"


# ---------- 全流程到 READY（§6→§13） ----------

def _run_xy_stage(env: Env):
    for i, (name, u, v) in enumerate(cw.XY_PLAN_POINTS):
        sx, sy = u * SCREEN_W, v * SCREEN_H
        env.jog_to_screen(sx, sy)
        env.tips.append((*cam(sx, sy), CAM_W, CAM_H))  # 相机像素 ↔ 屏幕像素按比例对应
        env.wizard.xy_tap_test(i)
        env.wizard.xy_accept(i)
    fit = env.wizard.xy_fit()
    assert fit["fit"]["residualRmsPx"] < 0.01
    for i, (name, u, v) in enumerate(cw.XY_VALIDATION_POINTS):
        env.jog_to_screen(u * SCREEN_W, v * SCREEN_H)
        result = env.wizard.xy_validate_tap(i)
        assert result["result"]["passed"] is True
    env.wizard.xy_finish()


def test_visual_verify_all_five_points(env):
    """5 点视觉验证：中心+四角全链路（摄像头判定→走位→点击→回报），汇总入档案。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    _run_xy_stage(env)
    env.wizard.edge_capture()
    env.wizard._target_detector = env.green_from_armed()
    env.wizard.camera_fit()
    r = env.wizard.visual_verify_all()["summary"]
    assert r["passedCount"] == 5 and r["passed"] is True
    assert r["contactVsNominalMaxPx"] < 3.0
    assert r["contactVsCameraMaxPx"] < 3.0
    bundle = env.wizard._bundle()
    assert bundle["camera"]["visualVerify"]["passed"] is True  # 档案留痕
    assert bundle["camera"]["visualVerify"]["contactVsNominalMaxPx"] < 3.0


def test_visual_verify_retries_transient_target_detection(env):
    """手机轮询/暗光导致前两帧绿靶丢失时，不应直接放弃该点点击。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    _run_xy_stage(env)
    env.wizard.edge_capture()
    good_detector = env.green_from_armed()
    calls = {"count": 0}

    def flaky_detector(jpg):
        calls["count"] += 1
        if calls["count"] <= 2:
            return None
        return good_detector(jpg)

    env.wizard._target_detector = flaky_detector
    env.wizard._target_detect_timeout_s = 0.6
    result = env.wizard.camera_fit()
    assert result["source"] == "screen_target"
    assert calls["count"] >= 7  # 首点重试 2 帧 + 余下 4 点


def test_camera_fit_prefers_screen_plane_targets(env):
    """单应优先用屏幕平面绿靶配对（与 OCR 同平面，无探头悬停视差）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    _run_xy_stage(env)
    env.wizard.edge_capture()
    env.wizard._target_detector = env.green_from_armed()
    result = env.wizard.camera_fit()
    assert result["source"] == "screen_target"
    assert result["homography"]["reprojectionRmsPx"] < 1.0
    assert result["homography"]["frameW"] == CAM_W
    assert result["stage"] == cw.STAGE_DEBUG_ACCEPTANCE


def test_visual_verify_camera_judged_click_chain(env):
    """视觉验证 = 摄像头判定标靶 → 走位 → 点击 → 三方对照（用户要求的链路）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    _run_xy_stage(env)
    env.wizard.edge_capture()
    env.wizard._target_detector = env.green_from_armed()
    env.wizard.camera_fit()  # 进入 debug_acceptance
    # 视觉验证在屏幕中心：绿框中心(相机)→ 单应 → 屏幕中心；探头尖在别处
    env.tips.append((*cam(SCREEN_W / 2, SCREEN_H / 2 - 300), CAM_W, CAM_H))  # 视觉偏上
    r = env.wizard.visual_verify(u=0.5, v=0.5, tap=True)["result"]
    assert r["cameraVsNominalPx"] < 3.0      # 单应（屏幕平面）准确
    assert r["contactVsNominalPx"] < 3.0     # 手机接触点在标靶上
    assert r["contactVsCameraPx"] < 3.0      # 摄像头判定→点击链路误差
    assert r["tipCam"][1] < r["targetCam"][1] - 50  # 探头尖视觉在绿框上方≈70相机px（用户看到的"偏上"）


def test_visual_verify_all_rejects_camera_misalignment_even_when_contact_tracks_wrong_target(env):
    """D 区不能只看接触点彼此接近；摄像头误判到另一位置也必须失败。"""
    env.wizard.state["stage"] = cw.STAGE_DEBUG_ACCEPTANCE
    env.wizard.state["geometry"]["homography"] = {
        "homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        "reprojectionRmsPx": 0.0,
    }
    env.wizard.state["xy"]["fit"] = {
        "a": 1, "b": 0, "c": 0, "d": 0, "e": 1, "f": 0,
        "residualRmsPx": 0.0, "maxResidualPx": 0.0,
    }
    env.wizard.state["screen"] = {"screenWidth": SCREEN_W, "screenHeight": SCREEN_H}

    def fake_verify(*, u, v, tap=True):
        nominal = [u * SCREEN_W, v * SCREEN_H]
        wrong = [nominal[0] + 100, nominal[1] + 100]
        result = {
            "nominalScreen": nominal,
            "cameraJudgedScreen": wrong,
            "contactScreen": wrong,
            "cameraVsNominalPx": 141.4,
            "contactVsNominalPx": 141.4,
            "contactVsCameraPx": 0.0,
        }
        return {"result": result}

    env.wizard.visual_verify = fake_verify
    summary = env.wizard.visual_verify_all()["summary"]
    assert summary["passedCount"] == 0
    assert summary["passed"] is False
    assert all("cameraVsNominalPx" in p["error"] for p in summary["points"])


def test_goto_robot_uses_daemon_segment_limit_and_calibrated_travel_feed(env):
    """校准向导应只发一条直达 XY 指令，并使用 F5000。"""
    env.wizard.state["z"]["hoverZ"] = 2.0
    env.daemon.x = env.daemon.y = 0.0
    env.daemon.z = 2.0

    env.wizard._goto_robot(29.0, 28.0)

    xy = [cmd for cmd in env.daemon.cmds if cmd.startswith("XY ")]
    assert xy == ["XY 29.000 28.000 F5000"]


def test_goto_robot_rejects_xy_over_direct_limit_before_motion(env):
    env.wizard.state["z"]["hoverZ"] = 2.0
    env.daemon.z = 5.0
    with pytest.raises(WizardError, match="目标位移超 300mm"):
        env.wizard._goto_robot(301.0, 0.0)
    assert not any(cmd.startswith("XY ") for cmd in env.daemon.cmds)


def test_camera_fit_excludes_bad_tip_pairing(env):
    """单点探头尖检测错位（302px 外点）：RANSAC 剔除后按内点拟合，
    给出重采建议而不是整体拒绝（现场：点2 坏配对导致 RMS 135px 被拒）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    _run_xy_stage(env)
    env.wizard.edge_capture()
    # 模拟一次探头尖检测错位：点2 的 tipX 偏 300px
    env.wizard.state["xy"]["accepted"][2]["tipX"] += 300
    result = env.wizard.camera_fit()
    h = result["homography"]
    assert h["inlierCount"] == 4
    assert h["excludedPoints"] and h["excludedPoints"][0]["index"] == 2
    assert any("剔除" in w for w in result["warnings"])
    assert result["stage"] == cw.STAGE_DEBUG_ACCEPTANCE  # 拟合成功直达验收


def test_acceptance_execute_runs_full_chain(env):
    """验收执行覆盖自动 OCR 与兼容的手工坐标两条路径。"""
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
    assert env.wizard.state["stage"] == cw.STAGE_DEBUG_ACCEPTANCE

    # 没有传 nx/ny 时，自动从摄像头 OCR 找到测试文字中心。
    # 搜索区已限定输入法区域（屏幕高度≥51%，经 H 投影到名义屏幕坐标判定）。
    env.wizard.state["screen"] = {"orientation": "portrait",
                                  "screenWidth": SCREEN_W, "screenHeight": SCREEN_H}
    # SAFE PARK 已确认时，验收第一步先把机械臂撤离屏幕（避免遮挡 OCR 视线）。
    env.wizard.state["safePark"] = {"confirmed": True, "parkX": 100.0, "parkY": 50.0,
                                    "parkZ": 5.0, "confirmedAt": "t", "source": "test"}
    env.daemon.cmds.clear()
    # 用拟合后的 H 把目标 sy 反解回画面 y，词位置与实现判定同一坐标系
    _H = env.wizard.state["geometry"]["homography"]["homography"]
    Hm = [float(v) for row in _H for v in (row if isinstance(row, list) else [row])]

    def _sy_at(x, y):
        w = Hm[6] * x + Hm[7] * y + Hm[8]
        return (Hm[3] * x + Hm[4] * y + Hm[5]) / w

    def _frame_y_for(sy_target, x=CAM_W * 0.5):
        target_px = sy_target * SCREEN_H  # H 输出域是名义屏幕像素
        y0, y1 = 0.0, float(CAM_H)
        for _ in range(60):
            ym = (y0 + y1) / 2
            if _sy_at(x, ym) < target_px:
                y0 = ym
            else:
                y1 = ym
        return (y0 + y1) / 2

    y_echo = _frame_y_for(0.30)   # 屏幕上部回显（30%，必须被区域过滤掉）
    y_cand = _frame_y_for(0.62)   # 输入法区域候选（62%，真机实测位置）
    env.wizard._ocr_engine = FakeOcr([
        FakeOcrWord("统一校准测试", CAM_W * 0.5 - 40, y_echo - 12),
        FakeOcrWord("统一校准测试", CAM_W * 0.5 - 40, y_cand - 12),
    ])
    r = env.wizard.acceptance_execute("debug", "ime_input_tap")
    assert r["executed"] is True
    assert r["targetSource"] == "camera_ocr"
    record = env.wizard.state["acceptance"]["debug"]["ime_input_tap"]
    assert record["parkedBeforeOcr"]["parkX"] == 100.0
    assert record["imeReported"]["status"] == "displayed"  # 真实回报替代盲等
    # 命中的是输入法区域内的候选（sy=62%），不是上部的回显（sy=30%）
    assert record["ocrTarget"]["bbox"][1] == pytest.approx(y_cand - 12, abs=2.0)
    assert record["ocrTarget"]["regionFiltered"] is True
    assert len(env.ime.cancelled_tasks) == 0  # 成功路径不取消任务
    # 停靠走位先于点击目标走位：第一条 XY 指令是驶向停止位（100, 50）
    xy_cmds = [c for c in env.daemon.cmds if c.startswith("XY")]
    assert xy_cmds, "停靠与目标走位至少各产生一条 XY 指令"
    assert len(xy_cmds) >= 2, "先停靠再走位到 OCR 目标"
    assert r["ocrTarget"]["text"] == "统一校准测试"
    assert env.ime.tasks_created and env.ime.tasks_created[0]["content"] == "统一校准测试"
    assert any(c.startswith("TAP") for c in env.daemon.cmds)  # 点击已执行
    assert env.wizard.state["acceptance"]["debug"]["ime_input_tap"]["executedAt"]

    # 删除键保留现场标记兼容路径，并使用独立长按参数。
    r2 = env.wizard.acceptance_execute("debug", "ime_delete_clear", nx=0.2, ny=0.9)
    assert r2["executed"] is True
    assert "TAP 3.000 F1500 P2.2" in env.daemon.cmds

    # 微信项不可执行（人工确认）
    with pytest.raises(WizardError, match="人工确认"):
        env.wizard.acceptance_execute("wechat", "send_message", nx=0.5, ny=0.5)
    # 自动 OCR 不可用时才提示回退到「仅标记」坐标；失败路径必须取消任务
    # （残留任务会让下一次验收显示上一次文本，队列卡死）。
    env.wizard._ocr_engine = None
    with pytest.raises(WizardError, match="仅标记"):
        env.wizard.acceptance_execute("debug", "ime_input_tap")
    assert len(env.ime.cancelled_tasks) == 1


def test_locate_text_by_ocr_joins_words_on_same_line(env):
    """Vision 把中文短语拆成多个词时，按同一行相邻词合并命中。"""
    env.wizard._ocr_engine = FakeOcr([
        FakeOcrWord("统一", 120, 180, width=55),
        FakeOcrWord("校准", 180, 180, width=55),
        FakeOcrWord("测试", 240, 180, width=55),
    ])
    result = env.wizard._locate_text_by_ocr("统一校准测试", timeout=0.2)
    assert result["text"] == "统一校准测试"
    assert result["bbox"] == [120, 180, 295, 204]
    assert result["cameraX"] == 207.5
    assert result["cameraY"] == 192.0


def test_full_flow_to_ready(env):
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)

    # Z 阶段
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    env.wizard.z_tap_test("delete", 3.0, 1500.0, 2200)
    env.wizard.z_save("delete")

    # XY 阶段
    _run_xy_stage(env)
    assert env.wizard.state["stage"] == cw.STAGE_SCREEN_GEOMETRY

    # 视觉阶段：边缘基准 + 单应 → 直达验收（目标定位改为运行期 OCR，无登记阶段）
    edge = env.wizard.edge_capture()["edge"]
    assert len(edge["corners"]) == 4
    h = env.wizard.camera_fit()["homography"]
    assert h["reprojectionRmsPx"] < 1.0
    assert env.wizard.state["stage"] == cw.STAGE_DEBUG_ACCEPTANCE

    # 验收阶段（不要求任何固定目标登记）
    for check in cw.DEBUG_ACCEPTANCE_CHECKS:
        env.wizard.acceptance_record("debug", check, True, "现场确认")
    assert cw.DEBUG_ACCEPTANCE_CHECKS == ("ime_input_tap", "ime_delete_clear")
    env.wizard.acceptance_advance()
    assert env.wizard.state["stage"] == cw.STAGE_READY
    assert env.wizard.state["stage"] == cw.STAGE_READY
    bundle = env.wizard._bundle()
    assert bundle["status"] == "ready"
    assert bundle["acceptance"]["debug"] is True
    assert bundle["acceptance"]["wechat"] is False
    assert len(bundle["acceptance"]["checks"]) == len(cw.DEBUG_ACCEPTANCE_CHECKS)

    # 门禁放行；目标未登记不拦截——运行期 OCR 定位。
    gate = env.wizard.gate(wechat_send=True)
    assert gate["ok"] is True, gate["checks"]


def test_finish_blocked_before_debug_checks(env):
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    _run_xy_stage(env)
    env.wizard.edge_capture()
    env.wizard.camera_fit()
    # 单应后直达调试台验收；验收项未完成不允许推进
    assert env.wizard.state["stage"] == cw.STAGE_DEBUG_ACCEPTANCE
    with pytest.raises(WizardError, match="调试台验收未完成"):
        env.wizard.acceptance_advance()


def test_gate_rejects_when_bundle_not_ready(env):
    drive_to_z_ready(env)
    gate = env.wizard.gate(wechat_send=True)
    assert gate["ok"] is False
    codes = [c["code"] for c in gate["checks"]]
    assert "bundle_not_ready" in codes
    assert "xy_mapping_missing" in codes
    assert "wechat_acceptance_missing" not in codes


def test_gate_estop_blocks(env):
    env.wizard.start("k40-01")
    env.daemon.estop_locked = True
    gate = env.wizard.gate()
    assert "motion_blocked" in [c["code"] for c in gate["checks"]]


def test_gate_edge_deviation_measured_live(env):
    """屏幕边缘实时偏差进入门禁：挪动手机 → screen_edge_changed（§11/§16 真机项）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    _run_xy_stage(env)
    env.wizard.edge_capture()
    env.wizard.camera_fit()
    # 挪动屏幕边缘 20px（超过 8px 容差）
    old = env.quad["corners"]
    env.quad["corners"] = [[x + 20, y + 20] for x, y in old]
    gate = env.wizard.gate()
    assert "screen_edge_changed" in [c["code"] for c in gate["checks"]]
    env.quad["corners"] = old  # 恢复后不再误报（页面内容变化不影响边缘）
    gate = env.wizard.gate()
    assert "screen_edge_changed" not in [c["code"] for c in gate["checks"]]


# ---------- 持久化与恢复 ----------

def test_wizard_state_survives_restart(tmp_path):
    env1 = Env(tmp_path)
    env1.ime.script = auto_report(env1.ime, env1.daemon)
    drive_to_z_ready(env1)
    env1.daemon.z = 2.0
    env1.wizard.capture_hover_z()
    env1.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env1.wizard.z_save("tap")

    env2 = Env(tmp_path)  # 模拟调试台重启：新实例读回状态
    assert env2.wizard.state["stage"] == cw.STAGE_XY
    assert env2.wizard.state["z"]["hoverZ"] == 2.0
    assert env2.wizard.state["z"]["tap"]["depth"] == 3.0


def test_session_restart_preserves_calibration_and_requires_rebind(tmp_path):
    """重新建立 daemon/IME 会话：参数、历史和 candidate 保留，执行资格撤销。"""
    env = Env(tmp_path)
    env.wizard.start("k40-01")
    old_sid = env.wizard.state["imeSessionId"]
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)

    with env.wizard._lock:
        env.wizard.state["z"].update({
            "hoverZ": 2.0,
            "hoverEvidence": {"sample": "hover-history"},
            "autoSafe": {"zSafe": 5.0, "tapDepth": 3.0, "validated": True},
            "trials": [{"sampleId": "z-1", "ok": True}],
        })
        env.wizard.state["xy"] = {
            "accepted": [{"index": 0, "screenX": 100.0, "screenY": 200.0}],
            "trials": [{"index": 0, "ok": True}],
            "fit": {"a": 10.0, "b": 0.0, "c": 100.0,
                    "d": 0.0, "e": 8.0, "f": 200.0,
                    "residualRmsPx": 0.2, "maxResidualPx": 0.4},
            "validation": [{"index": 0, "passed": True}],
            "validationPassed": True,
        }
        env.wizard.state["geometry"] = {
            "edge": {**env.quad, "frameSeq": 1},
            "homography": {"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                           "reprojectionRmsPx": 0.1},
        }
        env.wizard.state["safePark"] = {
            "confirmed": True, "parkX": 11.0, "parkY": 12.0, "parkZ": 5.0,
            "source": "test",
        }
        env.wizard.state["targets"] = {
            "ime_delete_key": {"nx": 0.1, "ny": 0.9, "enabled": True},
        }
        env.wizard.state["acceptance"] = {
            "debug": {"ime_input_tap": {"ok": True, "detail": "history"}},
            "wechat": {"search": {"ok": True, "detail": "history"}},
        }
        env.wizard.state["autoCalibration"] = {
            "runId": "run-history", "state": "PROMOTED",
            "history": [{"iteration": 1, "armRmsPx": 0.5}],
        }
        env.wizard._sync_bundle()
        env.wizard._save_state()

    candidate = {
        "runId": "run-history", "deviceId": "arm-01",
        "daemonSession": "sess-A", "phoneSessionId": old_sid,
        "state": cw.AUTO_STATE_PROMOTED,
        "history": [{"iteration": 1, "armRmsPx": 0.5}],
        "executionEligibility": {
            "eligible": True, "daemonSession": "sess-A",
            "phoneSessionId": old_sid,
        },
        "events": [{"event": "model_locked", "detail": {}}],
    }
    cb.save_candidate(candidate, tmp_path)

    env.wizard.start("k40-01")
    new_sid = env.wizard.state["imeSessionId"]
    assert new_sid != old_sid
    assert env.wizard.state["sessionRebind"]["required"] is True
    assert env.wizard.state["sessionRebind"]["previousImeSessionId"] == old_sid
    assert env.wizard.state["z"]["hoverZ"] == 2.0
    assert env.wizard.state["z"]["autoSafe"]["zSafe"] == 5.0
    assert env.wizard.state["xy"]["fit"]["c"] == 100.0
    assert env.wizard.state["geometry"]["homography"]["reprojectionRmsPx"] == 0.1
    assert env.wizard.state["safePark"]["parkX"] == 11.0
    assert env.wizard.state["targets"]["ime_delete_key"]["enabled"] is True
    assert env.wizard.state["autoCalibration"]["runId"] == "run-history"
    assert env.wizard.state["acceptance"]["debug"]["ime_input_tap"]["ok"] is True

    preserved_bundle = cb.load_bundle("arm-01", tmp_path)
    assert preserved_bundle["status"] == "draft"
    assert preserved_bundle["safePark"]["parkX"] == 11.0
    assert preserved_bundle["targets"]["ime_delete_key"]["enabled"] is True
    preserved_candidate = cb.load_candidate("arm-01", "run-history", tmp_path)
    assert preserved_candidate["history"] == [{"iteration": 1, "armRmsPx": 0.5}]
    assert preserved_candidate["executionEligibility"]["eligible"] is False

    # 新会话已创建但尚未复核时，不得产生机械动作。
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.cmds.clear()
    with pytest.raises(WizardError, match="尚未完成重新复核"):
        env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    assert env.daemon.cmds == []

    # 新会话一次真实 Z 确认只恢复 Z 资格（P10）：XY/停车位仍待复核。
    result = env.wizard.z_confirm_safe(tap_depth=3.0)
    assert result["validated"] is True
    rebind = env.wizard.state["sessionRebind"]
    assert rebind["required"] is True
    assert rebind["pendingScopes"] == {"z": False, "xy": True, "park": True}
    # Z 范围动作不受 XY/停车待复核影响。
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    assert any(command.startswith("TAP ") for command in env.daemon.cmds)
    # 涉及 XY 的动作与自动校准仍被拦（旧 XY/停车资格未复核）。
    env.daemon.cmds.clear()
    with pytest.raises(WizardError, match="尚未完成重新复核"):
        env.wizard.auto_start()
    assert env.daemon.cmds == []


def test_p09_park_entries_check_park_own_confirmed_session(tmp_path):
    """P09：停车位资格按其自身确认会话核对。

    顶层 daemonSession 已更新为 B、停车位仍确认于 A 时：
    - 自动校准起始停车守卫直接拒绝；
    - 异常收尾停车跳过 XY 走位、只做 Z 向收回并留痕。
    """
    env = Env(tmp_path)
    env.wizard.start("k40-01")
    with env.wizard._lock:
        env.wizard.state["safePark"] = {
            "confirmed": True, "parkX": 11.0, "parkY": 12.0, "parkZ": 5.0,
            "source": "test", "daemonSession": "sess-A",
            "mposAtConfirm": {"x": 11.0, "y": 12.0, "z": 5.0}}
        env.wizard.state["daemonSession"] = "sess-B"  # 顶层已换 B
    park = env.wizard.state["safePark"]

    # 守卫：停车位自身会话（A）≠ 当前（B）→ 拒绝任何停车走位。
    env.daemon.session = "sess-B"
    with pytest.raises(WizardError, match="停车位确认于机械坐标会话 sess-A"):
        env.wizard._require_park_session_current(park, action="测试停车")

    # 自动 run 收尾停车：只收 Z，不走 XY，事件留痕。
    run = {"safePark": dict(park), "z": {"zSafe": 8.0}}
    env.daemon.cmds.clear()
    env.wizard._auto_safe_park(run, best_effort=True)
    assert run["lastPark"].get("zOnly") is True
    assert not any(command.startswith("XY ") for command in env.daemon.cmds)
    assert any(command.startswith("Z ") for command in env.daemon.cmds)
    events = [event.get("event") for event in run.get("events", [])]
    assert "park_skipped_session_changed" in events

    # 停车位在当前会话重新确认后恢复：park_confirm 绑定当前会话并清偿资格。
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.wizard.z_confirm_safe(tap_depth=3.0)
    confirmed = env.wizard.park_confirm()
    assert confirmed["daemonSession"] == env.wizard.state["daemonSession"]
    assert env.wizard.state["sessionRebind"]["required"] is False


def test_p09_auto_start_rejects_park_from_another_session(tmp_path):
    """P09：自动校准起始停车使用停车位自身会话核对，不因顶层会话一致放行。"""
    env = Env(tmp_path)
    env.wizard.start("k40-01")
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.wizard.z_confirm_safe(tap_depth=3.0)  # Z 资格在当前会话恢复
    with env.wizard._lock:
        # 停车位来自更早的会话；顶层与 Z 复核均已落在当前会话。
        env.wizard.state["safePark"] = {
            "confirmed": True, "parkX": 11.0, "parkY": 12.0, "parkZ": 5.0,
            "source": "test", "daemonSession": "sess-old",
            "mposAtConfirm": {"x": 11.0, "y": 12.0, "z": 5.0}}
        # 越过阶段/复核门直达停车前置（其余前置由其他测试覆盖）。
        env.wizard.state["stage"] = cw.STAGE_TARGET
        env.wizard.state["sessionRebind"] = {
            **env.wizard.state["sessionRebind"],
            "required": False, "pendingScopes": {"z": False, "xy": False, "park": False}}
        env.wizard._save_state()
    env.daemon.cmds.clear()
    with pytest.raises(WizardError, match="停车位确认于机械坐标会话 sess-old"):
        env.wizard.auto_start()
    assert env.daemon.cmds == []


def test_old_raw_coordinate_calibration_is_invalidated_on_restart(tmp_path):
    env1 = Env(tmp_path)
    env1.wizard.start("k40-01")
    state_path = tmp_path / "devices" / "arm-01" / "calibration_wizard.json"
    import json

    saved = json.loads(state_path.read_text(encoding="utf-8"))
    saved.pop("coordinateSpaceVersion")
    state_path.write_text(json.dumps(saved), encoding="utf-8")
    env2 = Env(tmp_path)
    assert env2.wizard.state["stage"] == cw.STAGE_INVALIDATED
    assert env2.wizard.state["invalidReason"]["code"] == "coordinate_space_upgraded"


def test_xy_preview_keeps_target_for_manual_aiming(env):
    """XY 分步流程（§7.2）：显示标靶长驻 → 人工对位 → 测试点击复用预布防。

    现场问题回归：旧流程布防后 1.3s 即自动点击，用户来不及移臂，
    全部样本落在同一位置；新流程预布防期间标靶持续显示。
    """
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")

    # ① 显示标靶：布防但不点击，标靶长驻
    result = env.wizard.xy_preview(0)
    preview = result["preview"]
    assert preview["index"] == 0 and preview["u"] == cw.XY_PLAN_POINTS[0][1]
    sid = env.wizard.state["imeSessionId"]
    assert env.ime.sessions[sid]["armed"]["target"] is not None  # 手机显示标靶中
    armed_count = len(env.ime.arm_calls)

    # 用户慢慢对位（远超轮询延迟），期间无点击、无回报
    _time.sleep(0.5)
    assert env.ime.sessions[sid]["armed"] is not None

    # ② 测试点击：复用预布防（不重复布防），对位耗时抵扣等待
    sx, sy = cw.XY_PLAN_POINTS[0][1] * SCREEN_W, cw.XY_PLAN_POINTS[0][2] * SCREEN_H
    env.jog_to_screen(sx, sy)
    env.tips.append((*cam(sx, sy), CAM_W, CAM_H))
    trial = env.wizard.xy_tap_test(0)["trial"]
    assert trial["ok"] is True
    assert trial["errorPx"] < 6.0
    assert len(env.ime.arm_calls) == armed_count  # 没有重新布防
    assert env.wizard.state["xy"]["preview"] is None  # 回报后布防消耗、预览清除
    assert env.ime.sessions[sid]["armed"] is None


def test_xy_preview_stray_touch_rejected(env):
    """对位期间屏幕被误触：布防被消耗 → 测试点击拒绝并要求重新显示标靶。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    env.wizard.xy_preview(0)
    sid = env.wizard.state["imeSessionId"]
    # 模拟误触：直接按布防回报一个杂点
    armed = env.ime.sessions[sid]["armed"]
    env.ime.report(sid, armed["sampleId"], 1, 2)
    with pytest.raises(WizardError, match="误触"):
        env.wizard.xy_tap_test(0)
    # 重新显示标靶后可正常走流程
    env.ime.script = auto_report(env.ime, env.daemon)
    env.wizard.xy_preview(0)
    sx, sy = cw.XY_PLAN_POINTS[0][1] * SCREEN_W, cw.XY_PLAN_POINTS[0][2] * SCREEN_H
    env.jog_to_screen(sx, sy)
    env.tips.append((*cam(sx, sy), CAM_W, CAM_H))
    assert env.wizard.xy_tap_test(0)["trial"]["ok"] is True


def test_xy_accept_auto_previews_next_point(env):
    """接受一个点后自动为下一个未接受点显示标靶（减少现场点击）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    for i in (0, 1):
        env.wizard.xy_preview(i)
        u, v = cw.XY_PLAN_POINTS[i][1], cw.XY_PLAN_POINTS[i][2]
        env.jog_to_screen(u * SCREEN_W, v * SCREEN_H)
        env.tips.append((*cam(u * SCREEN_W, v * SCREEN_H), CAM_W, CAM_H))
        env.wizard.xy_tap_test(i)
    result = env.wizard.xy_accept(0, accept=True)
    assert result["acceptedCount"] == 1
    assert result["nextPreview"]["index"] == 1  # 下一个未接受的点是 1（已测未确认）
    assert env.wizard.state["xy"]["preview"]["index"] == 1


def test_xy_fit_rejects_clustered_samples(env):
    """样本机械位置太集中（没移臂重复点击）→ 拟合前给出明确错误。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    # 不移臂，在同一位置接受 3 个点
    for i in range(3):
        env.wizard.xy_preview(i)
        env.wizard.xy_tap_test(i)
        env.wizard.xy_accept(i, accept=True)
    with pytest.raises(WizardError, match="太集中"):
        env.wizard.xy_fit()


def test_xy_validation_auto_positions_via_fit(env):
    """验证点由拟合自动走位：不人工点动也能命中计划位置（验证映射本身，§7.4）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    for i, (name, u, v) in enumerate(cw.XY_PLAN_POINTS):
        env.wizard.xy_preview(i)
        env.jog_to_screen(u * SCREEN_W, v * SCREEN_H)
        env.tips.append((*cam(u * SCREEN_W, v * SCREEN_H), CAM_W, CAM_H))
        env.wizard.xy_tap_test(i)
        env.wizard.xy_accept(i, accept=True)
    env.wizard.xy_fit()
    # 故意把臂挪到远处：验证点仍应自动走位回计划位置并通过
    env.daemon.x, env.daemon.y = 0.0, 0.0
    u, v = cw.XY_VALIDATION_POINTS[1][1], cw.XY_VALIDATION_POINTS[1][2]
    result = env.wizard.xy_validate_tap(1)["result"]
    assert result["passed"] is True, result
    assert result["errorPx"] < 1.0  # 走位+回报闭环 = 计划位置（映射可逆）
    assert "XY" in " ".join(c for c in env.daemon.cmds[-8:])  # 确实发生了自动走位


def test_xy_reject_keeps_trial_out_of_fit(env):
    """继续修正的点保留为试验记录，不进入正式拟合样本（§7.2）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    # 偏心点击 + 接受偏心样本会推高残差；先测"拒绝不进样本"
    sx, sy = 300.0, 400.0  # 与 5 个计划点都不同
    env.jog_to_screen(sx, sy)
    env.tips.append((*cam(sx, sy), CAM_W, CAM_H))
    env.wizard.xy_tap_test(0)
    env.wizard.xy_accept(0, accept=False)
    assert env.wizard.state["xy"]["accepted"] == []
    assert len(env.wizard.state["xy"]["trials"]) == 1
    with pytest.raises(WizardError, match="至少需要 3 个"):
        env.wizard.xy_fit()


def test_xy_fit_residual_gate(env):
    """超差点混入样本：默认拒绝并列出逐点残差；force=True 强行拟合（记录在案）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    for i, (name, u, v) in enumerate(cw.XY_PLAN_POINTS):
        sx, sy = u * SCREEN_W, v * SCREEN_H
        env.jog_to_screen(sx, sy)
        env.tips.append((*cam(sx, sy), CAM_W, CAM_H))
        env.wizard.xy_tap_test(i)
        env.wizard.xy_accept(i)
    # 篡改一个已接受样本的屏幕坐标 50px → RMS 超限 → 拒绝并给出逐点残差
    env.wizard.state["xy"]["accepted"][2]["screenX"] += 50
    with pytest.raises(WizardError, match="残差超限") as exc_info:
        env.wizard.xy_fit()
    assert "点2" in exc_info.value.args[0]  # 逐点残差指认坏点
    assert env.wizard.state["xy"]["fit"] is None
    # 强行拟合：放行并在拟合记录中留痕；被篡改点（点2）残差应为全场最大
    result = env.wizard.xy_fit(force=True)
    assert result["fit"]["forced"] is True
    assert result["fit"]["warnings"]
    residuals = {r["index"]: r["residualPx"] for r in result["fit"]["residualsByPoint"]}
    assert residuals[2] > 10  # 被篡改点残差显著（最小二乘会把离群摊到邻点）


def test_xy_fit_detects_lost_steps(env):
    """丢步检测：MPos 与实际脱节（探头尖像素自洽、机械坐标偏 40mm）
    → 相机↔机械一致性残差爆炸 → 拒绝且 force 不可绕过。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    for i, (name, u, v) in enumerate(cw.XY_PLAN_POINTS):
        sx, sy = u * SCREEN_W, v * SCREEN_H
        env.jog_to_screen(sx, sy)
        env.tips.append((*cam(sx, sy), CAM_W, CAM_H))
        env.wizard.xy_tap_test(i)
        env.wizard.xy_accept(i)
    # 模拟丢步：点4 的 MPos 记录偏了 40mm（探头尖/屏幕仍按真实位置走）
    env.wizard.state["xy"]["accepted"][4]["robotX"] += 40
    with pytest.raises(WizardError, match="丢步") as exc_info:
        env.wizard.xy_fit()
    assert "断开" in exc_info.value.args[0]  # 错误里给出恢复路径
    with pytest.raises(WizardError, match="丢步"):
        env.wizard.xy_fit(force=True)  # 强行拟合不可绕过丢步
    assert env.wizard.state["xy"]["fit"] is None


def test_xy_validation_failure_blocks_finish(env):
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    for i, (name, u, v) in enumerate(cw.XY_PLAN_POINTS):
        env.jog_to_screen(u * SCREEN_W, v * SCREEN_H)
        env.tips.append((*cam(u * SCREEN_W, v * SCREEN_H), CAM_W, CAM_H))
        env.wizard.xy_tap_test(i)
        env.wizard.xy_accept(i)
    env.wizard.xy_fit()
    # 验证点点偏 30px（脚本回报加噪声）→ 全部验证点超差
    env.ime.script = auto_report(env.ime, env.daemon, noise=(30.0, 0.0))
    for i, (name, u, v) in enumerate(cw.XY_VALIDATION_POINTS):
        env.jog_to_screen(u * SCREEN_W, v * SCREEN_H)
        result = env.wizard.xy_validate_tap(i)
        assert result["result"]["passed"] is False
    with pytest.raises(WizardError, match="超差"):
        env.wizard.xy_finish()


def test_target_ops_are_optional_and_stage_gated(env):
    """目标登记是可选遗留能力：主流程不经过目标阶段；target 仍受阶段门禁。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    _run_xy_stage(env)
    env.wizard.edge_capture()
    env.wizard.camera_fit()
    # 单应后直达调试台验收（无目标阶段）；验收阶段不允许跑目标测试
    assert env.wizard.state["stage"] == cw.STAGE_DEBUG_ACCEPTANCE
    with pytest.raises(WizardError, match="阶段"):
        env.wizard.target_test("send_button")


# ---------- v1.1 自动校准（规格书 v1.1 + 补充文档 Stage 2 fake 全场景） ----------

from drivers import calibration_bundle as cb  # noqa: E402
from drivers import target_archive  # noqa: E402


class AutoEnv(Env):
    """自动校准 fake 环境：相机=按当前布防标靶的线性映射；触点=truth(机械位置)+噪声。

    物理模型与手动用例一致（truth/auto_report），叠加可注入钩子：
    - candidates_hook：覆盖绿色候选（注入外点/多候选/检测失败）
    - noise_fn + noise_active：触点噪声（按机械位置键控，可中途开关）
    - mismatch_mode：触点 sequence 错配（走 _tap_with_report 的失效路径）
    """

    def __init__(self, tmp_path, *, seed_error_px=2.0, real=False, workspace=None,
                 auto_config=None, phone_lag=0.02, ocr_words=None):
        super().__init__(tmp_path, wizard_extra={
            "target_candidates": lambda jpg: self.auto_candidates(),
            "ocr_engine": FakeOcr(ocr_words) if ocr_words is not None else None,
            "real_hardware": real,
            "auto_config": {**{"detect_interval_s": 0.02,
                               "acceptance_timeout_s": 8.0},
                            **({"arm_workspace_mm": workspace} if workspace else {}),
                            **(auto_config or {})},
            "touch_timeout": 0.6,
            "watcher_interval": 0.05,
            "phone_arm_lead_s": 0.05,
            "target_draw_wait_s": 0.02,
            "target_detect_timeout_s": 0.2,
        })
        self.ime.phone_lag = phone_lag
        self.candidates_hook = None
        self.noise_active = False
        self.noise_fn = None
        self.mismatch_mode = False
        self.seed_error_px = seed_error_px
        self._install_touch()

    def auto_candidates(self):
        if self.candidates_hook is not None:
            return self.candidates_hook()
        return self.real_candidates()

    def real_candidates(self):
        """默认候选（按当前布防标靶线性映射）；测试钩子应包裹它而不是 auto_candidates。"""
        for s in self.ime.sessions.values():
            if s["armed"] and s["armed"].get("target"):
                t = s["armed"]["target"]
                x, y = cam(t["u"] * SCREEN_W, t["v"] * SCREEN_H)
                return {"items": [{"x": x, "y": y, "area": 900.0}],
                        "frameW": CAM_W, "frameH": CAM_H}
        return {"items": [], "frameW": CAM_W, "frameH": CAM_H}

    def _install_touch(self):
        def on_tap(cmd_line: str):
            dwell_s = 0.0
            for part in cmd_line.split()[2:]:
                if part[:1] in ("P", "p"):
                    try:
                        dwell_s = float(part[1:])
                    except ValueError:
                        pass
            if dwell_s > 1.0:
                return  # 长按：校准页拒收
            for sid, s in self.ime.sessions.items():
                armed = s["armed"]
                if not armed:
                    continue
                if _time.time() - armed.get("armed_at", 0) < self.ime.phone_lag:
                    return
                sx, sy = truth(self.daemon.x, self.daemon.y)
                if self.noise_active and self.noise_fn:
                    dx, dy = self.noise_fn(self.daemon.x, self.daemon.y)
                    sx, sy = sx + dx, sy + dy
                if self.mismatch_mode:
                    # 直接落入 samples 且 sequence 与布防不符（模拟手机端错配上报）
                    s["samples"].append({
                        "sampleId": armed["sampleId"],
                        "sequence": armed["sequence"] + 5,
                        "screenX": float(sx), "screenY": float(sy),
                        "screenWidth": SCREEN_W, "screenHeight": SCREEN_H,
                        "orientation": "portrait"})
                    s["armed"] = None
                    return
                self.ime.report(sid, armed["sampleId"], sx, sy)

        self.daemon.on_tap = on_tap

    def seed_robot_to_screen(self) -> dict:
        err = float(self.seed_error_px)
        return {"a": 10.0, "b": 0.0, "c": 100.0 + err,
                "d": 0.0, "e": 8.0, "f": 200.0 + err}


def auto_ready_env(tmp_path, *, register_delete_target=True, **kwargs) -> AutoEnv:
    """自动校准正向夹具；显式 None/False 可保留 OCR/删除键缺配置场景。"""
    kwargs.setdefault("ocr_words", [FakeOcrWord("统一校准测试", 300, 400)])
    env = AutoEnv(tmp_path, **kwargs)
    env.wizard.start("k40-01")
    env.wizard.precheck()
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    # Stage visibility precedes the watcher's bundle write; wait for that write.
    with env.wizard._lock:
        env.wizard.state["screen"] = {"orientation": "portrait",
                                      "screenWidth": SCREEN_W, "screenHeight": SCREEN_H}
        env.wizard._save_state()
        bundle = cb.new_bundle("arm-01")
        bundle["xy"]["robotToScreen"] = env.seed_robot_to_screen()
        bundle["daemonSession"] = "sess-A"
        cb.save_bundle(bundle, tmp_path)
    env.wizard.z_confirm_safe(tap_depth=3.0)
    env.wizard.park_confirm()  # 停靠位 = 当前 (0,0)，Z=zSafe
    if register_delete_target:
        target_archive.save_target(
            "arm-01", "ime_delete_key",
            {"nx": 0.1, "ny": 0.9, "targetMode": "hover_only"}, tmp_path)
    return env


def wait_auto(env, predicate, timeout=40.0, desc=""):
    deadline = _time.time() + timeout
    view = None
    while _time.time() < deadline:
        view = env.wizard.auto_view()
        if predicate(view):
            return view
        _time.sleep(0.05)
    raise AssertionError(f"等待自动校准 {desc} 超时；最后状态："
                         f"{view and {k: view.get(k) for k in ('state', 'phase', 'lastFailure', 'pendingRetry')}}")


def wait_e_execution(env, run_id: str, check: str) -> dict:
    """等实际执行留痕；runner 失败时立即报告，不能提前签过或吞掉异常。"""
    view = wait_auto(
        env, lambda v: bool(
            ((v.get("acceptance") or {}).get("debug") or {}).get(
                check, {}).get("executedAt"))
        or v["state"] not in cw.AUTO_ACTIVE_STATES,
        desc=f"{check} 执行完成")
    assert view["runId"] == run_id
    assert view["state"] == cw.AUTO_STATE_RUNNING, view.get("lastFailure")
    assert view["phase"] == "E_ACCEPTANCE"
    record = view["acceptance"]["debug"][check]
    assert record["executedAt"] and len(record["executedRobot"]) == 2
    assert not record.get("ok"), "执行完成前不得提前确认"
    return record


def finish_e_acceptance(env, run_id: str) -> None:
    """逐项等待真实验收动作，再模拟人工确认；微信项不参与校准门禁。"""
    taps_after_input = None
    for check in cw.DEBUG_ACCEPTANCE_CHECKS:
        record = wait_e_execution(env, run_id, check)
        if check == "ime_input_tap":
            assert record["targetSource"] == "camera_ocr"
            assert record["ocrTarget"]["regionFiltered"] is True
            assert record["imeReported"]["status"] == "displayed"
            assert record["dispatchedTaskId"] == env.ime.tasks_created[-1]["taskId"]
            assert env.wizard._ocr_engine.calls > 0
            assert env.ime.cancelled_tasks == []
            debug = env.wizard.auto_view()["acceptance"]["debug"]
            assert not debug.get("ime_delete_clear", {}).get("executedAt")
            taps_after_input = env.daemon.tap_count
        else:
            assert env.daemon.tap_count == taps_after_input + 1
            assert env.daemon.cmds[-1].startswith("TAP ")
            assert " P2" in env.daemon.cmds[-1]
        env.wizard.auto_acceptance_record(run_id, "debug", check, True, "测试确认通过")
    candidate = cb.load_candidate(env.wizard.device, run_id, env.wizard.data_root)
    events = [(e["event"], e["detail"]["check"]) for e in candidate["events"]
              if e["event"] == "acceptance_executed"
              or (e["event"] == "acceptance_recorded"
                  and e["detail"]["kind"] == "debug")]
    assert events == [
        ("acceptance_executed", "ime_input_tap"),
        ("acceptance_recorded", "ime_input_tap"),
        ("acceptance_executed", "ime_delete_clear"),
        ("acceptance_recorded", "ime_delete_clear"),
    ]


def _first_active_run_id(env) -> str:
    return env.wizard.auto_view()["runId"]


def test_auto_full_convergence_promotes_ready(tmp_path):
    """正常收敛全链路：Phase0→Bootstrap→FirstClick→Phase1→Phase2→独立验证→E→READY。"""
    env = auto_ready_env(tmp_path)
    base_hash = cb.profile_hash(cb.load_bundle("arm-01", tmp_path))
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)

    def e_phase(view):
        return view["phase"] == "E_ACCEPTANCE"

    wait_auto(env, e_phase, desc="E 验收阶段")
    finish_e_acceptance(env, run_id)
    view = wait_auto(env, lambda v: v["state"] == "PROMOTED", desc="提升 READY")
    assert view["locks"]["cameraToScreen"]["state"] == "LOCKED"
    assert view["locks"]["armAffine"]["state"] == "LOCKED"

    promoted = cb.load_bundle("arm-01", tmp_path)
    assert promoted["status"] == "ready"
    assert promoted["autoCalibration"]["runId"] == run_id
    assert promoted["autoCalibration"]["state"] == "PROMOTED"
    assert promoted["validation"]["passed"] is True
    assert len(promoted["validation"]["points"]) == 5
    assert promoted["modelLocks"]["cameraToScreen"]["state"] == "LOCKED"
    assert promoted["modelLocks"]["armAffine"]["state"] == "LOCKED"
    assert promoted["z"]["zSafe"] == 5.0 and promoted["z"]["hoverZ"] == 5.0
    assert promoted["safePark"]["confirmed"] is True
    assert promoted["camera"]["visualVerify"]["passed"] is True
    # 4x4=16 点计划固定为 15/38/62/85%（2026-09-04 方案 A 加密）；
    # 独立验证 5 点不与标定点重合
    candidate = cb.load_candidate("arm-01", run_id, tmp_path)
    plan = [p["u"] for p in candidate["pointPlan"]]
    assert sorted(set(plan)) == [0.15, 0.38, 0.62, 0.85]
    assert len(candidate["pointPlan"]) == 16
    val_coords = {(p["u"], p["v"]) for p in candidate["validationPlan"]}
    plan_coords = {(p["u"], p["v"]) for p in candidate["pointPlan"]}
    assert val_coords.isdisjoint(plan_coords)
    # 样本带运行版本绑定（补充 §7）
    sample = candidate["samples"][0]
    assert sample["runId"] == run_id and sample["modelVersion"] >= 0
    assert sample["cameraId"] == "cam-01" and sample["phoneSessionId"]
    # 全部样本合格且等级 PASS
    assert all(s["eligible"] and s["errorLevel"] == "PASS"
               for s in candidate["samples"] if s["phase"] != "FIRST_CLICK_GATE")
    # 门禁视角：promoted 档案通过 model lock 扩展检查（不含 session 等现场事实）
    assert cb.profile_hash(cb.load_bundle("arm-01", tmp_path)) != base_hash
    assert env.wizard.state["stage"] == cw.STAGE_READY


@pytest.mark.parametrize("failure,failed_check,message", [
    ("ocr_missing", "ime_input_tap", "未配置本地 OCR"),
    ("ime_pending", "ime_input_tap", "未回报已显示"),
    ("delete_missing", "ime_delete_clear", "删除键位置尚未登记"),
])
def test_auto_e_execution_failure_never_promotes(tmp_path, failure, failed_check, message):
    kwargs = {"ocr_words": None} if failure == "ocr_missing" else {}
    env = auto_ready_env(
        tmp_path, register_delete_target=failure != "delete_missing", **kwargs)
    env.ime.ime_stuck_pending = failure == "ime_pending"
    base_hash = cb.profile_hash(cb.load_bundle("arm-01", tmp_path))
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    try:
        if failure == "delete_missing":
            wait_e_execution(env, run_id, "ime_input_tap")
            taps = env.daemon.tap_count
            env.wizard.auto_acceptance_record(run_id, "debug", "ime_input_tap", True)
        view = wait_auto(env, lambda v: v["state"] not in cw.AUTO_ACTIVE_STATES,
                         desc="E 执行失败")
        assert view["state"] == cw.AUTO_STATE_FAILED
        assert view["lastFailure"]["code"] == "acceptance_execution_failed"
        assert message in view["lastFailure"]["message"]
        candidate = cb.load_candidate("arm-01", run_id, tmp_path)
        assert candidate["validation"]["passed"] is True
        assert len(candidate["validation"]["points"]) == 5
        debug = candidate["acceptance"]["debug"]
        assert not debug.get(failed_check, {}).get("executedAt")
        assert not debug.get(failed_check, {}).get("ok")
        executed = [e["detail"]["check"] for e in candidate["events"]
                    if e["event"] == "acceptance_executed"]
        failed = [e["detail"]["check"] for e in candidate["events"]
                  if e["event"] == "acceptance_execute_failed"]
        assert failed == [failed_check]
        assert len(env.ime.tasks_created) == 1
        if failure == "delete_missing":
            assert executed == ["ime_input_tap"]
            assert env.daemon.tap_count == taps
            assert env.ime.cancelled_tasks == []
        else:
            assert executed == []
            assert env.ime.cancelled_tasks == [env.ime.tasks_created[0]["taskId"]]
            assert not debug.get("ime_delete_clear", {}).get("executedAt")
        assert cb.profile_hash(cb.load_bundle("arm-01", tmp_path)) == base_hash
        assert env.wizard.state["stage"] != cw.STAGE_READY
        with pytest.raises(WizardError, match="不存在或已结束"):
            env.wizard.auto_acceptance_record(run_id, "debug", failed_check, True)
    finally:
        env.wizard.auto_stop()
        env.wizard._auto["thread"].join(timeout=2)


def test_auto_acceptance_delete_key_ui_first_with_window_rebind(tmp_path):
    """2026-09-04 三步收敛：E 验收删除键优先读 ui_targets；验收期登记窗口在
    E 相位放行（bindPending）；promote 时 rebind 生效（绑定与最终档案一致）。"""
    from drivers import ui_targets

    env = auto_ready_env(tmp_path, register_delete_target=False)
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    wait_e_execution(env, run_id, "ime_input_tap")
    assert "ime_delete_key" not in target_archive.load_targets("arm-01", tmp_path)

    # 验收期登记窗口：auto run 处于 E 相位 → 放行（活动档案非 ready 也可登记）
    window = env.wizard.ui_target_registration_window()
    assert window == {"source": "auto_run", "runId": run_id}
    entry = ui_targets.register_draft("arm-01", "ime_delete_key", nx=0.1, ny=0.9,
                                      operator="操作员甲", data_root=tmp_path,
                                      registration_window=window)
    assert entry["bindPending"] is True

    # E 验收执行：删除键读取 ui 条目优先（无 legacy 标记也能执行成功）
    finish_e_acceptance(env, run_id)
    # Z 基准修复（2026-09-04"先下压后走位"事故）：auto 验收走位悬停必须用
    # candidate zSafe（本夹具 5.0），绝不落到手动 hoverZ/默认 2.0
    assert env.daemon.z == pytest.approx(5.0)

    wait_auto(env, lambda v: v["state"] == "PROMOTED", desc="提升 READY")
    promoted = cb.load_bundle("arm-01", tmp_path)

    # 提升 rebind：占位快照替换为最终档案事实，绑定一致、条目可解析
    entry = ui_targets.load_entry("arm-01", "ime_delete_key", tmp_path)
    assert "bindPending" not in entry
    assert ui_targets.binding_mismatches(entry, promoted) == []
    ui_targets.mark_test_result("arm-01", "ime_delete_key", passed=True,
                                evidence={"kind": "hover_test"}, data_root=tmp_path)
    entry = ui_targets.set_enabled("arm-01", "ime_delete_key", enabled=True,
                                   data_root=tmp_path)
    resolved = ui_targets.resolve_entry(entry, promoted)
    assert resolved["nx"] == 0.1 and resolved["targetMode"] == "hover_only"


def test_registration_window_states(tmp_path):
    """登记窗口：空闲向导（precheck/无 run）不开放；手动目标/验收阶段且几何
    齐备时开放 manual 窗口。"""
    from drivers import ui_targets

    env = auto_ready_env(tmp_path)
    assert env.wizard.ui_target_registration_window() is None
    env.wizard.state["stage"] = cw.STAGE_DEBUG_ACCEPTANCE
    env.wizard.state["geometry"] = {"homography": [1, 0, 0, 0, 1, 0, 0, 0, 1],
                                    "frameW": 640, "frameH": 480}
    env.wizard.state["xy"] = {"fit": {"a": 10.0, "b": 0.0, "c": 0.0}}
    assert env.wizard.ui_target_registration_window() == {"source": "manual"}
    # manual 窗口登记同样走 bindPending（由手动 finish 时 rebind）
    entry = ui_targets.register_draft("arm-01", "back_button", nx=0.1, ny=0.1,
                                      data_root=tmp_path,
                                      registration_window={"source": "manual"})
    assert entry["bindPending"] is True


def test_auto_requires_operator_confirmations(tmp_path):
    """未确认 zSafe / SAFE PARK / 屏幕基线时拒绝启动（§16/§17）。"""
    env = AutoEnv(tmp_path)
    env.wizard.start("k40-01")
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    with pytest.raises(WizardError, match="屏幕尺寸"):
        env.wizard.auto_start(defer_input_acceptance=False)
    env.wizard.state["screen"] = {"orientation": "portrait",
                                  "screenWidth": SCREEN_W, "screenHeight": SCREEN_H}
    env.wizard._save_state()
    with pytest.raises(WizardError, match="zSafe"):
        env.wizard.auto_start(defer_input_acceptance=False)
    result = env.wizard.z_confirm_safe(tap_depth=3.0)
    assert result["validated"] is True  # 原地测试点击收到触点回报
    assert result["report"]["screenWidth"] == SCREEN_W  # 屏幕基线同步建立
    with pytest.raises(WizardError, match="SAFE PARK"):
        env.wizard.auto_start(defer_input_acceptance=False)
    env.wizard.park_confirm()
    view = env.wizard.auto_start(defer_input_acceptance=False)  # fake 模式：无需口令/工作区
    assert view["state"] in ("RUNNING",) or view["lastRun"]


def test_auto_bootstrap_required_without_arm_seed(tmp_path):
    """无 Arm seed：Phase 0 正常完成并锁 H，但停在 BOOTSTRAP_REQUIRED，全程零 TAP。"""
    env = AutoEnv(tmp_path)
    env.wizard.start("k40-01")
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    env.wizard.state["screen"] = {"orientation": "portrait",
                                  "screenWidth": SCREEN_W, "screenHeight": SCREEN_H}
    env.wizard._save_state()
    env.wizard.z_confirm_safe(tap_depth=3.0)
    env.wizard.park_confirm()
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] == "BOOTSTRAP_REQUIRED",
                     desc="BOOTSTRAP_REQUIRED")
    assert "seed" in (view["lastFailure"] or {}).get("message", "")
    assert env.daemon.tap_count == 1  # 仅参数验证点击；run 内零 TAP（不得先点击再学习）
    candidate = cb.load_candidate("arm-01", view["runId"], tmp_path)
    assert candidate["locks"]["cameraToScreen"]["state"] == "LOCKED"  # H 已锁
    assert candidate["locks"]["armAffine"]["state"] == "UNINITIALIZED"
    assert cb.load_bundle("arm-01", tmp_path)["status"] == "draft"  # 活动档案未被伪装


def test_auto_first_click_offset_self_corrects_and_converges(tmp_path):
    """中心点超差（种子平移偏差）：不再中止——自校正偏移后走完全部点并收敛提升
    （用户拍板 2026-09-03，覆盖规格 §7.3 的"超差即中止"）。
    2026-09-05 起默认路径由点击测试锚点预校正（见
    test_auto_tap_anchor_pre_corrects_stale_seed）；本测试清掉锚点，
    专测 First Click 现场自校正这条后备路径。"""
    env = auto_ready_env(tmp_path, seed_error_px=25.0)  # 25√2≈35px 平移偏差
    env.wizard.state["z"]["tapAnchor"] = {}
    env.wizard._save_state()
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = env.wizard.auto_view()["runId"]
    wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE" or v["state"] == "FAILED",
              desc="E 验收或失败")
    finish_e_acceptance(env, run_id)
    view = wait_auto(env, lambda v: v["state"] in ("PROMOTED", "FAILED"), desc="终态")
    assert view["state"] == "PROMOTED"
    candidate = cb.load_candidate("arm-01", run_id, tmp_path)
    corr = candidate.get("firstClickCorrection") or {}
    assert corr.get("offsetPx"), "中心点偏移应被记录并修正"
    # 重拟合后的映射吃掉了种子偏差：promoted c/f ≈ 真值（100/200）±1px
    promoted = cb.load_bundle("arm-01", tmp_path)
    m = promoted["xy"]["robotToScreen"]
    assert abs(m["c"] - 100.0) < 1.0 and abs(m["f"] - 200.0) < 1.0
    assert promoted["status"] == "ready"


def test_auto_real_hardware_runs_without_command(tmp_path):
    """真机模式无口令（用户拍板移除）：启动即过 First Click Gate，中心点照点不误。"""
    env = auto_ready_env(tmp_path, real=True)
    view = env.wizard.auto_start(defer_input_acceptance=False)
    assert "awaitingFirstClick" not in view  # 口令门禁已移除
    wait_auto(env, lambda v: v["phase"] not in (None, "PRECHECK", "SAFE_PARK",
                                                "PHASE0_CAMERA", "H_LOCKED",
                                                "BOOTSTRAP_CHECK", "FIRST_CLICK_GATE")
              or v["state"] in ("FAILED", "ABORT"), desc="越过首次点击")
    assert env.daemon.tap_count >= 1  # 启动即视为确认，中心点已执行
    env.wizard.auto_stop()
    wait_auto(env, lambda v: v["state"] in ("STOPPED", "FAILED", "ABORT"), desc="停止")


def test_auto_phase0_outlier_rejected_and_bundle_untouched(tmp_path):
    """9 点网格出现外点：Phase 0 整体拒绝（4/5 式静默剔除不允许）。"""
    env = auto_ready_env(tmp_path)

    bottom_left = cw.AUTO_PLAN_POINTS[9]  # 4x4 行主序第 10 点（v=0.38 行首，左缘）

    def hook():
        for s in env.ime.sessions.values():
            if s["armed"] and s["armed"].get("target"):
                t = s["armed"]["target"]
                x, y = cam(t["u"] * SCREEN_W, t["v"] * SCREEN_H)
                if abs(t["u"] - bottom_left[1]) < 1e-6 \
                        and abs(t["v"] - bottom_left[2]) < 1e-6:
                    x += 120  # 检测外点（120px：4x4 网格相邻点距~220px，不触发 min-step 误判）
                return {"items": [{"x": x, "y": y, "area": 900.0}],
                        "frameW": CAM_W, "frameH": CAM_H}
        return {"items": [], "frameW": CAM_W, "frameH": CAM_H}

    env.candidates_hook = hook
    bundle_hash = cb.profile_hash(cb.load_bundle("arm-01", tmp_path))
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] == "FAILED", desc="外点拒绝")
    failure = view["lastFailure"]
    assert failure["code"] == "camera_outlier_present"
    assert failure.get("routedTo") == "CAMERA_RECALIBRATION"
    assert view["locks"]["cameraToScreen"]["state"] == "UNINITIALIZED"
    assert env.daemon.tap_count == 1  # 仅参数验证点击，Phase 0 零 TAP
    assert cb.profile_hash(cb.load_bundle("arm-01", tmp_path)) == bundle_hash


@pytest.mark.parametrize("wizard_bytes", [
    b"{\"schemaVersion\": 1, \"stage\": \"Z_READY\", \"z\": {\"hoverZ\":",  # 截断 JSON
    b"",                                                                  # 空文件
    b"[]",                                                                # 非 dict
    b"{\"schemaVersion\": 99, \"stage\": \"Z_READY\"}",                   # 不支持的 schema
], ids=["truncated-json", "empty-file", "non-dict", "unsupported-schema"])
def test_p02_corrupt_wizard_state_never_overwrites_saved_bundle(tmp_path, wizard_bytes):
    """P02：向导文件损坏/空/旧 schema → 主档参数、停车位、验证记录保留；
    留证据副本；状态视图明示"工作会话不可恢复"。"""
    # 先落一份有真实参数的主档。
    env = Env(tmp_path)
    env.wizard.start("k40-01")
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.wizard.z_confirm_safe(tap_depth=3.0)
    env.wizard.park_confirm()
    bundle_before = cb.load_bundle("arm-01", tmp_path)
    assert bundle_before["safePark"]["parkX"] is not None
    assert bundle_before["z"]["zSafe"] is not None

    state_path = tmp_path / "devices" / "arm-01" / "calibration_wizard.json"
    state_path.write_bytes(wizard_bytes)

    # 重新加载向导：空状态 + 不可恢复标记；任何 _sync_bundle 都不写主档。
    env2 = Env(tmp_path)
    assert env2.wizard._state_unrecoverable is not None
    assert env2.wizard._state_unrecoverable["code"] == "wizard_state_corrupt"
    with env2.wizard._lock:
        env2.wizard._sync_bundle()
        env2.wizard._save_state()
    bundle_after = cb.load_bundle("arm-01", tmp_path)
    assert bundle_after["safePark"]["parkX"] == bundle_before["safePark"]["parkX"]
    assert bundle_after["safePark"]["confirmed"] is True
    assert bundle_after["safePark"]["daemonSession"] == bundle_before["safePark"]["daemonSession"]
    assert bundle_after["z"]["zSafe"] == bundle_before["z"]["zSafe"]
    assert bundle_after["status"] == bundle_before["status"]
    # 证据副本留存（损坏原文件先备份再允许重建工作会话）。
    corrupt_copies = list((tmp_path / "devices" / "arm-01").glob("calibration_wizard.corrupt-*.json"))
    assert corrupt_copies, "损坏的向导文件必须保留证据副本"
    # 状态视图明示。
    view = env2.wizard.state_view()
    assert view["stateHealth"]["code"] == "wizard_state_corrupt"
    assert "已保存校准资料保留" in view["stateHealth"]["message"]


def test_p01_blank_wizard_state_never_rebuilds_saved_bundle(tmp_path):
    """P01：主档已有保存参数 + 空白向导 → 不以空草稿重建主档。"""
    bundle = cb.new_bundle("arm-01")
    bundle.update({
        "status": "draft",
        "safePark": {"parkX": 11.0, "parkY": 12.0, "parkZ": 5.0,
                     "confirmed": True, "source": "test"},
        "z": {"hoverZ": 2.0, "validated": True},
        "xy": {"robotToScreen": {"a": 10.0, "b": 0.0, "c": 100.0,
                                 "d": 0.0, "e": 8.0, "f": 200.0}},
    })
    cb.save_bundle(bundle, tmp_path)

    env = Env(tmp_path)  # 向导文件不存在 → 空白状态
    assert env.wizard._state_unrecoverable is None
    with env.wizard._lock:
        result = env.wizard._sync_bundle()
    after = cb.load_bundle("arm-01", tmp_path)
    assert after["safePark"]["parkX"] == 11.0
    assert after["z"]["hoverZ"] == 2.0
    assert after["z"]["validated"] is True
    assert after["xy"]["robotToScreen"]["c"] == 100.0
    assert result["safePark"]["parkX"] == 11.0


def test_auto_old_ready_never_polluted_on_failure(tmp_path):
    """旧 READY 档案在 run 失败时保持原样（§21.4/§28）。"""
    env = AutoEnv(tmp_path)
    env.wizard.start("k40-01")
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    env.wizard.state["screen"] = {"orientation": "portrait",
                                  "screenWidth": SCREEN_W, "screenHeight": SCREEN_H}
    env.wizard._save_state()
    ready = cb.new_bundle("arm-01")
    ready["status"] = "ready"
    ready["xy"]["robotToScreen"] = {"a": 1.0, "b": 0.0, "c": 999.0,
                                    "d": 0.0, "e": 1.0, "f": 999.0}
    cb.save_bundle(ready, tmp_path)
    env.wizard.z_confirm_safe(tap_depth=3.0)
    env.wizard.park_confirm()
    bundle_hash = cb.profile_hash(cb.load_bundle("arm-01", tmp_path))
    env.candidates_hook = lambda: {"items": [], "frameW": CAM_W, "frameH": CAM_H}
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] == "FAILED", desc="相机失败")
    assert view["lastFailure"]["code"] == "phase0_target_failed"
    after = cb.load_bundle("arm-01", tmp_path)
    assert cb.profile_hash(after) == bundle_hash
    assert after["status"] == "ready"
    assert after["xy"]["robotToScreen"]["c"] == 999.0


def test_auto_h_not_updated_by_touch(tmp_path):
    """Actual Touch 永不改写 H：全程触点带 +2px 常值噪声，H 仍是 Phase 0 精确解。"""
    env = auto_ready_env(tmp_path)
    env.noise_active = True
    env.noise_fn = lambda x, y: (2.0, 0.0)
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE", desc="E 验收")
    finish_e_acceptance(env, run_id)
    wait_auto(env, lambda v: v["state"] == "PROMOTED", desc="提升")
    promoted = cb.load_bundle("arm-01", tmp_path)
    sx, sy = cb.apply_homography(
        promoted["camera"]["cameraToScreen"]["homography"], *cam(540, 1200))
    assert abs(sx - 540) < 0.5 and abs(sy - 1200) < 0.5  # H 未被触点噪声污染
    # Arm 侧则吸收了触点噪声（c≈102），证明学习的是 R_eff↔Touch 对
    assert abs(promoted["xy"]["robotToScreen"]["c"] - 102.0) < 1.0


def test_auto_mpos_mismatch_aborts_without_tap(tmp_path):
    """MPos 残差超差：不 TAP、不学习、ABORT（§19.2/补充 Stage2 MPos 超差）。"""
    env = auto_ready_env(tmp_path)
    env.daemon.xy_loss_ratio = 0.2  # 50mm 行程丢 ~10mm 残差
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] in ("FAILED", "ABORT"),
                     desc="MPos 超差中止")
    assert view["lastFailure"]["code"] in ("arm_motion_failed", "arm_motion_mpos_exceeded")
    assert view["lastFailure"]["classification"] == "arm_motion_error"
    assert env.daemon.tap_count == 1  # 仅参数验证点击：走位被拒不 TAP
    candidate = cb.load_candidate("arm-01", view["runId"], tmp_path)
    rejected = [s for s in candidate["samples"] if not s["eligible"]]
    assert rejected and rejected[-1]["rejectedReason"]


def test_auto_position_tolerance_config_rejects_sample(tmp_path):
    """position_tolerance_mm 收紧到 0.5mm：1mm 残差样本不进入拟合（补充 §4）。"""
    env = auto_ready_env(tmp_path, auto_config={"position_tolerance_mm": 0.5})
    env.daemon.xy_loss_ratio = 0.02  # ~1mm 残差：低于 _goto_robot 的 3mm，但高于配置
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] in ("FAILED", "ABORT"),
                     desc="配置容差拦截")
    assert view["lastFailure"]["code"] == "arm_motion_mpos_exceeded"
    assert env.daemon.tap_count == 1  # 仅参数验证点击：MPos 超差不 TAP
    candidate = cb.load_candidate("arm-01", view["runId"], tmp_path)
    assert all(not s["eligible"] for s in candidate["samples"])


def test_auto_touch_timeout_fails_after_retries(tmp_path):
    """触点超时（手机没轮询到布防）：First Click 失败归类 touch_feedback_error。

    参数验证点击需要正常回报（确认时手机在线），确认完成后再断开手机轮询。"""
    env = auto_ready_env(tmp_path)
    env.ime.phone_lag = 5.0  # 确认之后再让手机看不到布防 → run 内触点超时
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] == "FAILED", desc="触点超时")
    assert view["lastFailure"]["classification"] == "touch_feedback_error"
    candidate = cb.load_candidate("arm-01", view["runId"], tmp_path)
    assert any(s["classification"] == "touch_feedback_error" for s in candidate["samples"])


def test_auto_touch_sequence_mismatch_invalidates(tmp_path):
    """触点 sequence 错配：向导与 run 同时失效（§27 sequence/sampleId 错配）。"""
    env = auto_ready_env(tmp_path)
    env.mismatch_mode = True
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] == "INVALIDATED", desc="错配失效")
    assert view["lastFailure"]["code"] == "wizard_rejected"
    assert env.wizard.state["stage"] == cw.STAGE_INVALIDATED
    assert env.wizard.state["invalidReason"]["code"] == "touch_report_mismatch"


def test_auto_hard_fail_aborts_and_parks(tmp_path):
    """常值噪声会被拟合并完全补偿（坐标系平移语义，正确行为）；
    真正的 HARD 拦截发生在噪声"变化"时：Phase 1 期间 +150px 被学习吸收，
    Phase 2 开始后翻转成 −150px → 300px 误差触发安全停止并回 SAFE PARK。"""
    env = auto_ready_env(tmp_path)
    sign = {"v": 1.0}
    env.noise_active = True
    env.noise_fn = lambda x, y: (sign["v"] * 150.0, 0.0)
    env.wizard.auto_start(defer_input_acceptance=False)
    # Phase 1 完成进入 Phase 2 后翻转噪声方向
    wait_auto(env, lambda v: v["phase"] == "PHASE2_VALIDATE" or v["state"] in ("FAILED", "ABORT"),
              desc="进入 Phase 2")
    sign["v"] = -1.0
    view = wait_auto(env, lambda v: v["state"] == "ABORT", desc="安全边界")
    assert view["lastFailure"]["code"] == "hard_fail"
    candidate = cb.load_candidate("arm-01", view["runId"], tmp_path)
    assert candidate["lastPark"] is not None  # 尽力停靠留痕
    assert abs(env.daemon.x) < 1e-6 and abs(env.daemon.y) < 1e-6  # 回到 (0,0) 停靠位
    assert candidate["arm"] is not None  # 学习发生过（吸收了 +150 常值偏移）
    assert env.daemon.tap_count >= 11  # 验证点击 + 中心点 + 采样点 + Phase 2 首点


def test_auto_cancel_mid_run(tmp_path):
    """操作员 stop：runner 安全停止（STOPPED），candidate 保留（§26/补充 cancel）。

    不配置工作区范围（用户拍板：无限位、计算门禁兜底）也能启动真机 run。"""
    env = auto_ready_env(tmp_path, real=True)
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    wait_auto(env, lambda v: v["phase"] in ("PHASE0_CAMERA", "FIRST_CLICK_GATE",
                                            "PHASE1_ARM"), desc="run 进行中")
    view = env.wizard.auto_stop()
    assert view["state"] in ("STOPPED", "IDLE") or view["lastRun"]["state"] == "STOPPED"
    assert env.wizard.auto_view()["activeRunId"] is None
    assert cb.load_candidate("arm-01", run_id, tmp_path)["state"] == "STOPPED"


def test_auto_estop_blocks_run(tmp_path):
    """急停锁定：run 立即 ABORT，零运动（补充 Stage2 emergency stop）。"""
    env = auto_ready_env(tmp_path)
    cmds_before = len(env.daemon.cmds)  # 含参数验证点击
    env.daemon.estop_locked = True
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] == "ABORT", desc="急停")
    assert view["lastFailure"]["code"] == "emergency_stop"
    assert not any(c.startswith(("XY", "Z", "TAP")) for c in env.daemon.cmds[cmds_before:])


def test_auto_daemon_session_change_invalidates(tmp_path):
    """daemon 会话变化：candidate 失效（§27 daemon session changed）。"""
    env = auto_ready_env(tmp_path)
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    wait_auto(env, lambda v: v["phase"] == "PHASE0_CAMERA", desc="Phase 0")
    env.daemon.session = "sess-B"  # 现场重启机械臂
    view = wait_auto(env, lambda v: v["state"] == "INVALIDATED", desc="会话失效")
    assert view["lastFailure"]["code"] == "daemon_session_changed"
    assert cb.load_candidate("arm-01", run_id, tmp_path)["state"] == "INVALIDATED"


def test_auto_service_restart_invalidates_run(tmp_path):
    """服务重启：磁盘上的活跃 run 作废（§31 服务重启后的 run 作废）。"""
    env = auto_ready_env(tmp_path)
    stall = {"armed": False}
    real_candidates = env.real_candidates

    def stalling():
        if not stall["armed"]:
            stall["armed"] = True
            _time.sleep(3.0)  # 检测停 3 秒：制造一个活跃中的 run 供重启作废
        return real_candidates()

    env.candidates_hook = stalling
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    while not stall["armed"]:
        _time.sleep(0.02)
    # 模拟服务重启：同一 data_root 构造新向导实例
    wizard2 = cw.CalibrationWizard(
        device="arm-01", daemon=env.daemon, ime=env.ime, data_root=tmp_path,
        camera_fetch=lambda: (b"jpg", 1),
        binding_facts=lambda: {
            "cameraId": "cam-01", "cameraDeviceUniqueId": "uvc-test-01",
            "bindingRevision": 1, "daemonAddr": "127.0.0.1:8795",
            "frameWidth": CAM_W, "frameHeight": CAM_H})
    candidate = cb.load_candidate("arm-01", run_id, tmp_path)
    assert candidate["state"] == "ABORT"
    assert candidate["lastFailure"]["code"] == "service_restart"
    assert wizard2.auto_view()["activeRunId"] is None


def test_auto_phase2_soft_fail_auto_iterates_to_convergence(tmp_path):
    """Phase 2 单点 SOFT_FAIL：不再暂停等操作员——自动重采该点+全量重拟合+复验，
    在迭代预算内收敛提升（用户拍板 2026-09-03：校准应自动计算而非报错/暂停）。

    注入：第一次 Phase 2 走到 calibration-8 的 3 次尝试（第 26-28 次点击）触点
    恒偏 +20px；迭代轮重采与复验时窗口已关闭 → 收敛。
    点击序号：1=zSafe 验证点击，2=中心点，3-18=Phase 1 十六点，
    19-25=Phase 2 前七点，26-28=calibration-8 三次尝试。
    """
    env = auto_ready_env(tmp_path)
    env.noise_active = True

    def windowed_noise(x, y):
        if 26 <= env.daemon.tap_count <= 28:
            return (20.0, 0.0)  # 6 < 20 ≤ 30 → SOFT_FAIL
        return (0.0, 0.0)

    env.noise_fn = windowed_noise
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE" or v["state"] == "FAILED",
              desc="E 验收或失败")
    finish_e_acceptance(env, run_id)
    view = wait_auto(env, lambda v: v["state"] in ("PROMOTED", "FAILED"), desc="终态")
    assert view["state"] == "PROMOTED"
    candidate = cb.load_candidate("arm-01", run_id, tmp_path)
    # 自动迭代发生：iteration_resample 事件针对 calibration-8，历史 ≥2 轮
    events = [e for e in candidate["events"] if e["event"] == "iteration_resample"]
    assert events and events[0]["detail"]["points"] == ["calibration-8"]
    assert len(candidate.get("history") or []) >= 2
    assert "calibration-8" in (candidate.get("phase2SoftFails") or {})
    # 复验通过：phase2 十六点全部记录且 promoted ready
    assert len(candidate.get("phase2") or {}) == 16
    assert cb.load_bundle("arm-01", tmp_path)["status"] == "ready"


def test_auto_arm_regression_rollback_keeps_previous_model(tmp_path):
    """新拟合 RMS 比上一稳定 candidate 明显变差：rollback 且保留旧模型（§13.3/§15.3）。"""
    env = AutoEnv(tmp_path)
    run = {
        "runId": "run-regression", "deviceId": "arm-01", "iteration": 2,
        "config": dict(cw.AUTO_CONFIG_DEFAULTS),
        "screen": {"screenWidth": SCREEN_W, "screenHeight": SCREEN_H},
        "pointPlan": [{"id": f"calibration-{i}", "u": u, "v": v}
                      for i, (u, v) in enumerate([(0.15, 0.15), (0.5, 0.15), (0.85, 0.15)])],
        "samples": [], "armHistory": [],
        "locks": {"cameraToScreen": {"state": "LOCKED", "version": 1},
                  "armAffine": {"state": "LOCKED", "version": 1}},
        "arm": {"robotToScreen": env.seed_robot_to_screen(),
                "residualRmsPx": 0.2, "maxResidualPx": 0.3, "sampleCount": 5,
                "iteration": 1},
    }
    # 5 个点 + 不规则噪声：规则图案（常值/交替/矩形对称）会被仿射参数精确吸收，
    # 只有非模式化噪声才留残差（rms≈1.3px：过质量门 3px，但比旧 0.2px 明显变差）
    base_positions = [(10.0, 20.0), (40.0, 20.0), (10.0, 60.0), (40.0, 60.0), (25.0, 40.0)]
    offsets = [(4.0, 0.0), (-2.6, 0.7), (2.0, -0.9), (-1.3, 1.1), (3.1, -0.4)]
    for i, (rx, ry) in enumerate(base_positions):
        sx, sy = truth(rx, ry)
        run["samples"].append({
            "phase": "PHASE1_ARM", "eligible": True, "targetId": f"calibration-{i}",
            "effectiveRobotXY": [rx, ry],
            "actualTouch": [sx + offsets[i][0], sy + offsets[i][1]],
        })
    with pytest.raises(cw.AutoRunError, match="变差"):
        env.wizard._auto_update_arm_candidate(run)
    assert run["arm"]["residualRmsPx"] == 0.2  # 旧模型未被覆盖（rollback）


def test_auto_profile_change_blocks_promote(tmp_path):
    """run 期间活动档案被改动：promote 拒绝（补充 §7 profile fingerprint mismatch）。"""
    env = auto_ready_env(tmp_path)
    stall = {"armed": False}
    real_candidates = env.real_candidates

    def stalling():
        if not stall["armed"]:
            stall["armed"] = True
            _time.sleep(3.0)  # 停 3 秒：篡改窗口
        return real_candidates()

    env.candidates_hook = stalling
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    while not stall["armed"]:
        _time.sleep(0.02)
    # run 进行中有人改动了活动档案的几何基础（映射矩阵）→ promote 拒绝
    tampered = cb.load_bundle("arm-01", tmp_path)
    tampered.setdefault("xy", {})["robotToScreen"] = {
        "a": 1.0, "b": 0.0, "c": 999.0, "d": 0.0, "e": 1.0, "f": 999.0}
    cb.save_bundle(tampered, tmp_path)
    wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE" or v["state"] == "FAILED",
              desc="推进或被拒")
    if env.wizard.auto_view().get("phase") == "E_ACCEPTANCE":
        finish_e_acceptance(env, run_id)
    view = wait_auto(env, lambda v: v["state"] in ("FAILED", "PROMOTED"),
                     desc="promote 判定")
    assert view["state"] == "FAILED"
    assert view["lastFailure"]["code"] == "promote_base_changed"
    assert cb.load_candidate("arm-01", run_id, tmp_path)["state"] == "FAILED"
    assert cb.load_bundle("arm-01", tmp_path)["status"] == "draft"


def test_auto_target_marking_mid_run_does_not_block_promote(tmp_path):
    """run 期间经合法入口标记目标（如输入法删除键）写档案：几何基础未变，
    promote 放行（2026-09-03 现场：E 验收标删除键曾被全字段哈希误杀）。"""
    env = auto_ready_env(tmp_path, register_delete_target=False)
    stall = {"armed": False}
    real_candidates = env.real_candidates

    def stalling():
        if not stall["armed"]:
            stall["armed"] = True
            _time.sleep(3.0)
        return real_candidates()

    env.candidates_hook = stalling
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    while not stall["armed"]:
        _time.sleep(0.02)
    marked = cb.load_bundle("arm-01", tmp_path)
    marked.setdefault("targets", {})["ime_delete_key"] = {
        "nx": 0.1, "ny": 0.9, "targetMode": "hover_only"}
    cb.save_bundle(marked, tmp_path)
    wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE" or v["state"] == "FAILED",
              desc="推进或被拒")
    if env.wizard.auto_view().get("phase") == "E_ACCEPTANCE":
        finish_e_acceptance(env, run_id)
    view = wait_auto(env, lambda v: v["state"] in ("FAILED", "PROMOTED"),
                     desc="promote 判定")
    assert view["state"] == "PROMOTED"
    promoted = cb.load_bundle("arm-01", tmp_path)
    assert promoted["status"] == "ready"
    assert "ime_delete_key" in promoted.get("targets", {})


# ---------- 全局连带审查回归（2026-09-03 第二轮：并发/残留布防/输入防护） ----------

def test_auto_manual_motion_blocked_while_run_active(tmp_path):
    """run 活动期间：手动运动/会话/收尾动作一律拒绝（防两路运动与布防错配）。"""
    env = auto_ready_env(tmp_path, real=True)
    stall = {"armed": False}
    real_candidates = env.real_candidates

    def stalling():
        if not stall["armed"]:
            stall["armed"] = True
            _time.sleep(3.0)  # 检测停 3 秒：持有一个活跃 run 做并发拒绝断言
        return real_candidates()

    env.candidates_hook = stalling
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = env.wizard.auto_view()["runId"]
    while not stall["armed"]:
        _time.sleep(0.02)
    for call in (
        lambda: env.wizard.z_tap_test("tap", 3.0, 1500.0, 0),
        lambda: env.wizard.start("k40-01"),
        lambda: env.wizard.xy_preview(0),
        lambda: env.wizard.finish(),
        lambda: env.wizard.auto_acceptance_execute(run_id, "ime_input_tap"),
    ):
        with pytest.raises(WizardError):
            call()
    # 拒绝不得污染 run：run 仍挂载且未产生点击（验证点击发生在 run 之前）
    assert env.wizard.auto_view()["activeRunId"] == run_id
    assert env.daemon.tap_count == 1
    env.wizard.auto_stop()
    wait_auto(env, lambda v: v["state"] in ("STOPPED", "FAILED", "ABORT"), desc="停止")


def test_auto_stale_arming_disarmed_on_detection_failure(tmp_path):
    """点击点检测失败 → 布防必须拆除，重试不得被 calibration_armed 卡死。

    中心点(0.5,0.5) 前两次检测（Phase 0 的 calibration-5 + First Click 第一次）
    返回空 → 两处都靠重采样恢复；若残留布防未拆除，下一次 arm 会 409。
    """
    env = auto_ready_env(tmp_path)
    fails_left = {"n": 2}

    def flaky():
        for s in env.ime.sessions.values():
            if s["armed"] and s["armed"].get("target"):
                t = s["armed"]["target"]
                if abs(t["u"] - 0.5) < 1e-9 and abs(t["v"] - 0.5) < 1e-9 \
                        and fails_left["n"] > 0:
                    fails_left["n"] -= 1
                    return {"items": [], "frameW": CAM_W, "frameH": CAM_H}
                x, y = cam(t["u"] * SCREEN_W, t["v"] * SCREEN_H)
                return {"items": [{"x": x, "y": y, "area": 900.0}],
                        "frameW": CAM_W, "frameH": CAM_H}
        return {"items": [], "frameW": CAM_W, "frameH": CAM_H}

    env.candidates_hook = flaky
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = env.wizard.auto_view()["runId"]
    # First Click 已恢复并通过 → 进入 Phase 1 即证明残留布防没有卡死重试
    wait_auto(env, lambda v: v["phase"] in ("PHASE1_ARM", "ARM_LOCKED",
                                            "PHASE2_VALIDATE"), desc="越过首次点击")
    assert fails_left["n"] == 0
    env.wizard.auto_stop()
    wait_auto(env, lambda v: v["state"] in ("STOPPED", "FAILED", "ABORT"), desc="停止")


def test_auto_start_rejects_non_sixteen_point_plan(tmp_path):
    env = auto_ready_env(tmp_path)
    with pytest.raises(WizardError, match="16 点"):
        env.wizard.auto_start(defer_input_acceptance=False, point_plan=[{"id": f"p{i}", "u": 0.1 + 0.08 * i,
                                           "v": 0.2 + 0.07 * i} for i in range(6)])


def test_z_confirm_safe_rejects_value_far_from_current_mpos(tmp_path):
    env = auto_ready_env(tmp_path)
    with pytest.raises(WizardError, match="相差超过"):
        env.wizard.z_confirm_safe(z_safe=50.0, tap_depth=3.0)  # 当前 Z=5
    ok = env.wizard.z_confirm_safe(z_safe=6.0, tap_depth=3.0)  # 差 1mm 允许
    assert ok["zSafe"] == 6.0


def test_manual_restart_resets_stale_auto_locks(tmp_path):
    """手动重开校准：旧模型和历史保留，但新会话不能直接继承执行资格。"""
    env = auto_ready_env(tmp_path)
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = env.wizard.auto_view()["runId"]
    wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE", desc="E 验收")
    finish_e_acceptance(env, run_id)
    wait_auto(env, lambda v: v["state"] == "PROMOTED", desc="提升")
    promoted = cb.load_bundle("arm-01", tmp_path)
    assert promoted["modelLocks"]["armAffine"]["state"] == "LOCKED"
    # 从 READY 重开手动校准
    env.wizard.start("k40-01")
    restarted = cb.load_bundle("arm-01", tmp_path)
    assert restarted["status"] == "draft"
    assert restarted["modelLocks"]["armAffine"]["state"] == "LOCKED"
    assert restarted["autoCalibration"]["state"] == "PROMOTED"
    assert restarted["validation"]["passed"] is True
    assert restarted["sessionRebind"]["required"] is True
    old_candidate = cb.load_candidate("arm-01", run_id, tmp_path)
    assert old_candidate["executionEligibility"]["eligible"] is False


def test_auto_phase0_rejects_static_green_object(tmp_path):
    """静态绿色物（手机熄屏时被误检的贴纸等）：9 点同位置 → 第二个点即拒绝。

    现场 2026-09-03：手机熄屏后检测器 9 次锁在青蓝色"9"号贴纸上，全点同一
    相机坐标，单应折叠才暴露；该防线把这类问题提前到第 2 点并给出人话原因。
    """
    env = auto_ready_env(tmp_path)

    def static_hook():
        # 位于屏幕四边形内但位置恒定（贴纸在画面内的情况）
        return {"items": [{"x": 400.0, "y": 300.0, "area": 1050.0}],
                "frameW": CAM_W, "frameH": CAM_H}

    env.candidates_hook = static_hook
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] == "FAILED", desc="静态绿物拒绝")
    assert view["lastFailure"]["code"] == "phase0_target_not_moving"
    assert "手机" in view["lastFailure"]["message"]
    assert env.daemon.tap_count == 1  # 仅参数验证点击


def test_auto_phase0_rejects_target_outside_screen_roi(tmp_path):
    """检测中心在屏幕四边形之外（§8.2 ROI 门禁）：第一个点即拒绝。"""
    env = auto_ready_env(tmp_path)
    real = env.real_candidates

    def roi_hook():
        # 第一个标定点（0.15,0.15）的三帧全部落在屏幕四边形之外
        for s in env.ime.sessions.values():
            if s["armed"] and s["armed"].get("target"):
                t = s["armed"]["target"]
                if t["u"] < 0.2 and t["v"] < 0.2:
                    return {"items": [{"x": 5.0, "y": 2.0, "area": 900.0}],
                            "frameW": CAM_W, "frameH": CAM_H}
        return real()

    env.candidates_hook = roi_hook
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] == "FAILED", desc="ROI 拒绝")
    assert view["lastFailure"]["code"] == "phase0_target_outside_screen"
    assert env.daemon.tap_count == 1  # 仅参数验证点击


def test_z_confirm_safe_validates_with_real_tap(tmp_path):
    """确认点击参数 = 原地真实点击一次 + 收到触点回报（用户拍板 2026-09-03）。"""
    env = AutoEnv(tmp_path)
    env.wizard.start("k40-01")
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    result = env.wizard.z_confirm_safe(tap_depth=3.0)
    assert result["validated"] is True
    assert any(c.startswith("TAP") for c in env.daemon.cmds)
    auto_safe = env.wizard.state["z"]["autoSafe"]
    assert auto_safe["validated"] and auto_safe["report"]["screenHeight"] == SCREEN_H
    # 触点回报顺带建立了屏幕基线（自动校准不再依赖手动 B 阶段）
    assert env.wizard.state["screen"]["screenWidth"] == SCREEN_W


def test_z_confirm_safe_without_report_blocks_auto_start(tmp_path):
    """验证点击无触点回报：确认失败，参数标记未验证，auto_start 拒绝。"""
    env = AutoEnv(tmp_path, phone_lag=5.0)  # 手机永远看不到布防 → 超时
    env.wizard.start("k40-01")
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    with pytest.raises(WizardError, match="未收到触点回报"):
        env.wizard.z_confirm_safe(tap_depth=3.0)
    auto_safe = env.wizard.state["z"]["autoSafe"]
    assert auto_safe["validated"] is False and auto_safe["validationError"]
    env.wizard.state["screen"] = {"orientation": "portrait",
                                  "screenWidth": SCREEN_W, "screenHeight": SCREEN_H}
    env.wizard._save_state()
    env.wizard.park_confirm()
    with pytest.raises(WizardError, match="尚未通过验证"):
        env.wizard.auto_start(defer_input_acceptance=False)
    # 档案不得写入未验证的 zSafe
    bundle = cb.load_bundle("arm-01", tmp_path)
    assert bundle["z"].get("zSafe") is None


def test_auto_phase0_rejects_occluded_target(tmp_path):
    """标靶被停靠的机械臂遮挡：检测面积骤降 → 采点时即拒绝并指出停靠位问题。"""
    env = auto_ready_env(tmp_path)
    real = env.real_candidates

    def occluded_bottom():
        payload = real()
        for s in env.ime.sessions.values():
            if s["armed"] and s["armed"].get("target"):
                t = s["armed"]["target"]
                if t["v"] > 0.7:  # 底行标靶只剩一条缝（面积 ~5% 正常值）
                    payload = {"items": [dict(payload["items"][0], area=380.0)],
                               "frameW": CAM_W, "frameH": CAM_H}
        return payload

    env.candidates_hook = occluded_bottom
    env.wizard.auto_start(defer_input_acceptance=False)
    view = wait_auto(env, lambda v: v["state"] == "FAILED", desc="遮挡拒绝")
    assert view["lastFailure"]["code"] == "phase0_target_occluded"
    assert "遮挡" in view["lastFailure"]["message"]
    assert "SAFE PARK" in view["lastFailure"]["message"]
    assert env.daemon.tap_count == 1  # 仅参数验证点击


def test_auto_phase0_tolerates_stale_stream_frames(tmp_path):
    """推流滞后（布防后前几帧还显示上一个标靶）：检测必须等画面真的变化。

    实测 2026-09-03：取流 ~8fps、布防→画面更新 0.2-0.9s；若固定 sleep 赌延时，
    滞后帧会把上一点的相机位置配给当前点。stale_center 机制要求新检测与上一
    位置错开 ≥30px 才计入确认序列。
    """
    env = auto_ready_env(tmp_path)
    state = {"target": None, "stale_left": 0, "prev": None, "last_real": None}

    def items_for(t):
        x, y = cam(t["u"] * SCREEN_W, t["v"] * SCREEN_H)
        return {"x": x, "y": y, "area": 900.0}

    def laggy():
        for s in env.ime.sessions.values():
            if s["armed"] and s["armed"].get("target"):
                t = s["armed"]["target"]
                key = (t["u"], t["v"])
                if state["target"] != key:
                    state["target"] = key
                    state["stale_left"] = 5  # 新布防后前 5 次取流仍是旧画面
                    state["prev"] = state["last_real"]
                if state["stale_left"] > 0 and state["prev"] is not None:
                    state["stale_left"] -= 1
                    return {"items": [state["prev"]], "frameW": CAM_W, "frameH": CAM_H}
                state["last_real"] = items_for(t)
                return {"items": [state["last_real"]],
                        "frameW": CAM_W, "frameH": CAM_H}
        return {"items": [], "frameW": CAM_W, "frameH": CAM_H}

    env.candidates_hook = laggy
    env.wizard.auto_start(defer_input_acceptance=False)
    # 滞后帧被忽略、等画面变化后采点：H 应正常锁定并推进到首次点击阶段
    wait_auto(env, lambda v: v["phase"] in ("H_LOCKED", "BOOTSTRAP_CHECK",
                                            "FIRST_CLICK_GATE", "PHASE1_ARM"),
              desc="越过 Phase 0")
    run_id = env.wizard.auto_view()["runId"]
    candidate = cb.load_candidate("arm-01", run_id, tmp_path)
    corr = candidate["h"]["correspondences"]
    centers = [(c["cameraX"], c["cameraY"]) for c in corr]
    # 9 个相机位置两两错开（无滞后污染）
    for i in range(len(centers)):
        for j in range(i + 1, len(centers)):
            dist = ((centers[i][0]-centers[j][0])**2 + (centers[i][1]-centers[j][1])**2)**0.5
            assert dist > 30, f"点{i+1}与点{j+1}相机位置过近（滞后污染）"
    env.wizard.auto_stop()
    wait_auto(env, lambda v: v["state"] in ("STOPPED", "FAILED", "ABORT"), desc="停止")


def test_auto_phase2_rejects_shifted_detection_and_recovers_from_park(tmp_path):
    """探头阴影/反光污染检测（稳定偏移 30-40px、jitter=0）：期望位置门拒绝，
    机械臂先回画面外停靠位再重检（干扰源物理消除），恢复后正常走完。

    现场 2026-09-03（run-0caae13b9fc1）：Phase 2 c3 被探头悬停 c2 时的玻璃
    反光切成半个绿框，质心稳定偏 34px，"稳定≠正确"，三链路自洽地走错 80px。
    """
    env = auto_ready_env(tmp_path)
    real = env.real_candidates

    # c3 的检测在"机械臂仍停在 c2 附近"时被阴影/反光稳定偏移 +34px（面积减半）；
    # 回停靠位（画面外）后恢复正常 —— 与现场物理一致
    # 前一点（4x4 网格 calibration-3 @0.62,0.15）机械坐标 ≈ (56.96, 18.5)
    def shadow_hook():
        payload = real()
        items = payload.get("items") or []
        if items:
            for s in env.ime.sessions.values():
                if s["armed"] and s["armed"].get("target"):
                    t = s["armed"]["target"]
                    if abs(t["u"] - 0.85) < 1e-9 and abs(t["v"] - 0.15) < 1e-9:
                        near_c2 = abs(env.daemon.x - 56.96) < 8 and abs(env.daemon.y - 18.5) < 8
                        if near_c2:
                            items = [dict(items[0], x=items[0]["x"] + 28,
                                          y=items[0]["y"] + 19, area=4000.0)]
        return {"items": items, "frameW": CAM_W, "frameH": CAM_H}

    env.candidates_hook = shadow_hook
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE" or v["state"] == "FAILED",
              desc="E 验收或失败")
    finish_e_acceptance(env, run_id)
    view = wait_auto(env, lambda v: v["state"] in ("PROMOTED", "FAILED"), desc="终态")
    assert view["state"] == "PROMOTED"
    candidate = cb.load_candidate("arm-01", run_id, tmp_path)
    retries = [e for e in candidate["events"] if e["event"] == "detect_retry_from_park"]
    assert retries and retries[0]["detail"]["targetId"] == "calibration-4"
    assert ("偏离期望位置" in retries[0]["detail"]["reason"]
            or "偏差" in retries[0]["detail"]["reason"]
            or "并入" in retries[0]["detail"]["reason"])  # 面积上限门同样触发停靠重检  # 期望位置门/面积门任一命中


def test_acceptance_ime_report_timeout_cancels_task(env):
    """输入法不回报 displayed：验收明确失败并自动取消任务，
    防止残留任务让下一次验收显示上一次文本（队列卡死）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    _run_xy_stage(env)
    env.wizard.edge_capture()
    env.wizard.camera_fit()
    env.wizard.state["stage"] = cw.STAGE_DEBUG_ACCEPTANCE

    env.ime.ime_stuck_pending = True
    with pytest.raises(WizardError, match="已取消"):
        env.wizard.acceptance_execute("debug", "ime_input_tap", nx=0.5, ny=0.5)
    assert len(env.ime.cancelled_tasks) == 1
    assert env.ime.tasks_created  # 任务确实下发过
    record = env.wizard.state["acceptance"]["debug"]["ime_input_tap"]
    assert record["imeTaskCancelled"]["taskId"] == env.ime.tasks_created[0]["taskId"]


def test_acceptance_ocr_ime_region_filter(tmp_path):
    """OCR 定位限定输入法区域（屏幕高度≥51%，经 H 投影判定）：
    屏幕上部的同文本回显（聊天记录/输入框）即使置信度更高也必须排除；
    区域内找不到不回退上部。无标定信息时不过滤（非验收调用方兜底）。"""
    env = AutoEnv(tmp_path, ocr_words=[
        FakeOcrWord("统一校准测试", 300, 100, confidence=0.99),  # 屏幕上部"回显"
        FakeOcrWord("统一校准测试", 300, 400, confidence=0.80),  # 输入法区域
    ])
    # 有 H+屏幕尺寸：区域过滤生效，命中下部实例
    target = env.wizard._locate_text_by_ocr(
        "统一校准测试", timeout=1.0,
        homography={"homography": [1, 0, 0, 0, 1, 0, 0, 0, 1]},
        screen_size=(CAM_W, CAM_H))
    assert target["cameraY"] > CAM_H * 0.51
    assert target["regionFiltered"] is True

    # 只有上部实例：不回退，明确报错
    env2 = AutoEnv(tmp_path, ocr_words=[
        FakeOcrWord("统一校准测试", 300, 100, confidence=0.99),
    ])
    with pytest.raises(WizardError, match="输入法区域"):
        env2.wizard._locate_text_by_ocr(
            "统一校准测试", timeout=1.0,
            homography={"homography": [1, 0, 0, 0, 1, 0, 0, 0, 1]},
            screen_size=(CAM_W, CAM_H))

    # 无标定信息：不过滤（旧行为兜底，按置信度取词）
    target = env2.wizard._locate_text_by_ocr("统一校准测试", timeout=1.0)
    assert target["regionFiltered"] is False
    assert target["confidence"] == pytest.approx(0.99)


def test_acceptance_ocr_truncated_prefix_match(tmp_path):
    """输入法对过长候选省略显示（如「统一校准…」）：去省略号后前缀命中，
    且区域过滤仍生效。"""
    env = AutoEnv(tmp_path, ocr_words=[
        FakeOcrWord("统一校…", 300, 400, confidence=0.88),   # 截断候选
        FakeOcrWord("无关内容", 300, 420, confidence=0.95),
    ])
    target = env.wizard._locate_text_by_ocr(
        "统一校准测试", timeout=1.0,
        homography={"homography": [1, 0, 0, 0, 1, 0, 0, 0, 1]},
        screen_size=(CAM_W, CAM_H))
    assert target["cameraY"] == 400 + 12  # 命中截断候选而非无关词
    assert target["matchedChars"] == 3    # 统一校 = min(3, 期望字数)


def test_auto_arm_fit_upgrades_to_quadratic(env):
    """方案 A：16 点样本带二次畸变（倾斜触点滑移）时，Arm 拟合自动升二次模型，
    网格间隙点残差被吃掉（仿射泄漏 ~5px → 二次 <0.5px）。"""
    # 真值 = 仿射 + 二次畸变（模拟倾斜滑移场）
    def truth_q(rx, ry):
        return (10.0 * rx + 0.5 * ry + 100.0 + 0.02 * rx * rx - 0.01 * rx * ry,
                0.3 * rx + 8.0 * ry + 50.0 + 0.015 * ry * ry)

    samples = []
    for row, v in enumerate((0.15, 0.38, 0.62, 0.85)):
        for col, u in enumerate((0.15, 0.38, 0.62, 0.85)):
            rx, ry = u * 60.0, v * 40.0
            sx, sy = truth_q(rx, ry)
            samples.append({
                "sampleId": row * 4 + col + 1, "phase": "PHASE1_ARM",
                "eligible": True, "targetId": f"calibration-{row * 4 + col + 1}",
                "effectiveRobotXY": [rx, ry], "actualTouch": [sx, sy],
            })
    run = {"config": dict(cw.AUTO_CONFIG_DEFAULTS), "samples": samples,
           "pointPlan": [{"id": f"calibration-{i + 1}", "u": 0.5, "v": 0.5}
                         for i in range(16)],
           "arm": None, "iteration": 1, "armHistory": [],
           "locks": {"armAffine": {}, "cameraToScreen": {}}}
    env.wizard._auto_update_arm_candidate(run)
    model = run["arm"]["robotToScreen"]
    assert "qx2" in model and "py2" in model, "16 点带二次畸变必须升级二次模型"
    assert run["arm"]["residualRmsPx"] < 0.5
    # 网格间隙点（旧泄漏热点 0.7,0.3）经反解应精准落回
    sx, sy = truth_q(0.7 * 60.0, 0.3 * 40.0)
    rx, ry = cb.robot_from_screen(model, sx, sy)
    assert abs(rx - 0.7 * 60.0) < 0.05 and abs(ry - 0.3 * 40.0) < 0.05


def test_auto_promote_refreshes_stale_edge_model(tmp_path):
    """2026-09-04 修复：基线档案带有旧屏幕边缘（重夹手机前）时，promote 必须
    用本轮 run 实测边缘覆盖——否则门禁 screen_edge_changed 大偏差误报。"""
    env = auto_ready_env(tmp_path)
    stale = [[10.0, 10.0], [1000.0, 10.0], [1000.0, 500.0], [10.0, 500.0]]
    base = cb.load_bundle("arm-01", tmp_path)
    base["screen"] = {**(base.get("screen") or {}),
                      "edgeModel": {"corners": stale, "tolerancePx": 8,
                                    "safeInsetPx": 24, "capturedAt": "old"}}
    cb.save_bundle(base, tmp_path)

    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE", desc="E")
    finish_e_acceptance(env, run_id)
    wait_auto(env, lambda v: v["state"] == "PROMOTED", desc="提升")

    promoted = cb.load_bundle("arm-01", tmp_path)
    corners = promoted["screen"]["edgeModel"]["corners"]
    assert corners != stale, "promote 不得保留重夹前的旧边缘"
    run_edge = (json.loads((Path(env.wizard._path).parent / "calibration_candidates"
                            / f"{run_id}.json").read_text(encoding="utf-8")).get("edge") or {})
    assert corners == run_edge.get("corners"), "档案边缘应等于本轮实测"
    assert env.wizard.state["geometry"]["edge"]["corners"] == corners, "state 同步"


def test_auto_promote_with_debug_only_wechat_deferred(tmp_path):
    """调试台输入法两项 ✓ 即可提升 ready；微信测试会话不再是门禁。"""
    env = auto_ready_env(tmp_path)
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = _first_active_run_id(env)
    wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE", desc="E 验收")

    # 只记录调试两项 ✓（微信四项不记）
    finish_e_acceptance(env, run_id)
    view = wait_auto(env, lambda v: v["state"] == "PROMOTED", desc="仅调试项提升")
    bundle = cb.load_bundle("arm-01", tmp_path)
    assert bundle["status"] == "ready"
    assert bundle["acceptance"]["debug"] is True
    assert bundle["acceptance"]["wechat"] is False  # 延后：发送门禁保持关闭

    # 自动校准完成+调试两项 ✓ 后，普通动作和发送门禁均放行。
    g_plain = env.wizard.gate(wechat_send=False)
    assert g_plain["ok"] and g_plain["checks"] == [], \
        f"非发送门禁必须全绿，实际: {[c['code'] for c in g_plain['checks']]}"
    g_send = env.wizard.gate(wechat_send=True)
    assert g_send["ok"] and g_send["checks"] == []

    # 绑定事实取自档案自身，避免夹具漂移
    facts = dict(device_id=bundle["deviceId"], camera_id=bundle["cameraId"],
                 camera_device_unique_id=bundle["cameraDeviceUniqueId"],
                 binding_revision=bundle["bindingRevision"],
                 daemon_addr=bundle["daemonAddr"],
                 frame_width=bundle["frame"]["width"], frame_height=bundle["frame"]["height"],
                 daemon_session=bundle["daemonSession"], estop_locked=False,
                 screen_orientation=bundle["screen"]["orientation"],
                 screen_width=bundle["screen"]["screenWidth"],
                 screen_height=bundle["screen"]["screenHeight"],
                 screen_edge_deviation_px=2.0, required_targets=(), wechat_send=False)
    assert cb.validate_calibration_bundle(bundle, **facts) == []
    facts["wechat_send"] = True
    assert cb.validate_calibration_bundle(bundle, **facts) == []


def test_gate_edge_contamination_is_not_phone_movement(env):
    """2026-09-05 真机定案：环境亮带把右上/右下角外扩（39px 级）、左上/左下
    像素级不动=非刚性检测干扰，不得误报 screen_edge_changed（当日用户实测
    输入/删除零偏移、6 次 run 边缘基准跨两日完全一致，手机未动）。"""
    drive_to_z_ready(env)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.daemon.z = 2.0
    env.wizard.capture_hover_z()
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    env.wizard.z_save("tap")
    _run_xy_stage(env)
    env.wizard.edge_capture()
    env.wizard.camera_fit()
    old = [list(c) for c in env.quad["corners"]]
    tl, tr, br, bl = old
    # 当日实测污染形态：TR(+31,+24)、BR(+11,-3)，TL/BL 不动
    env.quad["corners"] = [tl, [tr[0] + 31, tr[1] + 24],
                           [br[0] + 11, br[1] - 3], bl]
    gate = env.wizard.gate()
    codes = [c["code"] for c in gate["checks"]]
    assert "screen_edge_changed" not in codes
    assert gate["edgeDeviationPx"] is None
    assert gate["edgeCheck"]["status"] == "anomaly"
    # 恢复后正常
    env.quad["corners"] = old
    gate = env.wizard.gate()
    assert "screen_edge_changed" not in [c["code"] for c in gate["checks"]]


# ---------- 点击测试锚点：First Click 种子预校正（用户拍板 2026-09-05） ----------

def test_z_tap_test_records_tap_anchor(tmp_path):
    """z 点击测试成功=证实落在玻璃上：机械 XY + 触点屏幕坐标 + 会话落档。"""
    env = Env(tmp_path)
    env.ime.script = auto_report(env.ime, env.daemon)
    drive_to_z_ready(env)
    env.daemon.x, env.daemon.y = 12.5, -7.25  # 探头停在屏幕某处（模拟操作员对位）
    env.wizard.z_tap_test("tap", 3.0, 1500.0, 0)
    anchor = env.wizard.state["z"]["tapAnchor"]
    assert anchor["source"] == "z_tap_test"
    assert anchor["robotX"] == 12.5 and anchor["robotY"] == -7.25
    assert (anchor["screenX"], anchor["screenY"]) == truth(12.5, -7.25)
    assert anchor["daemonSession"] == env.daemon.session


def test_z_confirm_safe_records_tap_anchor(tmp_path):
    """确认点击参数的验证点击同样落档锚点（与 z_tap_test 同构）。"""
    env = Env(tmp_path)
    env.ime.script = auto_report(env.ime, env.daemon)
    drive_to_z_ready(env)
    env.daemon.z = 5.0
    env.wizard.z_confirm_safe(tap_depth=3.0)
    anchor = env.wizard.state["z"]["tapAnchor"]
    assert anchor["source"] == "z_confirm_safe"
    assert (anchor["robotX"], anchor["robotY"]) == (env.daemon.x, env.daemon.y)
    assert (anchor["screenX"], anchor["screenY"]) == truth(env.daemon.x, env.daemon.y)
    assert anchor["daemonSession"] == env.daemon.session


def test_auto_tap_anchor_pre_corrects_stale_seed(tmp_path):
    """真机事故回放：守护重连/G92 锚点归零 → 旧种子平移整体失效（85px 级），
    无锚点时 First Click 预测点落到屏幕外。点击测试锚点在 auto_start 即把
    种子平移拉回现场，First Click 零误差起步，全程收敛提升。"""
    env = auto_ready_env(tmp_path, seed_error_px=60.0)  # 60√2≈85px 平移偏差
    anchor = env.wizard.state["z"]["tapAnchor"]
    assert anchor["source"] == "z_confirm_safe"
    assert (anchor["robotX"], anchor["robotY"]) == (0.0, 0.0)
    assert (anchor["screenX"], anchor["screenY"]) == (100.0, 200.0)  # truth(0,0)
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = env.wizard.auto_view()["runId"]
    # 启动即完成预校正（candidate 在 spawn 前落盘）
    candidate = cb.load_candidate("arm-01", run_id, tmp_path)
    seed_anchor = candidate.get("seedAnchor") or {}
    assert seed_anchor.get("offsetPx") == [-60.0, -60.0]
    assert candidate["armSeed"]["robotToScreen"]["c"] == 100.0
    assert candidate["armSeed"]["robotToScreen"]["f"] == 200.0
    events = [e["event"] for e in candidate.get("events") or []]
    assert "seed_anchor_applied" in events
    # 全链路收敛：锚定后 First Click 误差≈0，Phase1 重拟合仍收敛到真值
    wait_auto(env, lambda v: v["phase"] == "E_ACCEPTANCE" or v["state"] == "FAILED",
              desc="E 验收或失败")
    finish_e_acceptance(env, run_id)
    view = wait_auto(env, lambda v: v["state"] in ("PROMOTED", "FAILED"), desc="终态")
    assert view["state"] == "PROMOTED"
    promoted = cb.load_bundle("arm-01", tmp_path)
    m = promoted["xy"]["robotToScreen"]
    assert abs(m["c"] - 100.0) < 1.0 and abs(m["f"] - 200.0) < 1.0


def test_auto_tap_anchor_skipped_on_session_mismatch(tmp_path):
    """锚点守护会话与当前不一致（跨重启采集的旧锚点）：坐标基已换，不可信，
    不应用且留痕 seed_anchor_skipped；种子保持原样交给 First Click 后备。"""
    env = auto_ready_env(tmp_path, seed_error_px=25.0)
    env.wizard.state["z"]["tapAnchor"]["daemonSession"] = "sess-old-restart"
    env.wizard._save_state()
    env.wizard.auto_start(defer_input_acceptance=False)
    run_id = env.wizard.auto_view()["runId"]
    candidate = cb.load_candidate("arm-01", run_id, tmp_path)
    assert not candidate.get("seedAnchor")
    assert candidate["armSeed"]["robotToScreen"]["c"] == 125.0  # 未被修正
    events = [e["event"] for e in candidate.get("events") or []]
    assert "seed_anchor_skipped" in events
    env.wizard.auto_stop()
    wait_auto(env, lambda v: v["state"] in ("STOPPED", "FAILED", "ABORT"), desc="停止")


def _recovery_candidate(run_id="run-recover-1", *, device="arm-01",
                        park=True, validation=True, locks=True):
    return {
        "runId": run_id, "deviceId": device,
        "state": cw.AUTO_STATE_PROMOTED,
        "daemonSession": "sess-A", "phoneSessionId": "ime-1",
        "binding": {"cameraId": "cam-01", "cameraDeviceUniqueId": "uvc-1",
                    "bindingRevision": 1, "daemonAddr": "127.0.0.1:8795",
                    "frameWidth": 1280, "frameHeight": 720},
        "h": {"homography": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "version": 1},
        "arm": {"robotToScreen": {"a": 10.0, "b": 0.0, "c": 1.0,
                                  "d": 0.0, "e": 8.0, "f": 2.0}},
        "locks": {"cameraToScreen": {"state": "LOCKED" if locks else "UNINITIALIZED"},
                  "armAffine": {"state": "LOCKED" if locks else "UNINITIALIZED"}},
        "validation": {"passed": validation},
        "z": {"zSafe": 5.0, "tapDepth": 3.0, "tapFeed": 1500.0,
              "validated": True, "daemonSession": "sess-A"},
        "safePark": ({"confirmed": True, "parkX": 11.0, "parkY": 12.0,
                      "parkZ": 5.0, "source": "test", "daemonSession": "sess-A"}
                     if park else {"confirmed": False, "parkX": None}),
        "edge": {"corners": [[80, 40], [880, 40], [880, 640], [80, 640]]},
    }


def test_p07_recovery_report_and_restore_imports_pending_review_only(tmp_path):
    """P07：完整候选 → 恢复报告可恢复；导入为待复核资料且不恢复执行资格。"""
    env = Env(tmp_path)
    env.wizard.start("k40-01")
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    env.ime.script = auto_report(env.ime, env.daemon)
    env.wizard.z_confirm_safe(tap_depth=3.0)
    env.wizard.park_confirm()
    bundle_before = cb.load_bundle("arm-01", tmp_path)

    cb.save_candidate(_recovery_candidate(), tmp_path)
    cb.save_candidate(_recovery_candidate("run-recover-2", park=False), tmp_path)
    # 跨设备资料：文件在 arm-01 目录内但内容属于别的设备（迁移错放场景）。
    misplaced = _recovery_candidate("run-recover-3", device="arm-99")
    misplaced_path = cb.candidates_dir("arm-01", tmp_path) / "run-recover-3.json"
    misplaced_path.parent.mkdir(parents=True, exist_ok=True)
    misplaced_path.write_text(json.dumps(misplaced, ensure_ascii=False), encoding="utf-8")

    report = env.wizard.candidate_recovery_report()
    by_run = {item["runId"]: item for item in report["items"]}
    assert by_run["run-recover-1"]["recoverable"] is True
    assert by_run["run-recover-1"]["missing"] == []
    assert by_run["run-recover-2"]["recoverable"] is False
    assert "safe_park_confirmed" in by_run["run-recover-2"]["missing"]
    assert "device_mismatch" in by_run["run-recover-3"]["missing"]

    # 完整候选导入：待复核 + 资格不恢复 + 主档不被改动。
    result = env.wizard.candidate_restore("run-recover-1")
    assert result["imported"] is True
    assert result["pendingScopes"] == {"z": True, "xy": True, "park": True}
    assert "execution_eligibility" in result["notRestored"]
    reference = env.wizard.state["recoveredReference"]
    assert reference["pendingReview"] is True
    assert reference["z"]["zSafe"] == 5.0
    assert reference["safePark"]["parkX"] == 11.0
    bundle_after = cb.load_bundle("arm-01", tmp_path)
    assert bundle_after["status"] == bundle_before["status"]
    assert bundle_after["safePark"]["parkX"] == bundle_before["safePark"]["parkX"]
    # 导入后执行资格被拦（全项待复核+阶段重置），不因导入放行任何动作。
    env.daemon.cmds.clear()
    with pytest.raises(WizardError, match="尚未完成重新复核|请先完成 A 阶段"):
        env.wizard.auto_start()
    assert env.daemon.cmds == []

    # 不完整候选导入被拒并指明缺失项。
    with pytest.raises(WizardError, match="safe_park_confirmed"):
        env.wizard.candidate_restore("run-recover-2")


def test_z_confirm_pose_reference_absent_does_not_block_operator_tap(tmp_path):
    """用户拍板 2026-09-29：操作员监督下的 zSafe 确认不被姿态定位拦截——
    点击是否有效由唯一触点回报裁决（回报含屏幕坐标=证明落在屏内）；
    四边不可定位时记录姿态缺席，验证通过照常保存。"""
    from drivers.screen_edge import ScreenEdgeError
    env = Env(tmp_path, wizard_extra={"edge_detector": lambda jpg: (_ for _ in ()).throw(
        ScreenEdgeError("画面中未检出屏幕四边形（确认屏幕点亮且在画面内）"))})
    env.wizard.start("k40-01")
    env.open_phone()
    wait_stage(env, cw.STAGE_Z_READY)
    env.ime.script = auto_report(env.ime, env.daemon)
    result = env.wizard.z_confirm_safe(tap_depth=3.0)
    assert result["validated"] is True
    record = env.wizard.state["z"]["autoSafe"]
    assert record["validated"] is True
    assert "姿态参考缺席" in (record.get("poseReference") or "")
    # 触点回报已保存（证明点击落在屏内的唯一裁决）。
    assert record["report"]["screenX"] > 0
