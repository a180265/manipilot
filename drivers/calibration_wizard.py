"""统一校准与输入法点击验收：向导状态机（调试台后端核心，HTTP 无关）。

对应《统一校准与输入法点击验收实施方案》：
- 状态机 §5：IDLE → PRECHECK → PHONE_CALIBRATION_OPEN → Z_READY →
  XY_CALIBRATING → SCREEN_GEOMETRY_CALIBRATING → TARGET_CALIBRATING →
  DEBUG_ACCEPTANCE → READY；旧 WECHAT_ACCEPTANCE 仅兼容；失效条件进入 INVALIDATED。
- Z 阶段（§6）：悬停高度证据化保存；普通点击带布防+触点回报闭环；长按清除
  用独立参数（校准页对 >1s 触点拒报，故 Z 阶段只做机械执行记录，清空效果
  在验收阶段确认——与 §6.6 "保存或标记待验收" 一致）。
- XY 阶段（§7）：5 点引导采样（布防点携带 target 标靶矩形，由手机校准页
  显示），仿射拟合 screen=A·robot+b，残差 RMS≤3px、单点≤6px，另加 3 个
  不参与拟合的独立验证点。
- 视觉阶段（§8）：屏幕边缘基准（OTSU 亮区四边形）+ 摄像头→屏幕单应
  （用已接受 XY 样本的探头尖像素 ↔ 触点回报像素配对，≥4 点）。
- 目标与验收（§9/§13）：目标登记/测试/人工接受；调试台输入法验收；
  finish 统一写入 calibration_bundle.json 并置 ready。微信测试会话不属于
  机械臂校准，真实发送由 WX/插件在每次任务中按目标、正文和发送后证据校验。

失效处理（§14）：daemon 会话变化、屏幕方向/尺寸变化、触点回报不匹配、
IME 会话丢失、拟合超差 → INVALIDATED，只允许查看原因/重新开始/人工取消。

本模块不依赖具体 HTTP/串口实现：daemon 与 ime 为注入对象（鸭子类型），
camera_fetch/tip_detector/edge_detector 为注入回调，便于协议级测试注入
fake（tests/test_calibration_wizard.py）。
"""
from __future__ import annotations

from copy import deepcopy
import json
import logging
import math
import threading
import time
import uuid

import numpy as np
from pathlib import Path

from drivers import calibration_bundle as cb
from drivers import gesture_program as gp
from drivers import screen_gestures as sg
from drivers import target_archive
from drivers import ui_targets
from drivers.daemon_protocol import (
    DEFAULT_TAP_FEED,
    XY_STROKE_MAX,
    TARGET_DISPLAY_NAMES,
    check_feed,
    split_segments,
)
from drivers.screen_edge import ScreenEdgeError, classify_quad_change, quad_digest
from drivers.target_archive import device_dir
from drivers.calibration_wizard_common import (
    DEBUG_ACCEPTANCE_CHECKS,
    GOAL_DISPLAY,
    GOAL_MOTION,
    GOAL_OBSERVATION,
    STAGE_DEBUG_ACCEPTANCE,
    STAGE_IDLE,
    STAGE_INVALIDATED,
    STAGE_PHONE_OPEN,
    STAGE_PRECHECK,
    STAGE_READY,
    STAGE_SCREEN_GEOMETRY,
    STAGE_TARGET,
    STAGE_WECHAT_ACCEPTANCE,
    STAGE_XY,
    STAGE_Z_READY,
    STAGE_DISPLAY,
    WECHAT_ACCEPTANCE_CHECKS,
    AutoRunError,
    ImeError,
    PoseUnlocatable,
    TouchReportTimeout,
    WizardError,
    _AutoCancelled,
    _AutoPauseRetry,
    _now,
)
from drivers.calibration_auto_runner import (
    AUTO_ACTIVE_STATES,
    AUTO_CAMERA_SOURCE,
    AUTO_CONFIG_DEFAULTS,
    AUTO_FIRST_CLICK_POINT,
    AUTO_PHASE_DISPLAY,
    AUTO_PLAN_GRID_U,
    AUTO_PLAN_GRID_V,
    AUTO_PLAN_POINTS,
    AUTO_PROMOTE_GUARD_KEYS,
    AUTO_STATE_ABORTED,
    AUTO_STATE_BOOTSTRAP_REQUIRED,
    AUTO_STATE_FAILED,
    AUTO_STATE_IDLE,
    AUTO_STATE_INVALIDATED,
    AUTO_STATE_PAUSED_RETRY,
    AUTO_STATE_PROMOTED,
    AUTO_STATE_RUNNING,
    AUTO_STATE_STOPPED,
    AUTO_STATE_STOPPING,
    AUTO_VALIDATION_POINTS,
    AutoRunner,
)


# XY 五点引导采样（归一化屏幕位置：中心 + 四角安全区，§7.1）
XY_PLAN_POINTS: list[tuple[str, float, float]] = [
    ("center", 0.50, 0.50),
    ("top_left", 0.18, 0.15),
    ("top_right", 0.82, 0.15),
    ("bottom_left", 0.18, 0.80),
    ("bottom_right", 0.82, 0.80),
]
# 独立验证点（不参与拟合，§7.4）
XY_VALIDATION_POINTS: list[tuple[str, float, float]] = [
    ("upper_left", 0.35, 0.22),
    ("lower_right", 0.68, 0.78),
    ("center_offset", 0.55, 0.50),
]

TARGET_RECT_HALF = 0.06  # 布防标靶矩形半宽/半高（归一化），手机校准页据此显示标靶

# ---------- F2 无机械动作的标靶反算（规范 §6.2，验收规范 2026-09-29） ----------
# 四个固定拟合点（归一化屏幕坐标）+ 中心独立验证点；中心绝不参与拟合（§6.2.4/6.2.5）。
TARGET_GEOMETRY_FIT_POINTS: tuple[tuple[str, float, float], ...] = (
    ("top_left", 0.2, 0.2),
    ("top_right", 0.8, 0.2),
    ("bottom_right", 0.8, 0.8),
    ("bottom_left", 0.2, 0.8),
)
TARGET_GEOMETRY_VERIFY_POINT: tuple[str, float, float] = ("center", 0.5, 0.5)
TARGET_GEO_FRAMES_PER_POINT = 3          # 每标靶 3 个递增来源号新帧（§6.2.6）
TARGET_GEO_MIN_FRAME_INTERVAL_S = 0.1    # 相邻采样间隔 ≥100ms（§6.2.6）
TARGET_GEO_JITTER_MAX_PX = 2.0           # 同标靶 3 帧中心两两差上限（§6.2.6）
TARGET_GEO_REPEAT_MAX_PX = 2.0           # 首标靶复测与本轮首次观测差上限（§6.2.8）
TARGET_GEO_DEADLINE_S = 15.0             # 采样总截止（§6.2.6）
TARGET_GEO_VERIFY_MAX_PX = cb.CAMERA_REPROJECTION_MAX_PX  # 中心验证：相机域/屏幕域各 ≤3px（§6.2.9）
TARGET_GEO_AREA_MIN_RATIO = 0.05         # 反算四角面积占比下限（§6.2.10）
TARGET_GEO_AREA_MAX_RATIO = 0.90         # 反算四角面积占比上限（§6.2.10）


# 必验目标（§9.3）改为运行期 OCR 定位（用户拍板 2026-09-01）：候选栏文字、
# 发送/删除、聊天会话用户名都由 OCR 找到位置后走位点击，不预先登记坐标。
DEFAULT_REQUIRED_TARGETS: tuple[str, ...] = ()

TOUCH_REPORT_TIMEOUT_S = 6.0
TAP_DWELL_MAX_MS = 2000
# 标定完成后的 XY 走位速度；点击下压速度仍使用已校准的 tap.feed。
CALIBRATED_TRAVEL_FEED = 5000.0
# 布防→点击的前置等待：K40 校准页只在轮询(750ms)看到布防后才上报触点，
# 否则 ACTION_UP 被静默忽略（armed==null 直接 return）——点击必须落在
# 手机已看到布防之后，否则真实竞态会随机吞掉触点（现场 7 次中 6 次丢失）。
PHONE_ARM_LEAD_S = 1.3
COORDINATE_SPACE_VERSION = 2  # v2: Android target/touch both use root-local x/y


def _is_placeholder_ime_device_id(value: object) -> bool:
    """旧版本留下的示例 ID，不得作为真实手机配对目标。"""
    return str(value or "").strip().lower() in {"k40-01", "phone-demo"}


def _consensus_center(observations: list[dict]) -> tuple[float, float]:
    """多点观测的质心（标靶反算的每点共识位置，§6.2.6）。"""
    if not observations:
        raise ValueError("没有可用观测")
    return (sum(float(item["x"]) for item in observations) / len(observations),
            sum(float(item["y"]) for item in observations) / len(observations))




# ---------- 输入法校准会话 HTTP 客户端（协议与 K40TaskServer 一致） ----------

class ImeCalibrationClient:
    """8788 屏幕输入法服务的校准会话客户端；测试可注入同接口 fake。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8788", timeout: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        import urllib.error
        import urllib.request

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                raise ImeError(exc.code, payload.get("error", "unknown"),
                               payload.get("message", "")) from exc
            except (ValueError, UnicodeDecodeError):
                raise ImeError(exc.code, "http_error", f"HTTP {exc.code}") from exc
        except OSError as exc:
            raise ImeError(503, "ime_unreachable",
                           f"输入法服务不可达（{self.base_url}）：{exc}") from exc

    def create_session(self, device_id: str) -> dict:
        return self._request("POST", "/api/v1/calibrations", {"deviceId": device_id})

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/api/v1/calibrations/{session_id}")

    def create_task(self, payload: dict) -> dict:
        """下发输入法文字任务（验收用：让手机输入法显示候选文字）。"""
        return self._request("POST", "/api/v1/tasks", payload)

    def task_detail(self, task_id: str) -> dict:
        return self._request("GET", f"/api/v1/tasks/{task_id}")

    def cancel_task(self, task_id: str) -> dict:
        """取消任务（尽力而为）：失败不抛出，返回错误状态由调用方留痕。

        用于验收失败后的清理——残留的未完成任务会让下一次验收显示
        上一次的文本（队列卡死）。
        """
        try:
            return self._request("POST", f"/api/v1/tasks/{task_id}/cancel", {})
        except ImeError as exc:
            return {"status": "cancel_failed", "error": str(exc)}

    def wait_task_displayed(self, task_id: str, *, timeout: float = 8.0) -> dict:
        """轮询任务状态直到输入法回报已显示（displayed）。

        真实回报替代盲等：8788 不可达立即失败；停在 pending=手机没拉到任务；
        typing_failed/cancelled 提前终止。
        """
        deadline = time.time() + max(1.0, float(timeout))
        last = None
        while time.time() < deadline:
            try:
                detail = self.task_detail(task_id)
            except ImeError:
                raise  # 服务不可达：立即失败，不空转到超时
            status = ((detail or {}).get("task") or {}).get("status")
            last = status
            if status in ("displayed", "typing", "inserted"):
                return {"status": status, "waitedS": round(timeout - (deadline - time.time()), 2)}
            if status in ("typing_failed", "cancelled"):
                break
            time.sleep(0.4)
        raise ImeError(408, "task_not_displayed",
                       f"输入法任务未回报已显示（最后状态：{last or '未知'}）："
                       "请确认手机输入法在线且键盘已激活")

    def list_devices(self) -> list[dict]:
        """输入法服务已知设备（含心跳在线状态），用于人工核对配对。

        返回 [{"deviceId","online","keyboardActive","lastSeenAt",...}, ...]；
        8788 不可达时返回空列表（调用方据此提示）。
        """
        try:
            payload = self._request("GET", "/api/v1/devices")
        except ImeError:
            return []
        items = payload.get("items") if isinstance(payload, dict) else payload
        return [item for item in items or [] if isinstance(item, dict)]

    def arm(self, session_id: str, sample_id: str, sequence: int,
            target: dict | None = None) -> dict:
        body: dict = {"sampleId": sample_id, "sequence": sequence}
        if target is not None:
            body.update({"targetU": target["u"], "targetV": target["v"],
                         "targetWidthU": target["widthU"],
                         "targetHeightV": target["heightV"]})
        return self._request("POST", f"/api/v1/calibrations/{session_id}/arm", body)

    def disarm(self, session_id: str, sample_id: str, sequence: int | None = None) -> dict:
        body = {"sampleId": sample_id}
        if sequence is not None:
            body["sequence"] = sequence
        return self._request("POST", f"/api/v1/calibrations/{session_id}/disarm",
                             body)

    def complete(self, session_id: str) -> dict:
        return self._request("POST", f"/api/v1/calibrations/{session_id}/complete")

    def cancel(self, session_id: str) -> dict:
        return self._request("POST", f"/api/v1/calibrations/{session_id}/cancel")

    def fetch_gesture_frame_metadata(self, ime_device_id: str,
                                     session_id: str) -> dict | None:
        """读取旧校准页缓存的坐标桥元数据（只读，规范第 4.2/22.4 节）。

        GET /api/v1/gesture-protocol/legacy-metadata；404（无同 run 元数据）
        或任何服务异常都返回 None——坐标桥旁档是尽力而为的增量能力，
        绝不让原机械校准失败。
        """
        from urllib.parse import quote

        path = ("/api/v1/gesture-protocol/legacy-metadata?deviceId="
                f"{quote(str(ime_device_id), safe='')}"
                f"&sessionId={quote(str(session_id), safe='')}")
        try:
            return self._request("GET", path)
        except ImeError:
            return None


# ---------- 向导 ----------



def build_gesture_coordinate_frame(bundle: dict, metadata: dict, *,
                                   ime_device_id: str, session_id: str) -> dict:
    """从同次校准的原始页面证据构造旁档；纯函数，不读写或操作硬件。"""
    if not isinstance(metadata, dict):
        raise WizardError("legacy metadata 缺失，不能重建坐标桥")
    page = metadata.get("page") or {}
    if (not ime_device_id or not session_id
            or metadata.get("deviceId") != ime_device_id
            or metadata.get("sessionId") != session_id
            or page.get("legacyCalibrationSessionId") != session_id
            or page.get("pageId") != "legacy-calibration"):
        raise WizardError("legacy metadata 设备、会话或原校准页面不匹配")
    if page.get("rotation") != 0 or page.get("coordinateSpaceVersion") != 2:
        raise WizardError("legacy metadata 坐标版本或屏幕方向不受支持")
    for field in ("rootWidthPx", "rootHeightPx", "displayWidthPx", "displayHeightPx"):
        if type(page.get(field)) is not int or page[field] <= 0:
            raise WizardError("legacy metadata 屏幕尺寸无效")
    origin = page.get("rootOriginDisplayPx")
    if (not isinstance(origin, list) or len(origin) != 2
            or any(type(value) is not int or value < 0 for value in origin)
            or origin[0] + page["rootWidthPx"] > page["displayWidthPx"]
            or origin[1] + page["rootHeightPx"] > page["displayHeightPx"]):
        raise WizardError("legacy metadata 校准根视图超出物理屏幕")
    density = page.get("density")
    if (type(density) not in (int, float) or not math.isfinite(density) or density <= 0
            or not isinstance(metadata.get("clientInstanceId"), str)
            or not metadata["clientInstanceId"].strip()):
        raise WizardError("legacy metadata 密度或客户端来源无效")
    frame = gp.build_coordinate_frame(
        binding=sg.binding_snapshot(bundle),
        calibration_run_id=(bundle.get("autoCalibration") or {}).get("runId") or "manual-finish",
        root_width_px=page["rootWidthPx"], root_height_px=page["rootHeightPx"],
        display_width_px=page["displayWidthPx"], display_height_px=page["displayHeightPx"],
        root_origin_display_px=origin, density=density,
        source_client_instance_id=metadata["clientInstanceId"])
    gp.validate_coordinate_frame(frame, bundle)
    return frame


class CalibrationWizard:
    """一台设备的统一校准向导（状态持久化于 data/devices/{device}/calibration_wizard.json）。"""

    def __init__(self, *, device: str, daemon, ime,
                 data_root: Path | None = None,
                 camera_fetch=None, tip_detector=None, edge_detector=None,
                 search_region_provider=None,
                 target_detector=None, ocr_engine=None, binding_facts=None,
                 touch_timeout: float = TOUCH_REPORT_TIMEOUT_S,
                 post_promote_hook=None,
                 watcher_interval: float = 0.5,
                 phone_arm_lead_s: float = PHONE_ARM_LEAD_S,
                 target_draw_wait_s: float = 1.2,
                 target_detect_timeout_s: float = 3.0,
                 target_geo_deadline_s: float = TARGET_GEO_DEADLINE_S,
                 allow_probe_fallback: bool = False,
                 target_candidates=None,
                 real_hardware: bool = True,
                 auto_config: dict | None = None,
                 position_read=None,
                 installation_lock_root=None):
        self.device = device
        self.daemon = daemon
        self._position_read = position_read
        self._position_context = threading.local()
        self.ime = ime
        self.data_root = Path(data_root) if data_root else cb.default_data_root()
        # R2-04（camera-binding 附件 §4.1）：安装锁根必须与帧桥/调试台同根派生；
        # 缺省回退 data_root（锁路径=data_root/installation_locks/{device_id}.lock）。
        self._installation_lock_root = (Path(installation_lock_root)
                                        if installation_lock_root else self.data_root)
        self._camera_fetch = camera_fetch
        self._tip_detector = tip_detector
        self._edge_detector = edge_detector
        # S2-06/07：本台生效粗框查询（返回 region 记录；legacy=None；新模式
        # 缺框/损坏抛 RegionContextError——校准首次定位与梯度回退均受框约束，
        # 上下文失效为 WizardError 终态，不得降为 H01 姿态提示）。
        self._search_region_provider = search_region_provider
        self._target_detector = target_detector
        self._target_candidates = target_candidates
        self._ocr_engine = ocr_engine
        self._binding_facts = binding_facts
        self._touch_timeout = touch_timeout
        self._phone_arm_lead_s = float(phone_arm_lead_s)
        self._target_draw_wait_s = float(target_draw_wait_s)
        # 检测预算下限 = 画靶等待 + 3s（实测 2026-09-03：布防→画面更新最长 ~0.9s，
        # 取流 ~8fps；固定 sleep 赌延时不可靠，改为"等画面真的变化"，见
        # _auto_detect_target_stable 的 stale_center 机制）
        self._target_detect_timeout_s = max(
            self._target_draw_wait_s + 3.0, float(target_detect_timeout_s))
        # F2 标靶反算总截止（§6.2.6 固定 15s；测试可注入更短预算）
        self._target_geo_deadline_s = max(0.5, float(target_geo_deadline_s))
        self._allow_probe_fallback = bool(allow_probe_fallback)
        self.post_promote_hook = post_promote_hook
        self._watcher_interval = float(watcher_interval)
        # v1.1 自动校准：真机默认 True——First Click 必须操作员口令确认；
        # fake/dry-run（测试注入）显式传 False。
        self._real_hardware = bool(real_hardware)
        cfg = dict(AUTO_CONFIG_DEFAULTS)
        if auto_config:
            cfg.update({k: v for k, v in auto_config.items() if v is not None})
        self._auto_cfg = cfg
        self._watcher_gen = 0  # 每次 start/cancel 递增，旧 watcher 线程自行退出
        self._lock = threading.RLock()
        # v1.1 自动校准 runner：容器/线程/锁已外迁 drivers/calibration_auto_runner.py
        # （Auto_CS#8/ADR-0003）；锁序恒为 self._lock → runner._auto_lock。
        self._auto_runner = AutoRunner(self)
        self._path = device_dir(device, self.data_root) / "calibration_wizard.json"
        # P02：向导文件损坏/schema 不支持时置位——本实例不得用空状态覆盖主档。
        self._state_unrecoverable = None
        self.state = self._load_state()
        self._auto_runner._auto_invalidate_stale_runs()
        # 服务重启后向导可能停在 precheck（状态从磁盘恢复，后台线程不存在）：
        # 会话仍在时补一个 watcher，保证手机页自动检测不因重启失效。
        if self.state.get("stage") == STAGE_PRECHECK and self.state.get("imeSessionId"):
            self._start_phone_watcher(self.state["imeSessionId"])

    def _read_position(self) -> dict:
        """统一只读位置入口；取消不作为失败，持续忙保留结构化原因。"""
        parking = getattr(self._position_context, "parking", False)
        cancel = self._auto_runner._auto["cancel"] if self._auto_runner._auto.get("run") and not parking else None
        if cancel is not None and cancel.is_set():
            raise _AutoCancelled()
        try:
            if self._position_read is None:
                return self.daemon.mpos()
            return self._position_read(
                cancel_event=cancel, **({"timeout": 0.5} if parking else {}))
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code == "status_read_cancelled":
                raise _AutoCancelled() from exc
            if code in {"status_read_busy", "daemon_command_timeout",
                        "daemon_unreachable", "device_context_mismatch"}:
                raise AutoRunError(code, str(exc), classification="hardware_investigation",
                                   park=code == "status_read_busy") from exc
            raise


    # ---------- 持久化 ----------

    def _load_state(self) -> dict:
        if self._path.exists():
            raw = None
            try:
                raw = self._path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("schemaVersion") == 1:
                    data.setdefault("z", {})
                    data.setdefault("xy", {})
                    data.setdefault("geometry", {})
                    data.setdefault("targets", {})
                    data.setdefault("binding", None)
                    data.setdefault("safePark", self._blank_safe_park())
                    data.setdefault("sessionRebind", self._blank_session_rebind())
                    # Auto_CS#20：历史会话无 goal 字段——一律视为运动档（原语义）
                    data.setdefault("goal", GOAL_MOTION)
                    rebind = data["sessionRebind"]
                    if not isinstance(rebind, dict):
                        data["sessionRebind"] = self._blank_session_rebind()
                    else:
                        for key, value in self._blank_session_rebind().items():
                            rebind.setdefault(key, value)
                    version = int(data.get("coordinateSpaceVersion") or 1)
                    if version < COORDINATE_SPACE_VERSION and data.get("stage") != STAGE_IDLE:
                        data["stage"] = STAGE_INVALIDATED
                        data["invalidReason"] = {
                            "code": "coordinate_space_upgraded",
                            "message": ("Android 触点回报已从 rawX/rawY 改为校准页本地 x/y；"
                                        "旧 XY/视觉映射含系统窗口偏移，必须重新校准"),
                            "at": _now(),
                        }
                    data["coordinateSpaceVersion"] = COORDINATE_SPACE_VERSION
                    if _is_placeholder_ime_device_id(data.get("imeDeviceId")):
                        data["imeDeviceId"] = None
                    return data
            except Exception:
                pass
            # P02（规范 §5.2.3）：向导文件存在但不可解析（截断 JSON/空文件/
            # schema 不支持）。不得静默生成空状态后覆盖已有主档；先留证据
            # 副本，工作会话标记为不可恢复——已保存校准资料保留。
            self._state_unrecoverable = {
                "code": "wizard_state_corrupt",
                "message": ("向导工作会话文件不可解析（损坏或 schema 不支持）："
                            "工作会话不可恢复，已保存校准资料保留"),
                "at": _now(),
            }
            try:
                stamp = _now().replace(":", "").replace("-", "").replace("+", "_")
                backup = self._path.with_name(f"calibration_wizard.corrupt-{stamp}.json")
                backup.write_bytes((raw or "").encode("utf-8", errors="replace")
                                   if isinstance(raw, str) else self._path.read_bytes())
            except Exception:
                pass
        return self._blank_state()

    @staticmethod
    def _blank_safe_park() -> dict:
        """SAFE PARK 默认全部 null（§17.1：禁止任何看似真实的示例值）。"""
        return {"parkX": None, "parkY": None, "parkZ": None,
                "confirmed": False, "source": None, "confirmedAt": None,
                "mposAtConfirm": None}

    @staticmethod
    def _blank_session_rebind() -> dict:
        """当前会话的执行资格；校准证据本身不因会话重建而丢失。

        资格按 Z / XY / 停车位三个独立条件分别复核（规范 §5.4）：一次
        Z 触点确认只恢复 Z；停车位按其自身确认会话核对；三项全部复核
        完成后 required 才回到 False。
        """
        return {
            "required": False,
            "reason": None,
            "previousDaemonSession": None,
            "previousImeSessionId": None,
            "requestedAt": None,
            "validatedAt": None,
            "validatedDaemonSession": None,
            "validatedImeSessionId": None,
            "pendingScopes": {"z": False, "xy": False, "park": False},
        }

    @staticmethod
    def _blank_state() -> dict:
        return {
            "schemaVersion": 1,
            "coordinateSpaceVersion": COORDINATE_SPACE_VERSION,
            "stage": STAGE_IDLE,
            # 目标档位（Auto_CS#20）：motion=动作校准（原语义零变化）；
            # observation=纯观测校准（零机械动作，产出 observation_ready）。
            "goal": GOAL_MOTION,
            # 必须由调试台从手机心跳清单中选择真实 ID，不能使用示例值。
            "imeDeviceId": None,
            "imeSessionId": None,
            "daemonSession": None,
            "binding": None,
            "invalidReason": None,
            "screen": None,  # 首个触点回报建立基线 {orientation, screenWidth, screenHeight}
            "safePark": CalibrationWizard._blank_safe_park(),
            "z": {"hoverZ": None, "autoSafe": None, "hoverEvidence": None, "trials": [],
                  "tap": None, "delete": None, "tapOkStreak": 0,
                  "tapAttempts": 0, "deleteAttempts": 0},
            "xy": {"accepted": [], "trials": [], "fit": None, "validation": [],
                   "preview": None},
            "geometry": {"edge": None, "homography": None},
            "targets": {},
            "acceptance": {"debug": {}, "wechat": {}},
            "sessionRebind": CalibrationWizard._blank_session_rebind(),
            "updatedAt": _now(),
        }

    def _save_state(self) -> None:
        self.state["updatedAt"] = _now()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.{uuid.uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(self._path)
        finally:
            tmp.unlink(missing_ok=True)

    # ---------- 基础工具 ----------

    def _bundle(self) -> dict:
        return cb.load_bundle(self.device, self.data_root) or cb.new_bundle(self.device)

    @staticmethod
    def _bundle_has_saved_parameters(bundle: dict) -> bool:
        """主档是否已保存任何校准参数（P01：空向导不得覆盖这些内容）。"""
        z = bundle.get("z") or {}
        xy = bundle.get("xy") or {}
        park = bundle.get("safePark") or {}
        screen = bundle.get("screen") or {}
        return bool(
            z.get("hoverZ") is not None
            or z.get("zSafe") is not None
            or xy.get("robotToScreen")
            or park.get("parkX") is not None
            or (screen.get("edgeModel") or {}).get("corners")
            or bundle.get("targets"))

    def _state_is_blank(self) -> bool:
        """向导状态是否为「尚未产生任何校准产物」的空白会话。"""
        state = self.state
        z = state.get("z") or {}
        xy = state.get("xy") or {}
        geo = state.get("geometry") or {}
        park = state.get("safePark") or {}
        return (state.get("stage") == STAGE_IDLE
                and not state.get("binding")
                and z.get("hoverZ") is None and not z.get("autoSafe")
                and not z.get("trials") and not z.get("tap")
                and not xy.get("fit") and not xy.get("accepted")
                and not geo.get("edge") and not geo.get("homography")
                and park.get("parkX") is None)

    def _sync_bundle(self, *, observation_rebuild: bool = False) -> dict:
        """把向导产物写入统一档案（status 保持 draft，finish 才置 ready）。

        ready 档案（已提升）不做整包重建：state 是手动流程视角，重建会冲掉
        promote 写入的几何/Z 参数/模型锁/验收标志。ready 时只做验收字段级
        合并历史兼容记录，其余一律保留档案现值；微信记录不参与门禁。
        observation_rebuild（Auto_CS#20）：纯观测 finish 的显式一次性重建——
        磁盘旧档（含 ready/motion_ready/draft）不并入观测档案，以空白档案
        为底；几何与绑定只认本会话证据（finish 已校验 edge+homography 在手）。
        边界（#20 评审实测）：同一向导会话先跑过运动档再 start(goal=observation)
        时，preserved state 里的旧 z/xy/safePark/targets 会随本函数尾段的
        state 回填复制进观测档案（磁盘旧档隔离不等于 state 清洗）。这不构成
        动作风险：observation_ready 被运动门禁双重独立拒绝
        （validate_calibration_bundle 判 bundle_not_ready + daemonSession=None
        判 daemon_session_unbound），档案仅供纯读使用。
        """
        current = cb.load_bundle(self.device, self.data_root)
        rebuild_base = None
        if observation_rebuild:
            current = None
            rebuild_base = cb.new_bundle(
                self.device, ime_device_id=self.state.get("imeDeviceId"))
        if current is not None and current.get("status") == cb.MOTION_READY_STATUS:
            # 补 E 只追加验收记录；自动校准模型不能被手动 seed 覆盖。
            merged = json.loads(json.dumps(current, ensure_ascii=False))
            merged["daemonSession"] = self.state.get("daemonSession")
            merged["sessionRebind"] = deepcopy(
                self.state.get("sessionRebind") or self._blank_session_rebind())
            merged["acceptance"]["checks"] = [
                {"kind": kind, "check": check, **record}
                for kind, records in self.state["acceptance"].items()
                if kind in {"debug", "wechat"}
                for check, record in records.items()
            ]
            if self.state["stage"] == STAGE_INVALIDATED:
                merged["status"] = "invalidated"
            elif merged["sessionRebind"].get("required"):
                # 保留 ready 档案的几何、模型和历史，但不能把旧会话资格
                # 当作新会话的执行资格。
                merged["status"] = "draft"
            return cb.save_bundle(merged, self.data_root)
        if current is not None and current.get("status") == "ready":
            merged = json.loads(json.dumps(current, ensure_ascii=False))
            acc = dict(merged.get("acceptance") or {})
            debug_ok = bool(acc.get("debug") or self._acceptance_done("debug"))
            wechat_ok = bool(acc.get("wechat") or self._acceptance_done("wechat"))
            merged["acceptance"] = {**acc, "debug": debug_ok, "wechat": wechat_ok,
                                    "lastRunAt": _now(),
                                    "deferred": {**(acc.get("deferred") or {}),
                                                 "wechat": not wechat_ok}}
            return cb.save_bundle(merged, self.data_root)
        if current is not None and self._state_unrecoverable is not None:
            # P02：向导工作会话不可恢复——已保存校准资料保留，不以空状态
            # 重建主档（规范 §5.2.3：显示"工作会话不可恢复"，不静默覆盖）。
            return current
        if (current is not None and self._state_is_blank()
                and self._bundle_has_saved_parameters(current)):
            # P01：主档已有保存参数而向导状态为空白——保留主档全部内容，
            # 空草稿不得重建主档（规范 §5.2.1）。历史参数的恢复复核走
            # sessionRebind 机制，不由空白状态背书。
            return current
        bundle = rebuild_base if rebuild_base is not None else self._bundle()
        bundle["imeDeviceId"] = self.state["imeDeviceId"]
        bundle["daemonSession"] = self.state["daemonSession"]
        bundle["sessionRebind"] = deepcopy(
            self.state.get("sessionRebind") or self._blank_session_rebind())
        binding = self.state.get("binding") or {}
        if binding:
            bundle["deviceId"] = self.device
            bundle["cameraId"] = binding["cameraId"]
            bundle["cameraDeviceUniqueId"] = binding["cameraDeviceUniqueId"]
            bundle["bindingRevision"] = int(binding["bindingRevision"])
            bundle["daemonAddr"] = binding["daemonAddr"]
            bundle["frame"] = {
                "width": int(binding["frameWidth"]),
                "height": int(binding["frameHeight"]),
            }
            bundle["bindingFingerprint"] = cb.binding_fingerprint(
                device_id=self.device,
                camera_id=binding["cameraId"],
                camera_device_unique_id=binding["cameraDeviceUniqueId"],
                daemon_addr=binding["daemonAddr"],
                frame_width=int(binding["frameWidth"]),
                frame_height=int(binding["frameHeight"]),
            )
            # FIX-07：可信绑定携带安装 UUID/来源几何身份时产出 v3 校准——
            # 身份只取自绑定事实，不从请求任意填值；缺身份保持 v2（不得
            # 声称 v3，也不得补字段升级续用）。
            for key in ("cameraBindingId", "sourceGeometryId"):
                if binding.get(key):
                    bundle[key] = str(binding[key])
            if all(str(bundle.get(key) or "").strip()
                   for key in cb.V3_REQUIRED_IDENTITY_FIELDS):
                bundle["schemaVersion"] = cb.BUNDLE_SCHEMA_VERSION_V3
        if self.state.get("screen"):
            bundle["screen"].update(self.state["screen"])
        # v1.1 自动校准：SAFE PARK 与 zSafe 只经 operator_confirmed 写入（§16/§17）
        bundle["safePark"] = dict(self.state.get("safePark") or self._blank_safe_park())

        z = self.state["z"]
        bz = bundle["z"]
        auto_safe = z.get("autoSafe") or {}
        if auto_safe.get("zSafe") is not None and auto_safe.get("validated"):
            bz["zSafe"] = round(float(auto_safe["zSafe"]), 3)
        if z.get("hoverZ") is not None:
            bz["hoverZ"] = round(float(z["hoverZ"]), 3)
        tap = z.get("tap") or {}
        for key, src in (("tapDepth", "depth"), ("tapFeed", "feed"), ("tapDwellMs", "dwellMs")):
            if src in tap:
                bz[key] = tap[src]
        delete = z.get("delete") or {}
        bz["deleteHoldMs"] = delete.get("holdMs", bz.get("deleteHoldMs"))
        bz["deleteDepth"] = delete.get("depth", bz.get("deleteDepth"))
        bz["deleteFeed"] = delete.get("feed", bz.get("deleteFeed"))
        bz["validated"] = bool(tap.get("validated"))
        bz["deleteValidated"] = bool(delete.get("validated"))

        xy = self.state["xy"]
        if xy.get("fit"):
            fit = xy["fit"]
            update = {
                "robotToScreen": {k: fit[k] for k in ("a", "b", "c", "d", "e", "f")},
                "samples": xy["accepted"],
                "validation": xy["validation"],
                "validated": bool(xy.get("validationPassed")),
            }
            # 质量键缺失（deferred promote 只写系数字典）时保留档案现值
            for key in ("residualRmsPx", "maxResidualPx"):
                if key in fit:
                    update[key] = fit[key]
            bundle["xy"].update(update)
        geo = self.state["geometry"]
        if geo.get("edge"):
            bundle["screen"]["edgeModel"] = geo["edge"]
        if geo.get("homography"):
            h = geo["homography"]
            bundle["camera"] = {"cameraToScreen": {
                "homography": h["homography"], "reprojectionRmsPx": h["reprojectionRmsPx"],
            }, "reprojectionRmsPx": h["reprojectionRmsPx"], "validated": True}
            # 5 点视觉验证（摄像头判定→点击 全链路）结果随档案留痕
            verify = self.state.get("visualVerify")
            if verify:
                bundle["camera"]["visualVerify"] = {
                    k: verify[k] for k in ("checkedAt", "contactVsNominalMaxPx",
                                           "contactVsCameraMaxPx", "passedCount", "passed")}
        for name, entry in self.state["targets"].items():
            bundle["targets"][name] = {**entry, "name_display": TARGET_DISPLAY_NAMES.get(name, name)}
        acceptance = self.state["acceptance"]
        checks = [{"kind": kind, "check": check, **rec}
                  for kind in ("debug", "wechat")
                  for check, rec in (acceptance.get(kind) or {}).items()]
        # 自动 run 的 ✓ 记录在 run["acceptance"]（手动流程在 state）：
        # 两处任一完成即视为该类验收完成（2026-09-04：应用项可延后，未完成时
        # wechat=False 保持发送门禁关闭）
        run_acceptance = ((getattr(self, "_auto", None) or {}).get("run") or {}).get("acceptance") or {}

        def _kind_done(kind, checks_tuple):
            recorded = run_acceptance.get(kind) or {}
            if all((recorded.get(c) or {}).get("ok") for c in checks_tuple):
                return True
            return self._acceptance_done(kind)

        bundle["acceptance"] = {
            "debug": _kind_done("debug", DEBUG_ACCEPTANCE_CHECKS),
            "wechat": _kind_done("wechat", WECHAT_ACCEPTANCE_CHECKS),
            "lastRunAt": acceptance.get("lastRunAt"),
            "checks": checks,
        }
        if self.state["stage"] == STAGE_INVALIDATED:
            bundle["status"] = "invalidated"
        elif bundle["sessionRebind"].get("required") \
                and bundle.get("status") == cb.MOTION_READY_STATUS:
            bundle["status"] = "draft"
        return cb.save_bundle(bundle, self.data_root)

    def _acceptance_done(self, kind: str) -> bool:
        required = DEBUG_ACCEPTANCE_CHECKS if kind == "debug" else WECHAT_ACCEPTANCE_CHECKS
        recorded = self.state["acceptance"].get(kind) or {}
        return all(recorded.get(check, {}).get("ok") for check in required)

    def _require_stage(self, *stages: str) -> str:
        stage = self.state["stage"]
        if stage not in stages:
            raise WizardError(f"当前阶段为「{STAGE_DISPLAY.get(stage, stage)}」，"
                              f"该操作要求阶段：{'、'.join(STAGE_DISPLAY.get(s, s) for s in stages)}")
        return stage

    def _require_no_auto_run(self, action: str = "该操作") -> None:
        """自动校准 run 活动期间禁止手动运动/会话类操作（连带审查修复）。

        runner 在后台独占机械臂与手机布防序列；手动点击/走位/验收与 runner
        交错会导致布防错配、样本污染甚至两路运动指令交织。调用方已持有 self._lock。
        """
        with self._auto_runner._auto_lock:
            run = self._auto_runner._auto["run"]
            if run and run.get("state") in AUTO_ACTIVE_STATES:
                raise WizardError(f"自动校准运行中（{run['runId']}，{run['state']}）："
                                  f"请先停止自动校准再执行{action}")

    def _has_persistent_calibration(self) -> bool:
        """是否已有需要跨会话保留、但不能直接复用的校准资料。"""
        state = self.state
        if state.get("stage") not in (STAGE_IDLE, STAGE_PRECHECK):
            return True
        z = state.get("z") or {}
        xy = state.get("xy") or {}
        geometry = state.get("geometry") or {}
        if any(z.get(key) not in (None, {}, []) for key in (
                "hoverZ", "hoverEvidence", "autoSafe", "tap", "delete", "trials")):
            return True
        if any(xy.get(key) not in (None, {}, []) for key in (
                "accepted", "trials", "fit", "validation")):
            return True
        if any(geometry.get(key) not in (None, {}, []) for key in ("edge", "homography")):
            return True
        if state.get("safePark", {}).get("confirmed") \
                or state.get("targets") or state.get("acceptance", {}).get("debug") \
                or state.get("acceptance", {}).get("wechat"):
            return True
        try:
            bundle = cb.load_bundle(self.device, self.data_root) or {}
            return bundle.get("status") in {
                cb.MOTION_READY_STATUS, "invalidated"
            } or any((bundle.get("modelLocks") or {}).get(name, {}).get("state")
                     == cb.MODEL_LOCK_LOCKED
                     for name in ("cameraToScreen", "armAffine"))
        except Exception:
            return False

    def _mark_session_rebind(self, reason: str, *,
                             daemon_session: str | None = None,
                             ime_session_id: str | None = None) -> None:
        """撤销当前会话资格，但保留校准证据、参数和候选历史。"""
        old = self.state.get("sessionRebind") or self._blank_session_rebind()
        previous_daemon = old.get("previousDaemonSession")
        previous_ime = old.get("previousImeSessionId")
        if not old.get("required"):
            previous_daemon = self.state.get("daemonSession") \
                if (reason == "daemon_session_changed"
                    or self.state.get("daemonSession") != daemon_session) else None
            previous_ime = self.state.get("imeSessionId") \
                if (reason.startswith("ime_session")
                    or self.state.get("imeSessionId") != ime_session_id) else None
        rebind = deepcopy(self._blank_session_rebind())
        rebind.update(old)
        rebind.update({
            "required": True,
            "reason": reason,
            "previousDaemonSession": previous_daemon,
            "previousImeSessionId": previous_ime,
            "requestedAt": old.get("requestedAt") or _now(),
            "validatedAt": None,
            "validatedDaemonSession": None,
            "validatedImeSessionId": None,
            # 坐标基准变化使 Z / XY / 停车位三项资格同时失效；
            # 之后只能按各自复核入口逐项恢复（规范 §5.4）。
            "pendingScopes": {"z": True, "xy": True, "park": True},
        })
        self.state["sessionRebind"] = rebind
        if daemon_session:
            self.state["daemonSession"] = daemon_session
        # phonePose 是当前会话的空间证据，不能被新会话误当成复核结果；
        # trials/hoverEvidence 等历史仍完整保留。
        self.state.setdefault("z", {})["phonePose"] = None
        if self.state.get("stage") not in (STAGE_IDLE, STAGE_PRECHECK):
            self.state["stage"] = STAGE_PRECHECK
        self._revoke_candidate_execution(previous_daemon, previous_ime, reason)
        try:
            self._sync_bundle()
        except Exception:
            pass
        self._save_state()

    def _clear_session_rebind(self, scope: str | None = None) -> None:
        """复核通过后清偿资格。scope 指定只恢复哪一项（z/xy/park）；
        三项全部恢复后 required 才置 False。scope=None 保持旧行为整体清空
        （仅用于不区分资格的既有调用方）。"""
        rebind = deepcopy(self.state.get("sessionRebind")
                          or self._blank_session_rebind())
        scopes = dict(rebind.get("pendingScopes")
                      or self._blank_session_rebind()["pendingScopes"])
        if scope is None:
            scopes = {key: False for key in scopes}
        elif scope in scopes:
            scopes[scope] = False
        rebind["pendingScopes"] = scopes
        if not any(scopes.values()):
            rebind.update({
                "required": False,
                "reason": None,
                "validatedAt": _now(),
                "validatedDaemonSession": self.state.get("daemonSession"),
                "validatedImeSessionId": self.state.get("imeSessionId"),
            })
        self.state["sessionRebind"] = rebind

    def _require_park_session_current(self, park: dict, *, action: str) -> None:
        """停车位走位前置：按该停车位自己的确认会话核对（规范 §5.4/P09）。

        顶层 daemonSession 更新不能替旧停车位重新背书；旧档停车位未记录
        自身会话时返回 True 由调用方走既有顶层会话兜底。
        """
        confirmed_session = park.get("daemonSession")
        if not confirmed_session:
            return
        try:
            status = self.daemon.status()
        except Exception as exc:
            raise WizardError(f"{action}：无法确认机械臂会话（{exc}），禁止停车走位")
        current = status.get("session") if status.get("connected") else None
        if current and confirmed_session != current:
            raise WizardError(
                f"{action}：停车位确认于机械坐标会话 {confirmed_session}，"
                f"当前会话 {current}——坐标基准可能已变，请重新确认 SAFE PARK")

    def _revoke_candidate_execution(self, daemon_session: str | None,
                                    ime_session_id: str | None,
                                    reason: str) -> None:
        """候选文件只追加失资格留痕，不删除历史或模型。"""
        try:
            root = cb.candidates_dir(self.device, self.data_root)
            if not root.exists():
                return
            for path in sorted(root.glob("*.json")):
                try:
                    candidate = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(candidate, dict):
                    continue
                same_daemon = daemon_session is not None \
                    and candidate.get("daemonSession") == daemon_session
                same_ime = ime_session_id is not None \
                    and candidate.get("phoneSessionId") == ime_session_id
                if not (same_daemon or same_ime):
                    continue
                candidate["executionEligibility"] = {
                    "eligible": False,
                    "reason": "session_rebind_required",
                    "detail": reason,
                    "revokedAt": _now(),
                }
                if candidate.get("state") in AUTO_ACTIVE_STATES:
                    candidate["state"] = AUTO_STATE_ABORTED
                    candidate["lastFailure"] = {
                        "code": "session_rebind_required",
                        "message": "校准会话已变化，旧 candidate 停止执行（历史保留）",
                        "at": _now(),
                    }
                candidate.setdefault("events", []).append({
                    "ts": _now(), "event": "session_execution_revoked",
                    "detail": {"reason": reason},
                })
                cb.save_candidate(candidate, self.data_root)
        except Exception:
            pass

    def _require_session(self, *, allow_rebind: bool = False,
                         scope: str | None = None) -> dict:
        """检查设备事实；会话变化保留资料并撤销旧执行资格。

        scope 指明本动作依赖哪项资格（"z"/"xy"/"park"）：会话复核期只拦
        该项未恢复的动作。scope=None（默认）为严格模式，任一项待复核即拦
        （走位/验收/自动校准等涉及 XY 的动作）。
        """
        status = self.daemon.status()
        if not status.get("connected") or not status.get("session"):
            raise WizardError("机械臂未连接：请先在调试台连接设备")
        bound = self.state.get("daemonSession")
        if bound and bound != status["session"]:
            self._mark_session_rebind(
                "daemon_session_changed",
                daemon_session=status["session"],
                ime_session_id=self.state.get("imeSessionId"))
            if not allow_rebind:
                raise WizardError("daemon 会话已变化：校准参数已保留，"
                                  "请重新完成一次点击参数复核")
        rebind = self.state.get("sessionRebind") or {}
        if rebind.get("required") and not allow_rebind:
            pending = rebind.get("pendingScopes") or {}
            if scope is None or pending.get(scope, True):
                pending_desc = "、".join(
                    key.upper() for key, value in sorted(pending.items()) if value
                ) or "点击参数"
                raise WizardError(
                    f"当前会话尚未完成重新复核（待复核：{pending_desc}）："
                    f"请分别完成对应确认（Z 点击复核 / SAFE PARK 确认 / XY 重新验证）")
        self._require_binding()
        return status

    def _read_binding_facts(self) -> dict | None:
        if self._binding_facts is None:
            return self.state.get("binding")
        facts = self._binding_facts()
        required = ("cameraId", "cameraDeviceUniqueId", "bindingRevision",
                    "daemonAddr", "frameWidth", "frameHeight")
        missing = [key for key in required if facts.get(key) in (None, "")]
        if missing:
            raise WizardError(f"设备绑定事实不完整：{', '.join(missing)}")
        normalized = {key: facts[key] for key in required}
        normalized["bindingRevision"] = int(normalized["bindingRevision"])
        normalized["frameWidth"] = int(normalized["frameWidth"])
        normalized["frameHeight"] = int(normalized["frameHeight"])
        if normalized["frameWidth"] <= 0 or normalized["frameHeight"] <= 0:
            raise WizardError("摄像头帧尺寸无效：必须先获得实时画面")
        # FIX-07：v3 安装/来源几何身份——可信绑定提供时随事实透传（不可信
        # 来源缺省即缺失，不从请求补造）；保存/提升链路据此产出 v3。
        for key in ("cameraBindingId", "sourceGeometryId", "sourceGenerationId"):
            value = facts.get(key)
            if isinstance(value, str) and value.strip():
                normalized[key] = value.strip()
        return normalized

    def _require_binding(self) -> dict | None:
        current = self._read_binding_facts()
        bound = self.state.get("binding")
        if bound and current and any(str(bound.get(key)) != str(current.get(key)) for key in (
                "cameraId", "cameraDeviceUniqueId", "bindingRevision",
                "daemonAddr", "frameWidth", "frameHeight")):
            self._invalidate("binding_fingerprint_changed",
                             "机械臂、摄像头、daemon 或帧尺寸绑定已经变化，必须重新校准")
            raise WizardError("设备绑定已经变化，统一校准已失效：请重新开始校准")
        return current

    def _require_ime_open(self) -> dict:
        if not self.state.get("imeSessionId"):
            raise WizardError("尚未创建校准会话：请先开始校准")
        try:
            session = self.ime.get_session(self.state["imeSessionId"])
        except ImeError as exc:
            if exc.code == "calibration_not_found":
                self._mark_session_rebind(
                    "ime_session_lost",
                    daemon_session=self.state.get("daemonSession"),
                    ime_session_id=self.state.get("imeSessionId"))
                raise WizardError("输入法校准会话丢失：校准参数已保留，"
                                  "请重新开始校准") from exc
            raise WizardError(f"校准会话异常：{exc.message}") from exc
        if session.get("sessionId") != self.state.get("imeSessionId"):
            self._mark_session_rebind(
                "ime_session_changed",
                daemon_session=self.state.get("daemonSession"),
                ime_session_id=self.state.get("imeSessionId"))
            raise WizardError("输入法校准会话已变化：校准参数已保留，"
                              "请重新开始校准")
        if session.get("status") != "active":
            self._mark_session_rebind(
                "ime_session_lost",
                daemon_session=self.state.get("daemonSession"),
                ime_session_id=self.state.get("imeSessionId"))
            raise WizardError("校准会话已结束：校准参数已保留，请重新开始校准")
        if session.get("clientState") != "open":
            raise WizardError("手机校准页未打开：请在手机上打开 K40 输入法「屏幕校准」页")
        return session

    def _invalidate(self, code: str, message: str) -> None:
        self.state["stage"] = STAGE_INVALIDATED
        self.state["invalidReason"] = {"code": code, "message": message, "at": _now()}
        try:
            self._sync_bundle()
        except Exception:
            pass
        self._save_state()

    def _advance(self, stage: str) -> None:
        self.state["stage"] = stage
        self._sync_bundle()
        self._save_state()

    def _advance_after_phone_open(self, session: dict | None) -> None:
        """手机校准页 open 后的阶段推进（watcher/wait_phone_open/state_view 兜底共用）。

        运动档 → Z 阶段（原语义零变化）；纯观测档（Auto_CS#20）→ 直接进入
        屏幕几何阶段：屏幕坐标域取本会话校准页 open 上报（与 F2 §6.2.1 同一
        信任源，禁止猜测旧尺寸）；元数据缺失不推进（留在 precheck，由主动
        诊断入口给出针对性提示）。
        """
        if self.state.get("goal") == GOAL_OBSERVATION:
            screen = (session or {}).get("screen") or {}
            width, height = screen.get("screenWidth"), screen.get("screenHeight")
            orientation = screen.get("orientation")
            if not (isinstance(width, int) and not isinstance(width, bool) and width > 0
                    and isinstance(height, int) and not isinstance(height, bool)
                    and height > 0
                    and isinstance(orientation, str) and orientation.strip()):
                return
            self.state["screen"] = {"orientation": orientation,
                                    "screenWidth": width, "screenHeight": height}
            self._advance(STAGE_SCREEN_GEOMETRY)
            return
        self._advance(STAGE_Z_READY)

    def _check_screen_consistency(self, sample: dict) -> None:
        """触点回报的方向/尺寸与基线不符 → 失效（§5/§14）。"""
        orientation = sample.get("orientation") or "portrait"
        width = int(sample.get("screenWidth") or 0)
        height = int(sample.get("screenHeight") or 0)
        baseline = self.state.get("screen")
        if baseline is None:
            self.state["screen"] = {"orientation": orientation,
                                    "screenWidth": width, "screenHeight": height}
            return
        if baseline.get("orientation") != orientation:
            self._invalidate("screen_orientation_changed",
                             f"屏幕方向变化：{baseline.get('orientation')} → {orientation}")
            raise WizardError("屏幕方向变化，统一校准已失效")
        if baseline.get("screenWidth") != width or baseline.get("screenHeight") != height:
            self._invalidate("screen_size_changed",
                             f"屏幕有效尺寸变化：{baseline.get('screenWidth')}x{baseline.get('screenHeight')}"
                             f" → {width}x{height}")
            raise WizardError("屏幕尺寸变化，统一校准已失效")

    # ---------- 生命周期 ----------

    def start(self, ime_device_id: str | None = None, *,
              goal: str | None = None) -> dict:
        with self._lock:
            self._require_no_auto_run("重新开始校准")
            if goal not in (None, GOAL_MOTION, GOAL_OBSERVATION):
                raise WizardError(
                    f"未知校准目标档位：{goal}"
                    f"（可选：{GOAL_DISPLAY[GOAL_MOTION]}、{GOAL_DISPLAY[GOAL_OBSERVATION]}）")
            # Auto_CS#20：goal 缺省沿用当前会话档位（观测会话重建不悄悄翻回运动档）。
            observation = (goal == GOAL_OBSERVATION
                           or (goal is None
                               and self.state.get("goal") == GOAL_OBSERVATION))
            status = None
            if not observation:
                status = self.daemon.status()
                if not status.get("connected") or not status.get("session"):
                    raise WizardError("机械臂未连接：请先在调试台连接设备再开始校准")
            binding = self._read_binding_facts()
            ime_device = str(ime_device_id or self.state.get("imeDeviceId") or "").strip()
            # 只禁止空值自动兜底；显式传入的设备 ID 原样使用，不能按格式猜测真假。
            if not ime_device:
                raise WizardError("未选择真实输入法设备 ID：请刷新列表并选择手机 App 心跳上报的 ID")
            previous = deepcopy(self.state)
            previous_daemon = previous.get("daemonSession")
            previous_ime = previous.get("imeSessionId")
            preserve = self._has_persistent_calibration()
            self._revoke_candidate_execution(
                previous_daemon, previous_ime, "manual_session_restart")
            # 重新开始：旧会话仍在时先取消，避免设备侧 calibration_active 冲突
            old_sid = previous_ime
            if old_sid:
                try:
                    self.ime.cancel(old_sid)
                except ImeError:
                    pass
            session = self.ime.create_session(ime_device)
            preserved = previous
            preserved["stage"] = STAGE_PRECHECK
            preserved["goal"] = GOAL_OBSERVATION if observation else GOAL_MOTION
            preserved["imeDeviceId"] = ime_device
            preserved["imeSessionId"] = session["sessionId"]
            preserved["daemonSession"] = status["session"] if status else None
            preserved["binding"] = binding
            preserved.pop("screenSearchContext", None)
            preserved["invalidReason"] = None
            preserved["phoneOpenedAt"] = None
            preserved["imeSequence"] = 0
            if preserve:
                preserved["sessionRebind"] = {
                    **self._blank_session_rebind(),
                    "required": True,
                    "reason": "manual_session_restart",
                    "previousDaemonSession": previous_daemon,
                    "previousImeSessionId": previous_ime,
                    "requestedAt": _now(),
                    # 会话重建使 Z/XY/停车三项资格同时待复核（规范 §5.4）。
                    "pendingScopes": {"z": True, "xy": True, "park": True},
                }
                preserved.setdefault("z", {})["phonePose"] = None
            else:
                preserved["sessionRebind"] = self._blank_session_rebind()
            self.state = preserved
            # 操作员显式重新开始：工作会话已重建，损坏标记解除
            # （此前期间主档未被空状态触碰）。
            self._state_unrecoverable = None
            self._sync_bundle()  # 立即落一份保留几何/历史的档案
            bundle = self._bundle()
            bundle["status"] = "draft"  # 新一轮校准：invalidated/ready 都回到 draft
            bundle["daemonSession"] = status["session"] if status else None
            bundle["imeSessionId"] = session["sessionId"]
            bundle["sessionRebind"] = deepcopy(self.state["sessionRebind"])
            cb.save_bundle(bundle, self.data_root)
            self._save_state()
            self._start_phone_watcher(session["sessionId"])
            return self.state_view()

    def _start_phone_watcher(self, session_id: str) -> None:
        """后台检测手机校准页回报 open：页面先开或后开都能自动进入 Z 阶段。

        轮询在锁外进行（不阻塞其他请求）；发现 open 后拿锁推进。
        stage 离开 precheck 或 generation 变化（再次 start/cancel）时线程退出。
        """
        self._watcher_gen += 1
        gen = self._watcher_gen

        def watch() -> None:
            while True:
                time.sleep(self._watcher_interval)
                if gen != self._watcher_gen:
                    return
                with self._lock:
                    if self.state["stage"] != STAGE_PRECHECK:
                        return
                    if self.state.get("imeSessionId") != session_id:
                        return
                try:
                    session = self.ime.get_session(session_id)
                except ImeError as exc:
                    if exc.code == "calibration_not_found":
                        # 会话是内存态：输入法服务重启即永久丢失（§14），明确失效并停止轮询
                        with self._lock:
                            if (gen == self._watcher_gen
                                    and self.state["stage"] == STAGE_PRECHECK
                                    and self.state.get("imeSessionId") == session_id):
                                self._mark_session_rebind(
                                    "ime_session_lost",
                                    daemon_session=self.state.get("daemonSession"),
                                    ime_session_id=session_id)
                        return
                    continue  # 其他瞬时错误：继续等
                except Exception:
                    continue  # 服务瞬时不可达：继续等，等待按钮/后续操作会给出诊断
                if session.get("clientState") == "open" and session.get("status") == "active":
                    with self._lock:
                        if (gen == self._watcher_gen
                                and self.state["stage"] == STAGE_PRECHECK
                                and self.state.get("imeSessionId") == session_id):
                            self.state["phoneOpenedAt"] = _now()
                            self._advance_after_phone_open(session)

        threading.Thread(target=watch, daemon=True,
                         name=f"wizard-phone-watch-{self.device}").start()

    def cancel(self) -> dict:
        with self._lock:
            # 手动取消时若有自动校准 run 在跑，先作废它（不 join：runner 自行安全退出）
            self._auto_runner._auto_abort_for_wizard_cancel()
            self._watcher_gen += 1  # 停掉自动检测线程
            sid = self.state.get("imeSessionId")
            if sid:
                try:
                    self.ime.cancel(sid)
                except ImeError:
                    pass
            self.state = self._blank_state()
            self._save_state()
            return self.state_view()

    def precheck(self) -> dict:
        """现场预检（§6.1 进入条件的本机部分）：逐项检查并返回清单，不抛错。

        只检查本机能确认的事实：daemon 连接与会话锚点、急停、摄像头取流、
        输入法服务与会话；手机侧是否打开校准页由自动检测/「手机页诊断」负责。
        """
        checks: list[dict] = []

        def add(name: str, ok: bool, detail: str) -> None:
            checks.append({"name": name, "ok": bool(ok), "detail": detail})

        with self._lock:
            stage = self.state["stage"]
            if stage != STAGE_PRECHECK:
                # 校准已开始/已推进时预检无需执行（本机条件在 start 时已确认）
                add("stage", stage not in (STAGE_IDLE, STAGE_INVALIDATED),
                    f"当前阶段：{STAGE_DISPLAY.get(stage, stage)}")
                return {"ok": stage not in (STAGE_IDLE, STAGE_INVALIDATED),
                        "checks": checks, "stage": stage,
                        "note": "校准会话已建立，本机预检可跳过；手机侧状态见实时徽章/「手机页诊断」"}
            status = self.daemon.status()
            add("daemon", bool(status.get("connected") and status.get("session")),
                f"连接={status.get('connected')} 会话锚点={status.get('session') or '无'}")
            if status.get("session"):
                bound = self.state.get("daemonSession")
                add("daemon_session_match", bound == status.get("session"),
                    f"档案锚点 {bound} == 当前 {status.get('session')}"
                    if bound == status.get("session")
                    else f"锚点变化：{bound} → {status.get('session')}（需重新开始校准）")
            add("estop", not status.get("estop_locked"),
                "急停未锁定" if not status.get("estop_locked") else "急停锁定中：先人工复位")
            camera_ok, camera_detail = True, ""
            try:
                self._camera_status()
                camera_detail = "取流桥有画面"
            except Exception as exc:
                camera_ok, camera_detail = False, str(exc)
            add("camera", camera_ok, camera_detail)
            ime_ok, ime_detail = True, ""
            try:
                session = self.ime.get_session(self.state["imeSessionId"])
                ime_detail = (f"会话 {session['sessionId'][:12]}… 状态 {session.get('status')}，"
                              f"手机页 clientState={session.get('clientState')}")
                ime_ok = session.get("status") == "active"
            except ImeError as exc:
                ime_ok, ime_detail = False, f"输入法服务/会话异常：{exc.message}"
            except Exception as exc:
                ime_ok, ime_detail = False, f"输入法服务不可达：{exc}"
            add("ime_session", ime_ok, ime_detail)
            return {"ok": all(c["ok"] for c in checks), "checks": checks,
                    "stage": stage,
                    "note": "预检只覆盖本机条件；手机校准页由自动检测负责，无需手动等待"}

    def wait_phone_open(self, timeout: float = 15.0) -> dict:
        """等待手机校准页回报 open（自动检测之外的主动诊断入口）。

        轮询在锁外进行避免阻塞页面状态刷新；已自动推进时立即返回。
        超时返回针对性诊断：clientState unknown → 手机端设备 ID 不一致或
        访问不到 8788（附输入法服务已知设备清单）；closed → 页面已退出。
        纯观测档（Auto_CS#20）不要求机械臂会话，推进直接到屏幕几何阶段。
        """
        with self._lock:
            if self.state["stage"] == STAGE_Z_READY:
                return {"open": True, "stage": self.state["stage"], "auto": True}
            if (self.state.get("goal") == GOAL_OBSERVATION
                    and self.state["stage"] == STAGE_SCREEN_GEOMETRY):
                return {"open": True, "stage": self.state["stage"], "auto": True}
            self._require_stage(STAGE_PRECHECK)
            if self.state.get("goal") != GOAL_OBSERVATION:
                self._require_session(allow_rebind=True)
            sid = self.state["imeSessionId"]
            ime_device = self.state["imeDeviceId"]
        deadline = time.time() + timeout
        last: dict = {}
        while time.time() < deadline:
            try:
                last = self.ime.get_session(sid)
            except ImeError as exc:
                if exc.code == "calibration_not_found":
                    with self._lock:
                        self._mark_session_rebind(
                            "ime_session_lost",
                            daemon_session=self.state.get("daemonSession"),
                            ime_session_id=sid)
                    raise WizardError("校准会话丢失：参数已保留，请重新开始")
                time.sleep(0.5)
                continue
            if last.get("clientState") == "open":
                with self._lock:
                    if self.state["stage"] == STAGE_PRECHECK:
                        self.state["phoneOpenedAt"] = _now()
                        self._advance_after_phone_open(last)
                    return {"open": True, "stage": self.state["stage"]}
            if last.get("status") != "active":
                with self._lock:
                    self._mark_session_rebind(
                        "ime_session_lost",
                        daemon_session=self.state.get("daemonSession"),
                        ime_session_id=sid)
                raise WizardError("校准会话已结束：参数已保留，请重新开始")
            time.sleep(0.5)
        return self._phone_diagnosis(last.get("clientState"), ime_device)

    def _phone_diagnosis(self, client_state, ime_device: str) -> dict:
        """手机页未 open 的针对性诊断（设备 ID 不一致是最常见的现场问题）。"""
        known: list[dict] = []
        try:
            known = self.ime.list_devices()
        except Exception:
            pass
        names = [str(d.get("deviceId")) for d in known if d.get("deviceId")]
        if client_state == "closed":
            hint = ("手机校准页打开过但已退出（closed）：请重新进入校准页，"
                    "本向导会自动检测")
        else:
            hint = (f"手机校准页从未回报 open。输入法 App 的设备 ID 是随机生成、"
                    f"不可在手机端修改，必须由调试台选对：请在上方「输入法设备」"
                    f"下拉里选中这台手机（在线设备靠心跳出现在列表，当前绑定的是"
                    f"「{ime_device}」）。其次检查手机 App 的服务器地址是否指向"
                    f"Mac 的 8788（默认 10.0.2.2:18080 是模拟器地址，必须改）。")
        return {"open": False, "clientState": client_state,
                "knownDevices": names, "imeDevices": known,
                "imeDeviceId": ime_device,
                "stage": self.state.get("stage"), "hint": hint}

    # ---------- Z 阶段（§6） ----------

    def _camera_status(self) -> dict:
        if self._camera_fetch is None:
            raise WizardError("摄像头不可用：调试台未接入取流桥")
        jpg, seq = self._camera_fetch()
        return {"frameSeq": seq, "bytes": len(jpg)}

    def _manual_z_phone_pose(self, reference: dict | None = None,
                             *, allow_rebind: bool = False,
                             scope: str | None = None) -> dict:
        """Keep manual Z evidence bound to one session and physical phone pose."""
        self._require_no_auto_run("手动 Z 校准")
        if self.state["stage"] == STAGE_INVALIDATED:
            raise WizardError("统一校准已失效：请重新开始校准")
        status = self._require_session(allow_rebind=allow_rebind, scope=scope)
        if status.get("estop_locked"):
            raise WizardError("急停锁定中：拒绝执行校准点击")
        session = self._require_ime_open()
        facts = self._read_binding_facts()
        bound = self.state.get("binding")
        keys = ("cameraId", "cameraDeviceUniqueId", "bindingRevision",
                "daemonAddr", "frameWidth", "frameHeight")
        if (not self.state.get("daemonSession") or not facts or not bound
                or any(facts.get(key) in (None, "") or bound.get(key) in (None, "")
                       for key in keys)):
            raise WizardError("手机校准缺少完整设备绑定：禁止点击，请重新开始校准")
        sid = self.state["imeSessionId"]
        if session.get("sessionId") != sid:
            raise WizardError("手机校准会话已变化：拒绝执行本次点击")
        if reference is None:
            reference = self.state["z"].get("phonePose")
        if allow_rebind and (self.state.get("sessionRebind") or {}).get("required"):
            reference = None
        implicit_reference = reference is None
        if implicit_reference:
            # §6.1 定位顺序：已存边缘基准 → 实测边缘 → 本轮标靶反算几何；
            # 都缺失时报具体证据与下一步，不再把「四边不可定位」当唯一出口。
            reference = self._build_manual_phone_reference(status, facts, sid)
        if (reference.get("daemonSession") != status["session"]
                or reference.get("phoneSessionId") != sid
                or any(str((reference.get("binding") or {}).get(key)) != str(facts[key])
                       for key in keys)):
            raise WizardError("手机定位基准的会话或绑定已变化：禁止继承旧确认，请重新校准")
        reference = self._verify_manual_phone_reference(
            reference, implicit=implicit_reference, status=status, facts=facts, sid=sid)
        # Frame acquisition may wait; do not authorize motion with pre-wait identity.
        status = self._require_session(allow_rebind=allow_rebind, scope=scope)
        session = self._require_ime_open()
        current = self._read_binding_facts()
        self._require_no_auto_run("手动 Z 校准")
        if (status.get("estop_locked")
                or status.get("session") != reference["daemonSession"]
                or self.state.get("imeSessionId") != sid
                or session.get("sessionId") != sid
                or any(str((current or {}).get(key)) != str(facts[key]) for key in keys)):
            raise WizardError("手机定位期间设备或会话已变化：拒绝执行本次点击")
        self.state["z"]["phonePose"] = reference
        return reference

    def capture_hover_z(self) -> dict:
        """标定 Z 悬停高度：读当前 MPos.z 存为 hoverZ，并保存证据（§6.2）。"""
        with self._lock:
            self._require_motion_goal()
            self._require_stage(STAGE_Z_READY)
            phone_reference = self._manual_z_phone_pose(scope="z")
            mpos = self._read_position()
            evidence = {
                "daemonSession": self.state["daemonSession"],
                "device": self.device,
                "mpos": {k: mpos.get(k) for k in ("x", "y", "z")},
                "confirmedAt": _now(),
                "cameraFrameSeq": phone_reference["edge"]["frameSeq"],
                "screenEdgeDigest": quad_digest(phone_reference["edge"]),
            }
            self.state["z"]["hoverZ"] = float(mpos["z"])
            self.state["z"]["hoverEvidence"] = evidence
            self._save_state()
            return {"hoverZ": round(float(mpos["z"]), 3), "evidence": evidence}

    def z_tap_test(self, kind: str, depth: float, feed: float, dwell_ms: int) -> dict:
        """测试一次点击（kind=tap 带触点回报闭环 / kind=delete 仅机械执行记录）。
        普通点击限于 Z 阶段；长按允许后续阶段补测（§6.6）。"""
        with self._lock:
            self._require_motion_goal()
            self._require_no_auto_run("手动点击测试")
            if kind == "tap":
                self._require_stage(STAGE_Z_READY)
            elif kind == "delete":
                self._require_stage(STAGE_Z_READY, STAGE_XY, STAGE_SCREEN_GEOMETRY,
                                    STAGE_TARGET)
            else:
                raise WizardError("kind 必须是 tap 或 delete")
            depth, feed, dwell_ms = float(depth), float(feed), int(dwell_ms)
            check_feed(feed, axis="Z")
            if not 0.5 <= abs(depth) <= 10.0:
                raise WizardError(f"下压深度 {depth}mm 超出 0.5~10mm")
            if kind == "tap":
                if not 0 <= dwell_ms <= TAP_DWELL_MAX_MS:
                    raise WizardError(f"普通点击停留 0~{TAP_DWELL_MAX_MS}ms")
            elif kind == "delete":
                if not cb.DELETE_HOLD_MIN_MS <= dwell_ms <= cb.DELETE_HOLD_MAX_MS:
                    raise WizardError(f"长按停留需在 {cb.DELETE_HOLD_MIN_MS}~{cb.DELETE_HOLD_MAX_MS}ms"
                                      f"（输入法阈值 2000ms，建议留 100~300ms 裕量）")
            else:
                raise WizardError("kind 必须是 tap 或 delete")

            phone_reference = self._manual_z_phone_pose(scope="z")
            self.daemon.ensure_debug_lease()
            if kind == "delete":
                # 校准页对 >1s 触点拒报（CalibrationActivity MAX_TOUCH_DURATION_MS），
                # 长按只做机械执行记录；清空效果在验收阶段确认（§6.6 待验收）。
                self._manual_z_phone_pose(phone_reference)
                self._daemon_tap(depth, feed, dwell_ms)
                self._manual_z_phone_pose(phone_reference)
                self.state["z"]["deleteAttempts"] += 1
                trial = {"sampleId": f"delete-{uuid.uuid4().hex[:6]}", "kind": "delete",
                         "depth": depth, "feed": feed, "dwellMs": dwell_ms,
                         "ok": True, "report": None,
                         "note": "机械执行完成；清空效果待验收阶段确认",
                         "ts": _now()}
                self._push_trial(trial)
                self._save_state()
                return {"trial": self._trial_view(trial)}

            sample_id = f"z-{uuid.uuid4().hex[:6]}"
            trial = {"sampleId": sample_id, "kind": "tap", "depth": depth,
                     "feed": feed, "dwellMs": dwell_ms, "ok": False,
                     "report": None, "ts": _now()}
            self.state["z"]["tapAttempts"] += 1
            try:
                report = self._tap_with_report(
                    sample_id, lambda: self._daemon_tap(depth, feed, dwell_ms),
                    phone_reference=phone_reference, scope="z")
            except TouchReportTimeout as exc:
                trial["error"] = str(exc)
                self._push_trial(trial)  # 超时也留痕（§14 touch_report_timeout）
                self._save_state()
                raise
            trial["ok"] = True
            trial["report"] = report
            self.state["z"]["tapOkStreak"] = self.state["z"].get("tapOkStreak", 0) + 1
            self._save_tap_anchor(self._read_position(), report, "z_tap_test")
            self._push_trial(trial)
            self._save_state()
            return {"trial": self._trial_view(trial),
                    "tapOkStreak": self.state["z"]["tapOkStreak"]}

    def _save_tap_anchor(self, mpos: dict, report: dict, source: str) -> None:
        """把"已证实落在屏幕上"的一次点击记为 First Click 种子锚点（用户拍板
        2026-09-05）。z 点击测试先于自动校准且必有触点回报（=玻璃上），其
        机械↔屏幕对应关系可校正守护重连/G92 锚点归零后的种子平移失效。
        TAP 只动 Z，mpos 的 XY 即点击时位置。"""
        try:
            self.state["z"]["tapAnchor"] = {
                "robotX": round(float(mpos["x"]), 3),
                "robotY": round(float(mpos["y"]), 3),
                "screenX": float(report["screenX"]),
                "screenY": float(report["screenY"]),
                "daemonSession": self.state.get("daemonSession"),
                "source": source,
                "ts": _now(),
            }
        except (KeyError, TypeError, ValueError):
            pass  # 回报缺字段：不锚定（First Click 自校正仍是后备）

    def _push_trial(self, trial: dict) -> None:
        trials = self.state["z"].setdefault("trials", [])
        trials.append(trial)
        del trials[:-50]

    @staticmethod
    def _trial_view(trial: dict) -> dict:
        return dict(trial)

    def _daemon_tap(self, depth: float, feed: float, dwell_ms: int) -> dict:
        cmd = f"TAP {depth:.3f} F{feed:g}"
        if dwell_ms > 0:
            cmd += f" P{dwell_ms / 1000.0:g}"
        return self.daemon.cmd(cmd)

    def _tap_with_report(self, sample_id: str, move, *, target: dict | None = None,
                         timeout: float | None = None,
                         pre_armed: dict | None = None,
                         phone_reference: dict | None = None,
                         allow_rebind: bool = False,
                         scope: str | None = None) -> dict:
        """布防 sampleId+sequence → 执行机械动作 → 等待匹配触点回报（§14 拒绝乱序/超时）。

        pre_armed：复用已存在的布防（xy_preview 预先布防、标靶长驻供人工对位），
        含 sequence 与 armedAt——只需补足剩余的手机轮询等待，不再重新布防。
        """
        sid = self.state["imeSessionId"]
        run_context = self._auto_runner._auto["run"]
        auto_run = run_context if phone_reference is None else None
        auto_edge = auto_run.get("edge") if auto_run is not None else None
        auto_corners = deepcopy(auto_edge.get("corners")) if isinstance(auto_edge, dict) else None

        def check_phone_context(*, post_report: bool = False):
            if (self.state.get("imeSessionId") != sid
                    or self._auto_runner._auto["run"] is not run_context):
                raise WizardError("校准运行或手机会话已变化：本次点击不计入")
            if phone_reference is not None:
                self._manual_z_phone_pose(phone_reference, allow_rebind=allow_rebind,
                                          scope=scope)
            elif auto_run is not None:
                self._auto_runner._auto_check_cancel()
                if auto_run.get("state") not in AUTO_ACTIVE_STATES:
                    raise WizardError("自动校准运行已变化：本次点击不计入")
                if (auto_run.get("edge") is not auto_edge
                        or not isinstance(auto_edge, dict)
                        or auto_edge.get("corners") != auto_corners):
                    raise AutoRunError(
                        "camera_measurement_error",
                        "本次点击的手机定位基准已变化：拒绝触点样本进入学习",
                        classification="camera_measurement_error",
                        routed_to="CAMERA_RECALIBRATION", park=True)
                pose_quad = self._auto_runner._auto_require_stable_phone(
                    auto_run, after_travel=True)
                if pose_quad is None and post_report:
                    # 采样姿态复核超限已按 H01 提示+留痕（pose_gate_skipped）；
                    # 但回报后仍无法核实=本样本的物理位置语境不可核实——
                    # 样本不得计入学习（门的原始语义：不混合不同物理位置的
                    # 手机触点样本）。回报前（post_report=False）不硬拒：
                    # 按 2026-10-03 测试更正，采样有效性由触点回报门兜底。
                    raise AutoRunError(
                        "camera_measurement_error",
                        "采样期间手机姿态无法核实：本样本不计入学习",
                        classification="camera_measurement_error", park=True)
                self._auto_runner._auto_guard(auto_run)
                if (self._auto_runner._auto["run"] is not auto_run
                        or auto_run.get("state") not in AUTO_ACTIVE_STATES
                        or self.state.get("imeSessionId") != sid
                        or auto_run.get("phoneSessionId") != sid
                        or auto_run.get("edge") is not auto_edge
                        or auto_edge.get("corners") != auto_corners):
                    raise WizardError("自动校准运行或手机会话已变化：本次点击不计入")

        self._require_ime_open()
        if pre_armed is not None:
            seq = int(pre_armed["sequence"])
            # 布防已久（人工对位耗时不定）：只补足从布防算起的最小等待
            lead = self._phone_arm_lead_s - (time.time() - float(pre_armed["armedAt"]))
        else:
            seq = int(self.state.get("imeSequence", 0)) + 1
            self.state["imeSequence"] = seq
            self.ime.arm(sid, sample_id, seq, target)
            lead = self._phone_arm_lead_s
        try:
            # 等手机端轮询到布防（标靶已显示）再动机械：否则触点会被校准页
            # 静默忽略（CalibrationActivity armed==null 时 ACTION_UP 直接丢弃）
            if lead > 0:
                time.sleep(lead)
            # 轮询等待会让旧快照失效，动作前重查页面、布防和设备事实。
            if self.state.get("imeSessionId") != sid:
                raise WizardError("手机校准会话已变化：拒绝执行本次点击")
            session = self._require_ime_open()
            armed = session.get("armed") or {}
            if (session.get("sessionId") != sid
                    or armed.get("sampleId") != sample_id
                    or type(armed.get("sequence")) is not int
                    or armed["sequence"] != seq):
                raise WizardError("手机布防已变化：本次点击不计入")
            if any(s.get("sampleId") == sample_id for s in session.get("samples", [])):
                raise WizardError("手机样本已回报：拒绝重复机械动作")
            status = self._require_session(allow_rebind=allow_rebind, scope=scope)
            if status.get("estop_locked"):
                raise WizardError("急停锁定中：拒绝执行校准点击")
            check_phone_context()
            move()
        except Exception:
            try:
                self.ime.disarm(sid, sample_id, seq)
            except ImeError:
                pass
            raise
        deadline = time.time() + (timeout or self._touch_timeout)
        while time.time() < deadline:
            session = self.ime.get_session(sid)
            sample = next((s for s in session.get("samples", [])
                           if s.get("sampleId") == sample_id), None)
            if sample is not None:
                if sample.get("sequence") != seq:
                    self._invalidate("touch_report_mismatch",
                                     f"触点回报序号不匹配：期望 {seq}，收到 {sample.get('sequence')}")
                    raise WizardError("触点与布防点不匹配：已失效")
                if (session.get("status") != "active"
                        or session.get("clientState") != "open"
                        or session.get("sessionId") != sid):
                    raise WizardError("回报期间手机校准会话已变化：本次点击不计入")
                check_phone_context(post_report=True)
                self._check_screen_consistency(sample)
                return {"screenX": sample["screenX"], "screenY": sample["screenY"],
                        "screenWidth": sample["screenWidth"],
                        "screenHeight": sample["screenHeight"],
                        "orientation": sample.get("orientation", "portrait")}
            if session.get("clientState") != "open":
                try:
                    self.ime.disarm(sid, sample_id, seq)
                except ImeError:
                    pass
                raise WizardError("等待回报期间手机校准页被关闭：本次点击不计入")
            time.sleep(0.2)
        try:
            self.ime.disarm(sid, sample_id, seq)
        except ImeError:
            pass
        raise TouchReportTimeout(
            f"touch_report_timeout：{timeout or self._touch_timeout:.0f}s 内未收到触点回报"
            f"（不自动加深深度；可调整 Z 参数后重测，§14）")

    def z_save(self, kind: str) -> dict:
        """保存点击参数（kind=tap 需 ≥1 次合法回报；§6.3/§6.6）。
        普通点击必须在 Z 阶段保存；长按允许在后续阶段补测补存（§6.6 "保存或待验收"）。"""
        with self._lock:
            self._require_motion_goal()
            if kind == "tap":
                self._require_stage(STAGE_Z_READY)
            elif kind == "delete":
                self._require_stage(STAGE_Z_READY, STAGE_XY, STAGE_SCREEN_GEOMETRY,
                                    STAGE_TARGET)
            else:
                raise WizardError("kind 必须是 tap 或 delete")
            z = self.state["z"]
            if z.get("phonePose") is None:
                raise WizardError("点击记录缺少手机姿态基准：请重新测试点击，不得继承旧确认")
            self._manual_z_phone_pose(z["phonePose"])
            if z.get("hoverZ") is None:
                raise WizardError("尚未标定悬停高度：请先点「标定 Z 轴高度」")
            if kind == "tap":
                latest = next((t for t in reversed(z["trials"])
                               if t["kind"] == "tap" and t["ok"]), None)
                if latest is None:
                    raise WizardError("还没有收到合法触点回报：先完成一次成功的测试点击")
                z["tap"] = {"depth": latest["depth"], "feed": latest["feed"],
                            "dwellMs": latest["dwellMs"], "validated": True,
                            "sampleId": latest["sampleId"], "savedAt": _now()}
            elif kind == "delete":
                latest = next((t for t in reversed(z["trials"])
                               if t["kind"] == "delete" and t["ok"]), None)
                if latest is None:
                    raise WizardError("还没有长按测试记录：先执行一次长按测试")
                z["delete"] = {"holdMs": latest["dwellMs"], "depth": latest["depth"],
                               "feed": latest["feed"], "validated": False,
                               "note": "待验收：清空效果在调试台输入法验收阶段确认",
                               "savedAt": _now()}
            else:
                raise WizardError("kind 必须是 tap 或 delete")
            self._sync_bundle()
            self._save_state()
            # 普通点击参数保存后进入 XY 阶段（长按允许稍后补测）
            if kind == "tap":
                self._advance(STAGE_XY)
            return {"saved": kind, "z": {"tap": z.get("tap"), "delete": z.get("delete")},
                    "stage": self.state["stage"]}

    # ---------- XY 阶段（§7） ----------

    def xy_plan(self) -> list[dict]:
        return [{"index": i, "name": name, "u": u, "v": v,
                 "accepted": any(a["index"] == i for a in self.state["xy"]["accepted"])}
                for i, (name, u, v) in enumerate(XY_PLAN_POINTS)]

    def _planned_px(self, u: float, v: float) -> tuple[float, float] | None:
        screen = self.state.get("screen")
        if not screen or not screen.get("screenWidth"):
            return None
        return (u * screen["screenWidth"], v * screen["screenHeight"])

    def xy_preview(self, index: int) -> dict:
        """「显示标靶」（§7.2 步骤 1-4）：布防带标靶矩形并长驻，供人工点动对位。

        标靶会一直显示在手机校准页上（直到点击回报或撤销），用户通过 XY 点动
        把探头对到标靶中心后，再点「测试点击」。
        """
        with self._lock:
            self._require_motion_goal()
            self._require_no_auto_run("显示标靶")
            self._require_stage(STAGE_XY)
            self._require_session()
            if not 0 <= index < len(XY_PLAN_POINTS):
                raise WizardError(f"采样点编号 {index} 不存在")
            name, u, v = XY_PLAN_POINTS[index]
            self._disarm_preview()
            sid = self.state["imeSessionId"]
            seq = int(self.state.get("imeSequence", 0)) + 1
            self.state["imeSequence"] = seq
            sample_id = f"xy{index}-{uuid.uuid4().hex[:6]}"
            target = self._target_rect(u, v)
            self.ime.arm(sid, sample_id, seq, target)
            self.state["xy"]["preview"] = {"index": index, "name": name,
                                           "sampleId": sample_id, "sequence": seq,
                                           "armedAt": time.time(),
                                           "u": u, "v": v, "target": target}
            self._save_state()
            return {"preview": self.state["xy"]["preview"],
                    "hint": "标靶已显示并保持：用 XY 点动把探头对到标靶中心，"
                            "然后点「测试点击」"}

    def _disarm_preview(self) -> None:
        """撤销当前预布防（切换标靶点/清空样本时调用）。"""
        preview = self.state["xy"].get("preview")
        if not preview:
            return
        sid = self.state.get("imeSessionId")
        if sid:
            try:
                self.ime.disarm(sid, preview["sampleId"], preview["sequence"])
            except ImeError:
                pass
        self.state["xy"]["preview"] = None

    def xy_tap_test(self, index: int) -> dict:
        """XY 采样点「测试点击」：优先复用预布防标靶（人工已对位），
        否则布防+等待+点击一步完成。回报后配对 (机械, 屏幕) 并计算误差（§7.2）。
        """
        with self._lock:
            self._require_motion_goal()
            self._require_no_auto_run("手动 XY 采样点击")
            self._require_stage(STAGE_XY)
            self._require_session()
            if not 0 <= index < len(XY_PLAN_POINTS):
                raise WizardError(f"采样点编号 {index} 不存在")
            name, u, v = XY_PLAN_POINTS[index]
            target = self._target_rect(u, v)
            sid = self.state["imeSessionId"]

            preview = self.state["xy"].get("preview")
            pre_armed = None
            sample_id = f"xy{index}-{uuid.uuid4().hex[:6]}"
            if preview and preview.get("index") == index:
                session = self.ime.get_session(sid)
                stray = next((s for s in session.get("samples", [])
                              if s.get("sampleId") == preview["sampleId"]), None)
                if stray is not None:
                    # 对位期间屏幕被误触（手指/探头蹭到）：布防已被消耗且样本无效
                    self.state["xy"]["preview"] = None
                    self._save_state()
                    raise WizardError("标靶布防已被屏幕误触消耗（对位时请勿触碰屏幕）："
                                      "请重新点「显示标靶」")
                armed_now = session.get("armed") or {}
                if armed_now.get("sampleId") == preview["sampleId"]:
                    pre_armed = dict(preview)
                    sample_id = preview["sampleId"]
                # 布防已丢失（页面重开等）→ 走全新布防路径
            elif preview:
                self._disarm_preview()  # 布防的是别的点：先撤销

            def move() -> None:
                self.daemon.ensure_debug_lease()
                z = self.state["z"]
                self._daemon_tap(float(z["tap"]["depth"]), float(z["tap"]["feed"]),
                                 int(z["tap"]["dwellMs"]))

            trial = {"index": index, "name": name, "sampleId": sample_id,
                     "accepted": False, "ok": False, "ts": _now()}
            mpos = self._read_position()
            try:
                report = self._tap_with_report(sample_id, move, target=target,
                                               pre_armed=pre_armed)
            except TouchReportTimeout as exc:
                trial["error"] = str(exc)
                self.state["xy"]["preview"] = None
                trials = self.state["xy"].setdefault("trials", [])
                trials.append(trial)
                del trials[:-100]
                self._save_state()
                raise
            trial.update({"robotX": round(float(mpos["x"]), 3),
                          "robotY": round(float(mpos["y"]), 3),
                          "screenX": report["screenX"], "screenY": report["screenY"],
                          "screenWidth": report["screenWidth"],
                          "screenHeight": report["screenHeight"],
                          "ok": True})
            planned = self._planned_px(u, v)
            if planned:
                trial["plannedX"] = round(planned[0], 1)
                trial["plannedY"] = round(planned[1], 1)
                trial["errorPx"] = round(((planned[0] - report["screenX"]) ** 2 +
                                          (planned[1] - report["screenY"]) ** 2) ** 0.5, 2)
            trial["tipX"], trial["tipY"] = self._detect_tip()
            self.state["xy"]["preview"] = None  # 回报即消耗布防（标靶消失）
            trials = self.state["xy"].setdefault("trials", [])
            trials.append(trial)
            del trials[:-100]
            self._save_state()
            return {"trial": trial}

    def _target_rect(self, u: float, v: float) -> dict:
        half = TARGET_RECT_HALF
        uu = min(max(u, half), 1 - half)
        vv = min(max(v, half), 1 - half)
        return {"u": round(uu, 4), "v": round(vv, 4),
                "widthU": round(half * 2, 4), "heightV": round(half * 2, 4)}

    def _detect_tip(self) -> tuple[float | None, float | None]:
        """点击后抓帧检测探头尖（配对摄像头像素，供摄像头→屏幕单应拟合）。"""
        if self._camera_fetch is None or self._tip_detector is None:
            return None, None
        try:
            jpg, _ = self._camera_fetch()
            detected = self._tip_detector(jpg)
        except Exception:
            return None, None
        if not detected:
            return None, None
        if len(detected) >= 4:
            x, y, frame_w, frame_h = detected[0], detected[1], detected[2], detected[3]
            if frame_w and frame_h:
                self.state["geometry"]["tipFrame"] = {"frameW": int(frame_w),
                                                      "frameH": int(frame_h)}
        else:
            x, y = detected[0], detected[1]
        return round(float(x), 1), round(float(y), 1)

    def _detect_target(self):
        """Wait for the phone poll/render cycle and detect the target across fresh frames."""
        if self._camera_fetch is None or self._target_detector is None:
            raise WizardError("摄像头或绿色标靶检测器不可用")
        time.sleep(self._target_draw_wait_s)
        deadline = time.time() + max(
            0.0, self._target_detect_timeout_s - self._target_draw_wait_s)
        attempts = 0
        last_error: Exception | None = None
        while True:
            attempts += 1
            try:
                jpg, frame_seq = self._camera_fetch()
                detected = self._target_detector(jpg)
                if detected is not None and len(detected) >= 2:
                    return detected, frame_seq
                last_error = ScreenEdgeError("检测器未返回标靶中心")
            except Exception as exc:
                last_error = exc
            if time.time() >= deadline:
                break
            time.sleep(0.2)
        raise WizardError(
            f"连续 {attempts} 帧未检出绿色标靶：{last_error or '未知错误'}")

    def xy_accept(self, index: int, accept: bool = True) -> dict:
        """接受/继续修正当前点：只有接受的点进入正式拟合样本（§7.2）。

        接受后自动为下一个未接受点显示标靶，减少现场点击次数。
        """
        with self._lock:
            self._require_motion_goal()
            self._require_stage(STAGE_XY)
            trials = [t for t in self.state["xy"]["trials"] if t["index"] == index]
            if not trials:
                raise WizardError(f"采样点 {index} 还没有测试记录")
            latest = trials[-1]
            if not latest.get("ok"):
                raise WizardError("最近一次测试未成功（无触点回报）：请重新测试本点")
            accepted = [a for a in self.state["xy"]["accepted"] if a["index"] != index]
            if accept:
                accepted.append(latest)
            self.state["xy"]["accepted"] = sorted(accepted, key=lambda a: a["index"])
            # 样本变化后旧拟合作废，需重新拟合
            self.state["xy"]["fit"] = None
            self.state["xy"]["validation"] = []
            self.state["xy"]["validationPassed"] = False
            self._save_state()
            next_preview = None
            if accept:
                next_index = next((i for i in range(len(XY_PLAN_POINTS))
                                   if i != index
                                   and not any(a["index"] == i
                                               for a in self.state["xy"]["accepted"])), None)
                if next_index is not None:
                    try:
                        next_preview = self.xy_preview(next_index)["preview"]
                    except (WizardError, ImeError):
                        next_preview = None  # 自动预布防失败不阻塞接受
            return {"index": index, "accepted": accept,
                    "acceptedCount": len(self.state["xy"]["accepted"]),
                    "nextPreview": next_preview}

    def xy_clear(self) -> dict:
        with self._lock:
            self._require_motion_goal()
            self._require_stage(STAGE_XY)
            self._disarm_preview()
            self.state["xy"] = {"accepted": [], "trials": [], "fit": None,
                                "validation": [], "preview": None}
            self._save_state()
            return {"cleared": True}

    def xy_fit(self, force: bool = False) -> dict:
        """仿射拟合 screen=A·robot+b（≥3 不共线点，§7.3）。

        - 先做丢步检测（相机↔机械一致性，>2mm 直接拒绝且不可强制）；
        - 单点最大残差 >6px 只警告（文档为"建议"值）；
        - RMS >3px 默认拒绝，操作者可 force=True 强行拟合（记录在案）。
        """
        with self._lock:
            self._require_motion_goal()
            self._require_stage(STAGE_XY)
            accepted = self.state["xy"]["accepted"]
            if len(accepted) < 3:
                raise WizardError(f"正式样本只有 {len(accepted)} 个：至少需要 3 个不共线点")
            # 分散度预检：机械位置挤在一起（如没移臂重复点击）时直接给出人话错误
            xs = [a["robotX"] for a in accepted]
            ys = [a["robotY"] for a in accepted]
            if (max(xs) - min(xs) < 5.0 and max(ys) - min(ys) < 5.0) \
                    or (max(xs) - min(xs) < 1.0) or (max(ys) - min(ys) < 1.0):
                raise WizardError(
                    "样本机械位置太集中（XY 分布不足 5mm）：请对每个点先「显示标靶」、"
                    "把机械臂移到标靶位置后再「测试点击」，不要在同一位置重复采样")
            from adapters.screen_ime.geometry import CalibrationMathError, fit_affine

            # 丢步检测：用样本自带的探头尖像素拟合 相机↔机械 一致性（单位 mm）。
            # 摄像头无需校准——只要机械没丢步，探头尖像素与 MPos 应是稳定仿射；
            # 残差大说明 MPos 与物理位置脱节（快速大步点动丢步），此时任何
            # 屏幕↔机械拟合都不可信，强行放行只会把偏差烤进映射。
            with_tips = [a for a in accepted
                         if a.get("tipX") is not None and a.get("tipY") is not None]
            if len(with_tips) >= 3:
                cam_samples = [{"robotX": a["tipX"], "robotY": a["tipY"],
                                "screenX": a["robotX"], "screenY": a["robotY"]}
                               for a in with_tips]
                try:
                    cam_fit = fit_affine(cam_samples)
                except CalibrationMathError:
                    cam_fit = None
                if cam_fit and cam_fit["residualRms"] > 2.0:
                    worst_i = max(range(len(with_tips)),
                                  key=lambda i: cam_fit["residuals"][i])
                    worst = with_tips[worst_i]
                    detail = "；".join(
                        f"点{a['index']}({a['name']}) {r:.1f}mm"
                        for a, r in zip(with_tips, cam_fit["residuals"]))
                    raise WizardError(
                        f"检测到机械坐标与实际位置不一致（丢步）：相机↔机械一致性残差 "
                        f"RMS {cam_fit['residualRms']:.1f}mm（应 ≤2mm）。摄像头看到探头"
                        f"实际到过各标靶（你的点击没问题），但 MPos 记录的位置与实际"
                        f"脱节（最差：点{worst['index']}）。各点残差：{detail}。"
                        f"处理：断开→重新「连接设备」（MPos 归零）→ 重新校准；"
                        f"点动请用低速（F≤1500）小步长，避免快速大步换向。此错误"
                        f"不可强行拟合（拟合结果会带着等量偏差）。")

            samples = [{"robotX": a["robotX"], "robotY": a["robotY"],
                        "screenX": a["screenX"], "screenY": a["screenY"]}
                       for a in accepted]
            try:
                fit = fit_affine(samples)
            except CalibrationMathError as exc:
                raise WizardError(f"拟合失败：{exc.message}") from exc
            rms, worst = fit["residualRms"], max(fit["residuals"])
            residual_detail = "；".join(
                f"点{a['index']}({a['name']}) {r:.1f}px"
                for a, r in zip(accepted, fit["residuals"]))
            warnings = []
            if worst > cb.XY_RESIDUAL_MAX_PX:
                warnings.append(f"单点最大残差 {worst:.2f}px 超过建议值 "
                                f"{cb.XY_RESIDUAL_MAX_PX}px（建议修正该点，不阻断）")
            if rms > cb.XY_RESIDUAL_RMS_MAX_PX and not force:
                raise WizardError(
                    f"残差超限：RMS {rms:.2f}px（≤{cb.XY_RESIDUAL_RMS_MAX_PX}）、"
                    f"单点最大 {worst:.2f}px。各点残差：{residual_detail}。"
                    f"可重新采样偏差大的点（显示标靶→对位→测试点击→接受 替换），"
                    f"或点「强行拟合」放行（会在档案中记录）")
            if rms > cb.XY_RESIDUAL_RMS_MAX_PX and force:
                warnings.append(f"已强行拟合：RMS {rms:.2f}px 超过阈值 "
                                f"{cb.XY_RESIDUAL_RMS_MAX_PX}px（操作者强制放行）")
            self.state["xy"]["fit"] = {**{k: fit[k] for k in ("a", "b", "c", "d", "e", "f")},
                                       "residualRmsPx": round(rms, 3),
                                       "maxResidualPx": round(worst, 3),
                                       "residualsByPoint": [
                                           {"index": a["index"], "name": a["name"],
                                            "residualPx": round(r, 2)}
                                           for a, r in zip(accepted, fit["residuals"])],
                                       "forced": bool(force and rms > cb.XY_RESIDUAL_RMS_MAX_PX),
                                       "warnings": warnings,
                                       "sampleCount": len(accepted),
                                       "fittedAt": _now()}
            self._sync_bundle()
            self._save_state()
            return {"fit": self.state["xy"]["fit"]}

    def xy_validate_tap(self, index: int) -> dict:
        """独立验证点：由拟合映射自动走位到计划屏幕位置 → 点击 → 误差只用于验收。

        验证的是"映射本身"（§7.4）：屏幕像素 → 机械坐标 → 实际点击回报，
        不依赖人工对位精度（人工对位误差会直接淹没 ≤6px 的验收阈值）。
        """
        with self._lock:
            self._require_motion_goal()
            self._require_no_auto_run("手动验证点击")
            self._require_stage(STAGE_XY)
            fit = self.state["xy"].get("fit")
            if not fit:
                raise WizardError("尚未完成拟合：请先「拟合映射」")
            if not 0 <= index < len(XY_VALIDATION_POINTS):
                raise WizardError(f"验证点编号 {index} 不存在")
            name, u, v = XY_VALIDATION_POINTS[index]
            planned = self._planned_px(u, v)
            if not planned:
                raise WizardError("屏幕基线缺失：无法计算验证点计划位置")
            target = self._target_rect(u, v)
            sample_id = f"vxy{index}-{uuid.uuid4().hex[:6]}"

            # 用拟合逆解自动走位（回悬停 → 分段平移 → 残差检查）
            rx, ry = cb.robot_from_screen(
                {k: fit[k] for k in ("a", "b", "c", "d", "e", "f")},
                planned[0], planned[1])
            self.daemon.ensure_debug_lease()
            self._goto_robot(rx, ry)

            def move() -> None:
                z = self.state["z"]
                self._daemon_tap(float(z["tap"]["depth"]), float(z["tap"]["feed"]),
                                 int(z["tap"]["dwellMs"]))

            report = self._tap_with_report(sample_id, move, target=target)
            error = ((planned[0] - report["screenX"]) ** 2 +
                     (planned[1] - report["screenY"]) ** 2) ** 0.5
            result = {"index": index, "name": name, "plannedU": u, "plannedV": v,
                      "plannedX": round(planned[0], 1), "plannedY": round(planned[1], 1),
                      "reportedX": report["screenX"], "reportedY": report["screenY"],
                      "robotTarget": [round(rx, 2), round(ry, 2)],
                      "errorPx": round(error, 2),
                      "passed": bool(error <= cb.XY_RESIDUAL_MAX_PX),
                      "ts": _now()}
            validation = [r for r in self.state["xy"]["validation"] if r["index"] != index]
            validation.append(result)
            self.state["xy"]["validation"] = sorted(validation, key=lambda r: r["index"])
            self._save_state()
            return {"result": result}

    def xy_finish(self) -> dict:
        """3 个验证点全部通过 → XY 验收完成，进入视觉阶段（§7.4）。"""
        with self._lock:
            self._require_motion_goal()
            self._require_stage(STAGE_XY)
            validation = self.state["xy"].get("validation", [])
            if len(validation) < len(XY_VALIDATION_POINTS):
                raise WizardError(f"验证点还剩 "
                                  f"{len(XY_VALIDATION_POINTS) - len(validation)} 个未执行")
            failed = [r for r in validation if not r["passed"]]
            if failed:
                raise WizardError("存在超差验证点："
                                  + "；".join(f"{r['name']} 误差 {r['errorPx']}px" for r in failed)
                                  + "。请回到 XY 校准阶段重新采样")
            self.state["xy"]["validationPassed"] = True
            # 当前会话的独立验证点全部通过 → XY 资格恢复（规范 §5.4）。
            self._clear_session_rebind(scope="xy")
            self._advance(STAGE_SCREEN_GEOMETRY)
            return {"passed": True, "stage": self.state["stage"]}

    # ---------- 视觉阶段（§8） ----------

    def edge_capture(self) -> dict:
        """采集屏幕边缘基准（四角、容差、摘要）存入档案（§8.1）。"""
        with self._lock:
            self._require_stage(STAGE_SCREEN_GEOMETRY, STAGE_TARGET)
            quad = self._edge_quad()
            edge = {"corners": quad["corners"], "frameW": quad["frameW"],
                    "frameH": quad["frameH"], "digest": quad_digest(quad),
                    "safeInsetPx": cb.DEFAULT_SAFE_INSET_PX,
                    "tolerancePx": cb.DEFAULT_EDGE_TOLERANCE_PX,
                    "capturedAt": _now()}
            self.state["geometry"]["edge"] = edge
            self._save_state()
            return {"edge": edge}

    def _current_search_region(self):
        if self._search_region_provider is None:
            return None
        from runtime.phone_region_context import RegionContextError
        try:
            return self._search_region_provider()
        except (RegionContextError, RuntimeError) as exc:
            raise WizardError(
                f"region_context_invalid：生效粗框不可用（{exc}）；"
                "请在调试台重新核验手机搜索范围后重新校准") from exc

    def _pin_screen_search_context(self, region):
        if self._search_region_provider is None:
            return lambda: None
        current = {"region": deepcopy(region),
                   "binding": deepcopy(self._read_binding_facts())}
        if "screenSearchContext" not in self.state:
            self.state["screenSearchContext"] = deepcopy(current)
            self._save_state()
        elif self.state["screenSearchContext"] != current:
            raise WizardError(
                "region_context_changed：本轮校准搜索范围或来源已变化，请重新开始校准")
        if region is not None:
            facts = current["binding"] or {}
            size = region.get("source_size") or {}
            if (facts.get("frameWidth"), facts.get("frameHeight")) != (
                    size.get("width"), size.get("height")):
                raise WizardError("region_context_invalid：搜索范围与当前来源尺寸不一致")
            installation = region.get("installation_binding") or {}
            for key, fact_key in (
                ("camera_id", "cameraId"),
                ("camera_device_unique_id", "cameraDeviceUniqueId"),
                ("camera_binding_id", "cameraBindingId"),
            ):
                if key in installation and installation[key] != facts.get(fact_key):
                    raise WizardError("region_context_invalid：搜索范围与当前安装身份不一致")
            if region.get("source_geometry_id") is not None \
                    and region["source_geometry_id"] != facts.get("sourceGeometryId"):
                raise WizardError("region_context_invalid：搜索范围与当前来源几何不一致")

        def ensure_current():
            latest = {"region": self._current_search_region(),
                      "binding": self._read_binding_facts()}
            if latest != current:
                raise WizardError(
                    "region_context_changed：采样期间搜索范围或来源已变化，请重新开始校准")

        return ensure_current

    def _edge_quad(self, *, expected_corners: list | None = None) -> dict:
        if self._camera_fetch is None or self._edge_detector is None:
            raise PoseUnlocatable("摄像头不可用：调试台未接入取流桥")
        region = deepcopy(self._current_search_region())
        ensure_current = self._pin_screen_search_context(region)
        jpg, seq = self._camera_fetch()
        ensure_current()
        if region is not None:
            # S2-06/07：初始定位与回退都只在本台粗框内；上下文失效为终态
            # WizardError（不是 H01 姿态提示），输出恢复原图坐标。
            import cv2 as _cv2
            frame = _cv2.imdecode(np.frombuffer(jpg, np.uint8), _cv2.IMREAD_COLOR)
            if frame is None:
                raise WizardError("取流桥帧解码失败")
            size = region["source_size"]
            if frame.shape[:2] != (size["height"], size["width"]):
                raise WizardError("region_context_invalid：取流图像尺寸与搜索范围不一致")
            from runtime.phone_region_context import locate_quad_in_region
            try:
                located = locate_quad_in_region(
                    frame, region, expected_corners or self._bundle_edge_corners())
                ensure_current()
                return {"corners": located["corners"],
                        "frameW": located["frameW"], "frameH": located["frameH"],
                        "area": None, "frameSeq": seq,
                        "source": "search_region"}
            except Exception as region_exc:
                # 梯度回退同框：locate_quad_in_region 已含梯度回退；仍失败
                # = 框内不可定位（终态，不回全图）。
                raise WizardError(
                    f"粗框内无法定位手机屏幕：{region_exc}；"
                    "请改善轮廓可见度或重新核验搜索范围") from region_exc
        try:
            quad = self._edge_detector(jpg)
        except ScreenEdgeError as free_exc:
            # §6.1.1 开灯回退：亮桌面/暗屏下自由阈值检测会锁到背景亮区，
            # 用梯度直线法在预测四角附近定位（锚=调用方指定或档案四角）。
            anchor = expected_corners or self._bundle_edge_corners()
            if not anchor:
                raise PoseUnlocatable(
                    f"screen_edge_not_detected：{free_exc}") from free_exc
            quad = self._gradient_edge_quad(jpg, seq, anchor,
                                            fallback_error=free_exc)
        if not isinstance(quad, dict):
            raise PoseUnlocatable("手机屏幕四边不可定位：拒绝校准点击")
        quad = deepcopy(quad)
        try:
            from drivers.screen_pose import validate_corners

            validate_corners(quad["corners"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PoseUnlocatable(f"手机屏幕四边不可定位：{exc}") from exc
        quad["frameSeq"] = seq
        ensure_current()
        return quad

    def _bundle_edge_corners(self) -> list | None:
        saved = ((self._bundle().get("screen") or {}).get("edgeModel")
                 or {}).get("corners")
        return saved if saved and len(saved) == 4 else None

    def _gradient_edge_quad(self, jpg, seq: int, anchor: list,
                            *, fallback_error: Exception | None = None) -> dict:
        import time as _time

        from drivers.screen_edge import locate_quad_by_gradient_lines

        # 三帧角点取中值：梯度定位单帧刚性噪声 ~2.7px，取中值后 <1.5px
        # （2026-09-29 真机实测）。
        corners_samples = []
        last_error: ScreenEdgeError | None = None
        last_payload = None
        for attempt in range(3):
            try:
                frame = self._decode_jpg(
                    jpg if attempt == 0 else self._camera_fetch()[0])
                located = locate_quad_by_gradient_lines(
                    frame, anchor, tolerance_px=32.0)
            except ScreenEdgeError as gradient_exc:
                last_error = gradient_exc
            else:
                corners_samples.append(np.asarray(located["corners"], dtype=float))
                last_payload = located
            if len(corners_samples) == 3:
                break
            _time.sleep(0.12)
        if len(corners_samples) < 2:
            detail = str(last_error) if last_error else "定位无有效帧"
            prefix = (f"screen_edge_not_detected：{fallback_error}；"
                      if fallback_error else "screen_edge_not_detected：")
            raise WizardError(f"{prefix}校准预测定位失败：{detail}")
        stacked = np.stack(corners_samples)
        corners = np.median(stacked, axis=0)
        return {"corners": corners.tolist(),
                "frameW": (last_payload or {}).get("frameW"),
                "frameH": (last_payload or {}).get("frameH"), "area": None,
                "frameSeq": seq, "source": "calibrated_prediction"}

    def _decode_jpg(self, jpg):
        import cv2
        import numpy as np

        image = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if image is None:
            raise WizardError("screen_edge_not_detected：当前帧无法解码")
        return image

    def camera_fit(self, force: bool = False) -> dict:
        """摄像头→屏幕单应（§8.4）。生产流程必须使用"屏幕平面标靶法"。

        - 标靶法（推荐）：依次在 5 个标定点布防标靶（手机画绿色框），摄像头检测
          绿框中心 ↔ 名义屏幕像素。配对双方都在屏幕平面上——与 OCR 文字同平面，
          不含探头悬停高度的视差（探头尖法把悬停平面映射到屏幕坐标，用于 OCR
          定位会带入 ~75px 级的跨平面偏差）。无需移动机械臂。
        - 探头尖法只供离线测试/诊断显式开启；生产不允许退回，
          因为 accepted 样本的探头尖在悬停平面，与屏幕内容存在视差。
        """
        with self._lock:
            self._require_no_auto_run("手动摄像头拟合（会占用手机布防与画面检测）")
            self._require_stage(STAGE_SCREEN_GEOMETRY, STAGE_TARGET)
            from adapters.screen_ime.camera_calibration import (
                CameraCalibrationError,
                fit_camera_to_screen,
            )

            fit, usable, source, warnings = self._fit_camera_by_targets()
            if fit is None:
                target_error = self.state["geometry"].get("targetFitError")
                if not self._allow_probe_fallback:
                    raise WizardError(
                        "屏幕平面绿色标靶拟合未成功，已拒绝退回到悬停探头映射"
                        "（探头与屏幕内容不在同一平面，会产生视差）。"
                        f"最近错误：{target_error or '未检出绿色标靶'}")
                accepted = self.state["xy"]["accepted"]
                usable = [a for a in accepted
                          if a.get("tipX") is not None and a.get("tipY") is not None]
                if len(usable) < 4:
                    raise WizardError(
                        f"标靶法与探头尖法都不可用：标靶检测失败，且有效探头尖配对只有 "
                        f"{len(usable)} 组（需 ≥4）。请确认手机停留在校准页（标靶法需要），"
                        "或重采带清晰探头尖的样本")
                source = "probe_tip"
                correspondences = [{"cameraX": a["tipX"], "cameraY": a["tipY"],
                                    "screenX": a["screenX"], "screenY": a["screenY"]}
                                   for a in usable]
                try:
                    fit = fit_camera_to_screen(correspondences)
                except CameraCalibrationError as exc:
                    raise WizardError(f"单应拟合失败：{exc}") from exc
                warnings = []
            # 屏幕平面标靶是已知五点，不允许用 RANSAC 恰好拟合四点后把
            # 第五点静默当作外点；这种“数学上 RMS=0、实际区域性发散”
            # 正是边角视觉验证明显偏移的来源。若现场确实遮挡了一点，
            # _fit_camera_by_targets 已显式跳过，传入的点数本来就是 4。
            if source == "screen_target" and len(usable) >= 5 \
                    and fit.get("inlierCount", len(usable)) < len(usable):
                bad = [
                    f"点{item.get('index')}({item.get('name')}) {error:.1f}px"
                    for item, error, keep in zip(
                        usable, fit.get("errors", []), fit.get("inliers", []))
                    if not keep
                ]
                raise WizardError(
                    "屏幕平面标靶存在错误配对，拒绝生成摄像头映射"
                    + (f"：{'；'.join(bad)}" if bad else "")
                    + "。请重新显示标靶并确保摄像头没有误检")
            self._finish_camera_fit(fit, usable, source, warnings, force)
            return {"homography": self.state["geometry"]["homography"],
                    "source": source, "warnings": warnings,
                    "stage": self.state["stage"]}

    def _fit_camera_by_targets(self):
        """屏幕平面标靶法：依次布防 5 个标靶，摄像头检测绿框中心配对名义屏幕像素。

        返回 (fit, usable, source, warnings)；检测失败返回 (None, [], None, [])。
        全程不动机械臂，只布防/拍照/解除。四角先行、中心最后（探头常停在中央，
        会遮挡画在正中的绿框）；被探头遮挡（尖距绿框中心 <90px）的点跳过，
        最多跳 1 个（剩 4 个仍可拟合），超过则提示移开机械臂。
        """
        if self._camera_fetch is None or self._target_detector is None:
            return None, [], None, []
        screen = self.state.get("screen") or {}
        if not screen.get("screenWidth"):
            return None, [], None, []
        try:
            self._require_ime_open()
        except WizardError:
            return None, [], None, []
        sid = self.state["imeSessionId"]
        correspondences, usable, skipped = [], [], []
        self.state["geometry"].pop("targetFitError", None)
        order = [1, 2, 3, 4, 0]  # 四角先行，中心最后
        seq0 = int(self.state.get("imeSequence", 0))
        for n, i in enumerate(order):
            name, u, v = XY_PLAN_POINTS[i]
            sample_id = f"cam{i}-{uuid.uuid4().hex[:6]}"
            seq = seq0 + n + 1
            try:
                self.ime.arm(sid, sample_id, seq, self._target_rect(u, v))
            except ImeError:
                return None, [], None, []
            try:
                detected, _ = self._detect_target()
                gx, gy = float(detected[0]), float(detected[1])
                if len(detected) >= 4 and detected[2] and detected[3]:
                    self.state["geometry"]["tipFrame"] = {
                        "frameW": int(detected[2]), "frameH": int(detected[3])}
            except Exception as exc:
                try:
                    self.ime.disarm(sid, sample_id, seq)
                except ImeError:
                    pass
                self.state["geometry"]["targetFitError"] = f"点{i}({name})：{exc}"
                self._save_state()
                return None, [], None, []
            try:
                self.ime.disarm(sid, sample_id, seq)
            except ImeError:
                pass
            tip = self._detect_tip()
            if tip and tip[0] is not None and ((tip[0] - gx) ** 2 + (tip[1] - gy) ** 2) ** 0.5 < 90:
                skipped.append(f"点{i}({name}) 被探头遮挡")
                continue
            correspondences.append({
                "cameraX": float(gx), "cameraY": float(gy),
                "screenX": u * screen["screenWidth"],
                "screenY": v * screen["screenHeight"]})
            usable.append({"index": i, "name": name, "tipX": round(float(gx), 1),
                           "tipY": round(float(gy), 1),
                           "screenX": u * screen["screenWidth"],
                           "screenY": v * screen["screenHeight"]})
        self.state["imeSequence"] = seq0 + len(order)
        # 中心点是画面映射的关键约束。若它被探头遮挡，四角拟合仍可能
        # 得到数学上“成功”的单应，但中心/中部会出现几十像素的区域性偏差。
        if any("点0(center)" in item for item in skipped):
            self.state["geometry"]["targetFitError"] = (
                "中心标靶被探头遮挡：请先把机械臂移离屏幕中心，再重新拟合摄像头映射")
            return None, [], None, []
        if len(skipped) > 1:
            raise WizardError(f"标靶被机械臂遮挡的点太多（{'、'.join(skipped)}）："
                              "请先用 XY 点动把探头移离屏幕中央，再重新拟合")
        from adapters.screen_ime.camera_calibration import fit_camera_to_screen

        try:
            fit = fit_camera_to_screen(correspondences)
        except Exception:
            return None, [], None, []
        warnings = [f"已跳过被探头遮挡的标靶：{'、'.join(skipped)}"] if skipped else []
        return fit, usable, "screen_target", warnings

    # ---------- F2：无机械动作的标靶反算（规范 §6.2） ----------

    def screen_geometry_from_targets(self) -> dict:
        """四点标靶反算当前屏幕绝对几何（§6.2，全程无任何机械动作）。

        - 前置：当前 IME 会话 open 且由校准页回报 screenWidth/Height/orientation
          （§6.2.1/6.2.2），禁止猜旧屏幕尺寸；会话/设备/绑定不符时拒绝，
          不布置标靶、不执行任何动作。
        - 只布防标靶 + 拍照 + 解除布防；本方法不得下发任何 daemon 运动指令
          （XY/Z/TAP/TAP2/SWIPE/PATH1/自动停车全部禁止，§6.2.3）。
        - 四点 (0.2,0.2)(0.8,0.2)(0.8,0.8)(0.2,0.8) 拟合相机→屏幕单应；中心
          (0.5,0.5) 只做独立验证，绝不并入训练（§6.2.4/6.2.5）。
        - 结果保存 state.geometry.targetGeometry（绝对四角 + 拟合记录）；
          matrix/发布指纹为空，不冒充已校准（§6.2/§7.1），不授予任何运动资格。
        """
        with self._lock:
            self._require_motion_goal()
            self._require_no_auto_run("标靶反算屏幕几何（占用手机布防与画面检测）")
            status = self._require_session(allow_rebind=True)
            if status.get("estop_locked"):
                raise WizardError("急停锁定中：拒绝标靶反算采集")
            session = self._require_ime_open()
            # §6.2.1：屏幕坐标域必须来自本会话、本设备的实际回报
            screen = session.get("screen") or {}
            width, height = screen.get("screenWidth"), screen.get("screenHeight")
            orientation = screen.get("orientation")
            if not (isinstance(width, int) and not isinstance(width, bool) and width > 0
                    and isinstance(height, int) and not isinstance(height, bool) and height > 0
                    and isinstance(orientation, str) and orientation.strip()):
                raise WizardError(
                    "当前校准会话缺少手机回报的屏幕尺寸/方向元数据（校准页 open 上报）："
                    "禁止猜测旧屏幕尺寸或全机截图分辨率（§6.2.1）。"
                    "请让手机校准页重新回报 open 后重试")
            if str(session.get("deviceId") or "") != str(self.state.get("imeDeviceId") or ""):
                raise WizardError(
                    f"校准会话设备（{session.get('deviceId')}）与向导绑定设备"
                    f"（{self.state.get('imeDeviceId')}）不一致：拒绝布置标靶")
            if self._camera_fetch is None or self._target_detector is None:
                raise WizardError("摄像头不可用：调试台未接入取流桥")
            facts = self._read_binding_facts()
            sid = self.state["imeSessionId"]
            deadline = time.time() + self._target_geo_deadline_s

            collected: list[dict] = []
            for name, u, v in TARGET_GEOMETRY_FIT_POINTS:
                collected.append({
                    "name": name, "u": u, "v": v,
                    "observations": self._target_geo_collect(name, u, v, deadline)})
            verify_observations = self._target_geo_collect(
                TARGET_GEOMETRY_VERIFY_POINT[0], TARGET_GEOMETRY_VERIFY_POINT[1],
                TARGET_GEOMETRY_VERIFY_POINT[2], deadline)
            repeat_observations = self._target_geo_collect(
                TARGET_GEOMETRY_FIT_POINTS[0][0], TARGET_GEOMETRY_FIT_POINTS[0][1],
                TARGET_GEOMETRY_FIT_POINTS[0][2], deadline)

            # §6.2.8：首标靶复测与本轮首次观测差 ≤2px，防手机在顺序采点中移动
            first_center = _consensus_center(collected[0]["observations"])
            repeat_center = _consensus_center(repeat_observations)
            repeat_movement = math.hypot(repeat_center[0] - first_center[0],
                                         repeat_center[1] - first_center[1])
            if repeat_movement > TARGET_GEO_REPEAT_MAX_PX:
                raise WizardError(
                    f"首标靶复测与本轮首次观测相差 {repeat_movement:.1f}px"
                    f"（>{TARGET_GEO_REPEAT_MAX_PX:.0f}px）：手机在采点期间移动，"
                    "本轮几何采集拒绝，不保存为可执行基准，请重新采集")

            for item in collected:
                center = _consensus_center(item["observations"])
                item["center"] = [round(center[0], 2), round(center[1], 2)]
            verify_center = _consensus_center(verify_observations)

            # §6.2.4：训练集只含四个固定拟合点；中心验证点绝不并入（C03）
            from adapters.screen_ime.camera_calibration import (
                CameraCalibrationError,
                fit_camera_to_screen,
            )

            correspondences = [
                {"cameraX": item["center"][0], "cameraY": item["center"][1],
                 "screenX": item["u"] * width, "screenY": item["v"] * height}
                for item in collected]
            try:
                fit = fit_camera_to_screen(correspondences)
            except CameraCalibrationError as exc:
                raise WizardError(f"标靶单应拟合失败：{exc.message}") from exc
            if int(fit.get("inlierCount") or 0) != len(correspondences):
                raise WizardError("标靶四点存在错误配对：拒绝反算屏幕几何，请重新采集")

            import numpy as np

            h_matrix = np.array(fit["homography"], np.float64)
            try:
                h_inverse = np.linalg.inv(h_matrix)
            except np.linalg.LinAlgError as exc:
                raise WizardError("标靶单应矩阵不可逆：无法反算屏幕四角") from exc

            def _project(matrix, x: float, y: float) -> tuple[float, float]:
                vec = matrix @ np.array([x, y, 1.0], np.float64)
                if not np.isfinite(vec).all() or abs(float(vec[2])) < 1e-12:
                    raise WizardError("标靶单应映射退化（非有限值或除数接近零）")
                return float(vec[0] / vec[2]), float(vec[1] / vec[2])

            # §6.2.9：中心独立验证，相机域与屏幕域分开记录，均 ≤3px
            center_screen = (TARGET_GEOMETRY_VERIFY_POINT[1] * width,
                             TARGET_GEOMETRY_VERIFY_POINT[2] * height)
            expected_camera = _project(h_inverse, *center_screen)
            observed_screen = _project(h_matrix, *verify_center)
            camera_error = math.hypot(expected_camera[0] - verify_center[0],
                                      expected_camera[1] - verify_center[1])
            screen_error = math.hypot(observed_screen[0] - center_screen[0],
                                      observed_screen[1] - center_screen[1])
            if (camera_error > TARGET_GEO_VERIFY_MAX_PX
                    or screen_error > TARGET_GEO_VERIFY_MAX_PX):
                raise WizardError(
                    f"中心独立验证未通过：相机域反投影误差 {camera_error:.1f}px、"
                    f"屏幕域误差 {screen_error:.1f}px（各需 ≤{TARGET_GEO_VERIFY_MAX_PX:.0f}px）。"
                    "验证点不参与拟合，禁止并入训练消除误差；请检查标靶遮挡后重新采集")

            # §6.2.10：H 逆映射四个屏幕角 → 相机域绝对四角
            frame_w, frame_h = int(facts["frameWidth"]), int(facts["frameHeight"])
            for item in collected + [{"observations": verify_observations},
                                     {"observations": repeat_observations}]:
                for observation in item["observations"]:
                    if (observation.get("frameW") not in (None, frame_w)
                            or observation.get("frameH") not in (None, frame_h)):
                        raise WizardError("标靶观测帧尺寸与设备绑定不一致：拒绝反算")
            screen_corners = ((0.0, 0.0), (float(width), 0.0),
                              (float(width), float(height)), (0.0, float(height)))
            corners = [_project(h_inverse, sx, sy) for sx, sy in screen_corners]
            try:
                from drivers.screen_pose import validate_corners

                validate_corners(corners)
            except (KeyError, TypeError, ValueError) as exc:
                raise WizardError(f"反算屏幕四角无效（非凸/退化）：{exc}") from exc
            if not all(0.0 <= x < frame_w and 0.0 <= y < frame_h for x, y in corners):
                raise WizardError("反算屏幕四角未全部位于有效相机帧内：本轮不通过")
            quad = np.asarray(corners, np.float64)
            area = abs(float(np.dot(quad[:, 0], np.roll(quad[:, 1], 1))
                             - np.dot(quad[:, 1], np.roll(quad[:, 0], 1)))) / 2.0
            area_ratio = area / float(frame_w * frame_h)
            if not TARGET_GEO_AREA_MIN_RATIO <= area_ratio <= TARGET_GEO_AREA_MAX_RATIO:
                raise WizardError(
                    f"反算屏幕四角面积占比 {area_ratio:.1%} 不在 "
                    f"{TARGET_GEO_AREA_MIN_RATIO:.0%}~{TARGET_GEO_AREA_MAX_RATIO:.0%}：本轮不通过")

            last_seq = max(observation["frameSeq"]
                           for item in collected + [{"observations": verify_observations},
                                                    {"observations": repeat_observations}]
                           for observation in item["observations"])
            rounded_corners = [[round(x, 2), round(y, 2)] for x, y in corners]
            target_geometry = {
                "geometrySource": "calibration_targets",
                "corners": rounded_corners,
                "frameW": frame_w,
                "frameH": frame_h,
                "frameSeq": last_seq,
                "screen": {"screenWidth": width, "screenHeight": height,
                           "orientation": orientation},
                "fit": {
                    "homography": fit["homography"],
                    "pointCount": int(fit["pointCount"]),
                    "reprojectionRms": fit["reprojectionRms"],
                    "maxErrorPx": max(float(e) for e in (fit.get("errors") or [0.0])),
                    "points": [
                        {"name": item["name"], "u": item["u"], "v": item["v"],
                         "center": item["center"],
                         "screenX": round(item["u"] * width, 1),
                         "screenY": round(item["v"] * height, 1),
                         "observations": item["observations"]}
                        for item in collected],
                    "verifyPoint": {
                        "name": TARGET_GEOMETRY_VERIFY_POINT[0],
                        "u": TARGET_GEOMETRY_VERIFY_POINT[1],
                        "v": TARGET_GEOMETRY_VERIFY_POINT[2],
                        "center": [round(verify_center[0], 2), round(verify_center[1], 2)],
                        "observations": verify_observations,
                        "cameraErrorPx": round(camera_error, 2),
                        "screenErrorPx": round(screen_error, 2),
                        "maxPx": TARGET_GEO_VERIFY_MAX_PX,
                        "usedInFit": False,
                    },
                },
                "repeatCheck": {
                    "name": TARGET_GEOMETRY_FIT_POINTS[0][0],
                    "maxPx": TARGET_GEO_REPEAT_MAX_PX,
                    "movementPx": round(repeat_movement, 2),
                    "center": [round(repeat_center[0], 2), round(repeat_center[1], 2)],
                    "observations": repeat_observations,
                },
                "areaRatio": round(area_ratio, 4),
                "safeInsetPx": cb.DEFAULT_SAFE_INSET_PX,
                "digest": quad_digest({"corners": rounded_corners,
                                       "frameW": frame_w, "frameH": frame_h}),
                "phoneSessionId": sid,
                "daemonSession": status["session"],
                "binding": deepcopy(facts),
                "capturedAt": _now(),
                "candidateId": f"target-geometry-{uuid.uuid4().hex[:8]}",
                # §6.2/§7.1：标靶反算只证明当前屏幕几何。相对补偿矩阵为空
                # （本记录即当前绝对几何，不用单位矩阵冒充已补偿）；发布指纹
                # 为空（不冒充已校准）。不授予任何机械运动资格。
                "matrix": None,
                "publishedCalibrationFingerprint": None,
            }
            self.state["geometry"]["targetGeometry"] = target_geometry
            self._save_state()
            return {"targetGeometry": deepcopy(target_geometry)}

    def _target_geo_collect(self, name: str, u: float, v: float,
                            deadline: float) -> list[dict]:
        """布防单标靶并采集 3 个递增来源号新帧（§6.2.6/6.2.7）。

        间隔 ≥100ms、同标靶位置两两差 ≤2px；每次显示后观测必须晚于显示
        请求前取得的来源帧。结束/异常都解除布防。全程无机械动作。
        """
        if self._camera_fetch is None or self._target_detector is None:
            raise WizardError("摄像头或绿色标靶检测器不可用")
        sid = self.state["imeSessionId"]
        # §6.2.7：先取得显示请求前的来源帧，观测必须晚于它
        _, pre_seq = self._camera_fetch()
        seq = int(self.state.get("imeSequence", 0)) + 1
        sample_id = f"tg-{uuid.uuid4().hex[:6]}"
        self.ime.arm(sid, sample_id, seq, self._target_rect(u, v))
        self.state["imeSequence"] = seq
        try:
            time.sleep(self._target_draw_wait_s)
            observations: list[dict] = []
            last_capture: float | None = None
            last_error: str | None = None
            while len(observations) < TARGET_GEO_FRAMES_PER_POINT:
                if time.time() >= deadline:
                    raise WizardError(
                        f"标靶 {name} 采集超时（总截止 {TARGET_GEO_DEADLINE_S:.0f}s）："
                        f"{last_error or '未检出绿色标靶'}")
                if last_capture is not None:
                    wait = TARGET_GEO_MIN_FRAME_INTERVAL_S - (time.time() - last_capture)
                    if wait > 0:
                        time.sleep(wait)
                try:
                    jpg, frame_seq = self._camera_fetch()
                    detected = self._target_detector(jpg)
                    if detected is None or len(detected) < 2:
                        raise ScreenEdgeError("检测器未返回标靶中心")
                    if type(frame_seq) is not int or frame_seq < 0:
                        raise ScreenEdgeError("取流缺少有效来源帧序号")
                    if frame_seq <= pre_seq:
                        raise ScreenEdgeError("观测帧未晚于显示请求前的来源帧")
                    if observations and frame_seq <= observations[-1]["frameSeq"]:
                        raise ScreenEdgeError("观测帧来源序号未递增")
                    x, y = float(detected[0]), float(detected[1])
                    for previous in observations:
                        jitter = math.hypot(x - previous["x"], y - previous["y"])
                        if jitter > TARGET_GEO_JITTER_MAX_PX:
                            raise ScreenEdgeError(
                                f"标靶 {name} 位置抖动 {jitter:.1f}px 超过 "
                                f"{TARGET_GEO_JITTER_MAX_PX:.0f}px")
                    observation = {"x": round(x, 2), "y": round(y, 2),
                                   "frameSeq": frame_seq, "capturedAt": _now()}
                    if len(detected) >= 4 and detected[2] and detected[3]:
                        observation["frameW"] = int(detected[2])
                        observation["frameH"] = int(detected[3])
                    observations.append(observation)
                    last_capture = time.time()
                    last_error = None
                except ScreenEdgeError as exc:
                    last_error = str(exc)
                    if time.time() >= deadline:
                        raise WizardError(f"标靶 {name} 采集失败：{exc}") from exc
                    time.sleep(0.2)
            return observations
        finally:
            try:
                self.ime.disarm(sid, sample_id, seq)
            except ImeError:
                pass

    def _target_geometry_edge(self, status: dict, facts: dict, sid: str) -> dict | None:
        """本轮标靶反算几何作为定位参考（§6.1.3）：仅会话/绑定一致时可用。"""
        target_geometry = self.state["geometry"].get("targetGeometry")
        if not isinstance(target_geometry, dict) or not target_geometry.get("corners"):
            return None
        keys = ("cameraId", "cameraDeviceUniqueId", "bindingRevision",
                "daemonAddr", "frameWidth", "frameHeight")
        binding = target_geometry.get("binding") or {}
        if (target_geometry.get("phoneSessionId") != sid
                or target_geometry.get("daemonSession") != status.get("session")
                or any(str(binding.get(key)) != str(facts.get(key)) for key in keys)):
            return None
        return {"corners": deepcopy(target_geometry["corners"]),
                "frameW": target_geometry.get("frameW"),
                "frameH": target_geometry.get("frameH"),
                "frameSeq": target_geometry.get("frameSeq"),
                "geometrySource": "calibration_targets",
                "digest": target_geometry.get("digest")}

    def _build_manual_phone_reference(self, status: dict, facts: dict,
                                      sid: str) -> dict:
        """§6.1 定位顺序构建手机定位基准：已存边缘基准 → 实测边缘 → 本轮标靶反算。

        都不可用时按 §6.1.5 报具体缺失的证据与下一步，不删除参数、不假称设备离线。
        """
        base = {"binding": deepcopy(facts), "daemonSession": status["session"],
                "phoneSessionId": sid}
        saved = deepcopy(self.state["geometry"].get("edge"))
        if saved is not None:
            # 有基准时由稳定性检查负责实测复核（fit_pose 对比当前帧）
            if saved.get("frameSeq") is None:
                saved["frameSeq"] = self._camera_status()["frameSeq"]
            return {"edge": saved, **base}
        edge_error: WizardError | None = None
        try:
            edge = self._edge_quad()
        except WizardError as exc:
            edge_error = exc
        else:
            return {"edge": edge, **base}
        target = self._target_geometry_edge(status, facts, sid)
        if target is not None:
            return {"edge": target, **base}
        saved_geometry = self.state["geometry"].get("targetGeometry")
        if isinstance(saved_geometry, dict) and saved_geometry.get("corners"):
            target_reason = ("标靶反算结果属于其他会话/绑定（坐标基准或手机会话已变化），"
                             "需重新执行标靶反算")
        else:
            target_reason = "尚未执行无机械动作的标靶反算"
        raise PoseUnlocatable(
            f"手机屏幕四边不可定位：{edge_error}；{target_reason}。"
            "缺少的证据：当前帧四边轮廓证据，或校准页标靶反算几何。"
            "下一步：手机停留在校准页时执行无机械动作的标靶反算"
            "（screen_geometry_from_targets）后重试，或改善轮廓可见度。"
            "已保存参数保持不变，设备连接状态不变。")

    def _verify_manual_phone_reference(self, reference: dict, *, implicit: bool,
                                       status: dict, facts: dict, sid: str) -> dict:
        """校验手机未移动后返回生效基准。

        边缘基准用实测复核（fit_pose 对比当前帧）；标靶基准复显首拟合标靶
        复核（§6.2.8）。implicit（向导自建基准）在轮廓不足时按 §6.1.3 降级到
        本轮标靶几何再验一次；显式传入的基准必须原样通过，不暗中换证据源。
        """
        try:
            self._manual_phone_pose_stability(reference)
            return reference
        except AutoRunError as exc:
            if not implicit:
                raise WizardError(exc.message) from exc
            target = self._target_geometry_edge(status, facts, sid)
            if target is None:
                raise WizardError(exc.message) from exc
            fallback = {"edge": target, "binding": deepcopy(facts),
                        "daemonSession": status["session"], "phoneSessionId": sid}
            self._manual_phone_pose_stability(fallback)
            return fallback

    def _manual_phone_pose_stability(self, reference: dict) -> None:
        edge = reference.get("edge") or {}
        if edge.get("geometrySource") == "calibration_targets":
            self._verify_target_geometry_reference(reference)
            return
        # 手动 Z 点击是真实接触动作（非采样姿态复核）：H01 不放宽（reject）。
        self._auto_runner._auto_require_stable_phone(reference, on_out_of_range="reject")

    def _verify_target_geometry_reference(self, reference: dict) -> None:
        """标靶几何基准的实时复核：复显首拟合标靶防手机移动（§6.1.3/§6.2.8）。

        已有触点样本布防时不能占用布防通道，退化为探头落点检查（§6.3：
        视觉只排除明显屏外位置，不能证明悬停高度或消除视差）。
        """
        edge = reference.get("edge") or {}
        corners = edge.get("corners")
        if not corners:
            raise WizardError("标靶反算几何缺少四角：请重新执行标靶反算")
        session = self._require_ime_open()
        tip = self._detect_tip()
        if tip is not None and tip[0] is not None:
            # §6.3 落点区域检查：可见探头必须在反算屏幕内且距边缘 ≥safeInsetPx
            self._require_tip_inside_target_geometry(tip, corners)
        if session.get("armed") is not None:
            return  # 布防通道被触点样本占用：只做落点检查，不占布防
        target_geometry = self.state["geometry"].get("targetGeometry") or {}
        points = (target_geometry.get("fit") or {}).get("points") or []
        first = next((p for p in points
                      if p.get("name") == TARGET_GEOMETRY_FIT_POINTS[0][0]), None)
        if not first or not first.get("center"):
            raise WizardError("标靶几何缺少首拟合点记录：请重新执行标靶反算")
        observations = self._target_geo_collect(
            TARGET_GEOMETRY_FIT_POINTS[0][0], TARGET_GEOMETRY_FIT_POINTS[0][1],
            TARGET_GEOMETRY_FIT_POINTS[0][2],
            time.time() + self._target_geo_deadline_s)
        center = _consensus_center(observations)
        movement = math.hypot(center[0] - float(first["center"][0]),
                              center[1] - float(first["center"][1]))
        if movement > TARGET_GEO_REPEAT_MAX_PX:
            raise WizardError(
                f"标靶复核发现手机移动 {movement:.1f}px"
                f"（>{TARGET_GEO_REPEAT_MAX_PX:.0f}px）：拒绝执行本次点击，"
                "请重新执行标靶反算")
        edge["frameSeq"] = observations[-1]["frameSeq"]

    def _require_tip_inside_target_geometry(self, tip, corners) -> None:
        """§6.3：可见探头的预期落点必须在反算屏幕内且距边缘 ≥safeInsetPx。"""
        import numpy as np

        if not tip or tip[0] is None:
            return  # 探头不可见：视觉无法排除屏外位置，不能伪造"已验证"
        quad = np.asarray(corners, np.float64)
        if quad.shape != (4, 2) or not np.isfinite(quad).all():
            raise WizardError("标靶反算四角无效：请重新执行标靶反算")
        inset = float(cb.DEFAULT_SAFE_INSET_PX)
        point = np.array([float(tip[0]), float(tip[1])], np.float64)
        for index in range(4):
            a = quad[index]
            edge_vec = quad[(index + 1) % 4] - a
            length = float(np.linalg.norm(edge_vec))
            if length < 1e-6:
                raise WizardError("标靶反算四角退化：请重新执行标靶反算")
            normal = np.array([edge_vec[1], -edge_vec[0]]) / length
            others = [quad[j] for j in range(4) if j not in (index, (index + 1) % 4)]
            if any(float(np.dot(normal, other - a)) < 0 for other in others):
                normal = -normal
            distance = float(np.dot(normal, point - a))
            if distance < inset:
                raise WizardError(
                    f"可见探头距手机屏幕边缘仅 {distance:.0f}px（需 ≥{inset:.0f}px）："
                    "拒绝原地 Z 确认，请先把探头移到屏幕内安全悬停位置")

    def _pick_verify_point(self) -> tuple[float, float, str]:
        """选一个不被探头遮挡的验证点：距探头尖（摄像头像素）最远的标定点。

        前一次验证后探头停在验证点上方，会挡住画在同一位置的绿框
        （现场：第 2/3 次检测偏移、第 4 次起检测失败、绿框"一闪即逝"）。
        探头尖未检出时回退中心。
        """
        tip = self._detect_tip()
        if not tip or tip[0] is None:
            return 0.5, 0.5, "center（探头尖未检出，默认中心）"
        homography = self.state["geometry"].get("homography") or {}
        import numpy as np

        h_matrix = np.array(homography.get("homography") or [], np.float64)
        if h_matrix.size != 9:
            return 0.5, 0.5, "center（单应不可逆，默认中心）"
        try:
            h_inv = np.linalg.inv(h_matrix)
        except np.linalg.LinAlgError:
            return 0.5, 0.5, "center（单应矩阵不可逆，默认中心）"
        best, best_dist, best_note = (0.5, 0.5), -1.0, "center"
        for name, u, v in XY_PLAN_POINTS:
            screen = self.state.get("screen") or {}
            px = np.array([[[u * (screen.get("screenWidth") or 1),
                             v * (screen.get("screenHeight") or 1), 1.0]]], np.float64)
            cam = (h_inv @ px.reshape(3, 1)).reshape(3)
            cam = (cam[0] / cam[2], cam[1] / cam[2])
            dist = ((cam[0] - tip[0]) ** 2 + (cam[1] - tip[1]) ** 2) ** 0.5
            if dist > best_dist:
                best, best_dist, best_note = (u, v), dist, name
        return best[0], best[1], f"{best_note}（已避开探头遮挡，距探头 {best_dist:.0f}px）"

    def visual_verify(self, u: float | None = None, v: float | None = None,
                      tap: bool = True) -> dict:
        """「摄像头判定位置→移动机械臂点击」验证（用户要求的链路，逐项给出数字）。

        流程：选择未被探头遮挡的标定点 → 布防标靶（手机画绿框）→ 摄像头检测
        绿框中心（屏幕平面）→ 检测探头尖 → 单应换算绿框的屏幕像素 → 逆仿射得
        机械坐标 → 自动走位 → 点击 → 手机回报真实接触点。三方对照：
        绿框名义位置 / 摄像头判定的位置 / 手机回报的接触点。
        """
        with self._lock:
            self._require_motion_goal()
            self._require_no_auto_run("手动视觉验证（真实走位点击）")
            self._require_stage(STAGE_XY, STAGE_SCREEN_GEOMETRY, STAGE_TARGET,
                                STAGE_DEBUG_ACCEPTANCE, STAGE_WECHAT_ACCEPTANCE)
            self._require_session()
            homography = self.state["geometry"].get("homography")
            fit = self.state["xy"].get("fit")
            if not homography or not fit:
                raise WizardError("单应/XY 拟合缺失：请先完成 D 阶段")
            if self._camera_fetch is None or self._target_detector is None:
                raise WizardError("摄像头不可用：调试台未接入取流桥")
            screen = self.state.get("screen") or {}
            note = "指定位置"
            if u is None or v is None:
                u, v, note = self._pick_verify_point()
            nominal = (u * (screen.get("screenWidth") or 0),
                       v * (screen.get("screenHeight") or 0))

            sid = self.state["imeSessionId"]
            sample_id = f"vv-{uuid.uuid4().hex[:6]}"
            arm_seq = int(self.state.get("imeSequence", 0)) + 1
            arm_time = time.time()
            self.ime.arm(sid, sample_id, arm_seq, self._target_rect(u, v))
            self.state["imeSequence"] = arm_seq
            frame_seq = 0
            from adapters.screen_ime.camera_calibration import CameraScreenTransform

            transform = CameraScreenTransform.from_dict(
                {"homography": homography["homography"]})
            try:
                detected, frame_seq = self._detect_target()
                target_cam = [round(float(detected[0]), 1), round(float(detected[1]), 1)]
                tip = self._detect_tip()
            except Exception as exc:
                try:
                    self.ime.disarm(sid, sample_id, arm_seq)
                except ImeError:
                    pass
                raise WizardError(f"视觉检测失败：{exc}") from exc

            cam_screen = transform.screen_from_camera(target_cam[0], target_cam[1])
            screen_w = float(screen.get("screenWidth") or 0)
            screen_h = float(screen.get("screenHeight") or 0)
            margin = float(cb.DEFAULT_SAFE_INSET_PX)
            if (screen_w <= 0 or screen_h <= 0
                    or not (-margin <= cam_screen[0] <= screen_w + margin)
                    or not (-margin <= cam_screen[1] <= screen_h + margin)):
                raise WizardError("摄像头判定位置落在屏幕范围外：拒绝驱动机械臂，请重新标定")
            camera_vs_nominal = round(
                ((cam_screen[0] - nominal[0]) ** 2
                 + (cam_screen[1] - nominal[1]) ** 2) ** 0.5, 1)
            if camera_vs_nominal > cb.XY_RESIDUAL_MAX_PX:
                try:
                    self.ime.disarm(sid, sample_id, arm_seq)
                except ImeError:
                    pass
                raise WizardError(
                    f"摄像头判定位置偏离标靶 {camera_vs_nominal:.1f}px，"
                    f"超过 {cb.XY_RESIDUAL_MAX_PX:g}px；已拒绝走位和点击，请重新拟合摄像头映射")

            robot_x, robot_y = cb.robot_from_screen(
                fit, cam_screen[0], cam_screen[1])

            contact = None
            if tap:
                def move() -> None:
                    z = self.state["z"]
                    self._daemon_tap(float(z["tap"]["depth"]), float(z["tap"]["feed"]),
                                     int(z["tap"]["dwellMs"]))

                self.daemon.ensure_debug_lease()
                self._goto_robot(robot_x, robot_y)
                pre = {"sequence": arm_seq, "armedAt": arm_time}  # 绿框显示等待已计入
                report = self._tap_with_report(sample_id, move,
                                               pre_armed=pre)
                contact = [round(report["screenX"], 1), round(report["screenY"], 1)]

            def dist(a, b):
                return round(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5, 1)

            result = {
                "verifyPoint": note,
                "nominalScreen": [round(nominal[0], 1), round(nominal[1], 1)],
                "targetCam": target_cam, "tipCam": list(tip) if tip else None,
                "cameraJudgedScreen": [round(cam_screen[0], 1), round(cam_screen[1], 1)],
                "contactScreen": contact,
                "cameraVsNominalPx": camera_vs_nominal,
                "contactVsNominalPx": dist(contact, nominal) if contact else None,
                "contactVsCameraPx": dist(contact, cam_screen) if contact else None,
                "frameSeq": frame_seq,
                "ts": _now(),
            }
            self.state.setdefault("visualVerifies", []).append(result)
            del self.state["visualVerifies"][:-20]
            self._save_state()
            return {"result": result,
                    "hint": "对照三行数字：摄像头判定 vs 名义标靶（单应的跨平面误差）、"
                            "手机接触点 vs 名义标靶（映射真实精度）、接触点 vs 摄像头判定"
                            "（你要的『摄像头判定→点击』链路误差）"}

    def visual_verify_all(self) -> dict:
        """5 点视觉验证（用户拍板）：中心+四角逐点执行"摄像头判定→走位→点击→回报"。

        顺序 = 距当前探头尖由远及近的逆序（先点最远的），每点点击后探头停在该点、
        下一点自然不同位置，不会遮挡后续绿框。结果汇总进校准档案（bundle.camera。
        visualVerify）：全链路误差最大值、是否有超差点，供验收与留痕。
        """
        with self._lock:
            self._require_motion_goal()
            tip = self._detect_tip()
            points = list(XY_PLAN_POINTS)
            if tip and tip[0] is not None:
                import numpy as np

                homography = self.state["geometry"].get("homography") or {}
                try:
                    h_inv = np.linalg.inv(np.array(homography.get("homography"), np.float64))
                except np.linalg.LinAlgError as exc:
                    raise WizardError("单应矩阵不可逆：请重新完成摄像头标定") from exc
                screen = self.state.get("screen") or {}

                def cam_pos(u, v):
                    px = np.array([[[u * (screen.get("screenWidth") or 1),
                                     v * (screen.get("screenHeight") or 1), 1.0]]],
                                  np.float64)
                    c = (h_inv @ px.reshape(3, 1)).reshape(3)
                    return c[0] / c[2], c[1] / c[2]

                points.sort(key=lambda p: -((cam_pos(p[1], p[2])[0] - tip[0]) ** 2
                                            + (cam_pos(p[1], p[2])[1] - tip[1]) ** 2))
            results = []
            for name, u, v in points:
                try:
                    r = self.visual_verify(u=u, v=v, tap=True)["result"]
                    r["name"] = name
                    checks = {
                        "cameraVsNominalPx": r.get("cameraVsNominalPx"),
                        "contactVsNominalPx": r.get("contactVsNominalPx"),
                        "contactVsCameraPx": r.get("contactVsCameraPx"),
                    }
                    failed = [f"{name}>{cb.XY_RESIDUAL_MAX_PX:g}px"
                              for name, value in checks.items()
                              if value is None or float(value) > cb.XY_RESIDUAL_MAX_PX]
                    r["passed"] = not failed
                    if failed:
                        r["error"] = "；".join(failed)
                except (WizardError, TouchReportTimeout) as exc:
                    r = {"name": name, "u": u, "v": v, "passed": False,
                         "error": str(exc)}
                results.append(r)
            errors = [r["contactVsNominalPx"] for r in results
                      if r.get("contactVsNominalPx") is not None]
            camera_errs = [r["contactVsCameraPx"] for r in results
                           if r.get("contactVsCameraPx") is not None]
            summary = {
                "points": results,
                "checkedAt": _now(),
                "contactVsNominalMaxPx": max(errors) if errors else None,
                "contactVsCameraMaxPx": max(camera_errs) if camera_errs else None,
                "passedCount": sum(1 for r in results if r.get("passed")),
                "passed": bool(results) and all(r.get("passed") for r in results),
            }
            self.state["visualVerify"] = summary
            self._sync_bundle()
            self._save_state()
            return {"summary": summary,
                    "hint": ("5 点全部通过，可进入 E 区验收"
                             if summary["passed"]
                             else f"{summary['passedCount']}/5 通过：见各点误差，"
                                  "超差点多请重做单应拟合或重采 XY 样本")}

    def _finish_camera_fit(self, fit, usable, source, warnings, force) -> None:
        """共用的内点校验、留痕与落盘（两种配对来源一致）。"""
        excluded = [{"index": a["index"], "name": a["name"], "errorPx": e}
                    for a, keep, e in zip(usable, fit["inliers"], fit["errors"])
                    if not keep]
        if excluded:
            detail = "；".join(f"点{x['index']}({x['name']}) 偏 {x['errorPx']:.0f}px"
                              for x in excluded)
            warnings.append(f"标靶/探头尖配对异常已自动剔除：{detail}")
        if fit["inlierCount"] < 4:
            raise WizardError(f"坏配对太多（内点不足 4 个）：{detail or '检测异常'}")
        rms = fit["reprojectionRms"]
        if rms > cb.CAMERA_REPROJECTION_MAX_PX and not force:
            raise WizardError(f"重投影残差 {rms:.2f}px 超过阈值 "
                              f"{cb.CAMERA_REPROJECTION_MAX_PX}px"
                              "（可重试或强行拟合放行并留痕）")
        if rms > cb.CAMERA_REPROJECTION_MAX_PX and force:
            warnings.append(f"已强行拟合：重投影残差 {rms:.2f}px 超过阈值 "
                            f"{cb.CAMERA_REPROJECTION_MAX_PX}px（操作者强制放行）")
        self.state["geometry"]["homography"] = {
            "homography": fit["homography"],
            "reprojectionRmsPx": round(rms, 3),
            "source": source,
            "sampleCount": len(usable),
            "inlierCount": fit["inlierCount"],
            "excludedPoints": excluded,
            "forced": bool(force and rms > cb.CAMERA_REPROJECTION_MAX_PX),
            "warnings": warnings,
            "frameW": (self.state["geometry"].get("tipFrame") or {}).get("frameW"),
            "frameH": (self.state["geometry"].get("tipFrame") or {}).get("frameH"),
            "fittedAt": _now(),
        }
        self._save_state()
        # 运行期目标定位改为 OCR（用户拍板）：不再经过固定目标登记阶段，
        # 摄像头单应就绪后直接进入调试台验收
        if self.state["stage"] == STAGE_SCREEN_GEOMETRY:
            if self.state.get("goal") == GOAL_OBSERVATION:
                # Auto_CS#20：观测档停在屏幕几何阶段，由 finish 直接产出
                # observation_ready（跳过 Z/XY/标靶/验收动作阶段）。
                return
            self._advance(STAGE_DEBUG_ACCEPTANCE)

    # ---------- 目标阶段（§9） ----------

    def target_import(self) -> dict:
        """旧 targets.json 兼容导入（validated=false 待重测，§10 迁移策略）。"""
        with self._lock:
            bundle = cb.import_legacy(self.device, self.data_root,
                                      ime_device_id=self.state["imeDeviceId"])
            for name, entry in (bundle.get("targets") or {}).items():
                self.state["targets"][name] = {k: entry.get(k) for k in
                                               ("nx", "ny", "targetMode", "validated")}
            self._save_state()
            return {"imported": sorted(self.state["targets"])}

    def target_save(self, name: str, nx: float, ny: float) -> dict:
        """登记/更新目标画面位置（仅存位置；机械坐标执行期按映射现算）。"""
        with self._lock:
            self._require_motion_goal()
            self._require_stage(STAGE_TARGET, STAGE_DEBUG_ACCEPTANCE,
                                STAGE_WECHAT_ACCEPTANCE)
            if name not in TARGET_DISPLAY_NAMES:
                raise WizardError(f"未知目标：{name}（已知：{'、'.join(sorted(TARGET_DISPLAY_NAMES))}）")
            if not 0 <= float(nx) <= 1 or not 0 <= float(ny) <= 1:
                raise WizardError("画面位置必须是 0~1 的归一化坐标")
            self.state["targets"][name] = {"nx": round(float(nx), 4),
                                           "ny": round(float(ny), 4),
                                           "targetMode": "hover_only" if name == "ime_delete_key" else "tap",
                                           "validated": False}
            self._save_state()
            return {"saved": name}

    def target_test(self, name: str) -> dict:
        """目标测试点击：视觉/档案位置 → 单应 → 逆仿射 → 自动走位 → 布防点击 → 回报误差（§8.3）。"""
        with self._lock:
            self._require_motion_goal()
            self._require_no_auto_run("手动目标测试（真实走位点击）")
            self._require_stage(STAGE_TARGET)
            self._require_session()
            entry = self.state["targets"].get(name)
            if entry is None:
                raise WizardError(f"目标未登记：{name}")
            homography = self.state["geometry"].get("homography")
            fit = self.state["xy"].get("fit")
            if not homography or not fit:
                raise WizardError("摄像头/XY 映射尚未就绪：请先完成视觉校准")
            screen = self.state.get("screen") or {}
            # 1) 画面归一化 → 摄像头像素 →（单应）→ 屏幕像素 →（逆仿射）→ 机械坐标
            from adapters.screen_ime.camera_calibration import CameraScreenTransform

            frame_w = float(homography.get("frameW") or 0)
            frame_h = float(homography.get("frameH") or 0)
            if not frame_w or not frame_h:
                raise WizardError("单应档案缺帧尺寸：请重新拟合摄像头映射")
            camera_x = float(entry["nx"]) * frame_w
            camera_y = float(entry["ny"]) * frame_h
            transform = CameraScreenTransform.from_dict({"homography": homography["homography"]})
            screen_x, screen_y = transform.screen_from_camera(camera_x, camera_y)
            robot_x, robot_y = cb.robot_from_screen(
                fit, screen_x, screen_y)
            # 2) 回悬停 → 单条 XY 直达 → 布防 → 点击 → 回报
            self.daemon.ensure_debug_lease()
            self._goto_robot(robot_x, robot_y)
            z = self.state["z"]
            expected = {"u": round(screen_x / max(1, screen.get("screenWidth") or 1), 4),
                        "v": round(screen_y / max(1, screen.get("screenHeight") or 1), 4)}
            sample_id = f"tg-{name}-{uuid.uuid4().hex[:6]}"

            def move() -> None:
                self._daemon_tap(float(z["tap"]["depth"]), float(z["tap"]["feed"]),
                                 int(z["tap"]["dwellMs"]))

            report = self._tap_with_report(sample_id, move,
                                           target=self._target_rect(expected["u"], expected["v"]))
            error = ((screen_x - report["screenX"]) ** 2 +
                     (screen_y - report["screenY"]) ** 2) ** 0.5
            result = {"name": name, "sampleId": sample_id,
                      "predictedScreen": [round(screen_x, 1), round(screen_y, 1)],
                      "reportedScreen": [report["screenX"], report["screenY"]],
                      "robotTarget": [round(robot_x, 2), round(robot_y, 2)],
                      "errorPx": round(error, 2),
                      "withinTolerance": bool(error <= cb.XY_RESIDUAL_MAX_PX),
                      "ts": _now()}
            self.state.setdefault("targetTrials", [])
            self.state["targetTrials"].append(result)
            del self.state["targetTrials"][:-50]
            self._save_state()
            return {"result": result}

    def target_accept(self, name: str, accept: bool = True) -> dict:
        """人工确认目标测试结果：接受后才 validated（§8.3 接受此点）。"""
        with self._lock:
            self._require_motion_goal()
            self._require_stage(STAGE_TARGET, STAGE_DEBUG_ACCEPTANCE)
            entry = self.state["targets"].get(name)
            if entry is None:
                raise WizardError(f"目标未登记：{name}")
            trials = [t for t in self.state.get("targetTrials", []) if t["name"] == name]
            if accept:
                if not trials:
                    raise WizardError("该目标还没有测试记录：请先「测试目标」")
                entry["validated"] = True
                entry["testedAt"] = trials[-1]["ts"]
                entry["errorPx"] = trials[-1]["errorPx"]
                entry["sendAuthorized"] = False  # 真实发送仍由 业务 任务级证据门禁授权
            else:
                entry["validated"] = False
            self._sync_bundle()
            self._save_state()
            return {"name": name, "validated": entry["validated"]}

    def _goto_robot(self, rx: float, ry: float, *, z_safe: float | None = None,
                    record: dict | None = None, before_motion=None) -> None:
        """回悬停 → XY 单条直达 → 残差检查（与调试台 goto 同规则）。

        v1.1 自动 runner 扩展（§35）：z_safe 显式指定会话 Z 安全高度（自动校准
        的 zSafe，不与旧 hoverZ 混用）；record 传入时记录 preMove/commanded/
        postMove MPos 与残差（controller position check，不是物理位置反馈）。
        旧手动调用不传新参数，行为不变。
        """
        if z_safe is not None:
            hover_z = float(z_safe)
        else:
            # 手动流 hoverZ 必经 B 阶段采集；无任何基准时拒绝猜高度
            # （0.0 是合法悬停值，不做真值判断；审计 R5）
            hover_z = (self.state.get("z") or {}).get("hoverZ")
            if hover_z is None:
                raise WizardError("无可用悬停高度基准（z_safe 未传且 state z hoverZ 缺失）："
                                  "拒绝走位——先完成 B 阶段 Z 参数保存，或调用方显式传入 zSafe")
            hover_z = float(hover_z)
        rx, ry = float(rx), float(ry)
        mpos = self._read_position()
        if record is not None:
            record["preMoveMPos"] = {k: round(float(mpos.get(k)), 3) for k in ("x", "y", "z")}
        dz = hover_z - float(mpos["z"])
        if abs(dz) >= 0.05:
            for chunk in split_segments(dz, 9.5):
                if before_motion is not None:
                    before_motion()
                self.daemon.cmd(f"Z {chunk:.3f} F{min(1500.0, DEFAULT_TAP_FEED):g}")
        mpos = self._read_position()
        dx, dy = rx - float(mpos["x"]), ry - float(mpos["y"])
        if (dx * dx + dy * dy) ** 0.5 > 300.0:
            raise WizardError(f"目标位移超 300mm：映射可能异常，拒绝走位")
        if abs(dx) > XY_STROKE_MAX or abs(dy) > XY_STROKE_MAX:
            raise WizardError(f"XY 单次直达位移超出上限 {XY_STROKE_MAX:g}mm")
        if abs(dx) > 0.05 or abs(dy) > 0.05:
            if before_motion is not None:
                before_motion()
            mpos = self.daemon.cmd(f"XY {dx:.3f} {dy:.3f} F{CALIBRATED_TRAVEL_FEED:g}")
        mpos = self._read_position()
        residual = ((rx - float(mpos["x"])) ** 2 + (ry - float(mpos["y"])) ** 2) ** 0.5
        if record is not None:
            record["commandedXY"] = [round(rx, 3), round(ry, 3)]
            record["postMoveMPos"] = {k: round(float(mpos.get(k)), 3) for k in ("x", "y", "z")}
            record["postMoveResidualMm"] = round(residual, 3)
        if residual > 3.0:
            raise WizardError(f"走位后残差 {residual:.1f}mm 过大：映射可能异常")

    # ---------- 验收与收尾（§13/§16） ----------

    @staticmethod
    def _normalize_ocr_text(value: str) -> str:
        import re
        return re.sub(r"\s+", "", str(value or "")).strip().lower()

    def _park_arm_before_ocr(self, record: dict, *, before_motion=None) -> None:
        """OCR 前把机械臂移到停止位（SAFE PARK）。

        机械臂悬在屏幕中间会遮挡摄像头视线，导致验收候选文字搜索失败
        （2026-09-04 现场反馈）。SAFE PARK 已确认时，验收执行第一步先撤离；
        未确认（手动流程未设置）则跳过，保持原流程与失败语义。
        """
        park = self.state.get("safePark") or {}
        if not park.get("confirmed"):
            with self._auto_runner._auto_lock:
                park = ((self._auto_runner._auto.get("run") or {}).get("safePark") or {})
        if not park.get("confirmed") or park.get("parkX") is None:
            return
        z_state = self.state.get("z") or {}
        z_hover = next((float(v) for v in (park.get("parkZ"),
                                           (z_state.get("autoSafe") or {}).get("zSafe"),
                                           z_state.get("hoverZ"))
                        if v is not None), None)
        if z_hover is None:
            return  # 无可信悬停基准：跳过停靠（保持原流程），不猜高度
        self.daemon.ensure_debug_lease()
        self._goto_robot(float(park["parkX"]), float(park["parkY"]), z_safe=z_hover,
                         before_motion=before_motion)
        record["parkedBeforeOcr"] = {"parkX": park["parkX"], "parkY": park["parkY"],
                                     "parkZ": park.get("parkZ")}
        # 停靠后等推流出新帧（~8fps），确保 OCR 用的是机械臂撤离后的画面
        time.sleep(0.8)

    # 输入法候选区下界（竖屏屏幕高度比例，2026-09-04 现场标定）：实测候选栏在
    # ~61%、输入法面板顶边 ~55%；51% 留出余量兼容不同 App 的输入法高度差
    # （操作员 2026-09-04 手绘范围换算）。上方是页面内容区（同文本回显干扰）。
    IME_REGION_SCREEN_FLOOR = 0.51

    def _screen_projector(self, homography, screen_size):
        """返回 词中心(摄像头px) -> (sx_ratio, sy_ratio) 投影函数；参数缺失返回 None。"""
        if not homography or not screen_size:
            return None
        try:
            import numpy as np
            H = np.array(homography["homography"] if isinstance(homography, dict)
                         else homography, dtype=float).reshape(3, 3)
            sw = float(screen_size[0]); sh = float(screen_size[1])
            if sw <= 0 or sh <= 0:
                return None
        except Exception:
            return None

        def project(x, y):
            v = H @ np.array([float(x), float(y), 1.0])
            return (v[0] / v[2] / sw, v[1] / v[2] / sh)

        return project

    def _ocr_text_match_score(self, ocr_text: str, wanted: str) -> int:
        """前缀/包含匹配得分（0=不匹配）。

        输入法对过长候选会省略显示（如「统一校准…」），且省略形式不固定：
        OCR 文本去掉省略号后，是期望文本的前缀（≥min(3, 期望字数) 字）或
        包含完整期望文本即命中。返回命中字数用于多候选排序。
        """
        stripped = (self._normalize_ocr_text(ocr_text)
                    .replace("…", "").replace("⋯", "").replace("...", ""))
        if not stripped:
            return 0
        if wanted in stripped:
            return len(wanted)
        if stripped in wanted and len(stripped) >= min(3, len(wanted)):
            return len(stripped)
        return 0

    def _locate_text_by_ocr(self, expected_text: str, *, timeout: float = 8.0,
                            homography=None, screen_size=None) -> dict:
        """在摄像头画面中找输入法刚显示的测试文字，返回像素框中心。

        搜索区限定（2026-09-04）：词中心经已标定单应 H 投影回竖屏屏幕坐标，
        只接受屏幕高度 ≥ IME_REGION_SCREEN_FLOOR 的输入法区域——画面里
        "输入法在左/屏幕横放"的方向陷阱由投影自动消除。区域内找不到不回退
        屏幕上部（那里可能有同文本回显，误点比找不到更危险）。

        Vision 返回的是摄像头 PNG 的左上原点像素坐标，直接把 bbox 中心
        送入 camera->screen->robot 链路。
        """
        if self._camera_fetch is None or self._ocr_engine is None:
            raise WizardError("未配置本地 OCR 引擎，无法自动寻找输入法测试文字；"
                              "请先在摄像头画面上用「仅标记」模式提供坐标")
        wanted = self._normalize_ocr_text(expected_text)
        if not wanted:
            raise WizardError("OCR 目标文字不能为空")
        project = self._screen_projector(homography, screen_size)
        deadline = time.time() + max(1.0, float(timeout))
        last = None
        while time.time() < deadline:
            jpg, frame_seq = self._camera_fetch()
            try:
                result = self._ocr_engine.recognize(
                    jpg, region="input", request_id=f"cal-input-{uuid.uuid4().hex[:10]}")
            except Exception as exc:
                last = f"OCR 识别失败：{exc}"
                time.sleep(0.35)
                continue

            def in_ime_region(word) -> bool:
                if project is None:
                    return True  # 无标定信息时不过滤（验收流程 D 阶段后必有 H）
                sx, sy = project(word.x + word.width / 2, word.y + word.height / 2)
                return sy >= self.IME_REGION_SCREEN_FLOOR

            # 先尝试单个 OCR 行/词，输入法候选通常会作为一行返回。
            scored: list[tuple[int, float, list]] = []  # (命中字数, 置信度, 词组)

            def consider(group):
                if not all(in_ime_region(w) for w in group):
                    return
                joined = "".join(self._normalize_ocr_text(w.text) for w in group)
                score = self._ocr_text_match_score(joined, wanted)
                if score:
                    conf = min(float(w.confidence) for w in group)
                    scored.append((score, conf, group))

            words = sorted(list(result.words), key=lambda w: (w.y + w.height / 2, w.x))
            for word in words:
                consider([word])
            # Vision 可能把中文短语拆成多个词；按相邻横向词滑窗拼接。
            rows: list[list] = []
            for word in words:
                cy = word.y + word.height / 2
                row = next((r for r in rows
                            if abs(cy - (r[0].y + r[0].height / 2))
                            <= max(word.height, r[0].height) * 0.8), None)
                if row is None:
                    rows.append([word])
                else:
                    row.append(word)
            for row in rows:
                row.sort(key=lambda w: w.x)
                for start in range(len(row)):
                    for end in range(start + 1, len(row) + 1):
                        consider(row[start:end])
            if scored:
                # 前缀命中字数优先（截断显示时取最完整的），置信度次之
                scored.sort(key=lambda item: (-item[0], -item[1]))
                _, _, chosen = scored[0]
                x1 = min(float(w.x) for w in chosen)
                y1 = min(float(w.y) for w in chosen)
                x2 = max(float(w.x + w.width) for w in chosen)
                y2 = max(float(w.y + w.height) for w in chosen)
                return {
                    "text": expected_text,
                    "cameraX": round((x1 + x2) / 2, 2),
                    "cameraY": round((y1 + y2) / 2, 2),
                    "bbox": [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
                    "confidence": round(min(float(w.confidence) for w in chosen), 4),
                    "engine": result.engine,
                    "frameSeq": frame_seq,
                    "frameW": int(getattr(result, "width", 0) or 0),
                    "frameH": int(getattr(result, "height", 0) or 0),
                    "regionFiltered": project is not None,
                    "matchedChars": scored[0][0],
                }
            region_note = "" if project is not None else \
                f"（已限定输入法区域：屏幕高度≥{self.IME_REGION_SCREEN_FLOOR:.0%}）"
            last = f"输入法区域未找到候选文字「{expected_text}」{region_note}"
            time.sleep(0.35)
        raise WizardError(last or f"OCR 超时：未找到文字「{expected_text}」")

    def acceptance_execute(self, kind: str, check: str,
                           nx: float | None = None, ny: float | None = None) -> dict:
        """执行验收动作（机械部分真实执行，结果由操作者 ✓/✘ 确认）。

        输入法输入项默认走完整自动链路：输入法服务下发固定文字，摄像头 OCR
        找到文字框中心，再经画面像素 →（单应）屏幕像素 →（拟合逆解）机械坐标
        → 回悬停走位 → 点击。传入 nx/ny 时保留手工坐标兼容路径；删除键仍使用
        现场标记坐标和独立长按参数。
        """
        with self._lock:
            self._require_motion_goal()
            self._require_no_auto_run("手动验收执行（真实走位）")
            self._require_session()
            if kind != "debug":
                raise WizardError("微信验收项为现场人工确认（发送/返回属微信界面场景）")
            if check not in DEBUG_ACCEPTANCE_CHECKS:
                raise WizardError(f"未知验收项：{check}")
            self._require_stage(STAGE_DEBUG_ACCEPTANCE)
            deferred_bundle = self._deferred_acceptance_bundle()
            homography = ({"homography": deferred_bundle["camera"]["cameraToScreen"]["homography"],
                           "frameW": deferred_bundle["frame"]["width"],
                           "frameH": deferred_bundle["frame"]["height"]}
                          if deferred_bundle else self.state["geometry"].get("homography"))
            fit = (deferred_bundle["xy"]["robotToScreen"] if deferred_bundle
                   else self.state["xy"].get("fit"))
            if not homography or not fit:
                raise WizardError("摄像头单应/XY 拟合缺失：请先完成 D 阶段")
            record = self.state["acceptance"].setdefault("debug", {}).setdefault(check, {})
            if deferred_bundle:
                record.clear()
                self._save_state()
            result = self._execute_acceptance_debug(
                check, homography, fit, nx, ny, record,
                z_safe=deferred_bundle["z"]["zSafe"] if deferred_bundle else None,
                phone_edge=(deferred_bundle["screen"].get("edgeModel")
                            if deferred_bundle else self.state["geometry"].get("edge")))
            if deferred_bundle:
                self._deferred_acceptance_bundle()
                record["calibrationFingerprint"] = self._acceptance_fingerprint(deferred_bundle)
                self._save_state()
            return result

    @staticmethod
    def _acceptance_fingerprint(bundle: dict) -> str:
        return cb.profile_hash(bundle, keys=(
            "deviceId", "imeDeviceId", "daemonSession", "bindingFingerprint", "bindingRevision",
            "frame", "screen", "z", "xy", "camera", "safePark"))

    def _deferred_acceptance_bundle(self) -> dict | None:
        """补验使用当前机械档案；会话变化不能借人工确认提升旧模型。"""
        bundle = self._bundle()
        if bundle.get("status") != cb.MOTION_READY_STATUS:
            if (self.state.get("acceptance", {}).get("deferred") or {}).get("debug") is True:
                raise WizardError("输入法待验的机械档案已变化，不能按旧验收记录完成校准")
            return None
        status = self._require_session()
        facts = self._read_binding_facts() or {}
        screen = self.state.get("screen") or {}
        checks = cb.validate_calibration_bundle(
            bundle, device_id=self.device, daemon_session=status.get("session"),
            estop_locked=bool(status.get("estop_locked")),
            camera_id=facts.get("cameraId"),
            camera_device_unique_id=facts.get("cameraDeviceUniqueId"),
            binding_revision=facts.get("bindingRevision"),
            daemon_addr=facts.get("daemonAddr"),
            frame_width=facts.get("frameWidth"), frame_height=facts.get("frameHeight"),
            screen_orientation=screen.get("orientation"),
            screen_width=screen.get("screenWidth"), screen_height=screen.get("screenHeight"))
        if checks:
            raise WizardError("机械校准已变化，不能补 E 验收：" +
                              "；".join(item["message"] for item in checks))
        if (self.state.get("imeDeviceId") != bundle.get("imeDeviceId")
                or self.state.get("safePark") != bundle.get("safePark")):
            raise WizardError("手机绑定或安全停车位已变化，不能补 E 验收")
        z = bundle["z"]
        state_z = self.state.get("z") or {}
        expected = {
            "tap": {"depth": z["tapDepth"], "feed": z["tapFeed"], "dwellMs": z["tapDwellMs"]},
            "delete": {"depth": z["deleteDepth"], "feed": z["deleteFeed"],
                       "holdMs": z["deleteHoldMs"]},
        }
        if any((state_z.get(kind) or {}).get(key) != value
               for kind, values in expected.items() for key, value in values.items()) \
                or (state_z.get("autoSafe") or {}).get("zSafe") != z["zSafe"]:
            raise WizardError("点击或长按参数已变化，不能补 E 验收")
        return bundle

    def _acceptance_hover_z(self, z_safe: float | None = None) -> float:
        """验收走位的悬停高度基准；无任何可信来源时拒绝执行（不静默用 2.0）。

        优先级：调用方显式传入（自动 run 的 candidate zSafe）→ state z autoSafe
        → 手动 B 阶段 hoverZ → SAFE PARK parkZ。
        """
        if z_safe is not None:
            return float(z_safe)
        z_state = self.state.get("z") or {}
        for value in ((z_state.get("autoSafe") or {}).get("zSafe"),
                      z_state.get("hoverZ"),
                      (self.state.get("safePark") or {}).get("parkZ")):
            if value is not None:
                return float(value)
        raise WizardError("无可用悬停高度基准（zSafe/hoverZ/SAFE PARK 均缺失）："
                          "拒绝验收走位，请先完成 Z 参数确认或 SAFE PARK 确认")

    def _acceptance_phone_pose(self, homography: dict, reference: dict,
                               *, after_travel: bool = False,
                               after_frame_seq: int | None = None) -> dict:
        """Check a new phone frame without changing calibration geometry."""
        from drivers.screen_pose import fit_pose_robust

        if self._camera_fetch is None or self._edge_detector is None:
            raise WizardError("手机位置不可定位：拒绝验收走位和接触")
        run = reference["run"]
        if self._auto_runner._auto["run"] is not run:
            raise WizardError("验收运行已变化：拒绝验收走位和接触")
        if run is not None:
            self._auto_runner._auto_check_cancel()
            # E 验收点击是真实接触动作（H01 不放宽的普通动作门）：
            # 姿态超限/无法核实必须拒绝（reject），不能吞成 None 再误用。
            quad = self._auto_runner._auto_require_stable_phone(
                run, after_travel=after_travel, after_frame_seq=after_frame_seq,
                on_out_of_range="reject")
            if quad is None:
                raise AutoRunError(
                    "camera_measurement_error",
                    "手机位置不可核实：拒绝验收走位和接触",
                    classification="camera_measurement_error", park=False)
            edge = run["edge"]
        else:
            edge = reference.get("edge") or {}
            if not edge.get("corners"):
                raise WizardError("手机位置缺少校准基准：拒绝验收走位和接触")
            try:
                previous_seq = reference.get("frameSeq", edge.get("frameSeq"))
                if previous_seq is None:
                    _, previous_seq = self._camera_fetch()
                if type(previous_seq) is not int or previous_seq < 0:
                    raise ValueError("手机定位缺少推流基准帧")
                if after_frame_seq is not None:
                    previous_seq = max(previous_seq, after_frame_seq)
                for attempt in range(3):
                    quad = self._edge_quad()
                    seq = quad.get("frameSeq")
                    if type(seq) is int and seq > previous_seq:
                        break
                    if attempt < 2:
                        time.sleep(0.08)
                else:
                    raise ValueError("手机定位推流帧没有推进")
            except (ScreenEdgeError, WizardError, ValueError, KeyError, TypeError) as exc:
                raise WizardError(f"手机位置不可确认：{exc}") from exc
        try:
            if (edge.get("frameW", homography["frameW"]) != homography["frameW"]
                    or edge.get("frameH", homography["frameH"]) != homography["frameH"]
                    or quad.get("frameW") != homography["frameW"]
                    or quad.get("frameH") != homography["frameH"]):
                raise ValueError("手机定位帧尺寸与校准不一致")
            # 验收接触门不用剔角容忍（POSE-POLICY.md 普通动作域）：
            # 非刚性单角形变在此语境下按"不可核实"失败关闭。
            pose = fit_pose_robust(edge["corners"], quad["corners"],
                                   allow_single_corner_drop=False)
            if reference.get("corners") is not None:
                fit_pose_robust(reference["corners"], quad["corners"], max_movement=4.0,
                                allow_single_corner_drop=False)
            reference.setdefault("corners", pose["corners"])
            reference["frameSeq"] = quad["frameSeq"]
            if self._auto_runner._auto["run"] is not run:
                raise WizardError("验收运行已变化：拒绝验收走位和接触")
            if run is not None:
                self._auto_runner._auto_check_cancel()
            return {**pose, "frameSeq": quad["frameSeq"]}
        except (ValueError, KeyError, TypeError) as exc:
            raise WizardError(f"手机位置变化或轮廓不可确认：{exc}") from exc

    def _execute_acceptance_debug(self, check: str, homography: dict, fit: dict,
                                  nx: float | None, ny: float | None,
                                  record: dict, z_safe: float | None = None,
                                  phone_edge: dict | None = None) -> dict:
        """调试台验收动作执行核心（手动 E 与 v1.1 自动 E 共用，§25 复用现有验收）。

        homography/fit 由调用方注入：手动流程用 wizard 工作态；自动 run 用
        candidate 已锁定的 H/Arm。record 为结果留痕目标 dict。
        """
        if (nx is None or ny is None) and check == "ime_delete_clear":
            # 删除键是设备级固定目标。读取优先级（2026-09-04 三步收敛）：
            # ① ui_targets 新层（「固定目标登记」面板，校准验收期登记窗口内
            #    即可先登记，bindPending，提升时 rebind 生效）；
            # ② 迁移期兜底：旧 bundle targets 标记（旧面板隐藏未删，真机
            #    验证通过后随旧代码一并移除）。
            ui_entry = ui_targets.load_entry(self.device, "ime_delete_key", self.data_root)
            if ui_entry is not None and ui_entry.get("nx") is not None:
                nx, ny = float(ui_entry["nx"]), float(ui_entry["ny"])
            else:
                saved = (target_archive.load_targets(self.device, self.data_root)
                         or {}).get("ime_delete_key")
                if not saved or saved.get("nx") is None or saved.get("ny") is None:
                    raise WizardError(
                        "删除键位置尚未登记：请在「固定目标登记」面板用「仅标记」"
                        "标出删除键后保存草稿（校准验收期即可登记，提升后绑定自动生效）")
                nx, ny = float(saved["nx"]), float(saved["ny"])
        frame_w = float(homography.get("frameW") or 0)
        frame_h = float(homography.get("frameH") or 0)
        if not frame_w or not frame_h:
            raise WizardError("单应档案缺帧尺寸：请重新拟合摄像头映射")
        phone_reference = {"edge": phone_edge, "run": self._auto_runner._auto["run"]}
        self._acceptance_phone_pose(homography, phone_reference)

        def before_motion():
            self._acceptance_phone_pose(
                homography, phone_reference,
                after_travel=bool(phone_reference.get("travelStarted")))
            phone_reference["travelStarted"] = True

        from adapters.screen_ime.camera_calibration import CameraScreenTransform
        from drivers.screen_pose import physical_point

        ocr_target = None
        camera_x = camera_y = None
        if nx is not None and ny is not None:
            camera_x, camera_y = float(nx) * frame_w, float(ny) * frame_h
        dispatch = None
        if check == "ime_input_tap":
            # 第一步先停靠：机械臂撤离屏幕可视区，避免遮挡 OCR 视线
            self._park_arm_before_ocr(record, before_motion=before_motion)
            self._acceptance_phone_pose(
                homography, phone_reference,
                after_travel=bool(phone_reference.get("travelStarted")))
            # 真实下发固定测试文字（手机输入法显示候选，§13 步骤 6）。
            # 从下发到点击成功之间任何失败都必须取消任务：残留的未完成
            # 任务会让下一次验收显示上一次的文本（队列卡死）。
            task_id = f"accept-{uuid.uuid4().hex[:8]}"
            try:
                dispatch = self.ime.create_task({
                    "taskId": task_id, "deviceId": self.state["imeDeviceId"],
                    "targetUser": "统一校准验收", "content": "统一校准测试",
                })
                reported = self.ime.wait_task_displayed(task_id, timeout=8.0)
                record["imeReported"] = reported
                if camera_x is None or camera_y is None:
                    screen_cfg = self.state.get("screen") or {}
                    screen_size = (screen_cfg.get("screenWidth"),
                                   screen_cfg.get("screenHeight"))
                    ocr_target = self._locate_text_by_ocr(
                        "统一校准测试", homography=homography, screen_size=screen_size)
                    ocr_seq = ocr_target.get("frameSeq")
                    if (type(ocr_seq) is not int
                            or ocr_seq <= phone_reference["frameSeq"]
                            or ocr_target.get("frameW") != frame_w
                            or ocr_target.get("frameH") != frame_h):
                        raise WizardError("OCR 帧过期或尺寸与校准不一致：拒绝验收走位和接触")
                    camera_x, camera_y = ocr_target["cameraX"], ocr_target["cameraY"]
            except Exception as exc:
                cancel = self.ime.cancel_task(task_id)
                record["imeTaskCancelled"] = {"taskId": task_id, "result": cancel,
                                              "reason": str(exc)}
                if isinstance(exc, WizardError):
                    raise
                raise WizardError(f"输入法验收链路失败（任务已取消 {task_id}）：{exc}") from exc
        try:
            if camera_x is None or camera_y is None:
                raise WizardError("未得到有效的摄像头目标坐标")

            self.daemon.ensure_debug_lease()
            pose = self._acceptance_phone_pose(
                homography, phone_reference,
                after_travel=bool(phone_reference.get("travelStarted")),
                after_frame_seq=ocr_target["frameSeq"] if ocr_target is not None else None)
            motion_model = {
                "frame": {"width": frame_w, "height": frame_h},
                "screen": self.state["screen"],
                "camera": {"cameraToScreen": homography},
                "xy": {"robotToScreen": fit},
            }
            robot_x, robot_y = physical_point(
                motion_model, pose, camera_x, camera_y, already_live=ocr_target is not None)
            if ocr_target is None:
                camera_x, camera_y = cb.apply_homography(pose["matrix"], camera_x, camera_y)
            transform = CameraScreenTransform.from_dict(
                {"homography": homography["homography"]})
            screen_x, screen_y = transform.screen_from_camera(camera_x, camera_y)

            # 自动 run 必须使用 candidate 的会话悬停高度，不回退到旧坐标系。
            self._goto_robot(robot_x, robot_y, z_safe=self._acceptance_hover_z(z_safe),
                             before_motion=before_motion)
            z_state = self.state.get("z") or {}
            auto_alt = z_state.get("autoSafe") or {}
            self._acceptance_phone_pose(
                homography, phone_reference,
                after_travel=bool(phone_reference.get("travelStarted")))
            if check == "ime_delete_clear":
                delete = z_state.get("delete") or {}
                hold_ms = int(delete.get("holdMs", 2000))
                depth = float(delete.get("depth", auto_alt.get("tapDepth", 3.0)))
                feed = float(delete.get("feed", auto_alt.get("tapFeed", DEFAULT_TAP_FEED)))
                self._daemon_tap(depth, feed, hold_ms)
                action_note = f"已长按删除键（{hold_ms}ms），请确认输入框已清空"
            else:
                tap = z_state.get("tap") or {}
                depth = float(tap.get("depth", auto_alt.get("tapDepth", 3.0)))
                feed = float(tap.get("feed", auto_alt.get("tapFeed", DEFAULT_TAP_FEED)))
                self._daemon_tap(depth, feed, int(tap.get("dwellMs", 0)))
                action_note = "已点击候选栏（标记处），请确认文字已进入输入框"
        except Exception as exc:
            if check == "ime_input_tap":
                try:
                    cancel = self.ime.cancel_task(task_id)
                except Exception as cancel_error:
                    cancel = {"error": str(cancel_error), "status": "unknown"}
                record["imeTaskCancelled"] = {
                    "taskId": task_id, "result": cancel, "reason": str(exc),
                }
                self._save_state()
            raise
        record["executedAt"] = _now()
        record["executedRobot"] = [round(robot_x, 2), round(robot_y, 2)]
        if ocr_target is not None:
            record["targetSource"] = "camera_ocr"
            record["ocrTarget"] = ocr_target
        if dispatch:
            record["dispatchedTaskId"] = dispatch.get("taskId")
        self._save_state()
        return {"executed": True, "check": check,
                "screenTarget": [round(screen_x, 1), round(screen_y, 1)],
                "robotTarget": [round(robot_x, 2), round(robot_y, 2)],
                "dispatched": dispatch, "targetSource": "camera_ocr" if ocr_target else "manual",
                "ocrTarget": ocr_target,
                "hint": action_note + "，然后点 ✓通过 / ✘失败"}

    def acceptance_record(self, kind: str, check: str, ok: bool, detail: str = "") -> dict:
        with self._lock:
            self._require_motion_goal()
            self._require_no_auto_run("手动验收确认")
            required = {"debug": DEBUG_ACCEPTANCE_CHECKS,
                        "wechat": WECHAT_ACCEPTANCE_CHECKS}.get(kind)
            if required is None:
                raise WizardError("kind 必须是 debug 或 wechat")
            if check not in required:
                raise WizardError(f"未知验收项：{check}（{kind} 阶段：{'、'.join(required)}）")
            target_stage = (STAGE_DEBUG_ACCEPTANCE if kind == "debug"
                            else STAGE_WECHAT_ACCEPTANCE)
            if kind == "wechat":
                self._require_stage(STAGE_WECHAT_ACCEPTANCE, STAGE_READY)
            else:
                self._require_stage(STAGE_DEBUG_ACCEPTANCE)
            record = self.state["acceptance"].setdefault(kind, {}).setdefault(check, {})
            deferred_bundle = self._deferred_acceptance_bundle()
            if deferred_bundle and ok and (
                    not record.get("executedAt")
                    or record.get("calibrationFingerprint")
                    != self._acceptance_fingerprint(deferred_bundle)):
                raise WizardError("请先在当前机械校准下执行本项 E 验收，再确认通过")
            record.update({"ok": bool(ok), "detail": detail, "ts": _now()})
            if kind == "debug" and check == "ime_delete_clear":
                delete = self.state["z"].get("delete")
                if delete is not None:
                    delete["validated"] = bool(ok)
                    delete["validatedAt"] = _now() if ok else None
            self.state["acceptance"]["lastRunAt"] = _now()
            self._sync_bundle()
            self._save_state()
            return {"kind": kind, "check": check, "ok": bool(ok),
                    "done": self._acceptance_done(kind)}

    def acceptance_advance(self) -> dict:
        """阶段推进：调试台输入法验收全部通过后直接完成校准。

        微信业务目标登记不作为通用校准的必经阶段；输入法验收所需目标
        可在验收期登记。旧状态若停在 TARGET，直接推进到调试台验收。旧版本留下的
        ``wechat_acceptance`` 阶段仅作兼容状态，不再要求微信测试会话。
        """
        with self._lock:
            self._require_motion_goal()
            stage = self.state["stage"]
            if stage == STAGE_TARGET:
                self._advance(STAGE_DEBUG_ACCEPTANCE)
            elif stage == STAGE_DEBUG_ACCEPTANCE:
                if not self._acceptance_done("debug"):
                    missing = [c for c in DEBUG_ACCEPTANCE_CHECKS
                               if not (self.state["acceptance"].get("debug") or {})
                               .get(c, {}).get("ok")]
                    raise WizardError(f"调试台验收未完成：{'、'.join(missing)}")
                self.finish()
            elif stage == STAGE_WECHAT_ACCEPTANCE:
                # 历史状态兼容：不再因为缺少业务测试会话而阻断。
                self.finish()
            else:
                raise WizardError(f"当前阶段「{STAGE_DISPLAY.get(stage, stage)}」无可推进项")
            return {"stage": self.state["stage"]}


    def ui_target_registration_window(self) -> dict | None:
        """校准验收期固定目标登记窗口（ui_targets 层）。

        E 验收发生在 promote（ready）之前，删除键等验收目标必须能在验收期
        先登记（否则新面板的 ready 门禁会死锁 E 验收）：
        - 自动 run 处于 E_ACCEPTANCE/PROMOTE 相位 → auto_run 窗口；
        - 无 run 且手动流程已到目标/验收阶段、几何齐备 → manual 窗口。
        窗口内登记的条目 bindPending，提升/完成时 rebind_pending 统一生效。
        """
        with self._lock:
            with self._auto_runner._auto_lock:
                run = self._auto_runner._auto["run"]
            if run is not None and run.get("phase") in ("E_ACCEPTANCE", "PROMOTE"):
                return {"source": "auto_run", "runId": run.get("runId")}
            if run is None and self.state.get("stage") in (
                    STAGE_TARGET, STAGE_DEBUG_ACCEPTANCE, STAGE_WECHAT_ACCEPTANCE):
                geometry = (self.state.get("geometry") or {}).get("homography")
                fit = (self.state.get("xy") or {}).get("fit")
                if geometry and fit:
                    return {"source": "manual"}
            return None

    def finish(self) -> dict:
        """调试台输入法验收通过后置 ready，绑定当前会话（§13 步骤 14）。

        微信测试会话不是机械臂校准前置；WX 发送仍由每次任务的目标、正文
        哈希、账号/会话和发送后效果证据门禁负责。
        纯观测档（goal=observation）从屏幕几何阶段直达 observation_ready，
        不要求机械臂会话（_finish_observation，Auto_CS#20）。
        """
        with self._lock:
            self._require_no_auto_run("手动完成校准（写 ready 与 candidate 提升互斥）")
            if self.state.get("goal") == GOAL_OBSERVATION:
                return self._finish_observation()
            self._require_session()
            self._require_stage(STAGE_DEBUG_ACCEPTANCE, STAGE_WECHAT_ACCEPTANCE,
                                STAGE_READY)
            if self.state["stage"] == STAGE_READY:
                return {"status": "ready", "stage": self.state["stage"]}
            if not self._acceptance_done("debug"):
                raise WizardError("调试台输入法验收未完成：不能置 ready")
            deferred_bundle = self._deferred_acceptance_bundle()
            if deferred_bundle:
                fingerprint = self._acceptance_fingerprint(deferred_bundle)
                if any(not record.get("executedAt")
                       or record.get("calibrationFingerprint") != fingerprint
                       for record in (self.state["acceptance"]["debug"][check]
                                      for check in DEBUG_ACCEPTANCE_CHECKS)):
                    raise WizardError("本轮 E 验收执行证据不完整：不能置 ready")
            self._require_binding()
            self._sync_bundle()
            bundle = self._bundle()
            if deferred_bundle:
                bundle["acceptance"]["debug"] = True
                bundle["acceptance"]["deferred"]["debug"] = False
                bundle["acceptance"]["lastRunAt"] = _now()
                bundle["z"]["deleteValidated"] = True
                self.state["acceptance"]["deferred"] = {"debug": False}
            bundle["status"] = "ready"
            bundle["daemonSession"] = self.state["daemonSession"]
            cb.save_bundle(bundle, self.data_root)
            # 坐标桥旁档尽力写入（第 4.2/22.4 节）：失败不影响校准结果
            self._save_gesture_coordinate_frame(bundle)
            # 验收期登记（bindPending）的固定目标绑定快照随最终档案生效
            try:
                ui_targets.rebind_pending(self.device, bundle,
                                          data_root=self.data_root,
                                          operator="manual_finish")
            except Exception:
                pass
            self._advance(STAGE_READY)
            sid = self.state.get("imeSessionId")
            if sid:
                try:
                    self.ime.complete(sid)
                except ImeError:
                    pass
            return {"status": "ready", "stage": self.state["stage"]}

    def _finish_observation(self) -> dict:
        """纯观测档收尾（Auto_CS#20）：屏幕几何+摄像头单应 → observation_ready。

        从 SCREEN_GEOMETRY 阶段直达（跳过 Z/XY/标靶/验收动作阶段），全程零
        机械动作、不要求串口会话；产出相机-only 档案（运动字段一律未校准，
        运行前运动门禁对其一律判 bundle_not_ready——纯观测档案永远不能驱动
        动作）。观测不绑定 daemonSession：物理漂移由 screen-observation 的
        live 四角定位每帧实测拒绝。坐标桥旁档与目标绑定快照是动作域产物，
        观测档不写。
        """
        self._require_stage(STAGE_SCREEN_GEOMETRY)
        geometry = self.state.get("geometry") or {}
        if not (self.state.get("screen")
                and (geometry.get("edge") or {}).get("corners")
                and (geometry.get("homography") or {}).get("homography")):
            raise WizardError(
                "屏幕边缘基准或摄像头单应缺失：请先完成 edge_capture 与 "
                "camera_fit（零机械动作），不能置 observation_ready")
        self._require_binding()
        self._sync_bundle(observation_rebuild=True)
        bundle = self._bundle()
        bundle["status"] = cb.OBSERVATION_READY_STATUS
        bundle["daemonSession"] = None
        cb.save_bundle(bundle, self.data_root)
        # 显式落阶段（不经 _advance：观测档案已定稿，不再触发整包重建）
        self.state["stage"] = STAGE_READY
        self._save_state()
        sid = self.state.get("imeSessionId")
        if sid:
            try:
                self.ime.complete(sid)
            except ImeError:
                pass
        return {"status": cb.OBSERVATION_READY_STATUS, "stage": self.state["stage"]}

    def _require_motion_goal(self) -> None:
        """纯观测档（goal=observation）禁用一切运动域操作（Auto_CS#20）。

        零动作是观测通道的结构性保证而非使用约定：观测会话里任何走位/
        点击/验收类入口都在触碰机械臂之前拒绝。
        """
        if self.state.get("goal") == GOAL_OBSERVATION:
            raise WizardError(
                "本向导会话为纯观测校准（observation）：不含机械臂动作阶段，"
                "运动/验收类操作不可用；如需动作校准请以运动目标重新开始")

    def _save_gesture_coordinate_frame(self, bundle: dict) -> None:
        """坐标桥旁档（第 4.2/22.4 节）：完成/提升后尽力写入，独立于 bundle。

        只用同一次原校准页面（legacy-calibration）缓存并核对 session/device
        后的元数据生成，不得事后用普通页面参数推测；元数据缺失记录原因，
        原机械校准照常成功，新手势仍由缺失/旧绑定门禁拦截。
        任何异常写日志并向活动自动 run 留痕（gesture_frame_save_failed），
        绝不让校准失败，也不改 bundle 内容（旁档是独立文件）。
        """
        try:
            ime_device = self.state.get("imeDeviceId")
            session_id = self.state.get("imeSessionId")
            if not ime_device or not session_id:
                raise WizardError("legacy metadata 的原校准设备或会话缺失")
            fetch = getattr(self.ime, "fetch_gesture_frame_metadata", None)
            if fetch is None:
                raise WizardError("legacy metadata 只读查询不可用")
            metadata = fetch(ime_device, session_id)
            frame = build_gesture_coordinate_frame(
                bundle, metadata, ime_device_id=ime_device, session_id=session_id)
            gp.save_coordinate_frame(self.device, frame, data_root=self.data_root)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "设备 %s 校准结果已保留，但坐标桥未更新：%s", self.device, exc)
            with self._auto_runner._auto_lock:
                run = self._auto_runner._auto["run"]
            if run is not None:
                run.setdefault("events", []).append(
                    {"ts": _now(), "phase": run.get("phase"),
                     "event": "gesture_frame_save_failed",
                     "detail": str(exc)})

    def gate(self, required_targets: list[str] | None = None,
             wechat_send: bool = False) -> dict:
        with self._lock:
            bundle = self._bundle()
            status = self.daemon.status()
            estop = bool(status.get("estop_locked"))
            deviation, edge_check = self._edge_deviation(bundle)
            binding = self._read_binding_facts() or {}
            targets = required_targets if required_targets is not None \
                else list(DEFAULT_REQUIRED_TARGETS)
            checks = cb.validate_calibration_bundle(
                bundle,
                device_id=self.device,
                camera_id=binding.get("cameraId"),
                camera_device_unique_id=binding.get("cameraDeviceUniqueId"),
                binding_revision=binding.get("bindingRevision"),
                daemon_addr=binding.get("daemonAddr"),
                frame_width=binding.get("frameWidth"),
                frame_height=binding.get("frameHeight"),
                daemon_session=status.get("session"),
                estop_locked=estop,
                screen_orientation=(self.state.get("screen") or {}).get("orientation"),
                screen_width=(self.state.get("screen") or {}).get("screenWidth"),
                screen_height=(self.state.get("screen") or {}).get("screenHeight"),
                screen_edge_deviation_px=deviation,
                required_targets=targets,
                wechat_send=wechat_send,
            )
            return {"ok": not checks, "checks": checks,
                    "edgeDeviationPx": round(deviation, 2) if deviation is not None else None,
                    "edgeCheck": edge_check,
                    "requiredTargets": targets, "wechatSend": wechat_send}

    def _edge_deviation(self, bundle: dict) -> tuple[float | None, dict]:
        """实测边缘与档案基准比对。

        返回 (偏差, 判定详情)。检测失败或判定为"检测异常（环境干扰、非刚性）"
        时偏差为 None——与 screen_edge_not_detected 同语义：无法核实则跳过，
        不阻断业务；真实刚性位移（平移/旋转/缩放）仍返回偏差硬拦。
        """
        edge = (bundle.get("screen") or {}).get("edgeModel") or {}
        corners = edge.get("corners")
        if not corners:
            return None, {"status": "no_baseline"}
        try:
            quad = self._edge_quad()
        except (WizardError, Exception):
            return None, {"status": "not_detected"}
        tolerance = float(edge.get("tolerancePx")
                          or cb.DEFAULT_EDGE_TOLERANCE_PX)
        verdict = classify_quad_change(corners, quad["corners"], tolerance)
        if verdict["status"] == "anomaly":
            return None, verdict
        return verdict["deviationPx"], verdict

    # ---------- v1.1 自动校准：操作员确认入口（zSafe / tapDepth / SAFE PARK） ----------

    def z_confirm_safe(self, z_safe: float | None = None, tap_depth: float | None = None,
                       tap_feed: float | None = None) -> dict:
        """确认 zSafe/tapDepth 并当场验证：原地执行一次测试点击，收到触点回报才算确认成功。

        用户拍板 2026-09-03：只有真实点一次、手机回报触点，才能肯定 Z 参数没问题
        （深度太浅无接触、太深危险都会在这里暴露）；SAFE PARK 确认只记位置、不点击。
        前置：先把探头停在手机屏幕上方安全悬停高度（XY 在屏幕内，点击打在当前位置），
        手机亮屏停留在当前校准会话页面。触点回报同时建立/复核屏幕基线。
        z_safe 省略时取当前 MPos.z；验证失败保留参数但 validated=False，不得启动自动校准。
        """
        with self._lock:
            self._require_motion_goal()
            self._require_no_auto_run("确认点击参数（含一次测试点击）")
            # 这是新 daemon/IME 会话恢复执行资格的唯一复核入口；
            # 普通动作仍由 _require_session 的 rebind 门禁拦截。
            self._require_session(allow_rebind=True)
            previous = self.state["z"].get("autoSafe")
            if previous:
                previous["validated"] = False
                previous["validationError"] = "点击参数重新确认中，旧确认不再有效"
                self._sync_bundle()
                self._save_state()
            try:
                phone_reference = self._manual_z_phone_pose(allow_rebind=True)
                pose_note = None
            except PoseUnlocatable as exc:
                # 用户拍板 2026-09-29：操作员监督下的 zSafe 确认不被"姿态定位
                # 不可得"拦截——四边形是校准的产物而非前置条件。点击是否有效
                # 由唯一触点回报裁决（回报含屏幕坐标=证明落在屏内）；无回报则
                # 验证失败、参数不保存。缺席留痕进记录。
                # 容忍范围只有 PoseUnlocatable（定位不到）一族；手机位移/帧
                # 冻结/尺寸不一致/绑定缺失等质量门失败不在此列，照常拒绝接触。
                phone_reference = None
                pose_note = f"姿态参考缺席（{exc}）；操作员监督下点击，以触点回报为准"
            mpos = self._read_position()
            current_z = round(float(mpos["z"]), 3)
            z_safe = round(current_z if z_safe is None else float(z_safe), 3)
            if abs(z_safe - current_z) > 10.0:
                raise WizardError(
                    f"zSafe={z_safe}mm 与当前 MPos.z={current_z}mm 相差超过 10mm："
                    f"请先把探头停到安全悬停高度再确认（防误输入；自动确认请省略 zSafe）")
            tap_depth = float(3.0 if tap_depth is None else tap_depth)
            tap_feed = float(DEFAULT_TAP_FEED if tap_feed is None else tap_feed)
            check_feed(tap_feed, axis="Z")
            if not 0.5 <= abs(tap_depth) <= 10.0:
                raise WizardError(f"点击深度 {tap_depth}mm 超出 0.5~10mm")
            record = {
                "zSafe": z_safe, "tapDepth": tap_depth, "tapFeed": tap_feed,
                "source": "operator_confirmed", "confirmedAt": _now(),
                "daemonSession": self.state.get("daemonSession"),
                "mposAtConfirm": {k: round(float(mpos.get(k)), 3) for k in ("x", "y", "z")},
                "validated": False, "validationError": None, "report": None,
            }
            self.state["z"]["autoSafe"] = record
            self._save_state()
            # 原地验证点击：布防 → TAP → 等待唯一触点回报（与手动 B 阶段同一协议）
            sample_id = f"zs-{uuid.uuid4().hex[:6]}"
            self.daemon.ensure_debug_lease()
            try:
                report = self._tap_with_report(
                    sample_id, lambda: self._daemon_tap(tap_depth, tap_feed, 0),
                    phone_reference=phone_reference, allow_rebind=True)
            except TouchReportTimeout as exc:
                record["validationError"] = str(exc)
                self._sync_bundle()
                self._save_state()
                raise WizardError(
                    f"点击参数未通过验证（未收到触点回报）：{exc}。请确认探头在手机屏幕"
                    f"上方、手机亮屏停留在校准页，调整 Z/深度后重新确认") from exc
            record["validated"] = True
            record["validatedAt"] = _now()
            record["report"] = {"screenX": report["screenX"], "screenY": report["screenY"],
                                "screenWidth": report["screenWidth"],
                                "screenHeight": report["screenHeight"],
                                "sampleId": sample_id}
            if pose_note:
                record["poseReference"] = pose_note
            self._save_tap_anchor(mpos, report, "z_confirm_safe")
            # 一次 Z 触点确认只恢复 Z 资格；旧 XY 与停车位保持待复核
            # （规范 §5.4：不能用一次 Z 点击授予旧 XY/停车位资格）。
            self._clear_session_rebind(scope="z")
            self._sync_bundle()
            self._save_state()
            return {"zSafe": z_safe, "tapDepth": tap_depth, "tapFeed": tap_feed,
                    "confirmedAt": record["confirmedAt"], "validated": True,
                    "report": record["report"]}

    def park_confirm(self) -> dict:
        """操作员确认 SAFE PARK（§17.2/§17.3）：以当前 MPos XY + zSafe 记录停靠位。

        这是停车位的重新确认入口（会话复核期可用）：新记录绑定当前
        daemon 会话与实际 MPos（规范 §5.4/P09），旧 confirmed 不算这次确认。
        """
        with self._lock:
            self._require_motion_goal()
            self._require_session(allow_rebind=True)
            auto_safe = self.state["z"].get("autoSafe") or {}
            if auto_safe.get("zSafe") is None:
                raise WizardError("先确认 Z 安全高度（zSafe），再确认 SAFE PARK")
            mpos = self._read_position()
            self.state["safePark"] = {
                "role": "calibration_safe_park",
                "roleDisplay": "校准安全停车位",
                "parkX": round(float(mpos["x"]), 3), "parkY": round(float(mpos["y"]), 3),
                "parkZ": round(float(auto_safe["zSafe"]), 3),
                "confirmed": True, "source": "operator_confirmed", "confirmedAt": _now(),
                "daemonSession": self.state.get("daemonSession"),
                "mposAtConfirm": {k: round(float(mpos.get(k)), 3) for k in ("x", "y", "z")},
            }
            self._clear_session_rebind(scope="park")
            self._sync_bundle()
            self._save_state()
            return dict(self.state["safePark"])

    def park_clear(self) -> dict:
        with self._lock:
            self.state["safePark"] = self._blank_safe_park()
            self._sync_bundle()
            self._save_state()
            return dict(self.state["safePark"])

    # ---------- P07（规范 §5.3）：候选资料恢复（只恢复资料，不恢复运动） ----------

    _RECOVERY_REQUIRED_FIELDS = (
        ("h", "camera_homography"), ("arm", "arm_model"),
        ("z", "z_parameters"), ("safePark", "safe_park"), ("binding", "binding"),
    )

    def candidate_recovery_report(self) -> dict:
        """按完整性检查历史候选（P07）：不选「修改时间最新」，只认完整可追溯。

        可恢复的模型必须来自同一轮、同一设备/相机绑定；模型锁、独立验证、
        Z 确认、停车位必须能追溯。缺失字段明确列出，供重新确认。
        """
        with self._lock:
            items: list[dict] = []
            root = cb.candidates_dir(self.device, self.data_root)
            for path in sorted(root.glob("*.json")) if root.exists() else []:
                try:
                    candidate = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    items.append({"runId": path.stem, "parseable": False,
                                  "recoverable": False,
                                  "missing": ["candidate_unreadable"]})
                    continue
                if not isinstance(candidate, dict):
                    items.append({"runId": path.stem, "parseable": False,
                                  "recoverable": False,
                                  "missing": ["candidate_not_a_dict"]})
                    continue
                missing: list[str] = []
                if candidate.get("deviceId") != self.device:
                    missing.append("device_mismatch")
                for key, label in self._RECOVERY_REQUIRED_FIELDS:
                    if not candidate.get(key):
                        missing.append(label)
                locks = candidate.get("locks") or {}
                for name in ("cameraToScreen", "armAffine"):
                    if (locks.get(name) or {}).get("state") != cb.MODEL_LOCK_LOCKED:
                        missing.append(f"lock_{name}")
                if not (candidate.get("validation") or {}).get("passed"):
                    missing.append("independent_validation")
                z = candidate.get("z") or {}
                if not (z.get("validated") and z.get("zSafe") is not None):
                    missing.append("z_confirmed")
                park = candidate.get("safePark") or {}
                if not (park.get("confirmed") and park.get("parkX") is not None):
                    missing.append("safe_park_confirmed")
                z, park = candidate.get("z") or {}, candidate.get("safePark") or {}
                items.append({
                    "runId": candidate.get("runId") or path.stem,
                    "state": candidate.get("state"),
                    "parseable": True,
                    # 整份候选导入，不与其它 run 拼接（§5.3.3）。
                    "singleRunSource": True,
                    "recoverable": not missing,
                    "missing": missing,
                    "fieldPreview": {
                        "zSafe": z.get("zSafe"), "tapDepth": z.get("tapDepth"),
                        "parkXY": [park.get("parkX"), park.get("parkY")],
                        "daemonSession": candidate.get("daemonSession"),
                        "phoneSessionId": candidate.get("phoneSessionId"),
                    },
                })
            return {"items": items,
                    "note": "恢复只导入待复核参考值，不恢复执行资格；"
                            "当前执行资格仍按会话复核机制逐项确认"}

    def candidate_restore(self, run_id: str) -> dict:
        """导入完整候选为待复核资料（P07）：不写 ready、不恢复执行资格。"""
        with self._lock:
            self._require_motion_goal()
            report = self.candidate_recovery_report()
            item = next((entry for entry in report["items"]
                         if entry.get("runId") == run_id), None)
            if item is None:
                raise WizardError(f"未找到候选 {run_id}")
            if not item.get("parseable"):
                raise WizardError("候选文件不可解析：保留现有资料，缺失项需重新确认")
            if not item.get("recoverable"):
                raise WizardError("候选缺少完整可追溯证据（"
                                  + "、".join(item["missing"])
                                  + "）：不能导入，请重新确认缺失项")
            candidate = cb.load_candidate(self.device, run_id, self.data_root)
            if candidate is None:
                raise WizardError(f"候选 {run_id} 读取失败")
            self.state["recoveredReference"] = {
                "runId": run_id,
                "importedAt": _now(),
                "z": deepcopy(candidate.get("z") or {}),
                "safePark": deepcopy(candidate.get("safePark") or {}),
                "homography": deepcopy(candidate.get("h") or {}),
                "arm": deepcopy(candidate.get("arm") or {}),
                "edge": deepcopy(candidate.get("edge") or {}),
                "binding": deepcopy(candidate.get("binding") or {}),
                "daemonSession": candidate.get("daemonSession"),
                "phoneSessionId": candidate.get("phoneSessionId"),
                "pendingReview": True,
                "notRestored": ["execution_eligibility", "profile_model_locks",
                                "acceptance"],
            }
            # 导入即全项待复核：历史参考值不得直接当作当前会话执行资格。
            self._mark_session_rebind("candidate_imported_pending_review")
            self._save_state()
            return {"imported": True, "runId": run_id,
                    "pendingScopes": self.state["sessionRebind"]["pendingScopes"],
                    "notRestored": self.state["recoveredReference"]["notRestored"]}


    # ---------- v1.1 自动校准：对外门面（实现外迁 drivers/calibration_auto_runner.py，ADR-0003） ----------

    def auto_run_active(self) -> bool:
        return self._auto_runner.auto_run_active()


    def auto_start(self, *, max_iterations: int | None = None,
                   point_plan: list[dict] | None = None,
                   defer_input_acceptance: bool = True) -> dict:
        # 纯观测档拒绝在 runner 入口（Auto_CS#20）：本方法受 seam 守卫约束，
        # 必须保持对 self._auto_runner 的纯委托。
        return self._auto_runner.auto_start(max_iterations=max_iterations, point_plan=point_plan, defer_input_acceptance=defer_input_acceptance)


    def auto_stop(self, run_id: str | None = None) -> dict:
        return self._auto_runner.auto_stop(run_id=run_id)


    def auto_retry(self, run_id: str, target_id: str) -> dict:
        return self._auto_runner.auto_retry(run_id=run_id, target_id=target_id)


    def auto_validate(self, run_id: str | None = None) -> dict:
        return self._auto_runner.auto_validate(run_id=run_id)


    def auto_resume_acceptance(self, run_id: str) -> dict:
        return self._auto_runner.auto_resume_acceptance(run_id=run_id)


    def auto_resume_motion_qualification(self, run_id: str) -> dict:
        return self._auto_runner.auto_resume_motion_qualification(run_id=run_id)


    def auto_acceptance_execute(self, run_id: str, check: str) -> dict:
        return self._auto_runner.auto_acceptance_execute(run_id=run_id, check=check)


    def auto_acceptance_record(self, run_id: str, kind: str, check: str, ok: bool,
                               detail: str = "") -> dict:
        return self._auto_runner.auto_acceptance_record(run_id=run_id, kind=kind, check=check, ok=ok, detail=detail)


    def auto_view(self) -> dict:
        return self._auto_runner.auto_view()


    # ---------- 视图 ----------

    def state_view(self) -> dict:
        import copy

        with self._lock:
            view = copy.deepcopy(self.state)
            if self._state_unrecoverable is not None:
                view["stateHealth"] = copy.deepcopy(self._state_unrecoverable)
        view["stageDisplay"] = STAGE_DISPLAY.get(view["stage"], view["stage"])
        view["goalDisplay"] = GOAL_DISPLAY.get(view.get("goal"), view.get("goal"))
        view["xyPlan"] = self.xy_plan()
        view["validationPlan"] = [{"index": i, "name": n, "u": u, "v": v}
                                  for i, (n, u, v) in enumerate(XY_VALIDATION_POINTS)]
        view["requiredDebugChecks"] = list(DEBUG_ACCEPTANCE_CHECKS)
        view["requiredWechatChecks"] = []
        view["targetNames"] = TARGET_DISPLAY_NAMES
        view["autoPlan"] = {
            "points": [{"id": n, "u": u, "v": v} for n, u, v in AUTO_PLAN_POINTS],
            "validation": [{"id": n, "u": u, "v": v}
                           for n, u, v in AUTO_VALIDATION_POINTS],
        }
        view["bundle"] = self._bundle()
        view["inputAcceptanceDeferred"] = cb.input_acceptance_deferred(view["bundle"])
        if (view["bundle"].get("status") == cb.MOTION_READY_STATUS
                and view["stage"] == STAGE_DEBUG_ACCEPTANCE):
            view["stageDisplay"] = "机械校准已保存，输入法待验"
        # 手机页实时状态（锁外取，网络异常不影响状态返回）；
        # 兼作自动检测兜底：watcher 线程异常退出/服务重启后，状态轮询也能推进阶段
        view["phone"] = self._phone_view(view.get("imeSessionId"))
        # v1.1 自动校准 runner 状态（§24：state 增加 auto run/phase/locks/sample/errors）
        view["auto"] = self.auto_view()
        phone = view["phone"] or {}
        if (view["stage"] == STAGE_PRECHECK
                and phone.get("clientState") == "open"
                and phone.get("status") == "active"):
            with self._lock:
                if self.state["stage"] == STAGE_PRECHECK:
                    self.state["phoneOpenedAt"] = _now()
                    self._advance_after_phone_open(phone)
            view["stage"] = self.state["stage"]
            view["stageDisplay"] = STAGE_DISPLAY.get(view["stage"], view["stage"])
        return view

    def _phone_view(self, session_id: str | None) -> dict | None:
        """手机校准页当前回报状态，供页面实时显示（unknown/open/closed）。"""
        if not session_id:
            return None
        try:
            session = self.ime.get_session(session_id)
        except Exception as exc:
            return {"clientState": None, "status": None, "error": str(exc)}
        return {"clientState": session.get("clientState"),
                "status": session.get("status"),
                # open 上报的只读屏幕元数据：观测档（Auto_CS#20）阶段推进的
                # 坐标域来源（与 F2 §6.2.1 同一信任源），亦供调试台显示。
                "screen": deepcopy(session.get("screen") or None)}

