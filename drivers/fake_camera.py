"""fake 摄像头驱动：生成真实 PNG 画面，支持界面状态与故障注入。

可配置项（测试夹具）：
- ui_state：当前外部应用界面状态（内部键）
- confidence：识别置信度 0~1
- quality：clear 清晰 / blocked 遮挡 / overexposed 过曝 / frozen 冻结 / abnormal 异常
- account_clue / conversation_clue：画面中可见的账号与会话线索

R2-08 移交（B1 报告 §2.4）：仿真采集在渲染现场打真实可证采样身份——
同宿主仿真与滑动消费端同进程同单调钟域（time.monotonic_ns），逐帧
source_capture_seq 严格递增、采集会话内 generation 稳定；因此滑动链
（frame_chain_identity 要求 camera_pts_host_clock.v1 证明帧）按同一契约
消费仿真帧。gap/duplicate/generation 反例在驱动注入点（restart_source_session/
_source_capture_seq/_source_captured_monotonic_ns）操作，不在消费端放宽。
"""
from __future__ import annotations

import math
import time
import uuid

from drivers.camera import CameraDriver
from runtime.status import CAMERA_QUALITY, UI_STATE, display
from runtime.util import encode_png, iso, new_id, sha256_bytes, utc_now

# 界面状态 -> 基色（便于肉眼区分测试画面）
_UI_BASE_COLORS: dict[str, tuple[int, int, int]] = {
    "unknown": (120, 120, 120),
    "wechat_main": (40, 140, 80),
    "conversation": (40, 100, 160),
    "login": (170, 140, 40),
    "qrcode": (40, 40, 40),
    "captcha": (180, 120, 40),
    "risk_control": (170, 40, 40),
    "error": (120, 30, 30),
    "other": (90, 90, 140),
}


def _axis_aligned_bounds(corners, width: int, height: int) -> tuple[float, float, float, float]:
    """校准屏幕四角 -> 轴对齐矩形边界 (x0, y0, x1, y1)。

    仿真渲染只支持轴对齐手机矩形；四角必须构成凸矩形、完全位于帧内、
    每边至少 2px。非法输入直接拒绝（F6：非法输入不得静默退化为全帧纹理）。
    """
    try:
        quad = []
        for point in corners:
            x, y = point[0], point[1]
            if (isinstance(x, bool) or isinstance(y, bool)
                    or not isinstance(x, (int, float)) or not isinstance(y, (int, float))):
                raise TypeError("non-numeric corner")
            quad.append([float(x), float(y)])
    except (TypeError, ValueError, IndexError) as exc:
        raise ValueError("仿真手机四角必须是四个 [x, y] 数值点") from exc
    if len(quad) != 4 or not all(
            math.isfinite(x) and math.isfinite(y) for x, y in quad):
        raise ValueError("仿真手机四角必须是四个有限数值点")
    xs = sorted({point[0] for point in quad})
    ys = sorted({point[1] for point in quad})
    if len(xs) != 2 or len(ys) != 2:
        raise ValueError("仿真手机四角必须构成轴对齐矩形")
    x0, x1 = xs
    y0, y1 = ys
    if x1 - x0 < 2 or y1 - y0 < 2:
        raise ValueError("仿真手机每条边至少 2 像素")
    if x0 < 0 or y0 < 0 or x1 > width or y1 > height:
        raise ValueError("仿真手机四角必须完全位于仿真帧内")
    return (x0, y0, x1, y1)


class FakeCameraDriver(CameraDriver):
    DRIVER_VERSION = "fake-camera-1.0.0"

    def __init__(
        self,
        device_id: str,
        ui_state: str = "wechat_main",
        confidence: float = 0.97,
        quality: str = "clear",
        account_clue: str | None = None,
        conversation_clue: str | None = None,
        width: int = 64,
        height: int = 48,
    ):
        if ui_state not in UI_STATE:
            raise ValueError(f"未登记的界面状态内部键：{ui_state}")
        self.device_id = device_id
        self._ui_state = ui_state
        self._confidence = confidence
        self._quality = quality
        self._account_clue = account_clue
        self._conversation_clue = conversation_clue
        self._width = width
        self._height = height
        self._scroll_offset_px = 0
        # 仿真手机几何（F6）：None = 全帧纹理（旧行为）；给出四角时渲染
        # 「手机内部列表内容 + 静态背景」的真实几何，边界不随列表滚动移动。
        self._screen_bounds: tuple[float, float, float, float] | None = None
        self._phone_offset = (0.0, 0.0)
        self._open = False
        self._last_frame_hash: str | None = None
        self._last_capture_at: str | None = None
        self._render_cache: tuple[tuple, bytes] | None = None
        # R2-08 移交：可证明采样身份（渲染现场打标，同进程单调钟域）
        self._source_generation_id = str(uuid.uuid4())
        self._source_capture_seq = 0
        self._source_uncertainty_ms: float = 1.0

    def restart_source_session(self) -> str:
        """采集会话重启（断流恢复/换绑重开的仿真对应物）：换代、序号归零。

        旧代际帧链立即不可续接（与帧桥会话接管语义一致）。
        """
        self._source_generation_id = str(uuid.uuid4())
        self._source_capture_seq = 0
        return self._source_generation_id

    @property
    def driver_version(self) -> str:
        return self.DRIVER_VERSION

    def is_open(self) -> bool:
        return self._open

    # ---- 配置（夹具） ----
    def set_ui_state(self, ui_state: str, confidence: float | None = None) -> None:
        if ui_state not in UI_STATE:
            raise ValueError(f"未登记的界面状态内部键：{ui_state}")
        self._ui_state = ui_state
        if confidence is not None:
            self._confidence = confidence

    def show_conversation(self, conversation_id: str) -> None:
        """fake 微信窗口联动：点击会话后画面切换到该会话。"""
        self._ui_state = "conversation"
        self._conversation_clue = conversation_id

    def show_main(self) -> None:
        """fake 微信窗口联动：返回微信主界面。"""
        self._ui_state = "wechat_main"
        self._conversation_clue = None

    def set_quality(self, quality: str) -> None:
        self._quality = quality

    def set_clues(self, account: str | None, conversation: str | None) -> None:
        self._account_clue = account
        self._conversation_clue = conversation

    def configure_simulation_frame(self, width: int, height: int, *,
                                   screen_corners=None) -> None:
        """调整仿真帧尺寸；screen_corners 给出校准手机屏幕四角。

        提供四角时启用真实手机几何：内部为可追踪的确定性列表内容，外部为
        不随滚动/手机位移变化的静态背景，手机边界不随列表滚动移动。不传时
        保持原全帧纹理行为，保证直接构建 FakeCameraDriver 的既有测试兼容。
        """
        if (isinstance(width, bool) or not isinstance(width, int) or width <= 0
                or isinstance(height, bool) or not isinstance(height, int) or height <= 0):
            raise ValueError("仿真画面尺寸必须是正整数")
        bounds = None if screen_corners is None else _axis_aligned_bounds(
            screen_corners, width, height)
        self._width = width
        self._height = height
        self._screen_bounds = bounds
        self._phone_offset = (0.0, 0.0)
        self._last_frame_hash = None

    def set_phone_offset(self, dx: float, dy: float) -> None:
        """平移整个手机及内容（背景锚定相机帧不动）。参数必须为有限数。"""
        for value in (dx, dy):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(float(value))):
                raise ValueError("手机偏移必须是有限数")
        self._phone_offset = (float(dx), float(dy))
        self._last_frame_hash = None

    def set_scroll_offset(self, offset_px: int) -> None:
        """设置仿真微信列表内容的垂直位移，不改变真实摄像头驱动语义。"""
        if isinstance(offset_px, bool) or not isinstance(offset_px, int):
            raise ValueError("仿真滚动位移必须是整数像素")
        self._scroll_offset_px = offset_px
        self._last_frame_hash = None

    def scroll_by(self, delta_px: int) -> None:
        """让仿真列表内容按指定像素产生真实的垂直画面位移。"""
        if isinstance(delta_px, bool) or not isinstance(delta_px, int):
            raise ValueError("仿真滚动增量必须是整数像素")
        self.set_scroll_offset(self._scroll_offset_px + delta_px)

    # ---- 接口实现 ----
    async def open(self) -> dict:
        self._open = True
        return {
            "camera_id": self.device_id,
            "driver_version": self.DRIVER_VERSION,
            "width": self._width,
            "height": self._height,
        }

    async def close(self) -> dict:
        self._open = False
        return {"closed": True}

    def _render(self) -> bytes:
        width, height = self._width, self._height
        quality, offset = self._quality, self._scroll_offset_px
        base = _UI_BASE_COLORS.get(self._ui_state, (100, 100, 100))
        phone_bounds = self._screen_bounds
        phone_dx, phone_dy = self._phone_offset
        key = (width, height, base, quality, offset,
               phone_bounds, (phone_dx, phone_dy))
        if self._render_cache is not None and self._render_cache[0] == key:
            return self._render_cache[1]
        if quality in {"blocked", "waiting"}:
            base = (10, 10, 10)
        elif quality == "overexposed":
            base = (250, 250, 250)

        def content_pixel(x: int, y: int) -> tuple[int, int, int]:
            r, g, b = base
            if quality in {"blocked", "waiting"}:
                # 黑屏/等待画面继续保持低信息量，不能被仿真纹理误判为可用。
                stripe = (x + y) % 8 == 0
                return (r + (5 if stripe else 0), g, b)
            if quality == "overexposed":
                stripe = (x + y) % 8 == 0
                return (min(r + (5 if stripe else 0), 255), g, b)

            # 清晰仿真画面使用确定性的“内容世界”纹理。纹理坐标随列表
            # 内容一起移动，前后帧因此具有可被 LK 光流追踪的真实位移。
            content_y = y + offset
            tile_w, tile_h = 96, 72
            tile_x, tile_y = x // tile_w, content_y // tile_h
            local_x, local_y = x % tile_w, content_y % tile_h
            seed = (tile_x * 73856093) ^ (tile_y * 19349663)
            seed &= 0xFFFFFFFF
            shade = (seed % 17) - 8
            color = (
                max(0, min(r + shade, 255)),
                max(0, min(g + shade, 255)),
                max(0, min(b + shade, 255)),
            )

            rect_x = 8 + ((seed >> 5) % 38)
            rect_y = 7 + ((seed >> 11) % 28)
            rect_w = 24 + ((seed >> 17) % 26)
            rect_h = 18 + ((seed >> 23) % 22)
            in_rect = (rect_x <= local_x < rect_x + rect_w
                       and rect_y <= local_y < rect_y + rect_h)
            second_x = 52 + ((seed >> 3) % 18)
            second_y = 39 + ((seed >> 9) % 15)
            in_second = (second_x <= local_x < second_x + 18
                         and second_y <= local_y < second_y + 14)
            if in_rect:
                return tuple(max(0, min(channel + 72, 255)) for channel in color)
            if in_second:
                return tuple(max(0, min(channel - 48, 255)) for channel in color)
            # A small fixed accent adds corners even when a generated rectangle
            # happens to be outside a narrow measurement ROI.
            accent = ((local_x - 4) ** 2 + (local_y - 4) ** 2) < 9
            if accent:
                return tuple(max(0, min(channel + 42, 255)) for channel in color)
            return color

        if phone_bounds is None:
            def pixel(x: int, y: int) -> tuple[int, int, int]:
                return content_pixel(x, y)
        else:
            # 手机整体平移只搬移边界与内容；列表滚动只移动内容。
            px0, py0, px1, py1 = phone_bounds
            bx0, by0 = px0 + phone_dx, py0 + phone_dy
            bx1, by1 = px1 + phone_dx, py1 + phone_dy
            ix, iy = int(round(phone_dx)), int(round(phone_dy))

            def pixel(x: int, y: int) -> tuple[int, int, int]:
                if bx0 <= x < bx1 and by0 <= y < by1:
                    color = content_pixel(x - ix, y - iy)
                    # 手机内部保持可分离亮度：背景约 38±5、屏幕下限 90，
                    # Otsu 阈值落在两者之间，轮廓检测得到清晰手机边界。
                    return tuple(max(90, channel) for channel in color)
                # 背景锚定相机帧：不随列表滚动或手机位移变化。
                shade = ((x * 31 + y * 17) % 11) - 5
                return (38 + shade, 38 + shade, 38 + shade)

        # Snapshot scene inputs for the whole frame, even if a swipe happens
        # during rendering. Cache only pixels; each capture keeps fresh metadata.
        image = encode_png(width, height, pixel)
        self._render_cache = (key, image)
        return image

    def capture_sync(self) -> dict:
        """同步采集一帧（供 MJPEG 流与异步接口共用）。

        R2-08 移交：采样身份在渲染现场产生——captured_ns 取本帧渲染/读取
        完成时刻的本进程单调钟（同宿主仿真与 Fleet 同钟域，逐帧可证明）；
        编码缓存命中（像素不变）仍是新的一次物理采集，序号照常递增。
        """
        if self._quality == "abnormal":
            raise RuntimeError("摄像头采集异常")
        now = utc_now()
        image = self._render()
        captured_ns = time.monotonic_ns()
        self._source_capture_seq += 1
        frame_hash = sha256_bytes(image)
        frozen = self._quality == "frozen"
        if frozen and self._last_frame_hash is not None:
            frame_hash = self._last_frame_hash  # 冻结：与上一帧哈希一致
        frame = {
            "frame_ref": f"frame-{new_id('f')}",
            "captured_at": iso(now),
            "width": self._width,
            "height": self._height,
            "sha256": frame_hash,
            "image": image,
            "frozen": frozen,
            # 冻结帧包 v2 采样身份（camera_pts_host_clock.v1 证明帧）
            "source_generation_id": self._source_generation_id,
            "source_capture_seq": self._source_capture_seq,
            "source_captured_monotonic_ns": captured_ns,
            "source_capture_uncertainty_ms": self._source_uncertainty_ms,
            "source_timing_provenance": "camera_pts_host_clock.v1",
        }
        self._last_frame_hash = frame_hash
        self._last_capture_at = frame["captured_at"]
        return frame

    async def capture(self) -> dict:
        frame = self.capture_sync()
        return {k: v for k, v in frame.items() if k != "image"} | {"image_bytes": frame["image"]}

    async def check_quality(self) -> dict:
        return {"quality": self._quality, "quality_display": self._quality_display()}

    def _quality_display(self) -> str:
        return CAMERA_QUALITY.get(self._quality, "画面异常")

    async def identify_ui_state(self) -> dict:
        return {
            "ui_state": self._ui_state,
            "ui_state_display": display(UI_STATE, self._ui_state),
            "confidence": round(self._confidence, 3),
            "observed_at": iso(utc_now()),
        }

    async def identify_account_and_conversation(self) -> dict:
        return {
            "account_clue": self._account_clue,
            "conversation_clue": self._conversation_clue,
            "confidence": round(self._confidence, 3),
            "ui_state": self._ui_state,
            "ui_state_display": display(UI_STATE, self._ui_state),
        }

    async def locate_text(self, text: str, *, region: str = "messages",
                          min_confidence: float = 0.75, exact: bool = False,
                          roi: dict | None = None) -> dict:
        """仿真 OCR：返回稳定的全帧中心框，让业务走与真机相同的动态目标协议。"""
        if not str(text or "").strip():
            raise RuntimeError("OCR 目标文字不能为空")
        width, height = self._width, self._height
        box_w = max(8, min(width * 0.6, max(8, len(str(text)) * 6)))
        box_h = max(8, min(height * 0.25, 12))
        x1 = max(0.0, (width - box_w) / 2)
        y1 = max(0.0, (height - box_h) / 2)
        if roi is not None:
            from adapters.vision.ocr import ocr_roi_bounds

            left, top, right, bottom = ocr_roi_bounds(
                roi, frame_width=width, frame_height=height)
            box_w = min(box_w, right - left)
            box_h = min(box_h, bottom - top)
            x1 = left + (right - left - box_w) / 2
            y1 = top + (bottom - top - box_h) / 2
        return {
            "found": True, "text": str(text),
            "cameraX": x1 + box_w / 2, "cameraY": y1 + box_h / 2,
            "bbox": [x1, y1, x1 + box_w, y1 + box_h],
            "confidence": max(float(min_confidence), self._confidence),
            "engine": "fake-ocr", "frameW": width, "frameH": height,
        }

    async def confirm_main_view(self, expected_ui_state: str = "wechat_main") -> dict:
        if self._quality != "clear":
            return {
                "confirmed": False,
                "reason": "画面质量异常，无法确认界面",
                "ui_state": self._ui_state,
                "ui_state_display": display(UI_STATE, self._ui_state),
            }
        if self._confidence < 0.6:
            return {
                "confirmed": False,
                "reason": "识别置信度过低，无法确认界面",
                "ui_state": self._ui_state,
                "ui_state_display": display(UI_STATE, self._ui_state),
            }
        if self._ui_state != expected_ui_state:
            return {
                "confirmed": False,
                "reason": f"当前界面为{display(UI_STATE, self._ui_state)}，与期望不一致",
                "ui_state": self._ui_state,
                "ui_state_display": display(UI_STATE, self._ui_state),
            }
        return {
            "confirmed": True,
            "reason": None,
            "ui_state": self._ui_state,
            "ui_state_display": display(UI_STATE, self._ui_state),
        }
