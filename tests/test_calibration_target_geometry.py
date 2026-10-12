"""F2 标靶反算屏幕几何验收（规范 §6.2 / §13.1 C01-C04、C07）。

复用 tests/test_calibration_wizard.py 的 Env/FakeIme/FakeDaemon 夹具（import，
不复制）。全部使用 fake daemon/IME 与合成检测器，不访问真实硬件：
- C01 未保存四角 + 轮廓失败 + 标靶清楚 → 无机械动作完成映射；反算通过后
  能合法原地 Z 确认。
- C02 缺屏幕元数据 / 错会话 / 错手机 → 不布置标靶、不 TAP、资料不变。
- C03 中心验证点误差超标 / 标靶遮挡 → 不通过；验证点不并入训练消除误差。
- C04 第六次重复首标靶移动 >2px → 本轮几何采集拒绝，不保存。
- C07 全新设备无主档 → 标靶定位不依赖机械资格；绝对几何可用、
  相对矩阵/发布指纹为空。
"""
from __future__ import annotations

from copy import deepcopy

import pytest

from drivers import calibration_bundle as cb
from drivers import calibration_wizard as cw
from drivers.calibration_wizard import WizardError
from drivers.screen_edge import ScreenEdgeError
from tests.test_calibration_wizard import (
    SCREEN_H,
    SCREEN_W,
    Env,
    auto_report,
    wait_stage,
)

CAM_W, CAM_H = 960, 540  # 与 Env 的相机帧一致
# 测试手机几何：屏幕占相机帧一块子区域（非全帧），面积占比 ≈74%，
# 满足 §6.2.10 的 5%~90% 且四角全部在帧内。
PHONE_QUAD = [[80.0, 30.0], [880.0, 30.0], [880.0, 510.0], [80.0, 510.0]]
SCREEN_META = {"screenWidth": SCREEN_W, "screenHeight": SCREEN_H,
               "orientation": "portrait"}
MOTION_PREFIXES = ("XY", "Z", "TAP", "TAP2", "SWIPE", "PATH1")


def pcam(sx, sy):
    """手机屏幕像素 → 相机像素（线性映射到 PHONE_QUAD）。"""
    return (PHONE_QUAD[0][0] + sx * (PHONE_QUAD[1][0] - PHONE_QUAD[0][0]) / SCREEN_W,
            PHONE_QUAD[0][1] + sy * (PHONE_QUAD[3][1] - PHONE_QUAD[0][1]) / SCREEN_H)


def _failing_edge(jpg):
    """轮廓不足（测试注入：反光削弱四边，检测器抛错）。"""
    raise ScreenEdgeError("四边轮廓不可定位（测试注入）")


class TargetScript:
    """动态绿靶检测器：按当前布防标靶返回相机像素。

    注入点：center_bias（中心检测偏移，制造验证误差）、center_occluded
    （中心标靶遮挡）、repeat_shift（首标靶第 4 次检测起位移，模拟采点期间
    手机移动）。
    """

    def __init__(self, env: Env, *, center_bias: float = 0.0,
                 center_occluded: bool = False, repeat_shift: float = 0.0):
        self.env = env
        self.center_bias = float(center_bias)
        self.center_occluded = bool(center_occluded)
        self.repeat_shift = float(repeat_shift)
        self.first_point_detections = 0

    def __call__(self, jpg):
        for session in self.env.ime.sessions.values():
            armed = session["armed"]
            if not (armed and armed.get("target")):
                continue
            t = armed["target"]
            x, y = pcam(t["u"] * SCREEN_W, t["v"] * SCREEN_H)
            if abs(t["u"] - 0.5) < 1e-9 and abs(t["v"] - 0.5) < 1e-9:
                if self.center_occluded:
                    raise ScreenEdgeError("中心标靶被遮挡（测试注入）")
                x += self.center_bias
            if abs(t["u"] - 0.2) < 1e-9 and abs(t["v"] - 0.2) < 1e-9:
                self.first_point_detections += 1
                if self.first_point_detections >= 4:  # 首标靶复测轮（第 6 次布防）
                    x += self.repeat_shift
            return (x, y, CAM_W, CAM_H)
        return None


def geo_env(tmp_path, *, deadline: float = 4.0, **detector_kw) -> Env:
    """标靶反算专用环境：四边检测失败 + 可注入的标靶检测脚本。"""
    env = Env(tmp_path, wizard_extra={"target_geo_deadline_s": deadline})
    env.wizard._edge_detector = _failing_edge
    env.wizard._target_detector = TargetScript(env, **detector_kw)
    return env


def drive_open(env: Env, screen_meta=SCREEN_META):
    env.wizard.start("k40-01")
    env.wizard.precheck()
    env.open_phone(screen_meta)
    wait_stage(env, cw.STAGE_Z_READY)


def motion_cmds(env: Env) -> list[str]:
    return [c for c in env.daemon.cmds if c.split()[0] in MOTION_PREFIXES]


# ---------- C01 ----------

def test_c01_target_geometry_without_motion_then_legal_inplace_z_confirm(tmp_path):
    """C01：无机械动作完成映射；四角反算通过后能合法原地 Z 确认（解除死锁）。"""
    env = geo_env(tmp_path)
    drive_open(env)
    auto_report(env.ime, env.daemon)
    env.jog_to_screen(SCREEN_W / 2, SCREEN_H / 2)  # 探头停在屏幕中央上方
    env.tips.extend([(480.0, 270.0)] * 16)  # 探头尖可见，位于反算屏幕内

    # 反算阶段：不得有任何机械动作（含租约占用）
    cmds_before = list(env.daemon.cmds)
    result = env.wizard.screen_geometry_from_targets()
    assert env.daemon.cmds == cmds_before
    assert motion_cmds(env) == []

    tg = env.wizard.state["geometry"]["targetGeometry"]
    assert tg is not None and tg["geometrySource"] == "calibration_targets"
    for got, want in zip(tg["corners"], PHONE_QUAD):
        assert got[0] == pytest.approx(want[0], abs=0.5)
        assert got[1] == pytest.approx(want[1], abs=0.5)
    # §6.2.4/6.2.5：训练集只有四个拟合点，中心只做独立验证
    assert tg["fit"]["pointCount"] == 4
    assert len(tg["fit"]["points"]) == 4
    assert tg["fit"]["verifyPoint"]["usedInFit"] is False
    assert tg["fit"]["verifyPoint"]["cameraErrorPx"] <= 3.0
    assert tg["fit"]["verifyPoint"]["screenErrorPx"] <= 3.0
    # §6.2.6/6.2.8：每点 3 帧递增序号、复测位移达标
    for point in tg["fit"]["points"]:
        seqs = [o["frameSeq"] for o in point["observations"]]
        assert len(seqs) == 3 and seqs == sorted(set(seqs))
    assert tg["repeatCheck"]["movementPx"] <= 2.0

    # 首次点击死锁解除：四边不可定位 + 标靶反算几何可用 → 合法原地 Z 确认
    confirmed = env.wizard.z_confirm_safe()
    assert confirmed["validated"] is True
    assert env.wizard.state["z"]["autoSafe"]["validated"] is True
    # 反算阶段无机械动作；Z 确认只允许原地点击（一条 TAP），不得有 XY 走位
    assert [c.split()[0] for c in motion_cmds(env)] == ["TAP"]
    # 定位基准来源标注为标靶反算
    pose = env.wizard.state["z"]["phonePose"]
    assert pose["edge"]["geometrySource"] == "calibration_targets"


# ---------- C02 ----------

def test_c02_missing_screen_metadata_no_arm_no_tap_no_state_change(tmp_path):
    """C02a：缺屏幕元数据 → 不布置标靶、不 TAP、资料不变（禁止猜旧尺寸）。"""
    env = geo_env(tmp_path)
    drive_open(env, screen_meta=None)  # open 但未回报屏幕元数据
    geometry_before = deepcopy(env.wizard.state["geometry"])
    cmds_before = list(env.daemon.cmds)
    with pytest.raises(WizardError, match="屏幕尺寸"):
        env.wizard.screen_geometry_from_targets()
    assert env.ime.arm_calls == []
    assert env.daemon.cmds == cmds_before
    assert env.wizard.state["geometry"] == geometry_before


def test_c02_wrong_session_or_wrong_phone_rejected_before_arming(tmp_path):
    """C02b/c：错手机 / 错会话 → 不布置错误坐标标靶、不执行 TAP、资料不变。"""
    env = geo_env(tmp_path)
    drive_open(env)
    geometry_before = deepcopy(env.wizard.state["geometry"])
    cmds_before = list(env.daemon.cmds)

    # 错手机：向导指向其他设备的校准会话
    other = env.ime.create_session("other-phone")
    env.ime.client(other["sessionId"], "open", screen=SCREEN_META)
    saved_sid = env.wizard.state["imeSessionId"]
    env.wizard.state["imeSessionId"] = other["sessionId"]
    with pytest.raises(WizardError, match="不一致"):
        env.wizard.screen_geometry_from_targets()
    env.wizard.state["imeSessionId"] = saved_sid

    # 错会话：本会话已结束（输入法服务重启等）
    env.ime.complete(saved_sid)
    with pytest.raises(WizardError, match="校准会话已结束"):
        env.wizard.screen_geometry_from_targets()

    assert env.ime.arm_calls == []
    assert env.daemon.cmds == cmds_before
    assert env.wizard.state["geometry"] == geometry_before


# ---------- C03 ----------

def test_c03_center_verification_error_rejects_without_training_absorption(tmp_path):
    """C03a：中心验证点误差超标 → 不通过；不能并入训练消除误差。"""
    env = geo_env(tmp_path, center_bias=12.0)  # 中心检测偏 12 相机像素
    drive_open(env)
    with pytest.raises(WizardError, match="中心独立验证未通过"):
        env.wizard.screen_geometry_from_targets()
    assert env.wizard.state["geometry"].get("targetGeometry") is None
    assert env.wizard.state["z"].get("phonePose") is None


def test_c03_center_target_occluded_fails_and_disarms(tmp_path):
    """C03b：中心标靶被遮挡 → 本轮不通过，不留残余布防。"""
    env = geo_env(tmp_path, center_occluded=True, deadline=3.0)
    drive_open(env)
    with pytest.raises(WizardError, match="center"):
        env.wizard.screen_geometry_from_targets()
    assert env.wizard.state["geometry"].get("targetGeometry") is None
    # §6.2.7：异常也要解除布防
    for session in env.ime.sessions.values():
        assert session["armed"] is None


# ---------- C04 ----------

def test_c04_repeat_target_moved_round_rejected(tmp_path):
    """C04：第六次重复首标靶移动 >2px → 本轮几何采集拒绝，不保存。"""
    env = geo_env(tmp_path, repeat_shift=5.0)
    drive_open(env)
    with pytest.raises(WizardError, match="手机在采点期间移动"):
        env.wizard.screen_geometry_from_targets()
    assert env.wizard.state["geometry"].get("targetGeometry") is None
    assert env.wizard.state["z"].get("phonePose") is None
    for session in env.ime.sessions.values():
        assert session["armed"] is None


# ---------- C07 ----------

def test_c07_fresh_device_absolute_geometry_without_motion_qualification(tmp_path):
    """C07：全新设备无主档 → 标靶定位不依赖机械资格；绝对几何可用，
    相对矩阵/发布指纹为空（不冒充已校准）。"""
    env = geo_env(tmp_path)
    env.wizard.start("k40-01")  # 不做 precheck/不进入 Z 阶段：不依赖机械资格
    env.open_phone(SCREEN_META)
    cmds_before = list(env.daemon.cmds)
    result = env.wizard.screen_geometry_from_targets()
    assert env.daemon.cmds == cmds_before
    assert motion_cmds(env) == []

    tg = result["targetGeometry"]
    for got, want in zip(tg["corners"], PHONE_QUAD):
        assert got[0] == pytest.approx(want[0], abs=0.5)
        assert got[1] == pytest.approx(want[1], abs=0.5)
    # §7.1：首次绝对几何不带相对矩阵与发布指纹，不用单位矩阵/假指纹冒充
    assert tg["matrix"] is None
    assert tg["publishedCalibrationFingerprint"] is None
    assert tg["candidateId"]
    # 机械资格全部未建立，也不因反算被授予
    state = env.wizard.state
    assert state["z"]["hoverZ"] is None and state["z"]["autoSafe"] is None
    assert state["xy"]["fit"] is None
    assert state["safePark"]["parkX"] is None
    bundle = cb.load_bundle("arm-01", env.wizard.data_root)
    assert bundle is not None and bundle.get("status") == "draft"
