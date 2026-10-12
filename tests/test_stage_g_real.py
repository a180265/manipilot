"""阶段 G：单台真机接入测试（模拟 Grbl 控制器 + 合成摄像头画面）。

覆盖现场文档（《给其他AI的机械臂现场总说明》《机械臂参数清单》2026-08-30）的核心安全规则：
- 连接握手（启动信息 + ? 非 Alarm/Door）并记录会话原点；
- 禁止 $H/$HX/$HY/$HZ 自动回零与自动寻找 Z 限位；
- Z 轴禁用进给表机制保留（当前为空）；运动只允许具名模板（相对小行程 G91）；
- ok/MPos 不是真实移动的证明：摄像头逐段帧差验证，未证实一律"结果未知"；
- 归位 = 回到会话原点（非 $H）；软停止/软件急停/人工复位语义；
- 丢行（无 ok）→ 超时 → 结果未知路径；拔线 → 连接中断路径；
- 真机摄像头质量分类（清晰/遮挡/过曝/冻结/异常）与"未标定必须回报未知"；
- 平台真机模式混合注册：arm-01 真机驱动、arm-02 仿真驱动，互不影响。
"""
from __future__ import annotations

import asyncio

import cv2
import numpy as np
import pytest

from drivers.arm import DriverError
from drivers.grbl import (
    FORBIDDEN_COMMANDS,
    EmulatedGrblController,
    GrblPolicyError,
    validate_line,
)
from drivers.real_arm import PRESET_TEMPLATES, RealArmDriver
from drivers.real_camera import CameraMotionVerifier, RealCameraDriver
from drivers.real_input import RealInputDriver


def test_legacy_serial_requires_explicit_offline_opt_in(monkeypatch):
    from drivers.grbl import PyserialGrblTransport
    import serial

    monkeypatch.delenv("ARMFLEET_ALLOW_DIRECT_SERIAL", raising=False)
    monkeypatch.setattr(serial, "Serial", lambda **kwargs: pytest.fail("opened real serial"))
    with pytest.raises(GrblPolicyError, match="直连串口默认禁用"):
        PyserialGrblTransport().open()


def test_legacy_serial_opt_in_uses_bounded_writes(monkeypatch):
    from drivers.grbl import PyserialGrblTransport
    import serial

    captured = {}
    class Port:
        def readline(self):
            return b"Grbl test\n"
        def close(self):
            pass
    def open_port(**kwargs):
        captured.update(kwargs)
        return Port()
    monkeypatch.setenv("ARMFLEET_ALLOW_DIRECT_SERIAL", "1")
    monkeypatch.setattr(serial, "Serial", open_port)
    transport = PyserialGrblTransport()
    assert transport.open() == "Grbl test"
    assert captured["write_timeout"] == 2
    transport.close()


# ---------- 合成场景摄像头 ----------

class SceneCamera:
    """合成 BGR 帧：控制器真实运动（on_motion 钩子）驱动画面中标记移动。

    帧角含计数器，保证哈希始终变化（不触发冻结判定）；
    phantom（失步）时控制器 MPos 变化但场景不动 → 帧差验证必须不通过。
    """

    MARKER_STEP_PX = 30.0

    def __init__(self):
        self.offset_px = 0.0
        self.counter = 0

    def move_marker(self) -> None:
        self.offset_px += self.MARKER_STEP_PX

    def frame(self) -> np.ndarray:
        self.counter += 1
        img = np.full((240, 320, 3), 60, dtype=np.uint8)
        x = int(20 + self.offset_px) % 260
        cv2.rectangle(img, (x, 90), (x + 24, 150), (255, 255, 255), -1)
        cv2.rectangle(img, (x + 6, 100), (x + 18, 140), (0, 140, 255), -1)
        cv2.putText(img, str(self.counter % 10), (300, 230), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)
        return img


class PhoneScene:
    """2026-09-29 规范 §7 后，质量/识别先定位手机 ROI——本场景在画面内
    放一台可定位的亮手机（标记与计数器语义同 SceneCamera：标记随控制器
    on_motion 移动、角计数器保证哈希变化不触发冻结）。"""

    MARKER_STEP_PX = SceneCamera.MARKER_STEP_PX

    def __init__(self):
        self.offset_px = 0.0
        self.counter = 0

    def move_marker(self) -> None:
        self.offset_px += self.MARKER_STEP_PX

    def frame(self) -> np.ndarray:
        self.counter += 1
        img = np.full((240, 320, 3), 30, dtype=np.uint8)
        cv2.rectangle(img, (30, 30), (289, 209), (185, 185, 185), -1)
        # 标记始终在手机矩形内（不得跨界破坏亮色手机轮廓的可定位性）
        x = int(50 + self.offset_px) % 200
        cv2.rectangle(img, (x, 90), (x + 24, 150), (255, 255, 255), -1)
        cv2.rectangle(img, (x + 6, 100), (x + 18, 140), (0, 140, 255), -1)
        cv2.putText(img, str(self.counter % 10), (300, 230),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (200, 200, 200), 1)
        return img

    def different_page(self) -> np.ndarray:
        """同一手机位形、完全不同的页面内容（匹配分数必须低）。"""
        img = self.frame()
        cv2.rectangle(img, (50, 50), (270, 190), (20, 20, 20), -1)
        cv2.putText(img, "OTHER", (80, 130), cv2.FONT_HERSHEY_SIMPLEX,
                    1.4, (255, 255, 255), 3)
        return img


def make_driver(scene: SceneCamera | None = None, **kwargs) -> tuple[RealArmDriver, EmulatedGrblController, SceneCamera]:
    scene = scene or SceneCamera()
    controller = EmulatedGrblController()
    controller.on_motion = scene.move_marker
    driver = RealArmDriver("arm-01", transport=controller, **kwargs)
    driver.motion_verifier = CameraMotionVerifier(
        RealCameraDriver("cam-01", frame_provider=scene.frame), diff_threshold=1.0
    )
    return driver, controller, scene


def connect(driver: RealArmDriver) -> dict:
    return asyncio.run(driver.connect())


# ---------- 策略层（纯校验） ----------

def test_forbidden_homing_commands_rejected():
    """现场明确禁止 $H/$HX/$HY/$HZ 自动回零。"""
    for command in FORBIDDEN_COMMANDS:
        with pytest.raises(GrblPolicyError):
            validate_line(command, distance_mode="G91", allow_session_home=True)


def test_dollar_commands_other_than_query_rejected():
    with pytest.raises(GrblPolicyError):
        validate_line("$1=255", distance_mode="G91")
    validate_line("?", distance_mode="G91")  # 只读查询放行


def test_z_forbidden_feed_table_enforced():
    """禁用值表机制保留：置值时拦截；当前为空（2026-09-18 设备更换解除 F300）。"""
    import drivers.grbl as grbl_mod
    validate_line("G1 Z3 F300", distance_mode="G91")  # 当前为空 → 放行
    try:
        grbl_mod.FORBIDDEN_Z_FEEDS = (300.0,)
        with pytest.raises(GrblPolicyError):
            validate_line("G1 Z3 F300", distance_mode="G91")
    finally:
        grbl_mod.FORBIDDEN_Z_FEEDS = ()


def test_relative_motion_within_limits_allowed():
    validate_line("G1 Z-3 F2000", distance_mode="G91")
    validate_line("G1 X5 Y5 F7000", distance_mode="G91")


def test_overstroke_and_bad_feed_rejected():
    with pytest.raises(GrblPolicyError):
        validate_line("G1 Z-11 F2000", distance_mode="G91")  # Z 软上限 10mm
    with pytest.raises(GrblPolicyError):
        validate_line("G1 X301 F7000", distance_mode="G91")  # XY 直达上限 300mm
    with pytest.raises(GrblPolicyError):
        validate_line("G1 Z3 F10", distance_mode="G91")  # 低于已验证最低进给
    with pytest.raises(GrblPolicyError):
        validate_line("G1 Z3 F5000", distance_mode="G91")  # 超出 Z 已验证范围


def test_absolute_motion_rejected_unless_session_home():
    with pytest.raises(GrblPolicyError):
        validate_line("G1 X10 Y10 Z400 F1500", distance_mode="G90")
    # 会话原点归位放行（仅在 G90 模式）
    validate_line("G1 X0 Y0 Z403.7 F1500", distance_mode="G90", allow_session_home=True)


def test_arbitrary_gcode_rejected():
    """禁止 AI/插件自由生成 G-code：只允许模板指令词。"""
    for line in ("G4 P500", "G28", "G38.2 Z-5", "M3 S1000", "G1 Z3 F2000 T1"):
        with pytest.raises(GrblPolicyError):
            validate_line(line, distance_mode="G91")


# ---------- 连接握手 ----------

def test_connect_handshake_records_session_origin():
    driver, controller, _scene = make_driver()
    result = connect(driver)
    assert result["connected"] is True
    assert "Grbl_ESP32" in result["firmware"]
    assert result["controller_state"] == "Idle"
    assert result["session_origin_mpos"] == {"x": 0.0, "y": 0.0, "z": 403.7}
    assert driver.session_origin == {"x": 0.0, "y": 0.0, "z": 403.7}


def test_connect_refuses_alarm_state():
    driver, controller, _scene = make_driver()
    controller.faults["alarm_on_connect"] = True
    with pytest.raises(DriverError) as excinfo:
        connect(driver)
    assert excinfo.value.category == "driver_connect_failed"
    assert "Alarm" in excinfo.value.message


def test_read_state_reports_controller_state():
    driver, _controller, _scene = make_driver()
    connect(driver)
    state = asyncio.run(driver.read_state())
    assert state["connected"] is True
    assert state["controller_state"] == "Idle"
    assert state["position"]["z"] == 403.7
    assert state["estop_latched"] is False


# ---------- 运动模板与归位 ----------

def test_move_preset_sends_documented_template_lines():
    driver, controller, _scene = make_driver()
    connect(driver)
    result = asyncio.run(driver.move_preset("z_recip_3mm_verified"))
    assert result["ok"] is True
    assert result["outcome_hint"] == "success"
    assert result["motion_verified"] is True
    # 模板行逐条下发且均收到 ok；绝不包含 $H 类命令
    assert "G1 Z-3 F2000" in controller.sent_lines
    assert "G1 Z3 F2000" in controller.sent_lines
    assert not any(line.upper().startswith("$H") for line in controller.sent_lines)


def test_move_preset_per_segment_camera_verification():
    """3mm 往复起点=终点：必须逐段验证，整体前后对比不构成证据。"""
    driver, _controller, scene = make_driver()
    connect(driver)
    result = asyncio.run(driver.move_preset("z_recip_3mm_verified"))
    assert len(result["verifications"]) == 2  # 两个 G1 运动段
    assert all(v["verified"] for v in result["verifications"])
    assert scene.offset_px == 2 * SceneCamera.MARKER_STEP_PX


def test_unknown_preset_rejected():
    driver, _controller, _scene = make_driver()
    connect(driver)
    with pytest.raises(DriverError) as excinfo:
        asyncio.run(driver.move_preset("free_move_x_100mm"))
    assert excinfo.value.category == "driver_policy_rejected"
    assert "未登记的运动模板" in excinfo.value.message


def test_go_home_returns_to_session_origin_without_homing():
    driver, controller, _scene = make_driver()
    connect(driver)
    asyncio.run(driver.move_preset("z_lower_3mm"))  # 向下（靠近屏幕）3mm
    state = asyncio.run(driver.read_state())
    assert state["position"]["z"] == pytest.approx(406.7)
    result = asyncio.run(driver.go_home())
    assert result["ok"] is True
    assert result["home_mode"].startswith("session_origin")
    assert result["position"]["z"] == pytest.approx(403.7)
    assert not any(line.upper().startswith("$H") for line in controller.sent_lines)


def test_go_home_requires_camera_verification_when_moved():
    """归位前有位移时：必须逐段摄像头验证真实归位，验证器正常工作时 ok=True。"""
    driver, _controller, scene = make_driver()
    connect(driver)
    asyncio.run(driver.move_preset("z_lower_3mm"))
    assert scene.offset_px == SceneCamera.MARKER_STEP_PX
    result = asyncio.run(driver.go_home())
    assert result["ok"] is True
    assert result["motion_verified"] is True


def test_go_home_unverified_motion_rejected():
    """phantom 失步复现：控制器归位、画面不动 → ok 必须为 False（需人工确认）。"""
    scene = SceneCamera()
    driver, controller, _scene = make_driver(scene)
    connect(driver)
    asyncio.run(driver.move_preset("z_lower_3mm"))
    controller.faults["phantom_motion"] = True  # 之后所有运动：坐标变、画面不动
    result = asyncio.run(driver.go_home())
    assert result["ok"] is False
    assert result["motion_verified"] is False
    assert "人工确认" in result["message"]


def test_phantom_motion_on_preset_reported_unknown():
    """核心规则：ok + MPos 变化 ≠ 真实移动。失步时结果必须"未知"而非成功。"""
    driver, controller, _scene = make_driver()
    connect(driver)
    controller.faults["phantom_motion"] = True
    result = asyncio.run(driver.move_preset("z_lift_1mm_conservative"))
    assert result["ok"] is True  # 控制器层面已接受
    assert result["motion_verified"] is False
    assert result["outcome_hint"] == "unknown"
    assert "结果未知" in result["message"]


def test_no_ack_becomes_timeout_not_retry():
    """丢行（无 ok 确认）→ 驱动超时；上层按结果未知处理，禁止重发该行。"""
    driver, controller, _scene = make_driver()
    connect(driver)
    controller.faults["drop_ack_next"] = True
    with pytest.raises(DriverError) as excinfo:
        asyncio.run(driver.move_preset("z_lift_1mm_conservative"))
    assert excinfo.value.category == "driver_timeout"
    assert "不得重发" in excinfo.value.message


def test_disconnect_mid_motion_raises_disconnected():
    driver, controller, _scene = make_driver()
    connect(driver)
    controller.faults["disconnect_after_lines"] = 2  # 模板第 2 行（首条 G1）执行后拔线
    with pytest.raises(DriverError) as excinfo:
        asyncio.run(driver.move_preset("z_recip_3mm_verified"))
    assert excinfo.value.category == "driver_disconnected"


# ---------- 停止与急停 ----------

def test_stop_sends_feed_hold_and_reports():
    driver, controller, _scene = make_driver()
    connect(driver)
    result = asyncio.run(driver.stop())
    assert result["ok"] is True
    assert result["stopped"] is True
    assert "!" in controller.sent_lines
    assert "硬件急停" in result["message"]


def test_estop_latches_and_blocks_motion_until_manual_reset():
    driver, _controller, _scene = make_driver()
    connect(driver)
    result = asyncio.run(driver.estop())
    assert result["estop_latched"] is True
    assert result["hardware_estop_required"] is True
    with pytest.raises(DriverError) as excinfo:
        asyncio.run(driver.move_preset("z_lift_1mm_conservative"))
    assert excinfo.value.category == "driver_estop_latched"
    driver.reset_estop("现场操作员")
    result2 = asyncio.run(driver.move_preset("z_lift_1mm_conservative"))
    assert result2["ok"] is True


# ---------- 点击 ----------

def test_click_moves_xy_then_taps_z_with_verification():
    driver, controller, scene = make_driver()
    connect(driver)
    result = asyncio.run(driver.click({"dx": 2.0, "dy": 1.0, "tap_depth_mm": 0.5}))
    assert result["ok"] is True
    assert result["outcome_hint"] == "success"
    assert [p["phase"] for p in result["phases"]] == ["move", "tap"]
    assert "G1 X2.000 Y1.000 F100" in controller.sent_lines
    assert "G1 Z0.500 F1500" in controller.sent_lines
    assert "G1 Z-0.500 F1500" in controller.sent_lines
    assert scene.offset_px == 3 * SceneCamera.MARKER_STEP_PX  # move + 下压 + 抬起


def test_click_requires_dx_dy():
    driver, _controller, _scene = make_driver()
    connect(driver)
    with pytest.raises(DriverError) as excinfo:
        asyncio.run(driver.click({"x": 1.0}))
    assert excinfo.value.category == "driver_motion_failed"


# ---------- 六方向验证与 G92 会话原点 ----------

def test_six_direction_templates_all_verified():
    """X±/Y±/Z± 六个方向逐一运动，摄像头逐段证实；结束回到原点。"""
    driver, _controller, scene = make_driver()
    connect(driver)
    pairs = [
        ("z_lift_1mm_conservative", "z_lower_1mm_conservative"),
        ("x_plus_1mm_conservative", "x_minus_1mm_conservative"),
        ("y_plus_1mm_conservative", "y_minus_1mm_conservative"),
    ]
    for outward, back in pairs:
        out = asyncio.run(driver.move_preset(outward))
        assert out["motion_verified"] is True, (outward, out)
        assert out["outcome_hint"] == "success"
        back_result = asyncio.run(driver.move_preset(back))
        assert back_result["motion_verified"] is True, (back, back_result)
    state = asyncio.run(driver.read_state())
    assert state["position"] == {"x": 0.0, "y": 0.0, "z": 403.7}
    assert scene.offset_px == 6 * SceneCamera.MARKER_STEP_PX  # 六段全部真实移动


@pytest.mark.parametrize("reason", ["视觉重定位后锚定原点", "ok", "123", "!@#$"])
def test_session_origin_anchors_without_motion(reason):
    """断电/挪动后重定位锚点：软件记录当前 MPos 为原点，零串口运动指令。"""
    driver, controller, _scene = make_driver()
    connect(driver)
    asyncio.run(driver.move_preset("z_lower_3mm"))  # 移到任意位置
    controller.sent_lines.clear()
    result = asyncio.run(driver.set_session_origin_here(reason=reason))
    assert result["ok"] is True
    assert driver.action_log[-1]["reason"] == reason
    assert driver.session_origin == {"x": 0.0, "y": 0.0, "z": 406.7}
    # 锚定本身不发送任何运动/坐标指令（G92 在本固件会引入隐藏偏移，禁用）；只允许只读查询
    assert controller.sent_lines == [] or all(line == "?" for line in controller.sent_lines)
    # 归位以新原点为基准（G90 绝对回原点）
    home = asyncio.run(driver.go_home())
    assert home["ok"] is True
    assert not any(line.upper().startswith("$H") for line in controller.sent_lines)


def test_connect_clears_stale_g92_offsets():
    """连接时发送 G92.1 清除上一会话残留的 G92 偏移，保证 G90 目标 = MPos 坐标系。"""
    driver, controller, _scene = make_driver()
    connect(driver)
    assert "G92.1" in controller.sent_lines


@pytest.mark.parametrize("reason", ["", " \t\n", None, 123])
def test_session_origin_requires_non_empty_reason(reason):
    driver, _controller, _scene = make_driver()
    connect(driver)
    with pytest.raises(DriverError) as excinfo:
        asyncio.run(driver.set_session_origin_here(reason=reason))
    assert excinfo.value.category == "driver_policy_rejected"


def test_g92_policy_rejects_bad_lines():
    """G92 已整体移出白名单（本固件语义陷阱），连合法格式也必须拒绝。"""
    for line in ("G92 X0 Y0 Z0", "G92", "G92 X0 F100", "G92 X5000"):
        with pytest.raises(GrblPolicyError):
            validate_line(line, distance_mode="G91")
    validate_line("G92.1", distance_mode="G91")  # 仅清除偏移的 G92.1 放行


# ---------- 真机摄像头 ----------

def _png(frame: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".png", frame)
    assert ok
    return buffer.tobytes()


def test_camera_quality_classification():
    """质量分类五态（2026-09-29 规范 §7 后）：分类先定位手机 ROI——
    纯噪声/纯白/无手机合成帧无法定位手机一律画面异常（由
    test_phone_localization 断言）；本用例用带手机矩形的帧逐态走到
    clear/waiting/overexposed/abnormal/frozen 真实分支。"""

    def phone_frame(background: int, level: int, *, texture: bool = True):
        frame = np.full((240, 320, 3), background, dtype=np.uint8)
        cv2.rectangle(frame, (30, 30), (289, 209), (level, level, level), -1)
        if texture:
            for y in range(45, 200, 12):
                cv2.line(frame, (45, y), (275, y),
                         (min(255, level + 70),) * 3, 2)
            cv2.putText(frame, "ABC 123", (60, 120),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0,
                        (max(0, level - 70),) * 3, 2)
        return frame

    current = {"frame": phone_frame(210, 40)}  # 手机在画面内、内容有纹理

    def provider():
        return current["frame"]

    camera = RealCameraDriver("cam-01", frame_provider=provider)
    quality = asyncio.run(camera.check_quality())
    assert quality["quality"] == "clear"
    assert quality["quality_display"] == "清晰"

    current["frame"] = np.full((240, 320, 3), 8, dtype=np.uint8)  # 黑屏例外
    quality = asyncio.run(camera.check_quality())
    assert quality["quality"] == "waiting"
    assert quality["quality_display"] == "等待画面恢复"

    current["frame"] = phone_frame(20, 252)  # 手机 ROI 过曝（§7.3.6）
    quality = asyncio.run(camera.check_quality())
    assert quality["quality"] == "overexposed"
    assert quality["quality_display"] == "过曝"

    current["frame"] = phone_frame(210, 128, texture=False)  # 手机无纹理 → 异常
    quality = asyncio.run(camera.check_quality())
    assert quality["quality"] == "abnormal"
    assert quality["quality_display"] == "画面异常"

    current["frame"] = phone_frame(210, 60)  # 连续同帧 → 流冻结
    asyncio.run(camera.check_quality())
    quality = asyncio.run(camera.check_quality())
    assert quality["quality"] == "frozen"
    assert quality["quality_display"] == "画面冻结"


def test_ui_state_requires_registered_reference():
    """未登记参考画面：识别必须回报未知 + 人工标定提示，不得猜测。

    画面须含可定位手机（§7 质量前置），否则在参考门之前就按
    "画面不可用"拒绝——那是 test_camera_quality_classification 的范畴。
    """
    scene = PhoneScene()
    camera = RealCameraDriver("cam-01", frame_provider=scene.frame)
    state = asyncio.run(camera.identify_ui_state())
    assert state["ui_state"] == "unknown"
    assert "标定" in state["note"]
    confirm = asyncio.run(camera.confirm_main_view("wechat_main"))
    assert confirm["confirmed"] is False

    frame = scene.frame()
    camera.register_reference("wechat_main", _png(frame))
    ok, _ = camera._match(_png(frame))
    assert ok == "wechat_main"
    camera._frame_provider = lambda: frame
    confirm = asyncio.run(camera.confirm_main_view("wechat_main"))
    assert confirm["confirmed"] is True
    # 换成不同页面：与参考差异大 → 其他界面，需人工确认
    camera._frame_provider = scene.different_page
    confirm = asyncio.run(camera.confirm_main_view("wechat_main"))
    assert confirm["confirmed"] is False


def test_account_identification_conservative():
    """账号/会话线索：真机 OCR 未标定时明确不可用，禁止编造线索。"""
    camera = RealCameraDriver("cam-01", frame_provider=lambda: SceneCamera().frame())
    result = asyncio.run(camera.identify_account_and_conversation())
    assert result["account_clue"] is None
    assert result["conversation_clue"] is None
    assert result["confidence"] == 0.0


# ---------- 输入驱动 ----------

def test_real_input_requires_calibration():
    inp = RealInputDriver("arm-01")
    with pytest.raises(DriverError) as excinfo:
        asyncio.run(inp.type_text("你好"))
    assert excinfo.value.category == "driver_uncalibrated"
    with pytest.raises(DriverError):
        asyncio.run(inp.press_key("delete"))


def test_real_input_calibrated_layout():
    inp = RealInputDriver("arm-01", keyboard_layout={"chars": {"你": {"dx": 1, "dy": 2}}, "keys": {"delete": {"dx": 3, "dy": 4}}})
    result = asyncio.run(inp.type_text("你"))
    assert result["ok"] is True
    assert result["text_typed"] == "你"
    assert asyncio.run(inp.press_key("delete"))["ok"] is True
    with pytest.raises(DriverError) as excinfo:
        asyncio.run(inp.type_text("好"))
    assert "未标定" in excinfo.value.message


# ---------- 平台集成：真机/仿真混合注册 ----------

def _runtime_with_real_device(tmp_path, controller: EmulatedGrblController, scene: SceneCamera):
    from tests.helpers import make_runtime

    runtime = make_runtime(tmp_path)
    runtime.devices.hardware_mode = "real"
    configs = [
        {
            "device_id": "arm-01",
            "name_display": "设备 arm-01（真机）",
            "arm_slot": 1,
            "camera_id": "cam-01",
            "wechat_account_id": "pending-real-acc-01",
            "driver_mode": "real",
            "serial_port": "/dev/cu.usbserial-212420",
            "baud_rate": 115200,
            "camera_device_name": "UVC Camera",
            "transport_factory": lambda: controller,
            "camera_frame_provider": scene.frame,
        },
        {
            "device_id": "arm-02",
            "name_display": "设备 arm-02",
            "arm_slot": 2,
            "camera_id": "cam-02",
            "wechat_account_id": "sim-acc-02",
        },
    ]
    return runtime, configs


def test_device_manager_mixed_real_and_simulation(tmp_path):
    controller = EmulatedGrblController()
    # 相机画面须含可定位手机（§7 质量前置）：无手机画面的自检按
    # 自检异常降级，与本用例"通用硬件就绪"的验证目标无关。
    scene = PhoneScene()
    controller.on_motion = scene.move_marker
    runtime, configs = _runtime_with_real_device(tmp_path, controller, scene)

    async def run():
        for cfg in configs:
            await runtime.devices.register_and_selfcheck(cfg)

    asyncio.run(run())
    real = runtime.devices.get("arm-01")
    sim = runtime.devices.get("arm-02")
    assert real["driver_versions"]["arm"] == RealArmDriver.DRIVER_VERSION
    assert real["driver_versions"]["camera"] == RealCameraDriver.DRIVER_VERSION
    assert sim["driver_versions"]["arm"].startswith("fake-")
    # 通用硬件就绪不依赖微信参考画面，应用核对仍不能伪造成功。
    assert real["status"] == "ready"
    camera = runtime.devices.camera("arm-01")
    assert asyncio.run(camera.confirm_main_view("wechat_main"))["confirmed"] is False
    assert sim["status"] == "ready"


def test_real_device_home_check_and_verified_motion(tmp_path):
    controller = EmulatedGrblController()
    # 归位检查要求摄像头画面清晰（§7：需可定位手机 ROI）——场景含手机。
    scene = PhoneScene()
    controller.on_motion = scene.move_marker
    runtime, configs = _runtime_with_real_device(tmp_path, controller, scene)

    async def run():
        for cfg in configs:
            await runtime.devices.register_and_selfcheck(cfg)
        home = await runtime.devices.home_check("arm-01")
        assert home["home_ok"] is True
        # 真机设备可领取任务（降级不阻塞任务，仅隔离/暂停/离线阻塞）
        taskable, reason = runtime.devices.is_taskable("arm-01")
        assert taskable is True, reason
        # 能力调用走真机驱动：move_preset 结果包含摄像头逐段验证
        result = await runtime.devices.driver("arm-01").move_preset("z_first_direction_verify")
        assert result["outcome_hint"] == "success"
        assert result["motion_verified"] is True
        home2 = await runtime.devices.home_check("arm-01")
        assert home2["home_ok"] is True

    asyncio.run(run())


def test_simulation_platform_falls_back_real_slot(tmp_path):
    """平台仿真模式下，真机槽位自动回退仿真驱动且绑定说明注明原因。"""
    from runtime.audit import AuditLog
    from runtime.devices import DeviceManager
    from runtime.events import EventBus
    from runtime.store import Store

    store = Store(tmp_path / "fallback.db")
    manager = DeviceManager(store, AuditLog(store, EventBus()), EventBus(), hardware_mode="simulation")
    device = manager.register_device(
        {
            "device_id": "arm-01",
            "arm_slot": 1,
            "camera_id": "cam-01",
            "wechat_account_id": "pending-real-acc-01",
            "driver_mode": "real",
        }
    )
    assert device["driver_versions"]["arm"].startswith("fake-")
    assert "回退仿真" in device["binding_note"]
