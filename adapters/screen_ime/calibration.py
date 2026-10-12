"""屏幕校准会话协调器：K40TaskServer CalibrationCoordinator 的 Python 移植。

边界（继承原设计）：
- 会话只保存在进程内存中，服务重启后自动失效，不写入任务数据库，也不进入任务历史；
  未完成会话必须转人工处理，不能自动续跑。
- 每台设备同时只允许一个 active 会话。
- 手机校准页必须先回报 client_state=open 才允许布防（arm）；页面回报 closed 立即清除布防。
- 没有匹配布防点（sampleId+sequence）的触点回报会被拒绝，防止手机端伪造采样时序。
- 采样点只保存屏幕像素与元数据；机械坐标、运动步骤和仿射拟合由 Agent/插件负责，
  手机端不保存不可信的机械坐标。
- target 为可选的归一化矩形 (u, v, widthU, heightV)，必须完整且落在 [0,1] 内。
"""
from __future__ import annotations

import asyncio
import time
import uuid

from adapters.screen_ime.service import ScreenImeApiError, now_iso


def _blank_to_unknown(value) -> str:
    if isinstance(value, str) and value.strip():
        return value
    return "unknown"


def _now_millis() -> int:
    return int(time.time() * 1000)


def _require_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScreenImeApiError(400, f"invalid_{field}", f"{field} is required")
    return value


def _target_from(u, v, width_u, height_v) -> dict | None:
    any_given = any(x is not None for x in (u, v, width_u, height_v))
    if not any_given:
        return None
    values = (u, v, width_u, height_v)
    if (any(x is None for x in values)
            or not all(isinstance(x, (int, float)) and float(x) == float(x) for x in values)):
        raise ScreenImeApiError(400, "invalid_calibration_target",
                                "Calibration target must be a bounded normalized rectangle")
    u_f, v_f, w_f, h_f = (float(x) for x in values)
    if (w_f <= 0 or h_f <= 0
            or u_f - w_f / 2 < 0 or u_f + w_f / 2 > 1
            or v_f - h_f / 2 < 0 or v_f + h_f / 2 > 1):
        raise ScreenImeApiError(400, "invalid_calibration_target",
                                "Calibration target must be a bounded normalized rectangle")
    return {"u": u_f, "v": v_f, "widthU": w_f, "heightV": h_f}


def _is_finite_number(value) -> bool:
    return isinstance(value, (int, float)) and float(value) == float(value) and abs(float(value)) != float("inf")


def _screen_metadata_update(screen_width, screen_height, orientation,
                            current: dict | None) -> dict | None:
    """校准页只读屏幕元数据（规范 §6.2.2）：宽高成对、正整数像素；方向非空文本。

    返回相对 current 的增量字段；未提供任何字段返回 None；非法值抛 400。
    """
    if screen_width is None and screen_height is None and orientation is None:
        return None
    if screen_width is not None or screen_height is not None:
        if (not isinstance(screen_width, int) or isinstance(screen_width, bool)
                or not isinstance(screen_height, int) or isinstance(screen_height, bool)
                or screen_width <= 0 or screen_height <= 0):
            raise ScreenImeApiError(400, "invalid_screen_metadata",
                                    "screenWidth/screenHeight must be positive integer pixels")
    if orientation is not None and (not isinstance(orientation, str) or not orientation.strip()):
        raise ScreenImeApiError(400, "invalid_screen_metadata",
                                "orientation must be a non-empty string")
    update: dict = {}
    if screen_width is not None:
        update["screenWidth"] = int(screen_width)
        update["screenHeight"] = int(screen_height)
    if orientation is not None:
        update["orientation"] = orientation
    # 一旦开始上报，会话内存的屏幕元数据必须保持完整三件套（供标靶反算等
    # 只读消费方使用）；缺项视为非法，禁止留下半套可被误读成"已回报"的资料。
    merged = dict(current or {})
    merged.update(update)
    if not (isinstance(merged.get("screenWidth"), int) and merged.get("screenWidth", 0) > 0
            and isinstance(merged.get("screenHeight"), int) and merged.get("screenHeight", 0) > 0
            and isinstance(merged.get("orientation"), str) and merged.get("orientation", "").strip()):
        raise ScreenImeApiError(400, "invalid_screen_metadata",
                                "screen metadata requires screenWidth, screenHeight and orientation together")
    return update


class CalibrationCoordinator:
    """内存态校准会话（与 Java 版语义一致）。"""

    def __init__(self):
        self._sessions: dict[str, dict] = {}
        self._active_by_device: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _view(self, session: dict) -> dict:
        return {
            "sessionId": session["sessionId"],
            "deviceId": session["deviceId"],
            "status": session["status"],
            "clientState": session["clientState"],
            "createdAt": session["createdAt"],
            "screen": (dict(session["screen"]) if session.get("screen") else None),
            "samples": [dict(sample) for sample in session["samples"]],
            "armed": (dict(session["armed"]) if session["armed"] else None),
        }

    def _session(self, session_id) -> dict:
        _require_text(session_id, "sessionId")
        session = self._sessions.get(session_id)
        if session is None:
            raise ScreenImeApiError(404, "calibration_not_found",
                                    "Calibration session not found")
        return session

    async def create(self, device_id) -> dict:
        device_id = _require_text(device_id, "deviceId")
        async with self._lock:
            existing = self._active_by_device.get(device_id)
            if existing is not None:
                raise ScreenImeApiError(409, "calibration_active",
                                        "Device already has an active calibration")
            session = {
                "sessionId": f"cal-{uuid.uuid4()}",
                "deviceId": device_id,
                "status": "active",
                "clientState": "unknown",
                "createdAt": now_iso(),
                "screen": None,
                "samples": [],
                "armed": None,
            }
            self._sessions[session["sessionId"]] = session
            self._active_by_device[device_id] = session["sessionId"]
            return self._view(session)

    async def current(self, device_id: str) -> dict | None:
        async with self._lock:
            session_id = self._active_by_device.get(device_id)
            if session_id is None:
                return None
            session = self._sessions.get(session_id)
            return self._view(session) if session else None

    async def get(self, session_id: str) -> dict:
        async with self._lock:
            return self._view(self._session(session_id))

    async def arm(self, session_id: str, sample_id, sequence, target_u, target_v,
                  target_width_u, target_height_v) -> dict:
        sample_id = _require_text(sample_id, "sampleId")
        if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
            raise ScreenImeApiError(400, "invalid_sequence", "sequence must be positive")
        target = _target_from(target_u, target_v, target_width_u, target_height_v)
        async with self._lock:
            session = self._session(session_id)
            if session["status"] != "active":
                raise ScreenImeApiError(409, "calibration_closed", "Calibration is not active")
            if session["clientState"] != "open":
                raise ScreenImeApiError(409, "phone_page_closed",
                                        "Phone calibration page is not open")
            if session["armed"] is not None:
                raise ScreenImeApiError(409, "calibration_armed", "A sample is already armed")
            if any(sample["sampleId"] == sample_id for sample in session["samples"]):
                raise ScreenImeApiError(409, "sample_duplicate", "sampleId was already reported")
            if any(sample["sequence"] == sequence for sample in session["samples"]):
                raise ScreenImeApiError(409, "sequence_duplicate", "sequence was already reported")
            armed = {"sampleId": sample_id, "sequence": sequence}
            if target is not None:
                armed["target"] = target
            session["armed"] = armed
            return self._view(session)

    async def disarm(self, session_id: str, sample_id, sequence=None) -> dict:
        sample_id = _require_text(sample_id, "sampleId")
        if sequence is not None and (type(sequence) is not int or sequence <= 0):
            raise ScreenImeApiError(400, "invalid_sequence", "sequence must be positive")
        async with self._lock:
            session = self._session(session_id)
            if session["status"] != "active":
                raise ScreenImeApiError(409, "calibration_closed", "Calibration is not active")
            if session["armed"] is None:
                return self._view(session)
            if (session["armed"]["sampleId"] != sample_id
                    or (sequence is not None and session["armed"]["sequence"] != sequence)):
                raise ScreenImeApiError(409, "sample_not_armed", "A different sample is armed")
            session["armed"] = None
            return self._view(session)

    async def touch(self, session_id: str, request: dict) -> dict:
        if not isinstance(request, dict):
            raise ScreenImeApiError(400, "invalid_request", "Request body is required")
        device_id = _require_text(request.get("deviceId"), "deviceId")
        sample_id = _require_text(request.get("sampleId"), "sampleId")
        sequence = request.get("sequence")
        if not isinstance(sequence, int) or isinstance(sequence, bool):
            raise ScreenImeApiError(400, "invalid_sequence", "sequence must be positive")
        screen_x = request.get("screenX")
        screen_y = request.get("screenY")
        screen_w = request.get("screenWidth")
        screen_h = request.get("screenHeight")
        async with self._lock:
            session = self._session(session_id)
            if session["status"] != "active":
                raise ScreenImeApiError(409, "calibration_closed", "Calibration is not active")
            if session["deviceId"] != device_id:
                raise ScreenImeApiError(409, "device_mismatch",
                                        "Device does not own calibration")
            armed = session["armed"]
            if (armed is None or armed["sampleId"] != sample_id
                    or armed["sequence"] != sequence):
                raise ScreenImeApiError(409, "sample_not_armed",
                                        "No matching sample is armed")
            if (not _is_finite_number(screen_x) or not _is_finite_number(screen_y)
                    or not isinstance(screen_w, int) or not isinstance(screen_h, int)
                    or screen_w <= 0 or screen_h <= 0):
                raise ScreenImeApiError(400, "invalid_screen_point",
                                        "Screen point metadata is invalid")
            screen_x, screen_y = float(screen_x), float(screen_y)
            if screen_x < 0 or screen_y < 0 or screen_x > screen_w or screen_y > screen_h:
                raise ScreenImeApiError(400, "screen_point_out_of_bounds",
                                        "Screen point is outside the reported screen")
            orientation = _blank_to_unknown(request.get("orientation"))
            event_time = request.get("eventTime")
            sample = {
                "sampleId": sample_id,
                "sequence": sequence,
                "screenX": screen_x,
                "screenY": screen_y,
                "screenWidth": screen_w,
                "screenHeight": screen_h,
                "orientation": orientation,
                "eventTime": event_time if isinstance(event_time, int) and not isinstance(event_time, bool) and event_time > 0 else _now_millis(),
                "receivedAt": now_iso(),
            }
            if armed.get("target") is not None:
                sample["target"] = dict(armed["target"])
            session["samples"].append(sample)
            session["armed"] = None
            session["clientState"] = "open"
            return self._view(session)

    async def client(self, session_id: str, device_id, state,
                     screen_width=None, screen_height=None,
                     orientation=None) -> dict:
        """校准页状态回报；open 时可附带只读屏幕元数据（规范 §6.2.2）。

        元数据在 open 时持久保存在会话内并经 _view 透出，供标靶反算等
        只读消费方使用；宽高/方向非法返回 400。closed 时不接受元数据更新
        （页面已退出，旧元数据保留供审计，不作为新会话事实）。
        """
        device_id = _require_text(device_id, "deviceId")
        if state not in ("open", "closed"):
            raise ScreenImeApiError(400, "invalid_client_state", "state must be open or closed")
        async with self._lock:
            session = self._session(session_id)
            if session["deviceId"] != device_id:
                raise ScreenImeApiError(409, "device_mismatch",
                                        "Device does not own calibration")
            if state == "open":
                update = _screen_metadata_update(
                    screen_width, screen_height, orientation, session.get("screen"))
                if update:
                    merged = dict(session.get("screen") or {})
                    merged.update(update)
                    session["screen"] = merged
            session["clientState"] = state
            if state == "closed":
                session["armed"] = None
            return self._view(session)

    async def finish(self, session_id: str, cancel: bool) -> dict:
        async with self._lock:
            session = self._session(session_id)
            session["status"] = "cancelled" if cancel else "completed"
            session["armed"] = None
            if self._active_by_device.get(session["deviceId"]) == session_id:
                self._active_by_device.pop(session["deviceId"], None)
            return self._view(session)
