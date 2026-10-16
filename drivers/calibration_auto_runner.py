"""v1.1 自动校准 runner（Auto_CS#8 自 calibration_wizard.py 外迁，ADR-0003）。

AutoRunHost = runner 对宿主的最窄消费面（成员=AST 实测清单，不预留未来成员）；
生产宿主=CalibrationWizard 自身（天然实现协议，get_wizard 几乎零改）。
runner 完全自治：run/thread/cancel/action_lock 容器与 _auto_lock 在本类，
锁序恒为 host._lock → self._auto_lock。协议只约束 runner→host 方向；
宿主对 runner 容器的白盒直达（wizard 侧 self._auto_runner._auto[...]）是
同包设计内访问，不进协议。
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from drivers import calibration_bundle as cb
from drivers import ui_targets
from drivers.calibration_wizard_common import (
    AUTO_ACTIVE_STATES,
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
    DEBUG_ACCEPTANCE_CHECKS,
    GOAL_OBSERVATION,
    STAGE_DEBUG_ACCEPTANCE,
    STAGE_IDLE,
    STAGE_INVALIDATED,
    STAGE_PRECHECK,
    STAGE_READY,
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
from drivers.daemon_protocol import DEFAULT_TAP_FEED, split_segments
from drivers.screen_edge import ScreenEdgeError


# ---------- v1.1 纯代码自动视觉闭环校准（规格书 §5/§Appendix A + 补充文档） ----------
# 9 点标定网格按《v1.1 补充》§2：15%/50%/85%（行主序：先 X 后 Y）。
# 注：一度以为 v=0.85 被校准页裁剪而收回 0.78，后经 HSV 取样证伪——真实原因是
# 绿色检测阈值骑在目标颜色边缘（见 screen_edge._TARGET_GREEN_HSV 注释），
# 阈值修正后 0.85 全深度实测满面积检出，已恢复 85%。
# 5 个独立验证点沿用规格书 Appendix A，与标定网格不重合、不参与拟合。
# 2026-09-04 方案 A（用户拍板）：3x3 → 4x4 加密网格 + 仿射 → 二次模型。
# 手机屏倾斜带来的触点滑移是平滑低阶非线性，仿射在网格间隙中央泄漏最大
# （实测 v2 点 8~15px）；格子尺寸 35%→23% 使泄漏缩至 ~1/4，二次项吃掉剩余。
AUTO_PLAN_GRID_V: tuple[float, ...] = (0.15, 0.38, 0.62, 0.85)
AUTO_PLAN_GRID_U: tuple[float, ...] = (0.15, 0.38, 0.62, 0.85)
AUTO_PLAN_POINTS: list[tuple[str, float, float]] = [
    (f"calibration-{row * len(AUTO_PLAN_GRID_U) + col + 1}", u, v)
    for row, v in enumerate(AUTO_PLAN_GRID_V)
    for col, u in enumerate(AUTO_PLAN_GRID_U)
]
AUTO_VALIDATION_POINTS: list[tuple[str, float, float]] = [
    ("validation-1", 0.30, 0.30),
    ("validation-2", 0.70, 0.30),
    ("validation-3", 0.30, 0.70),
    ("validation-4", 0.70, 0.70),
    ("validation-5", 0.50, 0.65),
]
AUTO_FIRST_CLICK_POINT = ("first-click-center", 0.50, 0.50)

# promote 守卫只锁定几何/身份基础字段：运行中经合法入口写入的 targets/safePark/z
# 不破坏提升；几何/绑定/状态被人改动才拒绝（2026-09-03：现场在 E 验收阶段标记
# 删除键会写档案，全字段哈希曾误杀最后一次提升）。
AUTO_PROMOTE_GUARD_KEYS: tuple[str, ...] = (
    "deviceId", "cameraId", "cameraDeviceUniqueId", "bindingRevision",
    "bindingFingerprint", "daemonAddr", "daemonSession", "frame",
    "screen", "xy", "camera", "status",
)
# First Click 口令门禁已按用户拍板移除（2026-09-03）：操作员点「启动自动校准」即视为
# 对首次中心点击的确认；安全仍由 zSafe/SAFE PARK 前置确认 + 计算门禁承担。



AUTO_PHASE_DISPLAY: dict[str, str] = {
    "PRECHECK": "运行前预检",
    "SAFE_PARK": "安全停靠",
    "PHASE0_CAMERA": "Phase 0 摄像头标定（只显示标靶，不点击）",
    "H_LOCKED": "H 已锁定",
    "BOOTSTRAP_CHECK": "Bootstrap 无点击安全检查",
    "FIRST_CLICK_GATE": "First Click Gate（仅中心点）",
    "PHASE1_ARM": "Phase 1 机械臂映射采样",
    "ARM_LOCKED": "Arm 映射已锁定",
    "PHASE2_VALIDATE": "Phase 2 闭环验证（只验证，不学习）",
    "INDEPENDENT_VALIDATE": "5 点独立验证",
    "E_ACCEPTANCE": "E 验收（调试台输入法）",
    "PROMOTE": "candidate 保存校准资格",
}

# 全部阈值配置化（补充文档 §6：不得在业务代码中散落硬编码；初值见规格书 §8.2/§14）
AUTO_CONFIG_DEFAULTS: dict = {
    "detect_confirm_frames": 3,        # 连续稳定帧数（建议初值，现场冻结）
    "center_jitter_px": 8.0,           # 连续帧中心两两距离上限
    "area_delta_ratio": 0.20,          # 连续帧面积相对变化上限
    "target_ambiguous_px": 40.0,       # 多候选中心间距小于该值视为无法唯一确定
    "target_min_step_px": 30.0,        # 相邻标定点的相机位移下限（静态绿物拦截）
    "target_area_min_ratio": 0.6,      # 标定 点检测面积相对中位的下限（遮挡拦截）
    "expected_max_dev_px": 20.0,       # 点击阶段检测中心距期望位置（H 逆投影）的上限
    "detect_interval_s": 0.1,          # 稳定检测的取帧间隔
    "target_tolerance_px": 10.0,       # 闭环点 PASS 上限（用户拍板 2026-09-04 由 8px 放宽；
                                       # 8px 时代 v2 点 8~15px 系倾斜滑移泄漏，方案 A（4x4 网格
                                       # + 二次模型）补偿后仍保留 2px 触点回报噪声裕量）
    "soft_fail_px": 30.0,              # SOFT_FAIL 上限（重采样，不学习）
    "hard_fail_px": 100.0,             # 安全停止边界（HARD，不等于精度目标）
    "position_tolerance_mm": 3.0,      # postMoveMPos 与指令位置容差
    "camera_rms_target_px": 3.0,       # Phase 0 H RMS 目标
    "camera_max_error_px": 6.0,        # Phase 0 H 最大误差目标
    "arm_rms_target_px": 3.0,          # Phase 1 Arm RMS 目标
    "arm_max_error_px": 6.0,           # Phase 1 Arm 最大误差目标
    "rms_regression_tol_px": 0.5,      # 新模型 RMS 允许的变差容差
    "model_shift_max_mm": 30.0,        # 新旧模型 9 点预测差异上限（mm）
    "max_resample": 3,                 # 单点自动重采样上限
    "touch_retry": 2,                  # 触点超时新 sampleId 重试次数
    "consecutive_anomaly_abort": 3,    # 连续异常进入 ABORT
    "max_iterations": 3,               # Phase1+Phase2 迭代上限
    "acceptance_timeout_s": 3600.0,    # E 验收等待操作员确认输入法结果的超时
    "arm_workspace_mm": None,          # [xmin,xmax,ymin,ymax]；真机必须现场提供
    "safe_margin_px": 24.0,            # 屏幕安全边界裕量
}
# 9 点网格任一点被 RANSAC 排除即整体拒绝（规格 §9.3）；fit_camera_to_screen_strict 保证
AUTO_CAMERA_SOURCE = "screen_target"


class AutoRunHost(Protocol):
    """runner 对宿主的最窄消费面（成员集即契约，签名以 wizard 实现为准）。"""

    state: dict
    daemon: Any
    ime: Any
    device: str
    data_root: Path
    _camera_fetch: Any
    _edge_detector: Any
    _target_detector: Any
    _target_candidates: Any
    _target_draw_wait_s: float
    _target_detect_timeout_s: float
    post_promote_hook: Any
    _installation_lock_root: Path
    _position_context: Any
    _real_hardware: bool
    _lock: Any
    _auto_cfg: dict

    def _acceptance_done(self, *args, **kwargs): ...
    def _bundle(self, *args, **kwargs): ...
    def _bundle_edge_corners(self, *args, **kwargs): ...
    def _camera_status(self, *args, **kwargs): ...
    def _clear_session_rebind(self, *args, **kwargs): ...
    def _daemon_tap(self, *args, **kwargs): ...
    def _edge_quad(self, *args, **kwargs): ...
    def _execute_acceptance_debug(self, *args, **kwargs): ...
    def _goto_robot(self, *args, **kwargs): ...
    def _mark_session_rebind(self, *args, **kwargs): ...
    def _read_binding_facts(self, *args, **kwargs): ...
    def _read_position(self, *args, **kwargs): ...
    def _require_ime_open(self, *args, **kwargs): ...
    def _require_park_session_current(self, *args, **kwargs): ...
    def _require_session(self, *args, **kwargs): ...
    def _save_gesture_coordinate_frame(self, *args, **kwargs): ...
    def _save_state(self, *args, **kwargs): ...
    def _tap_with_report(self, *args, **kwargs): ...
    def _target_rect(self, *args, **kwargs): ...


class AutoRunner:
    """v1.1 纯代码自动视觉闭环校准 runner（行为等价外迁，ADR-0003）。"""

    def __init__(self, host: AutoRunHost) -> None:
        self.host = host
        self._auto_lock = threading.RLock()  # 锁序恒为 host._lock → self._auto_lock
        self._auto = {
            "run": None,
            "thread": None,
            "cancel": threading.Event(),
            "lastRun": None,
            # R2-04：后台自动校准动作持有的共享安装锁句柄（跟随实际执行生命周期，
            # 认领→runner→最后安全收尾→释放；不由提前返回的 HTTP handler 代持）。
            "action_lock": None,
        }

    def _auto_invalidate_stale_runs(self) -> None:
        """服务重启后，磁盘上仍处于活跃态的 run/candidate 一律作废（§26/§31）。

        runner 是内存线程，重启后不存在；继续沿用旧 run 会把跨进程的样本混进
        同一 candidate。作废只写 candidate（保留历史），不触碰活动 bundle。
        """
        try:
            root = cb.candidates_dir(self.host.device, self.host.data_root)
            if not root.exists():
                return
            for path in sorted(root.glob("*.json")):
                try:
                    candidate = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if not isinstance(candidate, dict) \
                        or candidate.get("state") not in AUTO_ACTIVE_STATES:
                    continue
                candidate["state"] = AUTO_STATE_ABORTED
                candidate["executionEligibility"] = {
                    "eligible": False,
                    "reason": "service_restart",
                    "revokedAt": _now(),
                }
                candidate["lastFailure"] = {
                    "code": "service_restart",
                    "message": "8792 服务重启：runner 线程不存在，run 作废（candidate 保留为历史）",
                    "at": _now(),
                }
                candidate.setdefault("events", []).append(
                    {"ts": _now(), "event": "service_restart_invalidated"})
                cb.save_candidate(candidate, self.host.data_root)
        except Exception as exc:
            # 重启作废扫描失败只留痕不阻断加载；执行资格仍按 daemonSession
            # 逐项复核，扫描失败不会单独导致错误动作（独立验收建议）。
            print(f"重启候选作废扫描失败（不影响加载，执行资格照常复核）：{exc}",
                  flush=True)

    # ---------- v1.1 自动校准：对外 API（§24） ----------

    def auto_start(self, *, max_iterations: int | None = None,
                   point_plan: list[dict] | None = None,
                   defer_input_acceptance: bool = True) -> dict:
        """创建唯一 run/candidate 并启动后台 runner（§24 auto-start）。

        前置：A 会话已建立且手机校准页 open、daemon 会话与绑定一致、
        操作员已确认 zSafe/tapDepth 与 SAFE PARK、屏幕基线可用、摄像头可用。
        自动校准不依赖手动 B/C/D 阶段：屏幕基线优先取向导现场回报，
        缺失时回退活动档案（首次触点回报时会再校验一致性）。
        纯观测档（goal=observation，Auto_CS#20）在入口整体拒绝——观测会话
        永不进入运动域（wizard.auto_start 是纯委托门面，seam 守卫约束）。
        """
        if self.host.state.get("goal") == GOAL_OBSERVATION:
            raise WizardError(
                "本向导会话为纯观测校准（observation）：不含机械臂动作阶段，"
                "自动校准不可用；如需动作校准请以运动目标重新开始")
        if type(defer_input_acceptance) is not bool:
            raise WizardError("deferInputAcceptance 必须是显式布尔值")
        with self.host._lock:
            with self._auto_lock:
                current = self._auto["run"]
                if current and current["state"] in AUTO_ACTIVE_STATES:
                    raise WizardError(f"自动校准运行中（{current['runId']}，"
                                      f"{current['state']}）：请先停止再开始")
                # --- PRECHECK（§5）：全部通过才创建 run ---
                stage = self.host.state.get("stage")
                if stage in (STAGE_IDLE, STAGE_PRECHECK, STAGE_INVALIDATED):
                    raise WizardError("请先完成 A 阶段「开始校准」并让手机校准页打开")
                self.host._require_ime_open()
                # 用户拍板 2026-09-29：XY 复核可由「本会话已确认的点击锚」
                # 推算承担（按已设置的点击参数推算走位）——门触点实测 +
                # Phase1 全量重拟合 + Phase2 独立验证兜底；无新鲜锚则仍拦。
                rebind = self.host.state.get("sessionRebind") or {}
                anchor = (self.host.state.get("z") or {}).get("tapAnchor") or {}
                live_session = (self.host.daemon.status() or {}).get("session")
                if rebind.get("required") and (rebind.get("pendingScopes") or {}).get("xy") \
                        and anchor.get("daemonSession") == live_session \
                        and anchor.get("screenX") is not None:
                    self.host._clear_session_rebind(scope="xy")
                    self.host._save_state()
                    print("XY 资格由本会话点击锚推算放行", flush=True)
                status = self.host._require_session()
                # 屏幕基线：向导现场回报优先；缺失时回退活动档案（自动校准不依赖手动 B 阶段）。
                # 首次触点回报会经 _check_screen_consistency 用手机真实上报复核，不符即失效。
                screen = self.host.state.get("screen") or {}
                if not screen.get("screenWidth") or not screen.get("screenHeight"):
                    profile_screen = (cb.load_bundle(self.host.device, self.host.data_root) or {}).get("screen") or {}
                    if profile_screen.get("screenWidth") and profile_screen.get("screenHeight"):
                        screen = {"orientation": profile_screen.get("orientation") or "portrait",
                                  "screenWidth": int(profile_screen["screenWidth"]),
                                  "screenHeight": int(profile_screen["screenHeight"]),
                                  "source": "profile_bundle"}
                        self.host.state["screen"] = dict(screen)
                        self.host._save_state()
                    else:
                        raise WizardError("屏幕尺寸未知：向导与档案都没有屏幕基线，"
                                          "请先手动完成一次测试点击或重新校准")
                auto_safe = self.host.state["z"].get("autoSafe") or {}
                if not auto_safe.get("validated"):
                    raise WizardError("Z 安全高度/点击深度尚未通过验证：请先把探头停在"
                                      "手机屏幕上方，「确认 zSafe + 点击参数」会执行一次"
                                      "测试点击并等待触点回报")
                park = self.host.state.get("safePark") or {}
                if not park.get("confirmed") or park.get("parkX") is None:
                    raise WizardError("SAFE PARK 尚未由操作员确认：请先把机械臂停在"
                                      "安全停靠位并「确认 SAFE PARK」")
                # 停车位按其自身确认会话核对（P09）：顶层会话更新不能替
                # 旧停车位背书，自动校准起始停车不得使用跨坐标基准的坐标。
                self.host._require_park_session_current(
                    park, action="自动校准起始停车")
                self.host._camera_status()
                # 工作区范围改为可选（用户拍板 2026-09-03：机械无限位，不强制配置）：
                # 安全由计算门禁承担——屏幕安全区投影、300mm 单次位移上限、走位
                # 残差 ≤3mm、Bootstrap 逐点预测校验；配置了 arm_workspace_mm 时仍生效。
                plan = self._auto_plan_points(point_plan)
                base = self.host._bundle()
                run = {
                    "runId": f"run-{uuid.uuid4().hex[:12]}",
                    "deviceId": self.host.device,
                    "schemaVersion": 1,
                    "createdAt": _now(),
                    "baseProfileHash": cb.profile_hash(base),
                    "baseGeometryHash": cb.profile_hash(base, keys=AUTO_PROMOTE_GUARD_KEYS),
                    "baseStatus": base.get("status"),
                    "realHardware": bool(self.host._real_hardware),
                    "deferInputAcceptance": defer_input_acceptance,
                    "runtime": self._auto_runtime_facts(base),
                    "binding": dict(self.host._read_binding_facts() or {}),
                    "daemonSession": status.get("session"),
                    "phoneSessionId": self.host.state.get("imeSessionId"),
                    "screen": dict(screen),
                    "z": dict(auto_safe),
                    "safePark": dict(park),
                    "config": dict(self.host._auto_cfg),
                    "maxIterations": int(max_iterations or self.host._auto_cfg["max_iterations"]),
                    "pointPlan": plan,
                    "validationPlan": [{"id": n, "u": u, "v": v}
                                       for n, u, v in AUTO_VALIDATION_POINTS],
                    "state": AUTO_STATE_RUNNING,
                    "phase": None,
                    "iteration": 0,
                    "modelVersion": 0,
                    "locks": {
                        "cameraToScreen": {"state": cb.MODEL_LOCK_UNINITIALIZED, "version": 0},
                        "armAffine": {"state": cb.MODEL_LOCK_UNINITIALIZED, "version": 0},
                    },
                    "h": None,
                    "armSeed": {
                        "robotToScreen": ((base.get("xy") or {}).get("robotToScreen")),
                        "source": f"profile_seed({base.get('status')})",
                        "seedHUsable": self._auto_seed_h_usable(base),
                        "seedH": (((base.get("camera") or {}).get("cameraToScreen") or {})
                                  .get("homography")),
                    },
                    "arm": None,
                    "armHistory": [],
                    "samples": [],
                    "phase2": {},
                    "validationPoints": [],
                    "validation": {"points": [], "passed": False},
                    "acceptance": {"debug": {}, "wechat": {},
                                   "deferred": {"debug": defer_input_acceptance}},
                    "executionEligibility": {
                        "eligible": True,
                        "daemonSession": status.get("session"),
                        "phoneSessionId": self.host.state.get("imeSessionId"),
                        "grantedAt": _now(),
                    },
                    "events": [],
                    "lastFailure": None,
                    "pendingRetry": None,
                    "consecutiveAnomalies": 0,
                    "edge": None,
                    "lastPark": None,
                }
                self._auto_event(run, "run_created",
                                 {"baseStatus": run["baseStatus"],
                                  "realHardware": run["realHardware"]})
                self._auto_apply_tap_anchor(run)
                cb.save_candidate(run, self.host.data_root)
                self._auto["cancel"].clear()
                self._auto["run"] = run
                self._auto_spawn(run)
                return self.auto_view()

    def auto_stop(self, run_id: str | None = None) -> dict:
        """设置 cancel 并等待 runner 安全停止（§24 auto-stop）。"""
        # E 验收执行持有向导锁；先通知只读等待退出，再取锁收尾。
        active = self._auto["run"]
        if active and (not run_id or active["runId"] == str(run_id)) \
                and active["state"] in {AUTO_STATE_RUNNING, AUTO_STATE_STOPPING}:
            self._auto["cancel"].set()
        thread = None
        with self.host._lock:
            with self._auto_lock:
                run = self._auto["run"]
                if run is None or (run_id and run["runId"] != str(run_id)):
                    return self.auto_view()
                if run["state"] == AUTO_STATE_PAUSED_RETRY:
                    run["state"] = AUTO_STATE_STOPPED
                    run["lastFailure"] = {"code": "operator_stopped",
                                          "message": "操作员停止（暂停重试阶段）",
                                          "at": _now()}
                    self._auto_event(run, "operator_stopped")
                    cb.save_candidate(run, self.host.data_root)
                    self._auto["run"] = None
                    self._auto["lastRun"] = self._auto_run_summary(run)
                    # R2-04：暂停态被操作员停止=动作终局，释放安装锁。
                    self._release_action_installation_lock()
                elif run["state"] == AUTO_STATE_RUNNING:
                    run["state"] = AUTO_STATE_STOPPING
                    self._auto["cancel"].set()
                    thread = self._auto["thread"]
        if thread is not None:
            thread.join(timeout=15.0)
        return self.auto_view()

    def auto_retry(self, run_id: str, target_id: str) -> dict:
        """只重试当前暂停点，使用全新 sampleId/sequence（§24 auto-retry）。"""
        with self.host._lock:
            with self._auto_lock:
                run = self._auto["run"]
                if run is None or run["runId"] != str(run_id):
                    raise WizardError(f"运行 {run_id} 不存在或已结束")
                self._require_candidate_execution(run)
                if run["state"] != AUTO_STATE_PAUSED_RETRY:
                    raise WizardError(f"当前状态 {run['state']} 不是暂停重试：无需重试")
                pending = run.get("pendingRetry") or {}
                if str(target_id) != "*" and str(target_id) != str(pending.get("targetId")):
                    raise WizardError(f"当前暂停点是 {pending.get('targetId')}，"
                                      f"不能重试 {target_id}")
                pending["operatorRetries"] = int(pending.get("operatorRetries", 0)) + 1
                run["pendingRetry"] = pending
                if pending["operatorRetries"] > int(run["config"]["max_resample"]):
                    run["state"] = AUTO_STATE_FAILED
                    run["lastFailure"] = {"code": "retry_budget_exhausted",
                                          "message": f"暂停点 {pending.get('targetId')} "
                                                     "重试次数用尽", "at": _now()}
                    cb.save_candidate(run, self.host.data_root)
                    self._auto["run"] = None
                    self._auto["lastRun"] = self._auto_run_summary(run)
                    # R2-04：重试预算耗尽=动作终局（runner 已退出），释放安装锁。
                    self._release_action_installation_lock()
                    return self.auto_view()
                run["state"] = AUTO_STATE_RUNNING
                resume = {"phase": pending.get("phase"), "targetId": pending.get("targetId")}
                self._auto_event(run, "operator_retry", resume)
                cb.save_candidate(run, self.host.data_root)
                self._auto_spawn(run, resume=resume)
                return self.auto_view()

    def auto_validate(self, run_id: str | None = None) -> dict:
        """显式重跑 Phase 2 + 独立验证（只验证，不改锁定模型，§24 auto-validate）。"""
        with self.host._lock:
            with self._auto_lock:
                run = self._auto["run"]
                if run is None or (run_id and run["runId"] != str(run_id)):
                    raise WizardError(f"运行 {run_id} 不存在（candidate 只读）")
                self._require_candidate_execution(run)
                if run["state"] in AUTO_ACTIVE_STATES:
                    raise WizardError(f"runner 正在 {run['state']}：不能并发验证")
                if run["locks"]["armAffine"].get("state") != cb.MODEL_LOCK_LOCKED \
                        or run["locks"]["cameraToScreen"].get("state") != cb.MODEL_LOCK_LOCKED:
                    raise WizardError("H/Arm 未双锁定：没有可验证的模型")
                run["previousStateForValidate"] = run.get("state")
                run["state"] = AUTO_STATE_RUNNING
                run["validation"] = {"points": [], "passed": False}
                self._auto_event(run, "explicit_validate")
                cb.save_candidate(run, self.host.data_root)
                self._auto_spawn(run, validate_only=True)
                return self.auto_view()

    @staticmethod
    def _candidate_execution_revoked(run: dict, reason: str) -> None:
        eligibility = run.get("executionEligibility") or {}
        run["executionEligibility"] = {
            **eligibility,
            "eligible": False,
            "reason": "session_rebind_required",
            "detail": reason,
            "revokedAt": _now(),
        }

    def _require_candidate_execution(self, run: dict) -> None:
        """拒绝使用已与旧 daemon/IME 会话绑定的 candidate。"""
        eligibility = run.get("executionEligibility") or {}
        if eligibility.get("eligible") is False:
            raise WizardError("candidate 的执行资格已撤销：必须重新开始并完成会话复核")

    def _auto_acceptance_resume_guard(self, run: dict) -> None:
        """只读恢复门禁：冻结的定位结果和当前设备事实必须仍一致。"""
        self._require_candidate_execution(run)
        if self.host.state.get("stage") in {STAGE_IDLE, STAGE_PRECHECK, STAGE_INVALIDATED, STAGE_READY}:
            raise WizardError("当前向导阶段不允许恢复 E 验收")
        if self.host.state.get("daemonSession") != run.get("daemonSession") \
                or self.host.state.get("imeSessionId") != run.get("phoneSessionId"):
            raise WizardError("机械臂或手机校准会话已变化，不能只恢复 E 验收")
        self._auto_guard(run, check_cancel=False)
        base = cb.load_bundle(self.host.device, self.host.data_root)
        if not base or cb.profile_hash(base, keys=AUTO_PROMOTE_GUARD_KEYS) \
                != run.get("baseGeometryHash"):
            raise WizardError("定位几何或绑定基础已变化，不能只恢复 E 验收")
        for name, model_key in (("cameraToScreen", "h"), ("armAffine", "arm")):
            lock = (run.get("locks") or {}).get(name) or {}
            model = run.get(model_key) or {}
            if lock.get("state") != cb.MODEL_LOCK_LOCKED or not lock.get("version") \
                    or model.get("version") != lock.get("version"):
                raise WizardError("H/XY 定位模型锁或版本不完整，不能恢复 E 验收")
        if not (run.get("h") or {}).get("homography") \
                or not (run.get("arm") or {}).get("robotToScreen"):
            raise WizardError("H/XY 定位模型缺失，不能恢复 E 验收")
        validation = run.get("validation") or {}
        points = validation.get("points") or []
        expected = {point[0] for point in AUTO_VALIDATION_POINTS}
        tolerance = float((run.get("config") or {}).get("target_tolerance_px", 0))
        if validation.get("passed") is not True or len(points) != len(expected) \
                or validation.get("passedCount") != len(expected) \
                or {p.get("targetId") for p in points} != expected \
                or points != run.get("validationPoints") \
                or not math.isfinite(tolerance) or tolerance <= 0:
            raise WizardError("5 点独立验证未完整通过，不能恢复 E 验收")
        for point in points:
            error = point.get("touchTargetErrorPx")
            if point.get("passed") is not True or not isinstance(error, (int, float)) \
                    or not math.isfinite(error) or not 0 <= error <= tolerance:
                raise WizardError("独立验证成绩无效，不能恢复 E 验收")
        if not (run.get("z") or {}).get("validated") \
                or run.get("z") != (self.host.state.get("z") or {}).get("autoSafe") \
                or not (run.get("safePark") or {}).get("confirmed") \
                or run.get("safePark") != self.host.state.get("safePark") \
                or run.get("safePark") != base.get("safePark") \
                or (run.get("z") or {}).get("zSafe") != (base.get("z") or {}).get("zSafe"):
            raise WizardError("Z 参数或 SAFE PARK 已变化，不能恢复 E 验收")
        if any((run.get("screen") or {}).get(key) != (self.host.state.get("screen") or {}).get(key)
               for key in ("orientation", "screenWidth", "screenHeight")):
            raise WizardError("屏幕基线已变化，不能恢复 E 验收")

    def _auto_acceptance_resume_position(self, run: dict) -> dict:
        """只读确认当前已安全停靠；历史收尾成功或失败均不能替代新 MPOS。"""
        try:
            position = self.host._read_position()
            if position.get("state") != "Idle":
                raise ValueError("机械臂未确认 Idle")
            park = run["safePark"]
            targets = {"x": park["parkX"], "y": park["parkY"], "z": park["parkZ"]}
            for axis, target in targets.items():
                value = position.get(axis)
                if isinstance(value, bool) or not isinstance(value, (int, float)) \
                        or not math.isfinite(value) or not math.isfinite(float(target)) \
                        or abs(value - float(target)) >= 0.05:
                    raise ValueError(f"{axis.upper()} 未确认处于 SAFE PARK")
            z_safe = float(run["z"]["zSafe"])
            if not math.isfinite(z_safe) or abs(position["z"] - z_safe) >= 0.05:
                raise ValueError("Z 未确认处于安全悬停高度")
            self._auto_guard(run, check_cancel=False)
        except _AutoCancelled:
            raise
        except Exception as exc:
            raise AutoRunError(
                "acceptance_resume_position_unconfirmed",
                f"E 恢复前当前位置或安全收尾未确认：{exc}；未执行恢复或停车动作",
                classification="hardware_investigation", park=False) from exc
        return {"mpos": dict(position), "daemonSession": run["daemonSession"],
                "checkedAt": _now()}

    def auto_resume_acceptance(self, run_id: str) -> dict:
        """auto-acceptance-resume：只恢复最近一次 E 超时，不重跑定位或沿用旧确认。"""
        with self.host._lock, self._auto_lock:
            thread = self._auto.get("thread")
            if self._auto.get("run") is not None or (thread is not None and thread.is_alive()):
                raise WizardError("已有校准运行或收尾进行中，不能恢复 E 验收")
            latest = self._auto.get("lastRun") or self.host.state.get("autoCalibration") or {}
            if not run_id or latest.get("runId") != run_id:
                raise WizardError("只能恢复本向导最近一次 E 验收超时")
            run = cb.load_candidate(self.host.device, run_id, self.host.data_root)
            if not run or run.get("runId") != run_id or run.get("deviceId") != self.host.device \
                    or run.get("state") != AUTO_STATE_FAILED \
                    or run.get("phase") != "E_ACCEPTANCE" \
                    or (run.get("lastFailure") or {}).get("code") != "acceptance_timeout":
                raise WizardError("候选不是 E 验收超时，不能跳过定位恢复")
            self._auto_acceptance_resume_guard(run)
            position = self._auto_acceptance_resume_position(run)
            self._auto_event(run, "acceptance_resumed", {
                "previousFailure": run["lastFailure"],
                "previousDebug": (run.get("acceptance") or {}).get("debug") or {},
                "note": "保留 H/XY/Z/SAFE PARK；重新执行并人工确认两项 E 验收"})
            run.setdefault("acceptance", {})["debug"] = {}
            run["acceptanceResumePosition"] = position
            run["acceptanceResumePending"] = True
            run["lastFailure"] = None
            run["state"] = AUTO_STATE_RUNNING
            cb.save_candidate(run, self.host.data_root)
            self._auto["cancel"].clear()
            self._auto["run"] = run
            self._auto_spawn(run, resume={"phase": "E_ACCEPTANCE"})
            return self.auto_view()

    def auto_resume_motion_qualification(self, run_id: str) -> dict:
        """Explicitly retain verified motion geometry when input acceptance is unavailable."""
        with self.host._lock, self._auto_lock:
            thread = self._auto.get("thread")
            if self._auto.get("run") is not None or (thread is not None and thread.is_alive()):
                raise WizardError("已有校准运行或收尾进行中，不能保存机械资格")
            latest = self.host.state.get("autoCalibration") or {}
            if not run_id or latest.get("runId") != run_id:
                raise WizardError("只能恢复本向导最近一次 E 验收候选")
            run = cb.load_candidate(self.host.device, run_id, self.host.data_root)
            failure = (run or {}).get("lastFailure") or {}
            if (not run or run.get("runId") != run_id
                    or run.get("deviceId") != self.host.device
                    or run.get("state") not in {AUTO_STATE_FAILED, AUTO_STATE_STOPPED}
                    or run.get("phase") != "E_ACCEPTANCE"
                    or run.get("deferInputAcceptance") is True
                    or failure.get("code") not in {
                        "operator_stopped", "acceptance_timeout",
                        "input_acceptance_unavailable", "acceptance_execution_failed",
                    }):
                raise WizardError("候选不是可恢复的 E 验收中止，不能保存机械资格")
            self._auto_acceptance_resume_guard(run)
            position = self._auto_acceptance_resume_position(run)
            self._auto_event(run, "input_acceptance_deferred", {
                "previousFailure": failure, "resumePosition": position,
                "note": "仅保存已独立验证的机械资格；输入和发送仍被门禁拒绝"})
            run["acceptance"] = {
                "debug": {}, "wechat": {},
                "deferred": {"debug": True, "wechat": True},
            }
            run["deferInputAcceptance"] = True
            run["acceptanceResumePosition"] = position
            run["lastFailure"] = None
            run["state"] = AUTO_STATE_RUNNING
            cb.save_candidate(run, self.host.data_root)
            self._auto["cancel"].clear()
            self._auto["run"] = run
            self._auto_spawn(run, resume={"phase": "E_ACCEPTANCE"})
            return self.auto_view()

    def auto_acceptance_execute(self, run_id: str, check: str) -> dict:
        """自动 run 的 E 验收执行（复用 _execute_acceptance_debug，模型取自 candidate）。"""
        with self.host._lock:
            with self._auto_lock:
                run = self._auto["run"]
                if run is None or run["runId"] != str(run_id):
                    raise WizardError(f"运行 {run_id} 不存在或已结束")
                if run.get("deferInputAcceptance") is True:
                    raise WizardError("本轮输入法验收已延后，请在机械校准保存后使用手动 E 验收")
                self._require_candidate_execution(run)
                if check not in DEBUG_ACCEPTANCE_CHECKS:
                    raise WizardError(f"未知验收项：{check}")
                if run.get("phase") != "E_ACCEPTANCE":
                    raise WizardError(f"runner 当前在 {run.get('phase')}："
                                      f"E 验收执行只在 E_ACCEPTANCE 阶段允许")
                if not run.get("h") or not run.get("arm"):
                    raise WizardError("candidate 的 H/Arm 模型未就绪：E 验收不可执行")
                homography = {"homography": run["h"]["homography"],
                              "frameW": run["binding"].get("frameWidth"),
                              "frameH": run["binding"].get("frameHeight")}
                fit = run["arm"]["robotToScreen"]
                # 悬停基准必须来自 candidate（G92 锚定后的 zSafe），不用 state/默认值
                z_safe = (run.get("z") or {}).get("zSafe")
                if z_safe is None:
                    raise WizardError("candidate 缺少 zSafe：E 验收走位无悬停基准，拒绝执行")
                if run.get("acceptanceResumePending"):
                    self._auto_check_cancel()
                    self._auto_acceptance_resume_guard(run)
                    position = self._auto_acceptance_resume_position(run)
                    self._auto_check_cancel()
                    run["acceptanceResumePosition"] = position
                    run["acceptanceResumePending"] = False
                record = run["acceptance"].setdefault("debug", {}).setdefault(check, {})
                result = self.host._execute_acceptance_debug(check, homography, fit,
                                                         None, None, record,
                                                         z_safe=float(z_safe))
                self._auto_event(run, "acceptance_executed", {"check": check})
                cb.save_candidate(run, self.host.data_root)
                return result

    def auto_acceptance_record(self, run_id: str, kind: str, check: str, ok: bool,
                               detail: str = "") -> dict:
        """自动 run 的 E 验收操作员 ✓/✘ 记录（与手动 E 同语义）。"""
        with self.host._lock:
            with self._auto_lock:
                run = self._auto["run"]
                if run is None or run["runId"] != str(run_id):
                    raise WizardError(f"运行 {run_id} 不存在或已结束")
                self._require_candidate_execution(run)
                if run.get("deferInputAcceptance") is True:
                    raise WizardError("本轮输入法验收已延后，请在机械校准保存后使用手动 E 验收")
                required = {"debug": DEBUG_ACCEPTANCE_CHECKS,
                            "wechat": WECHAT_ACCEPTANCE_CHECKS}.get(kind)
                if required is None or check not in required:
                    raise WizardError(f"未知验收项：{kind}/{check}")
                record = run["acceptance"].setdefault(kind, {}).setdefault(check, {})
                record.update({"ok": bool(ok), "detail": str(detail), "ts": _now()})
                self._auto_event(run, "acceptance_recorded",
                                 {"kind": kind, "check": check, "ok": bool(ok)})
                cb.save_candidate(run, self.host.data_root)
                return {"kind": kind, "check": check, "ok": bool(ok),
                        "done": self._auto_acceptance_done(run)}

    def auto_view(self) -> dict:
        """runner 状态视图（§24 GET /api/wizard/state 的 auto 部分）。

        run 结束后顶层继续显示最后一次 run 的摘要（activeRunId=None 区分无活动 run），
        页面/测试据此看到最终状态（STOPPED/FAILED/PROMOTED…），而不是只改按钮样式。
        """
        with self._auto_lock:
            run = self._auto["run"]
            # S1（第六轮，执行单§3）：进程重建后终局摘要不得凭空消失——
            # lastRun 为空且本实例尚未尝试恢复时，从最新持久 candidate 回读
            # （events 含 pose_gate_skipped，摘要随 _auto_run_summary 重建）。
            if (run is None and self._auto.get("lastRun") is None
                    and not self._auto.get("lastRunRestoreTried")):
                self._auto["lastRunRestoreTried"] = True
                try:
                    candidates = cb.list_candidates(self.host.device, self.host.data_root)
                    if candidates:
                        latest = candidates[-1]
                        restored = cb.load_candidate(
                            self.host.device, latest.get("runId"), self.host.data_root)
                        if restored and restored.get("state") in (
                                AUTO_STATE_PROMOTED, AUTO_STATE_FAILED,
                                AUTO_STATE_ABORTED, AUTO_STATE_INVALIDATED,
                                AUTO_STATE_STOPPED):
                            self._auto["lastRun"] = self._auto_run_summary(restored)
                except Exception:
                    pass
            last = self._auto.get("lastRun")
            if run:
                view = self._auto_run_summary(run, full=True)
            else:
                view = {"activeRunId": None, "runId": None, "state": AUTO_STATE_IDLE,
                        "phase": None, "phaseDisplay": None, "iteration": None,
                        "modelVersion": None, "createdAt": None, "maxIterations": None,
                        "pendingRetry": None,
                        "locks": None, "lastFailure": None, "h": None, "arm": None,
                        "armSeed": None}
                if last:
                    view.update({k: v for k, v in last.items() if k != "activeRunId"})
            view["lastRun"] = last
        with self.host._lock:
            auto_safe = self.host.state.get("z", {}).get("autoSafe")
            view["confirmations"] = {
                "zSafe": auto_safe, "safePark": self.host.state.get("safePark"),
            }
            view["requiredDebugChecks"] = list(DEBUG_ACCEPTANCE_CHECKS)
            view["requiredWechatChecks"] = []
        try:
            view["candidates"] = cb.list_candidates(self.host.device, self.host.data_root)
        except Exception:
            view["candidates"] = []
        return view

    def auto_run_active(self) -> bool:
        """自动校准 run 是否活动（runner 持有运动权；调试台运动端点据此拒绝）。"""
        with self._auto_lock:
            return self._auto["run"] is not None

    # ---------- v1.1 自动校准：runner 内部 ----------

    @staticmethod
    def _auto_plan_points(point_plan: list[dict] | None) -> list[dict]:
        """标定计划：默认固定 4x4=16 点网格（2026-09-04 方案 A）；自定义计划必须
        恰好 16 点行主序网格（fit_camera_to_screen_strict 按 4x4 行主序做单调性检查）。"""
        if not point_plan:
            return [{"id": name, "u": u, "v": v} for name, u, v in AUTO_PLAN_POINTS]
        plan = []
        for item in point_plan:
            u, v = float(item.get("u")), float(item.get("v"))
            if not (0.05 <= u <= 0.95 and 0.05 <= v <= 0.95):
                raise WizardError(f"标定点 ({u},{v}) 超出安全区域 0.05~0.95")
            plan.append({"id": str(item.get("id") or f"point-{len(plan) + 1}"),
                         "u": u, "v": v})
        if len(plan) != len(AUTO_PLAN_POINTS):
            raise WizardError(f"标定点计划必须恰好 {len(AUTO_PLAN_POINTS)} 点"
                              f"（当前 {len(plan)}）：自动 Phase 0 按 4x4 行主序"
                              "网格做全点质量检查")
        return plan

    @staticmethod
    def _auto_seed_h_usable(base: dict) -> bool:
        """旧 H 能否作为 bootstrap 对比基线：必须是已验证且视觉验证未失败的档案。"""
        camera = base.get("camera") or {}
        visual = camera.get("visualVerify")
        return bool(camera.get("validated") and camera.get("cameraToScreen")
                    and not (isinstance(visual, dict) and visual.get("passed") is False))

    def _auto_runtime_facts(self, base: dict) -> dict:
        """运行版本绑定（补充文档 §7）：PID/路径/解释器/revision/绑定身份全记录。"""
        import os
        import subprocess
        import sys

        git_revision = None
        try:
            git_revision = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=str(self.host.data_root.parent),
                capture_output=True, text=True, timeout=5).stdout.strip() or None
        except Exception:
            git_revision = None
        binding = self.host._read_binding_facts() or {}
        frame = f"{binding.get('frameWidth')}x{binding.get('frameHeight')}"
        return {
            "pid": os.getpid(),
            "cwd": os.getcwd(),
            "python": sys.executable,
            "module": str(Path(__file__).resolve()),
            "gitRevision": git_revision,
            "sourceHash": cb.profile_hash(base)[:16],
            "cameraId": binding.get("cameraId"),
            "cameraResolution": frame,
            "armId": self.host.device,
            "phoneSessionId": self.host.state.get("imeSessionId"),
            "daemonSession": self.host.state.get("daemonSession"),
            "profileSchemaVersion": base.get("schemaVersion"),
            "profileFingerprint": base.get("bindingFingerprint"),
        }

    def _pose_warnings_of(self, run: dict, limit: int = 20) -> list:
        """从 run 持久事件提取 H01 采样姿态告警的中文展示列表。

        旧记录（仅 reason）兼容：分类/阶段/位移显示"未知"，不虚构数值。
        """
        warnings = []
        for e in (run.get("events") or []):
            if e.get("event") != "pose_gate_skipped":
                continue
            detail = e.get("detail") or {}
            measured = detail.get("measuredDisplacementPx")
            warnings.append({
                "ts": e.get("ts"),
                "phase": e.get("phase"),
                "categoryDisplay": detail.get("categoryDisplay") or "未知",
                "stageDisplay": detail.get("stageDisplay") or "未知",
                "reason": detail.get("reason") or "未知",
                "measuredPx": measured if isinstance(measured, (int, float)) else None,
                "measuredDisplay": (
                    f"{measured}px" if isinstance(measured, (int, float))
                    else "姿态未核实"),
                "thresholdDisplay": (
                    f"{detail.get('thresholdPx')}px"
                    if isinstance(detail.get("thresholdPx"), (int, float))
                    else "未知"),
            })
        return warnings[-limit:]

    def _auto_run_summary(self, run: dict, *, full: bool = False) -> dict:
        summary = {
            "activeRunId": run.get("runId"),
            "runId": run.get("runId"),
            "state": run.get("state"),
            "phase": run.get("phase"),
            "phaseDisplay": AUTO_PHASE_DISPLAY.get(run.get("phase") or "", run.get("phase")),
            "iteration": run.get("iteration"),
            "modelVersion": run.get("modelVersion"),
            "createdAt": run.get("createdAt"),
            "maxIterations": run.get("maxIterations"),
            "pendingRetry": run.get("pendingRetry"),
            "locks": run.get("locks"),
            "lastFailure": run.get("lastFailure"),
            "deferInputAcceptance": run.get("deferInputAcceptance", False),
            "qualificationStatus": run.get("qualificationStatus"),
            "h": ({k: run["h"].get(k) for k in
                   ("rms", "maxError", "version", "lockedAt", "source")}
                  if run.get("h") else None),
            "arm": ({k: run["arm"].get(k) for k in
                     ("residualRmsPx", "maxResidualPx", "sampleCount", "version",
                      "lockedAt", "iteration")}
                    if run.get("arm") else None),
            "armSeed": {"source": (run.get("armSeed") or {}).get("source"),
                        "hasSeed": bool((run.get("armSeed") or {}).get("robotToScreen")),
                        "anchor": (run.get("seedAnchor") or None)},
            # P4（第五轮§9.2 H01）：采样姿态告警进结束摘要——进行中与结束后
            # 调试台均可读；旧记录缺字段按"未知"显示，不编造数值。
            "poseWarnings": self._pose_warnings_of(run),
        }
        if full:
            samples = run.get("samples") or []
            summary["currentSample"] = samples[-1] if samples else None
            summary["recentSamples"] = samples[-12:]
            summary["phase2"] = run.get("phase2")
            summary["validation"] = run.get("validation")
            summary["validationPlan"] = run.get("validationPlan")
            summary["pointPlan"] = run.get("pointPlan")
            summary["acceptance"] = run.get("acceptance")
            summary["thresholds"] = run.get("config")
            summary["events"] = (run.get("events") or [])[-30:]
        return summary

    def _auto_event(self, run: dict, event: str, detail: dict | None = None) -> None:
        run.setdefault("events", []).append(
            {"ts": _now(), "phase": run.get("phase"), "event": event,
             "detail": detail or {}})

    def _auto_abort_for_wizard_cancel(self) -> None:
        """手动「取消/重置」向导时作废在跑的自动 run（不 join，runner 自行退出）。"""
        with self._auto_lock:
            run = self._auto["run"]
            if not run or run.get("state") not in AUTO_ACTIVE_STATES:
                return
            run["state"] = AUTO_STATE_ABORTED
            run["lastFailure"] = {"code": "wizard_cancelled",
                                  "message": "统一校准向导被手动重置：run 作废",
                                  "at": _now()}
            self._auto_event(run, "wizard_cancel_abort")
            try:
                cb.save_candidate(run, self.host.data_root)
            except Exception:
                pass
            self._auto["cancel"].set()
            self._auto["run"] = None
            self._auto["lastRun"] = self._auto_run_summary(run)

    # ---- R2-04（camera-binding 附件 §4.1）：安装锁跟随真实动作生命周期 ----
    # 共享安装锁由实际执行者持有：认领（_auto_spawn 前非阻塞取得）→ runner 实际
    # 执行 → 最后安全收尾（_auto_finalize 终局持久化完成）→ 释放。提前返回的
    # HTTP handler 不再是（唯一）持锁者；获取失败立即 busy，不排队等待。

    def _claim_action_installation_lock(self) -> None:
        """动作执行者非阻塞取得本设备共享安装锁；忙则立即拒绝（不排队）。

        同一动作生命周期内已持有（暂停重试/auto_retry/auto_validate 续用）时
        不重复获取；锁顺序恒为 安装锁 → 向导内部锁，获取永不等待。
        """
        if self._auto.get("action_lock") is not None:
            return
        import scripts.camera_binding_v2 as camera_v2  # 延迟导入避免环
        try:
            lock = camera_v2.installation_lock(
                self.host._installation_lock_root, self.host.device, exclusive=False)
            lock.__enter__()
        except Exception as exc:
            error = WizardError(
                f"设备 {self.host.device} 安装占用中（绑定写或另一动作进行），"
                f"自动校准动作拒绝：{exc}")
            error.code = "device_maintenance_busy"
            raise error from exc
        self._auto["action_lock"] = lock

    def _release_action_installation_lock(self) -> None:
        """释放动作安装锁（幂等）；只在动作终局/安全收尾后调用。"""
        lock = self._auto.get("action_lock")
        if lock is None:
            return
        self._auto["action_lock"] = None
        try:
            lock.__exit__(None, None, None)
        except Exception:
            pass

    def _auto_spawn(self, run: dict, *, resume: dict | None = None,
                    validate_only: bool = False) -> None:
        # R2-04：runner 线程启动前由实际执行者认领共享安装锁（busy 立即拒绝）；
        # 锁句柄随线程直到最后安全收尾，HTTP handler 返回不再终止保护。
        self._claim_action_installation_lock()
        thread = threading.Thread(
            target=self._auto_thread_main,
            args=(run, resume, validate_only),
            daemon=True, name=f"wizard-auto-{self.host.device}-{run['runId']}")
        self._auto["thread"] = thread
        thread.start()

    def _auto_thread_main(self, run: dict, resume: dict | None,
                          validate_only: bool) -> None:
        try:
            if validate_only:
                self._auto_execute_validate(run)
            else:
                self._auto_execute(run, resume)
        except Exception as exc:  # 兜底：任何未分类异常按 ambiguous 失败处理
            self._auto_finalize(run, AUTO_STATE_FAILED, {
                "code": "runner_unexpected_error", "message": str(exc), "at": _now()},
                park=True)
        finally:
            # R2-04：兜底释放——终局收尾已在 _auto_finalize 释放；此处覆盖
            # 向导取消作废（run 已被摘除但 runner 仍在收尾）等非常规路径。
            # 暂停重试（run 仍挂载且活跃）不在此释放：动作仍在途。
            still_active = (self._auto.get("run") is run
                            and run.get("state") in AUTO_ACTIVE_STATES)
            if not still_active:
                self._release_action_installation_lock()

    def _auto_execute(self, run: dict, resume: dict | None = None) -> None:
        try:
            acceptance_only = (resume or {}).get("phase") == "E_ACCEPTANCE"
            if acceptance_only:
                self._auto_check_cancel()
                self._auto_acceptance_resume_guard(run)
            elif resume is None:
                self._auto_phase_precheck(run)
                self._auto_phase_safe_park(run)
                self._auto_phase0_camera(run)
                self._auto_lock_camera_model(run)
                self._auto_bootstrap_check(run)
                self._auto_first_click_gate(run)
                failing: list[str] = []
                while run["iteration"] < run["maxIterations"]:
                    run["iteration"] += 1
                    if run["iteration"] == 1 or not failing:
                        self._auto_phase1_arm(run)  # 首轮：全部 9 点
                    else:
                        # 迭代轮：只重采上一轮 Phase 2 的失败点（新 sampleId），
                        # 样本集累积、全量等权重拟合，随后只复验失败点
                        self._auto_event(run, "iteration_resample",
                                         {"iteration": run["iteration"],
                                          "points": list(failing)})
                        for pid in failing:
                            point = next(p for p in run["pointPlan"] if p["id"] == pid)
                            self._auto_phase1_point(run, point)
                    self._auto_update_arm_candidate(run)
                    self._auto_lock_arm_model(run)
                    failing = self._auto_phase2_validate(run)
                    run.setdefault("history", []).append({
                        "iteration": run["iteration"],
                        "armRmsPx": (run.get("arm") or {}).get("residualRmsPx"),
                        "phase2MaxPx": self._auto_phase2_max(run),
                        "softFailing": list(failing),
                        "at": _now(),
                    })
                    if not failing and self._auto_check_convergence(run):
                        break
                else:
                    raise AutoRunError(
                        "max_iterations_exceeded",
                        f"迭代 {run['maxIterations']} 轮未收敛，未通过点："
                        f"{', '.join(failing)}（最后误差见 phase2SoftFails）",
                        classification="arm_mapping_error",
                        routed_to="ARM_RECALIBRATION")
            else:
                # 暂停重试恢复：锁必须完整；重采该点的 Phase 1 样本 → 全量重拟合
                # （含回归/跳变门禁）→ 重新锁定 → 只重验该 Phase 2 点及其余未过点。
                if run["locks"]["cameraToScreen"].get("state") != cb.MODEL_LOCK_LOCKED \
                        or run["locks"]["armAffine"].get("state") != cb.MODEL_LOCK_LOCKED:
                    raise AutoRunError("resume_locks_invalid",
                                       "恢复运行时模型锁不完整：candidate 已不可用",
                                       park=False)
                target_id = (resume or {}).get("targetId")
                point = next((p for p in run["pointPlan"] if p["id"] == target_id), None)
                if point is None:
                    raise AutoRunError("resume_target_invalid",
                                       f"恢复点 {target_id} 不在标定计划内", park=False)
                run["pendingRetry"] = None
                self._auto_phase1_point(run, point)
                self._auto_update_arm_candidate(run)
                self._auto_lock_arm_model(run)
                self._auto_phase2_validate(run, resume=resume)
                if not self._auto_check_convergence(run):
                    raise AutoRunError("retry_did_not_converge",
                                       f"重试点 {target_id} 后仍未收敛", park=True)
            if not acceptance_only:
                self._auto_validate_independent(run)
            if run.get("deferInputAcceptance") is True:
                self._auto_guard(run)
                if not acceptance_only:
                    self._auto_safe_park(run)
                self._auto_event(run, "automatic_validation_complete",
                                 {"note": "独立验证完成，准备保存 READY"})
            else:
                self._auto_e_acceptance(run)
            if acceptance_only:
                self._auto_check_cancel()
                self._auto_acceptance_resume_guard(run)
            self._auto_promote_candidate(run)
            self._auto_finalize(run, AUTO_STATE_PROMOTED)
            return
        except _AutoPauseRetry as pause:
            run["pendingRetry"] = {"phase": pause.phase, "targetId": pause.target_id,
                                   "reason": pause.reason, "operatorRetries": 0,
                                   "at": _now()}
            self._auto_event(run, "paused_for_retry", run["pendingRetry"])
            self._auto_finalize(run, AUTO_STATE_PAUSED_RETRY,
                                {"code": "soft_fail_resample_exhausted",
                                 "message": pause.reason, "at": _now()}, park=False)
        except _AutoCancelled:
            self._auto_event(run, "cancelled_by_operator")
            self._auto_finalize(run, AUTO_STATE_STOPPED,
                                {"code": "operator_stopped",
                                 "message": "操作员停止", "at": _now()}, park=True)
        except AutoRunError as exc:
            routed = f"（路由：{exc.routed_to}）" if exc.routed_to else ""
            self._auto_event(run, "run_failed", {"code": exc.code,
                                                 "classification": exc.classification})
            self._auto_finalize(run, exc.state, {
                "code": exc.code, "message": f"{exc.message}{routed}",
                "classification": exc.classification, "routedTo": exc.routed_to,
                "at": _now()}, park=exc.park)
        except TouchReportTimeout as exc:
            self._auto_finalize(run, AUTO_STATE_FAILED, {
                "code": "touch_report_timeout", "message": str(exc),
                "classification": "touch_feedback_error", "at": _now()}, park=True)
        except WizardError as exc:
            # 向导级拒绝（触点错配已触发失效 / 会话异常等）：向导已失效则 run 同步失效
            invalidated = self.host.state.get("stage") == STAGE_INVALIDATED
            self._auto_finalize(run,
                                AUTO_STATE_INVALIDATED if invalidated else AUTO_STATE_FAILED,
                                {"code": "wizard_rejected",
                                 "message": str(exc),
                                 "classification": "touch_feedback_error", "at": _now()},
                                park=True)

    def _auto_execute_validate(self, run: dict) -> None:
        """auto-validate 线程：只重跑 Phase 2 + 独立验证，不学习、不提升。"""
        previous_state = run.get("previousStateForValidate") or AUTO_STATE_STOPPED
        try:
            self._auto_phase2_validate(run)
            self._auto_validate_independent(run)
            self._auto_event(run, "explicit_validate_done",
                             {"passed": run["validation"]["passed"]})
            self._auto_finalize(run, previous_state)
        except _AutoCancelled:
            self._auto_finalize(run, AUTO_STATE_STOPPED,
                                {"code": "operator_stopped", "message": "操作员停止",
                                 "at": _now()}, park=True)
        except AutoRunError as exc:
            self._auto_finalize(run, previous_state, {
                "code": exc.code, "message": exc.message,
                "classification": exc.classification, "at": _now()}, park=exc.park)

    def _auto_finalize(self, run: dict, state: str,
                       failure: dict | None = None, *, park: bool = False) -> None:
        """run 终局：尽力 park → 写 candidate → 摘除活动 run → 同步向导摘要。

        失败/取消/暂停只写 candidate；活动 calibration_bundle.json 保持原样
        （§21.4），只有 PROMOTE 才会改活动档案。
        """
        if run.get("acceptanceResumePending"):
            park = False
            run["lastPark"] = None
            self._auto_event(run, "safe_park_skipped", {
                "reason": "E 恢复首动作门禁未通过，未执行恢复或停车动作"})
        if park:
            self._auto_safe_park(run, best_effort=True)
        elif failure and failure.get("code") in {
                "daemon_command_timeout", "daemon_unreachable", "device_context_mismatch"}:
            run["lastPark"] = None
            self._auto_event(run, "safe_park_skipped", {
                "code": failure["code"],
                "reason": "位置或绑定不可确认，未重连、未执行停车动作"})
        with self._auto_lock:
            run["state"] = state
            if failure:
                run["lastFailure"] = failure
            detached = self._auto.get("run") is not run
            # 暂停重试保持 run 挂载（runner 线程已退出，等待 auto_retry/auto_stop）
            if not detached and state != AUTO_STATE_PAUSED_RETRY:
                self._auto["run"] = None
                self._auto["lastRun"] = self._auto_run_summary(run)
            try:
                cb.save_candidate(run, self.host.data_root)
            except Exception:
                pass
        with self.host._lock:
            self.host.state["autoCalibration"] = {
                "runId": run["runId"], "state": state, "phase": run.get("phase"),
                "iteration": run.get("iteration"),
                "modelVersion": run.get("modelVersion"),
                "lastFailure": failure,
                "deferInputAcceptance": run.get("deferInputAcceptance", False),
                "qualificationStatus": run.get("qualificationStatus"),
                "updatedAt": _now(),
            }
            try:
                self.host._save_state()
            except Exception:
                pass
        if state == AUTO_STATE_PROMOTED and callable(self.host.post_promote_hook):
            # A5（2026-10-01 拍板）：PROMOTE 尾部平台同步链（由宿主注入，
            # 通常为 calibration-status→home-check→resume→事件）。钩子失败
            # 绝不影响已完成的提升结果，失败留痕转人工（现状兜底）。
            try:
                self.host.post_promote_hook(self.host.device, run.get("runId"))
            except Exception as exc:
                self._auto_event(run, "post_promote_sync_failed", {"error": str(exc)})
        # R2-04：最后安全收尾（park/候选持久化/摘要同步）完成才算动作结束；
        # 终局状态在这里释放安装锁。暂停重试仍属在途动作：锁保持持有，
        # auto_retry 续用同一句柄，直到 run 真正终局或被操作员停止。
        if state != AUTO_STATE_PAUSED_RETRY:
            self._release_action_installation_lock()

    def _auto_check_cancel(self) -> None:
        if self._auto["cancel"].is_set():
            raise _AutoCancelled()

    def _auto_phase_set(self, run: dict, phase: str) -> None:
        run["phase"] = phase
        self._auto_event(run, "phase_enter")

    def _auto_guard(self, run: dict, *, check_cancel: bool = True) -> None:
        """每个点前后的一致性守卫（补充文档 §7：任一变化立即停止学习）。"""
        if check_cancel:
            self._auto_check_cancel()
        status = self.host.daemon.status()
        if not status.get("connected") or not status.get("session"):
            raise AutoRunError("daemon_disconnected", "机械臂 daemon 未连接",
                               state=AUTO_STATE_ABORTED,
                               classification="hardware_investigation", park=False)
        if status.get("estop_locked"):
            raise AutoRunError("emergency_stop", "急停锁定中：停止自动校准",
                               state=AUTO_STATE_ABORTED,
                               classification="hardware_investigation", park=False)
        if status.get("session") != run.get("daemonSession"):
            reason = "daemon_session_changed"
            self._candidate_execution_revoked(run, reason)
            self._auto_event(run, "session_execution_revoked", {"reason": reason})
            with self.host._lock:
                self.host._mark_session_rebind(
                    reason,
                    daemon_session=status.get("session"),
                    ime_session_id=self.host.state.get("imeSessionId"))
            raise AutoRunError("daemon_session_changed",
                               f"daemon 会话变化：{run.get('daemonSession')} → "
                               f"{status.get('session')}，candidate 失效",
                               state=AUTO_STATE_INVALIDATED,
                               classification="hardware_investigation",
                               routed_to="HARDWARE_INVESTIGATION", park=False)
        try:
            facts = self.host._read_binding_facts()
        except (WizardError, RuntimeError) as exc:
            # R2-09：事实提供器不可用（含调试台 RuntimeError）按绑定不可用
            # 处理——失效不提升，不冒进继续学习。
            raise AutoRunError("camera_binding_unavailable", str(exc),
                               state=AUTO_STATE_INVALIDATED,
                               classification="camera_measurement_error",
                               routed_to="CAMERA_RECALIBRATION", park=False) from exc
        # R2-09（FIX-07）：安装 UUID/来源几何身份随绑定事实一起核对——run
        # 中途换装/换几何立即失效，不得带着旧身份提升 v3。
        for key in ("cameraId", "cameraDeviceUniqueId", "bindingRevision",
                    "daemonAddr", "frameWidth", "frameHeight",
                    "cameraBindingId", "sourceGeometryId"):
            if str((facts or {}).get(key)) != str((run.get("binding") or {}).get(key)):
                raise AutoRunError("camera_binding_changed",
                                   f"摄像头/绑定事实变化（{key}）：H 失效，停止学习",
                                   state=AUTO_STATE_INVALIDATED,
                                   classification="camera_measurement_error",
                                   routed_to="CAMERA_RECALIBRATION", park=False)
        sid = run.get("phoneSessionId")
        try:
            session = self.host.ime.get_session(sid) if sid else {}
        except ImeError as exc:
            reason = "ime_session_lost"
            self._candidate_execution_revoked(run, reason)
            self._auto_event(run, "session_execution_revoked", {"reason": reason})
            with self.host._lock:
                self.host._mark_session_rebind(
                    reason,
                    daemon_session=self.host.state.get("daemonSession"),
                    ime_session_id=sid)
            raise AutoRunError("ime_session_lost", f"输入法会话丢失：{exc.message}",
                               state=AUTO_STATE_INVALIDATED,
                               classification="touch_feedback_error", park=True) from exc
        if session.get("status") != "active":
            reason = "ime_session_lost"
            self._candidate_execution_revoked(run, reason)
            self._auto_event(run, "session_execution_revoked", {"reason": reason})
            with self.host._lock:
                self.host._mark_session_rebind(
                    reason,
                    daemon_session=self.host.state.get("daemonSession"),
                    ime_session_id=sid)
            raise AutoRunError("ime_session_lost", "输入法会话已结束",
                               state=AUTO_STATE_INVALIDATED,
                               classification="touch_feedback_error", park=True)
        if session.get("clientState") != "open":
            raise AutoRunError("phone_page_closed", "手机校准页未打开",
                               classification="touch_feedback_error", park=True)

    def _auto_phase_precheck(self, run: dict) -> None:
        self._auto_phase_set(run, "PRECHECK")
        self._auto_guard(run)
        binding = run.get("binding") or {}
        if any(binding.get(key) in (None, "") for key in (
                "cameraId", "cameraDeviceUniqueId", "bindingRevision",
                "daemonAddr", "frameWidth", "frameHeight")):
            raise AutoRunError(
                "camera_binding_unavailable", "本轮校准缺少完整设备绑定，禁止首次走位",
                state=AUTO_STATE_INVALIDATED, classification="camera_measurement_error",
                routed_to="CAMERA_RECALIBRATION", park=False)
        self.host._camera_status()
        # 屏幕边缘基准缺失时趁机采集（无运动）；promote 需要它通过运行前门禁
        if self.host._edge_detector is not None:
            try:
                quad = self.host._edge_quad(
                    expected_corners=self.host._bundle_edge_corners())
            except Exception as first_exc:
                # 暗屏唤醒重试（业主 2026-10-16 设计）：屏幕强制自动变暗后
                # 定位必然失败；探头在手机上方（tapAnchor 与当前会话一致）
                # 时，连发两击点亮屏幕后重试一次定位。仍失败才判失败。
                anchor = ((self.host.state.get("z") or {}).get("tapAnchor") or {})
                if not (anchor.get("daemonSession")
                        and anchor.get("daemonSession") == self.host.state.get("daemonSession")):
                    raise AutoRunError(
                        "camera_measurement_error",
                        f"本轮校准无法定位手机屏幕：{first_exc}",
                        classification="camera_measurement_error",
                        routed_to="CAMERA_RECALIBRATION", park=False) from first_exc
                try:
                    # 不调 ensure_debug_lease：运行期租约为 runner 自持，
                    # debug 租约互斥会把唤醒击挤掉（2026-10-16 现场实测）。
                    auto_safe = (self.host.state.get("z") or {}).get("autoSafe") or {}
                    w_depth = float(auto_safe.get("tapDepth") or 8.0)
                    w_feed = float(auto_safe.get("tapFeed") or 2000)
                    self.host._daemon_tap(w_depth, w_feed, 0)
                    time.sleep(0.25)
                    self.host._daemon_tap(w_depth, w_feed, 0)
                    time.sleep(0.8)
                except Exception:
                    pass  # 唤醒击失败不改变判定：下面的重试定位自行给出结论
                try:
                    quad = self.host._edge_quad(
                        expected_corners=self.host._bundle_edge_corners())
                except Exception as exc:
                    raise AutoRunError(
                        "camera_measurement_error", f"本轮校准无法定位手机屏幕：{exc}",
                        classification="camera_measurement_error",
                        routed_to="CAMERA_RECALIBRATION", park=False) from exc
            if type(quad.get("frameSeq")) is not int or quad["frameSeq"] < 0:
                raise ValueError("手机定位缺少持续推流的帧序号")
            run["edge"] = {"corners": quad["corners"],
                           "frameW": quad["frameW"], "frameH": quad["frameH"],
                           "frameSeq": quad["frameSeq"],
                           "capturedAt": _now()}
        # 预检=基础资格门（首次走位前），不是采样姿态复核：帧冻结/超限
        # 在此拒绝（R4-08-C 基础资格与采样告警分别断言；park=False 零动作）。
        self._auto_require_stable_phone(run, on_out_of_range="reject")

    def _auto_require_stable_phone(self, run: dict, *, after_travel: bool = False,
                                   after_frame_seq: int | None = None,
                                   on_out_of_range: str = "warn") -> dict | None:
        """Do not mix touch samples from different physical phone positions.

        on_out_of_range（H01 边界，POSE-POLICY.md）：
        - "warn"（默认）：校准采样姿态复核超限仅提示+留痕，返回 None 不作担保；
        - "reject"：普通动作/验收接触门（手动 Z 点击、E 验收点击）——超限或
          无法核实即拒绝（H01 只放宽采样姿态复核，不放宽点击安全门）。
        """
        if self.host._edge_detector is None:
            raise AutoRunError(
                "camera_measurement_error", "本轮校准无法定位手机屏幕，禁止点击",
                classification="camera_measurement_error", park=after_travel)
        edge = run.get("edge")
        if not edge or not edge.get("corners"):
            raise AutoRunError(
                "camera_measurement_error", "本轮校准缺少手机位置基准，禁止点击",
                classification="camera_measurement_error", park=after_travel)
        def _pose_gate_skip(reason: str, *, stage: str = "sampling",
                            measured_px=None, threshold_px=None) -> None:
            # H01（2026-10-05 用户确认，见第四轮执行入口 §3.2）：采样姿态
            # 复核超限仅提示+留痕，不硬拒；依据为本次决定，非历史授权。
            # （定义必须先于 try：早退路径抛错时 except 才能安全引用。）
            # P4（第五轮§9.2）：告警结构化——分类/阶段/原因/可测位移与阈值；
            # 无法测得时明确空值不编造。事件进 run 持久记录并经
            # _auto_run_summary.poseWarnings 进入结束摘要与调试台显示。
            unverified = not isinstance(measured_px, (int, float))
            self._auto_event(run, "pose_gate_skipped", {
                "category": "pose_sampling_warning",
                # L42：位移测不到时是"未核实"，不得显示成"超限"
                "categoryDisplay": "采样姿态未核实提示" if unverified else "采样姿态超限提示",
                "stage": stage,
                "stageDisplay": {"sampling": "采样姿态复核",
                                 "post_travel": "走位后复核"}.get(stage, stage),
                "reason": reason,
                "measuredDisplacementPx": measured_px,
                "thresholdPx": threshold_px,
            })

        quad = None
        try:
            from drivers.screen_pose import fit_pose_robust
            previous_seq = edge.get("frameSeq")
            if type(previous_seq) is not int or previous_seq < 0:
                # 姿态门尽力而为（H01，2026-10-05）：旧 run/无基准帧时按 0
                # 起锚，不硬失败也不跳过漂移检测（见 pose_gate_skipped 事件）。
                previous_seq = 0
            if after_frame_seq is not None:
                previous_seq = max(previous_seq, after_frame_seq)
            for attempt in range(3):
                # 与 run 起始捕获同锚（档案四角）：梯度定位答案随锚微滑
                # （多边界并存滞回），同锚使该滞回在两次捕获间抵消，
                # 剩余偏差才是真实的手机移动（2026-09-29 真机实测 8.9px
                # 确定性假阳性的根因）。
                # 传 run 基准角（2026-10-16）：采样期笔杆暗影切断亮斑时
                # 区域分割检测全灭，_edge_quad 的锚定梯度路径需要此锚。
                quad = self.host._edge_quad(
                    expected_corners=deepcopy(edge.get("corners")) or None)
                seq = quad.get("frameSeq")
                if type(seq) is int and seq > previous_seq:
                    break
                if attempt < 2:
                    time.sleep(0.08)
            else:
                raise ValueError("手机定位推流帧没有推进")
            binding = run.get("binding") or {}
            for frame_key, binding_key in (("frameW", "frameWidth"), ("frameH", "frameHeight")):
                expected = binding.get(binding_key)
                if expected is not None and (
                        edge.get(frame_key) != expected or quad.get(frame_key) != expected):
                    # S1（第六轮，执行单§3）：帧尺寸/绑定变化属绑定级异常，
                    # 不在 H01（采样姿态）宽捕获范围内——硬拒且零后续动作。
                    # AutoRunError 不被下方 except 元组捕获。
                    raise AutoRunError(
                        "binding_frame_mismatch",
                        "手机定位帧尺寸与设备绑定不一致：绑定变化必须重新校准，禁止继续采样",
                        classification="camera_measurement_error", park=True)
            # 鲁棒拟合：真实移动四角齐动照拦；单角边缘检测噪声
            # （实测一角漂 8px、三角 ±1px）不再误杀 run。剔角容忍只属于
            # 采样复核（warn）语义；动作/验收门（reject）不用剔角容忍
            # （POSE-POLICY.md 普通动作域：非刚性形变=不可核实，失败关闭）。
            # 采样复核阈值 4.0→10.0（业主现场授权 2026-10-16）：新手机/相机几何下
            # 多角检测漂移实测 4-10px（相机像素级稳定、手机无位移已现场确认），
            # 4px 误杀采样；点击/验收门（reject 域）维持 4.0 不变。
            # 走位瞬态沉降复核（2026-10-16 现场实测）：走位后立即复核多角漂移
            # 9.9-11.6px（走位减速瞬态，臂不在视野内），数十秒后自然回落
            # <0.5px——超限时等 2s 重新取帧复检一次，沉降通过即继续；仍超限
            # 落入下方 except 按原语义（采样 H01 warn / 点击 reject）。
            # 三次复核：原始帧 + 两次沉降重取（走位瞬态衰减自适应）
            pose_error: Exception | None = None
            for _attempt in range(3):
                if _attempt > 0:
                    time.sleep(2.0 * _attempt)
                    quad = self.host._edge_quad(
                        expected_corners=deepcopy(edge.get("corners")) or None)
                    seq = quad.get("frameSeq")
                try:
                    fit_pose_robust(edge["corners"], quad["corners"], max_movement=10.0,
                                    allow_single_corner_drop=on_out_of_range != "reject")
                    pose_error = None
                    break
                except (ScreenEdgeError, WizardError, ValueError, KeyError, TypeError) as exc:
                    pose_error = exc
            if pose_error is not None and not run.get("pose_reference_anchored") \
                    and on_out_of_range != "reject":
                # 首次走位锚定（2026-10-16 现场）：基准采集于臂停驻态（笔杆未入画），
                # 首次走位后笔杆贴屏入画改变屏幕边缘检测语境（实测多角 9.9-11.6px、
                # 随采样点变化）——非手机位移。刷新基准为走位后语境并锚定一次
                # （事件留痕），此后同语境比对恢复正常。reject 域（点击门）不锚定。
                edge["corners"] = [[float(pt[0]), float(pt[1])] for pt in quad["corners"]]
                edge["frameSeq"] = seq
                run["pose_reference_anchored"] = True
                self._auto_event(run, "pose_reference_anchored", {
                    "category": "pose_reference_anchored",
                    "categoryDisplay": "姿态基准走位锚定",
                    "reason": "首次走位后笔杆入画改变检测语境；基准已刷新为走位后状态（非手机位移）",
                })
                pose_error = None
            if pose_error is not None:
                raise pose_error
            edge["frameSeq"] = seq
            return quad
        except (ScreenEdgeError, WizardError, ValueError, KeyError, TypeError) as exc:
            # H01（2026-10-05）：采样姿态超限仅提示+留痕，不中止 run；
            # 采样有效性仍由触点回报+Phase2 几何门保证（见 POSE-POLICY.md）。
            # 可测得时给出位移（基准四角 vs 当前四角的最大欧氏距离）与阈值。
            measured = None
            try:
                live = ((quad or {}).get("corners"))
                saved_corners = edge.get("corners")
                if live and saved_corners and len(live) == len(saved_corners):
                    measured = round(max(
                        ((float(a[0]) - float(b[0])) ** 2 +
                         (float(a[1]) - float(b[1])) ** 2) ** 0.5
                        for a, b in zip(saved_corners, live)), 2)
            except Exception:
                measured = None  # 无法测得：明确空值，不编造
            _pose_gate_skip(str(exc),
                            stage="post_travel" if after_travel else "sampling",
                            measured_px=measured, threshold_px=4.0)
            if on_out_of_range == "reject":
                # H01 边界（POSE-POLICY.md）：此处是普通动作/验收接触门，
                # 不是采样姿态复核——超限/无法核实仍拒绝（留痕照记）。
                # 姿态定位不可得（PoseUnlocatable 家族）保持原类型传播：
                # z_confirm_safe 按用户拍板 2026-09-29 只对这一族缺席留痕。
                #
                # 锚点点击唤醒豁免（业主 2026-10-16 设计）：tapAnchor 与当前
                # daemon 会话一致 = 点击位置是操作员已核实的安全位置，不依赖
                # 相机可见性——暗屏/遮挡时定位必然失败，此时点击本身就是唤醒
                # 手段，放行并留痕。
                anchor = ((self.host.state.get("z") or {}).get("tapAnchor") or {})
                if (anchor.get("daemonSession")
                        and anchor.get("daemonSession") == self.host.state.get("daemonSession")):
                    _pose_gate_skip(
                        "锚点点击唤醒：定位不可得但锚点与当前会话一致，放行点击",
                        stage="post_travel" if after_travel else "sampling",
                        measured_px=measured, threshold_px=4.0)
                    # 暗屏单击点亮 / 锁屏双击唤醒（业主 2026-10-16 设计，与
                    # WX 双击唤醒一致）：连发两击（间隔 ~250ms）——暗屏时第一击
                    # 点亮、第二击产生触点；锁屏时两击构成双击唤醒，唤醒后的
                    # 正式点击（调用方随后的 z/tap 流程）产生触点回报。
                    try:
                        self.host.daemon.ensure_debug_lease()
                        auto_safe = (self.host.state.get("z") or {}).get("autoSafe") or {}
                        w_depth = float(auto_safe.get("tapDepth") or 8.0)
                        w_feed = float(auto_safe.get("tapFeed") or 2000)
                        self.host._daemon_tap(w_depth, w_feed, 0)
                        time.sleep(0.25)
                        self.host._daemon_tap(w_depth, w_feed, 0)
                    except Exception:
                        pass  # 唤醒击失败不阻塞：后续正式点击按原流程执行
                    return None
                if isinstance(exc, PoseUnlocatable):
                    raise exc
                raise AutoRunError(
                    "camera_measurement_error",
                    f"手机位置变化或不可核实：拒绝点击（{exc}）",
                    classification="camera_measurement_error", park=False) from exc
            return None

    def _auto_safe_park(self, run: dict, *, best_effort: bool = False) -> None:
        """正常流程停车失败即停止；终局清理仅记录失败，不掩盖原始原因。"""
        run["lastPark"] = None
        self.host._position_context.parking = best_effort
        try:
            park = run.get("safePark") or {}
            z_safe = float((run.get("z") or {}).get("zSafe"))
            # 停车位走位前按其自身确认会话核对（P09）：坐标基准变化后
            # 不得按旧停车位走 XY，只允许 Z 向收回（离开屏幕方向）。
            park_xy_usable = bool(park.get("confirmed") and park.get("parkX") is not None)
            if park_xy_usable and park.get("daemonSession"):
                try:
                    status = self.host.daemon.status()
                    current = status.get("session") if status.get("connected") else None
                except Exception:
                    current = None
                if current and park["daemonSession"] != current:
                    park_xy_usable = False
                    self._auto_event(run, "park_skipped_session_changed", {
                        "parkSession": park["daemonSession"],
                        "currentSession": current,
                        "fallback": "z_only_retract",
                    })
            # 不先等待运动租约：位置未知时禁止停车走位，也不能复用上次停车记录。
            mpos = self.host._read_position()
            if park_xy_usable:
                record: dict = {}
                self.host.daemon.ensure_debug_lease()
                self.host._goto_robot(float(park["parkX"]), float(park["parkY"]),
                                 z_safe=z_safe, record=record)
                run["lastPark"] = record
            else:
                dz = z_safe - float(mpos["z"])
                if abs(dz) >= 0.05:
                    for chunk in split_segments(dz, 9.5):
                        self.host.daemon.cmd(
                            f"Z {chunk:.3f} F{min(1500.0, DEFAULT_TAP_FEED):g}")
                run["lastPark"] = {"zOnly": True, "zSafe": z_safe}
            self._auto_event(run, "safe_park", {"park": park_xy_usable})
        except Exception as exc:
            self._auto_event(run, "safe_park_failed", {
                "code": getattr(exc, "code", "safe_park_failed"),
                "error": str(exc), "positionConfirmed": False})
            if not best_effort:
                if isinstance(exc, (_AutoCancelled, AutoRunError)):
                    raise
                raise AutoRunError("safe_park_failed", str(exc),
                                   classification="hardware_investigation", park=False) from exc
        finally:
            self.host._position_context.parking = False

    def _auto_phase_safe_park(self, run: dict) -> None:
        self._auto_phase_set(run, "SAFE_PARK")
        self._auto_guard(run)
        self._auto_safe_park(run)

    # ----- 检测 -----

    def _auto_candidates(self, jpg: bytes) -> list[dict]:
        if self.host._target_candidates is not None:
            payload = self.host._target_candidates(jpg)
            items = payload.get("items") if isinstance(payload, dict) else payload
            normalized = []
            for item in items or []:
                if item and item.get("x") is not None and item.get("y") is not None:
                    normalized.append({"x": float(item["x"]), "y": float(item["y"]),
                                       "area": (float(item["area"])
                                                if item.get("area") is not None else None)})
            return normalized
        if self.host._target_detector is not None:
            detected = self.host._target_detector(jpg)
            if detected is not None and len(detected) >= 2:
                return [{"x": float(detected[0]), "y": float(detected[1]), "area": None}]
            return []
        raise WizardError("摄像头或绿色标靶检测器不可用")

    def _auto_pick_candidate(self, candidates: list[dict], expected,
                             ambiguous_px: float) -> tuple[dict | None, str]:
        if not candidates:
            return None, "none"

        def dist(item):
            if expected is None:
                return 0.0
            return ((item["x"] - expected[0]) ** 2 + (item["y"] - expected[1]) ** 2) ** 0.5

        ordered = sorted(candidates, key=dist)
        if len(ordered) >= 2:
            gap = ((ordered[0]["x"] - ordered[1]["x"]) ** 2
                   + (ordered[0]["y"] - ordered[1]["y"]) ** 2) ** 0.5
            if gap < ambiguous_px:
                return None, "ambiguous"
        return ordered[0], "ok"

    def _auto_target_in_screen_roi(self, center) -> str | None:
        """§8.2 检测门禁：候选中心必须位于画面检出的屏幕四边形内。

        返回 None=通过；否则返回中文拒绝原因。无边缘检测器（注入环境）时跳过。
        """
        if self.host._edge_detector is None or self.host._camera_fetch is None:
            return None
        try:
            quad = self.host._edge_quad()
        except Exception:
            return "摄像头画面未检出屏幕（手机可能熄屏或移出画面）"
        corners = quad.get("corners") or []
        if len(corners) != 4 or not _point_in_convex_quad(center[0], center[1], corners):
            return "检测到的绿色目标在屏幕四边形之外"
        return None

    def _auto_detect_target_stable(self, run: dict, expected=None,
                                   stale_center=None, ref_area=None) -> dict:
        """多帧稳定检测（§8.2）：连续 N 帧中心抖动/面积变化达标才放行。

        stale_center：上一个标靶的相机位置。推流与实时帧率不同（实测 ~8fps、
        布防→画面更新 ~0.2-0.9s），布防后最初几帧可能还显示旧标靶——这些帧
        与 stale_center 距离 <30px 时视为"画面未更新"，不计入确认序列，
        继续等画面真正变化（在检测预算内），避免把上一点的位置配给当前点。
        """
        cfg = run["config"]
        confirm = int(cfg["detect_confirm_frames"])
        jitter = float(cfg["center_jitter_px"])
        area_ratio = float(cfg["area_delta_ratio"])
        stale_px = float(cfg["target_min_step_px"])
        deadline = time.time() + max(self.host._target_draw_wait_s,
                                     float(self.host._target_detect_timeout_s))
        last_reason = "none"
        while time.time() < deadline:
            self._auto_check_cancel()
            streak: list[dict] = []
            for _ in range(confirm):
                jpg, frame_seq = self.host._camera_fetch()
                picked, reason = self._auto_pick_candidate(
                    self._auto_candidates(jpg),
                    expected if expected is not None
                    else (streak[-1]["center"] if streak else None),
                    ambiguous_px=float(cfg["target_ambiguous_px"]))
                if picked is None:
                    last_reason = reason
                    streak = []
                    break
                center = (picked["x"], picked["y"])
                if stale_center is not None and (
                        (center[0] - stale_center[0]) ** 2
                        + (center[1] - stale_center[1]) ** 2) ** 0.5 < stale_px:
                    last_reason = "stale_frame(previous target still shown)"
                    streak = []
                    break
                # 期望位置门：H 锁定后检测必须落在 H 逆投影的期望位置附近——
                # 现场 2026-09-03：探头悬停上一点时其在玻璃上的阴影/反光会把绿框
                # 轮廓切掉一块，质心稳定偏移 30-40px 且 jitter=0，"稳定≠正确"。
                if expected is not None:
                    dev = ((center[0] - expected[0]) ** 2
                           + (center[1] - expected[1]) ** 2) ** 0.5
                    if dev > float(cfg["expected_max_dev_px"]):
                        last_reason = (f"candidate 偏离期望位置 {dev:.0f}px"
                                       f"（上限 {cfg['expected_max_dev_px']}px）")
                        streak = []
                        break
                # 面积基准门：与 Phase 0 该点基准面积差超 ~40%（被切半/与绿物合并）
                if ref_area is not None and picked["area"] is not None:
                    ratio_lo = float(cfg["target_area_min_ratio"])
                    ratio = picked["area"] / float(ref_area)
                    if ratio > 1.6:
                        raise AutoRunError(
                            "camera_target_unstable",
                            f"标靶检测面积 {picked['area']:.0f} 是参考 {ref_area:.0f} 的 "
                            f"x{ratio:.2f} 倍：疑似进度标记/残影并入绿色连通域，"
                            "停靠后重检（2026-09-04 row-3 质心偏移事故）")
                    if not (ratio_lo <= ratio <= 1.0 / ratio_lo):
                        last_reason = (f"检测面积 {picked['area']:.0f} 与基准 "
                                       f"{ref_area:.0f} 偏差 x{ratio:.2f}")
                        streak = []
                        break
                streak.append({"center": [picked["x"], picked["y"]],
                               "area": picked["area"], "frameSeq": frame_seq})
            if len(streak) == confirm:
                centers = [f["center"] for f in streak]
                max_gap = max(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5
                              for a in centers for b in centers)
                areas = [f["area"] for f in streak if f["area"] is not None]
                area_delta = (max(areas) - min(areas)) / max(areas) \
                    if len(areas) == confirm and max(areas) > 0 else 0.0
                if max_gap <= jitter and area_delta <= area_ratio:
                    self._auto_require_stable_phone(run)
                    return {"center": [(centers[0][0] + centers[-1][0]) / 2,
                                       (centers[0][1] + centers[-1][1]) / 2],
                            "area": streak[0]["area"], "frameSeq": streak[-1]["frameSeq"],
                            "quality": {"frames": confirm, "jitterPx": round(max_gap, 2),
                                        "areaDelta": round(area_delta, 4)}}
                last_reason = f"unstable(jitter={max_gap:.1f}px,areaΔ={area_delta:.2f})"
            time.sleep(float(cfg["detect_interval_s"]))
        if str(last_reason).startswith("stale_frame"):
            raise AutoRunError("phase0_target_not_moving",
                               "画面始终停留在上一个标靶位置（未随布防变化）：手机校准页"
                               "未刷新/熄屏，或画面里有静态绿色物体被误检。请点亮手机并"
                               "确认停留在当前校准会话页面后重试",
                               classification="camera_measurement_error", park=True)
        if last_reason == "ambiguous":
            raise AutoRunError("camera_target_ambiguous",
                               "绿色标靶多候选无法唯一确定：禁止点击",
                               classification="ambiguous", park=True)
        raise AutoRunError("camera_target_unstable",
                           f"绿色标靶检测不稳定/未检出（{last_reason}）",
                           classification="camera_measurement_error", park=True)

    # ----- 布防/触点（复用既有唯一 sampleId/sequence 协议） -----

    def _auto_arm_phone(self, run: dict, u: float, v: float, prefix: str) -> dict:
        with self.host._lock:
            self.host._require_ime_open()
            sid = self.host.state["imeSessionId"]
            seq = int(self.host.state.get("imeSequence", 0)) + 1
            self.host.state["imeSequence"] = seq
            sample_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
            self.host.ime.arm(sid, sample_id, seq, self.host._target_rect(u, v))
            return {"sampleId": sample_id, "sequence": seq, "armedAt": time.time(),
                    "sessionId": sid}

    def _auto_disarm(self, armed: dict) -> None:
        try:
            self.host.ime.disarm(armed["sessionId"], armed["sampleId"], armed["sequence"])
        except ImeError:
            pass

    # ----- 预测与安全检查 -----

    def _auto_arm_model(self, run: dict) -> dict | None:
        return ((run.get("arm") or {}).get("robotToScreen")
                or (run.get("armSeed") or {}).get("robotToScreen"))

    def _auto_apply_tap_anchor(self, run: dict) -> None:
        """用 z 点击测试锚点预校正种子平移（用户拍板 2026-09-05）。

        真机事故：守护重连/G92 锚点归零后，旧档案种子的平移分量整体失效，
        First Click 预测点落到屏幕外——下压扎空、触点回报超时中止，且存在
        磕碰边框风险。z 点击测试必然先于自动校准发生、且收到触点回报
        （=证实落在玻璃上），用它把种子平移拉回现场：典型失效（锚点归零）
        是纯平移，单点锚定即可完全修复；重夹手机的旋转残余由 First Click
        自校正与 Phase 1 全量重拟合兜底。锚点守护会话与当前不一致（跨
        重启/重连）则不应用——那种坐标基已换，锚点不可信。"""
        anchor = (self.host.state.get("z") or {}).get("tapAnchor") or {}
        seed = (run.get("armSeed") or {}).get("robotToScreen")
        if not seed or anchor.get("robotX") is None or anchor.get("screenX") is None:
            return
        if anchor.get("daemonSession") != run.get("daemonSession"):
            self._auto_event(run, "seed_anchor_skipped",
                             {"reason": "daemon_session_mismatch"})
            return
        sx, sy = cb.screen_from_robot(seed, float(anchor["robotX"]),
                                      float(anchor["robotY"]))
        offset = [float(anchor["screenX"]) - sx, float(anchor["screenY"]) - sy]
        if abs(offset[0]) < 0.5 and abs(offset[1]) < 0.5:
            return  # 种子平移本就与现场一致，无需锚定
        patched = dict(seed)
        patched["c"] = round(float(seed.get("c", 0.0)) + offset[0], 6)
        patched["f"] = round(float(seed.get("f", 0.0)) + offset[1], 6)
        run["armSeed"]["robotToScreen"] = patched
        run["seedAnchor"] = {
            "source": anchor.get("source") or "z_tap_test",
            "offsetPx": [round(o, 2) for o in offset],
            "anchor": {"robot": [anchor["robotX"], anchor["robotY"]],
                       "screen": [anchor["screenX"], anchor["screenY"]]},
            "appliedAt": _now(),
            "note": "点击测试锚点已校正种子平移（First Click 前置保险）",
        }
        self._auto_event(run, "seed_anchor_applied",
                         {"offsetPx": [round(o, 2) for o in offset],
                          "source": anchor.get("source")})

    def _auto_screen_size(self, run: dict) -> tuple[float, float]:
        screen = run.get("screen") or {}
        return float(screen.get("screenWidth") or 0), float(screen.get("screenHeight") or 0)

    def _auto_screen_safe(self, run: dict, sx: float, sy: float) -> bool:
        w, h = self._auto_screen_size(run)
        margin = float(run["config"]["safe_margin_px"])
        return (w > 0 and h > 0 and math.isfinite(sx) and math.isfinite(sy)
                and -margin <= sx <= w + margin and -margin <= sy <= h + margin)

    def _auto_workspace_ok(self, run: dict, rx: float, ry: float) -> bool:
        bounds = run["config"].get("arm_workspace_mm")
        if not bounds or len(bounds) != 4:
            return True  # 未配置工作区（可选）：由 300mm 位移/屏幕安全区等计算门禁兜底
        xmin, xmax, ymin, ymax = (float(v) for v in bounds)
        return xmin <= rx <= xmax and ymin <= ry <= ymax

    def _project_screen_to_camera(self, screen_xy: tuple, homography) -> tuple:
        """screen 像素 → camera 像素（H 逆变换）。homography 兼容 3x3/9 元素。"""
        import numpy as np

        matrix = np.asarray(homography, dtype=float).reshape(3, 3)
        inverse = np.linalg.inv(matrix)
        x, y = float(screen_xy[0]), float(screen_xy[1])
        vec = inverse @ np.array([x, y, 1.0])
        if abs(float(vec[2])) < 1e-9 or not np.isfinite(vec).all():
            raise ValueError("H 逆投影无效")
        return (float(vec[0] / vec[2]), float(vec[1] / vec[2]))

    def _auto_expected_camera(self, run: dict, sx: float, sy: float):
        """H 逆投影：期望的标靶摄像头像素（多候选选择与期望位置用）。"""
        if not run.get("h"):
            return None
        import numpy as np

        matrix = np.array(run["h"]["homography"], np.float64)
        try:
            inverse = np.linalg.inv(matrix)
        except np.linalg.LinAlgError:
            return None
        vec = inverse @ np.array([sx, sy, 1.0])
        if abs(vec[2]) < 1e-12:
            return None
        return float(vec[0] / vec[2]), float(vec[1] / vec[2])

    def _auto_predict_arm(self, run: dict, sx: float, sy: float) -> tuple[float, float]:
        model = self._auto_arm_model(run)
        if not model:
            raise AutoRunError("bootstrap_required",
                               "没有可用的 Arm seed：请先通过人工 C 流程建立初始映射",
                               state=AUTO_STATE_BOOTSTRAP_REQUIRED, park=False)
        if not self._auto_screen_safe(run, sx, sy):
            raise AutoRunError("predicted_screen_unsafe",
                               f"预测屏幕坐标 ({sx:.0f},{sy:.0f}) 落在屏幕安全区外",
                               classification="ambiguous",
                               routed_to="CAMERA_RECALIBRATION", park=True)
        try:
            rx, ry = cb.robot_from_screen(model, float(sx), float(sy))
        except ValueError as exc:
            raise AutoRunError("arm_model_not_invertible", str(exc),
                               classification="arm_mapping_error",
                               routed_to="ARM_RECALIBRATION", park=True) from exc
        if not (math.isfinite(rx) and math.isfinite(ry)):
            raise AutoRunError("predicted_arm_invalid", "预测机械坐标非有限值",
                               classification="arm_mapping_error",
                               routed_to="ARM_RECALIBRATION", park=True)
        if not self._auto_workspace_ok(run, rx, ry):
            raise AutoRunError("predicted_arm_out_of_workspace",
                               f"预测机械坐标 ({rx:.1f},{ry:.1f}) 超出现场允许范围",
                               classification="arm_mapping_error", park=True)
        mpos = self.host._read_position()
        displacement = ((rx - float(mpos["x"])) ** 2 + (ry - float(mpos["y"])) ** 2) ** 0.5
        if displacement > 300.0:
            raise AutoRunError("arm_target_displacement_exceeded",
                               f"预测目标位移 {displacement:.0f}mm 超 300mm 上限：拒绝运动",
                               classification="arm_mapping_error", park=True)
        return rx, ry

    # ----- 分类与判定（§12/补充 §5/§6） -----

    def _auto_error_level(self, run: dict, error_px: float | None) -> str | None:
        if error_px is None:
            return None
        cfg = run["config"]
        if error_px <= float(cfg["target_tolerance_px"]):
            return "PASS"
        if error_px <= float(cfg["soft_fail_px"]):
            return "SOFT_FAIL"
        if error_px <= float(cfg["hard_fail_px"]):
            return "HARD_FAIL"
        return "HARD_ABORT"

    @staticmethod
    def _auto_classify_error(run: dict, sample: dict) -> str:
        flags = sample.get("eligibleFlags") or {}
        projection = sample.get("cameraProjectionErrorPx")
        motion = sample.get("armMotionErrorMm")
        tolerance = float(run["config"]["target_tolerance_px"])
        if not flags.get("camera_detection_stable", False):
            return "camera_measurement_error"
        if projection is not None and float(projection) > tolerance \
                and run["locks"]["cameraToScreen"].get("state") == cb.MODEL_LOCK_LOCKED:
            return "camera_transform_error"
        if motion is not None and float(motion) > float(run["config"]["position_tolerance_mm"]):
            return "arm_motion_error"
        if not flags.get("actual_touch_received") or not flags.get("exactly_one_matching_touch") \
                or not flags.get("touch_coordinate_valid"):
            return "touch_feedback_error"
        touch_error = sample.get("touchTargetErrorPx")
        if touch_error is not None and float(touch_error) > tolerance:
            # 相机投影合格、MPos 合格、触点可重复超差 → 机械映射误差（不更新 H）
            return "arm_mapping_error"
        return "ok"

    def _auto_route_for(self, classification: str) -> str:
        return {
            "camera_transform_error": "CAMERA_RECALIBRATION",
            "camera_measurement_error": "CAMERA_RECALIBRATION",
            "arm_mapping_error": "ARM_RECALIBRATION",
            "arm_motion_error": "HARDWARE_INVESTIGATION",
            "touch_feedback_error": "HARDWARE_INVESTIGATION",
        }.get(classification, "ABORT")

    # ----- 采样（§12.1 记录对象 / 补充 §4 资格） -----

    def _auto_click_point(self, run: dict, target_id: str, u: float, v: float,
                          phase: str, *, use_camera: bool) -> dict:
        """单点闭环：布防 →（可选相机链路）预测 → zSafe 走位 → MPos 校验 → TAP → 触点。"""
        self._auto_guard(run)
        w, h = self._auto_screen_size(run)
        target_screen = [u * w, v * h]
        armed = self._auto_arm_phone(run, u, v, phase.split("_")[0].lower())
        sample = {
            "sampleId": armed["sampleId"], "runId": run["runId"], "phase": phase,
            "targetId": target_id, "targetScreen": [round(c, 1) for c in target_screen],
            "cameraPixel": None, "cameraQuality": None,
            "cameraPredictedScreen": None, "predictedArm": None,
            "commandedArm": None, "postMoveMPos": None, "effectiveRobotXY": None,
            "actualTouch": None, "preMoveMPos": None,
            "cameraProjectionErrorPx": None, "touchTargetErrorPx": None,
            "cameraTouchGapPx": None, "armMotionErrorMm": None,
            "motion": None, "errorLevel": None, "classification": None,
            "eligible": False, "eligibleFlags": {}, "rejectedReason": None,
            "modelVersion": run["modelVersion"],
            "profileFingerprint": run["runtime"].get("profileFingerprint"),
            "cameraId": run["binding"].get("cameraId"),
            "cameraFrameSize": [run["binding"].get("frameWidth"),
                                run["binding"].get("frameHeight")],
            "phoneSessionId": run.get("phoneSessionId"),
            "ts": _now(),
        }
        flags = dict.fromkeys(
            ("camera_detection_stable", "camera_to_screen_valid", "predicted_arm_valid",
             "arm_within_workspace", "post_move_mpos_within_tolerance",
             "actual_touch_received", "exactly_one_matching_touch",
             "touch_coordinate_valid", "session_unchanged", "camera_frame_size_unchanged",
             "model_version_matches", "no_emergency_stop"), True)
        try:
            expected = None
            if use_camera:
                if run.get("h"):
                    expected = self._auto_expected_camera(run, *target_screen)
                else:
                    # Phase0 引导期：用已提升档案的 cameraToScreen 把标靶
                    # 屏幕位置投影到相机像素（手机未移动时精确 ±10px），
                    # 页面绿色状态文字/背景杂物按「离期望最近」淘汰。
                    bundle_h = (((self.host._bundle() or {}).get("camera") or {})
                                .get("cameraToScreen") or {}).get("homography")
                    if bundle_h:
                        expected = self._project_screen_to_camera(
                            target_screen, bundle_h)
            # 仅当布防目标换成了新位置才用上一位置识别滞后帧；重采同一点时
            # 检测位置本就应与上次相同，不能误判为"画面未更新"
            target_key = [u, v]
            stale_center = run.get("lastCameraCenter") \
                if run.get("lastCameraTarget") != target_key else None
            corr_areas = {c["targetId"]: c.get("area")
                          for c in (run.get("h") or {}).get("correspondences") or []}
            ref_area = corr_areas.get(target_id)
            detect_attempts = 0
            while True:
                try:
                    detect = self._auto_detect_target_stable(
                        run, expected, stale_center=stale_center, ref_area=ref_area)
                    break
                except AutoRunError as exc:
                    # 检测被质量门拒绝（典型=探头悬停上一点，其阴影/反光污染检测）：
                    # 原地重检只会得到同样的坏结果——先把机械臂挪回画面外停靠位
                    # 再检一次，物理消除干扰源后继续。
                    if exc.code == "camera_target_unstable" and detect_attempts == 0 \
                            and (run.get("safePark") or {}).get("confirmed"):
                        detect_attempts += 1
                        self._auto_event(run, "detect_retry_from_park",
                                         {"targetId": target_id, "reason": exc.message})
                        self._auto_safe_park(run)
                        continue
                    raise
            run["lastCameraTarget"] = target_key
            run["lastCameraCenter"] = detect["center"]
            sample["cameraPixel"] = [round(c, 1) for c in detect["center"]]
            sample["cameraQuality"] = detect["quality"]
            if use_camera:
                from adapters.screen_ime.camera_calibration import CameraScreenTransform

                transform = CameraScreenTransform.from_dict(
                    {"homography": run["h"]["homography"]})
                px, py = transform.screen_from_camera(*detect["center"])
                flags["camera_to_screen_valid"] = self._auto_screen_safe(run, px, py)
            else:
                px, py = target_screen
            sample["cameraPredictedScreen"] = [round(px, 1), round(py, 1)]
            sample["cameraProjectionErrorPx"] = round(
                ((px - target_screen[0]) ** 2 + (py - target_screen[1]) ** 2) ** 0.5, 2)
            # 瞄准一律用名义屏幕点（模型反解）：摄像头检测只做在场/稳定见证与
            # 门禁（期望位置±20px、面积一致、jitter）。检测质心的偏移记录在
            # cameraProjectionErrorPx 里，不再有资格挪动点击位置。
            rx, ry = self._auto_predict_arm(run, *target_screen)
            sample["predictedArm"] = [round(rx, 3), round(ry, 3)]
            flags["predicted_arm_valid"] = True
            flags["arm_within_workspace"] = self._auto_workspace_ok(run, rx, ry)

            motion: dict = {}
            self.host.daemon.ensure_debug_lease()
            try:
                self.host._goto_robot(rx, ry, z_safe=float(run["z"]["zSafe"]), record=motion)
            except WizardError as exc:
                # 走位被拒（位移超限/行程超限/残差超限）：不 TAP、不学习（§19.2）
                sample["eligibleFlags"] = flags
                sample["classification"] = "arm_motion_error"
                sample["rejectedReason"] = f"走位失败：{exc}"
                self._auto_record_sample(run, sample)
                raise AutoRunError("arm_motion_failed", str(exc),
                                   classification="arm_motion_error", park=True) from exc
            sample["motion"] = motion
            sample["preMoveMPos"] = motion.get("preMoveMPos")
            sample["commandedArm"] = motion.get("commandedXY")
            sample["postMoveMPos"] = motion.get("postMoveMPos")
            residual = float(motion.get("postMoveResidualMm") or 0.0)
            sample["armMotionErrorMm"] = round(residual, 3)
            tolerance_mm = float(run["config"]["position_tolerance_mm"])
            flags["post_move_mpos_within_tolerance"] = residual <= tolerance_mm
            if not flags["post_move_mpos_within_tolerance"]:
                # MPos 超差：不 TAP、不学习（§19.2）
                sample["eligibleFlags"] = flags
                sample["classification"] = "arm_motion_error"
                sample["rejectedReason"] = (
                    f"postMoveMPos 残差 {residual:.2f}mm 超过 {tolerance_mm}mm：不点击")
                self._auto_record_sample(run, sample)
                raise AutoRunError("arm_motion_mpos_exceeded",
                                   f"走位后 MPos 残差 {residual:.2f}mm 超过 "
                                   f"{tolerance_mm}mm：不 TAP、不学习",
                                   classification="arm_motion_error", park=True)
            post = motion.get("postMoveMPos") or {}
            sample["effectiveRobotXY"] = [post.get("x"), post.get("y")]
            self._auto_require_stable_phone(run, after_travel=True)

            depth = float(run["z"]["tapDepth"])
            feed = float(run["z"].get("tapFeed") or DEFAULT_TAP_FEED)

            def move() -> None:
                self.host._daemon_tap(depth, feed, 0)

            report = self.host._tap_with_report(
                armed["sampleId"], move,
                pre_armed={"sequence": armed["sequence"], "armedAt": armed["armedAt"]})
            touch = [float(report["screenX"]), float(report["screenY"])]
            sample["actualTouch"] = [round(touch[0], 1), round(touch[1], 1)]
            flags["actual_touch_received"] = True
            flags["exactly_one_matching_touch"] = True  # 协议层已拒绝重复/错配
            flags["touch_coordinate_valid"] = (
                0 <= touch[0] <= w and 0 <= touch[1] <= h)
            sample["touchTargetErrorPx"] = round(
                ((touch[0] - target_screen[0]) ** 2
                 + (touch[1] - target_screen[1]) ** 2) ** 0.5, 2)
            sample["cameraTouchGapPx"] = round(
                ((touch[0] - px) ** 2 + (touch[1] - py) ** 2) ** 0.5, 2)
        except AutoRunError as exc:
            sample.setdefault("eligibleFlags", {}).update(flags)
            sample["eligibleFlags"] = flags
            if sample["classification"] is None:
                sample["classification"] = exc.classification
            if sample.get("rejectedReason") is None:
                sample["rejectedReason"] = exc.message
            self._auto_record_sample(run, sample)
            raise
        except (WizardError, TouchReportTimeout, ImeError):
            sample["eligibleFlags"] = flags
            sample["classification"] = "touch_feedback_error"
            sample["rejectedReason"] = "触点回报失败/超时"
            self._auto_record_sample(run, sample)
            raise
        finally:
            # 布防未被点击消耗（检测/预测/走位/回报失败）→ 拆除，否则下一次
            # 布防会被 8788 以 calibration_armed 拒绝，重采样链路被残留布防卡死
            if not sample.get("actualTouch"):
                self._auto_disarm(armed)
        # 触点成功：完成分类、资格与误差等级（补充 §4/§6）
        sample["eligibleFlags"] = flags
        sample["classification"] = self._auto_classify_error(run, sample)
        sample["errorLevel"] = self._auto_error_level(run, sample["touchTargetErrorPx"])
        # 资格=安全/一致性条件全集（补充 §4）；精度不进资格——学习阶段超差样本
        # 正是要采的数据，最终精度由 Phase 2/独立验证把关（用户拍板 2026-09-03）
        sample["eligible"] = all(flags.values())
        if not sample["eligible"] and not sample["rejectedReason"]:
            sample["rejectedReason"] = f"资格条件未满足（{sample['classification']}）"
        self._auto_record_sample(run, sample)
        return sample

    def _auto_record_sample(self, run: dict, sample: dict) -> None:
        with self._auto_lock:
            run.setdefault("samples", []).append(sample)
            # 反发散（§15.2）只统计基础设施失败/HARD 级异常：SOFT_FAIL 的重采样
            # 由 max_resample 与暂停-重试机制治理，重复计数会提前误杀正常流程。
            # 只统计资格失败（触点缺失/错配/坐标无效等基础设施异常）；
            # "安全合格但精度超差"是学习阶段的正常数据，不构成反发散
            if sample.get("eligible"):
                run["consecutiveAnomalies"] = 0
            else:
                run["consecutiveAnomalies"] = int(run.get("consecutiveAnomalies", 0)) + 1
            try:
                cb.save_candidate(run, self.host.data_root)
            except Exception:
                pass
        if int(run.get("consecutiveAnomalies", 0)) >= int(
                run["config"]["consecutive_anomaly_abort"]):
            raise AutoRunError("consecutive_anomalies",
                               f"连续 {run['consecutiveAnomalies']} 个异常样本：进入 ABORT",
                               classification="ambiguous")

    # ----- Phase 0：Camera -> Screen H（§9） -----

    def _auto_phase0_camera(self, run: dict) -> None:
        from adapters.screen_ime.camera_calibration import fit_camera_to_screen_strict

        self._auto_phase_set(run, "PHASE0_CAMERA")
        correspondences: list[dict] = []
        stale_camera = None  # 上一个已采点的相机位置（识别推流滞后帧）
        for point in run["pointPlan"]:
            attempts = 0
            last_error: Exception | None = None
            while attempts < int(run["config"]["max_resample"]):
                attempts += 1
                self._auto_guard(run)
                armed = self._auto_arm_phone(run, point["u"], point["v"], "p0")
                try:
                    time.sleep(self.host._target_draw_wait_s)
                    detect = self._auto_detect_target_stable(
                        run, stale_center=stale_camera)
                except AutoRunError as exc:
                    if exc.code in ("camera_target_unstable", "camera_target_ambiguous"):
                        self._auto_disarm(armed)
                        last_error = exc
                        continue  # 抖动/歧义可重采；确定性失败立即上抛
                    raise
                finally:
                    self._auto_disarm(armed)
                # ROI：检测中心必须落在摄像头画面检出的屏幕四边形内（§8.2；
                # 手机熄屏/画面外绿色物体在此直接拒绝，不会走到单应拟合）
                roi_reason = self._auto_target_in_screen_roi(detect["center"])
                if roi_reason:
                    self._auto_disarm(armed)
                    raise AutoRunError("phase0_target_outside_screen",
                                       f"标定靶检测失败：{roi_reason}（疑似手机熄屏或"
                                       f"画面里的非标靶绿色物体，已拒绝拟合）",
                                       classification="camera_measurement_error",
                                       routed_to="CAMERA_RECALIBRATION", park=False)
                # 位移守卫：新标定点的相机位置必须与已采点明显不同——手机校准页
                # 卡住/静态绿色物（贴纸等）会让所有点检测在同一位置。
                min_step = float(run["config"]["target_min_step_px"])
                for prev in correspondences:
                    gap = ((prev["cameraX"] - detect["center"][0]) ** 2
                           + (prev["cameraY"] - detect["center"][1]) ** 2) ** 0.5
                    if gap < min_step:
                        raise AutoRunError(
                            "phase0_target_not_moving",
                            f"标定点 {point['id']} 检测位置与已采点只差 {gap:.0f}px："
                            f"绿框没有跟着布防换位（手机校准页未刷新/熄屏，或画面里有"
                            f"静态绿色物体被误检）。请点亮手机并确认停留在当前校准页",
                            classification="camera_measurement_error",
                            routed_to="CAMERA_RECALIBRATION", park=False)
                # 遮挡守卫：被遮挡的标靶只剩部分绿区，检测面积骤降（现场 2026-09-03：
                # 停靠位挡住底行，正常 ~7200px² 只剩 ~400px²，质心漂移 60-70px 制造外点）
                area = detect.get("area")
                prev_areas = [c["area"] for c in correspondences
                              if c.get("area") is not None]
                if area is not None and prev_areas:
                    ref = sorted(prev_areas)[len(prev_areas) // 2]
                    ratio = float(run["config"]["target_area_min_ratio"])
                    if ref > 0 and (area < ratio * ref or ref < ratio * area):
                        raise AutoRunError(
                            "phase0_target_occluded",
                            f"标定点 {point['id']} 检测面积 {area:.0f}px² 与已完成点"
                            f"（中位 {ref:.0f}px²）差异过大：标靶被遮挡——机械臂停在屏幕前、"
                            f"或充电线等物体横在屏幕上都会触发。请确保 9 个标靶区域无遮挡"
                            f"（停靠位移出摄像头画面、屏幕表面无走线压住标靶带），必要时"
                            f"重新确认 SAFE PARK 后再启动自动校准",
                            classification="camera_measurement_error", park=False)
                w, h = self._auto_screen_size(run)
                stale_camera = detect["center"]
                # 同步登记到 run 级滞后链：首次中心点击/后续点击点据此识别旧画面
                run["lastCameraTarget"] = [point["u"], point["v"]]
                run["lastCameraCenter"] = detect["center"]
                correspondences.append({
                    "targetId": point["id"],
                    "cameraX": detect["center"][0], "cameraY": detect["center"][1],
                    "screenX": point["u"] * w, "screenY": point["v"] * h,
                    "area": detect["area"], "frameSeq": detect["frameSeq"],
                    "quality": detect["quality"],
                })
                break
            else:
                raise AutoRunError("phase0_target_failed",
                                   f"标定点 {point['id']} 检测重试用尽：{last_error}",
                                   classification="camera_measurement_error", park=False)
        quality = fit_camera_to_screen_strict(
            [{"cameraX": c["cameraX"], "cameraY": c["cameraY"],
              "screenX": c["screenX"], "screenY": c["screenY"]} for c in correspondences],
            rms_max_px=float(run["config"]["camera_rms_target_px"]),
            max_error_px=float(run["config"]["camera_max_error_px"]),
            grid_rows=len(AUTO_PLAN_GRID_V), grid_cols=len(AUTO_PLAN_GRID_U),
            screen_width=run["screen"].get("screenWidth"),
            screen_height=run["screen"].get("screenHeight"),
            boundary_margin_px=float(run["config"]["safe_margin_px"]))
        run["h"] = {"homography": quality["homography"],
                    "rms": quality["rms"], "maxError": quality["maxError"],
                    "source": AUTO_CAMERA_SOURCE, "correspondences": correspondences,
                    "quality": quality, "lockedAt": None, "version": 0}
        if not quality["ok"]:
            reason = quality["reason"] or {}
            raise AutoRunError(reason.get("code", "camera_quality_failed"),
                               f"Phase 0 H 质量未达标：{reason.get('message', '')}",
                               classification="camera_measurement_error",
                               routed_to="CAMERA_RECALIBRATION", park=False)
        self._auto_event(run, "phase0_fit", {"rms": quality["rms"],
                                             "maxError": quality["maxError"]})
        cb.save_candidate(run, self.host.data_root)

    def _auto_lock_camera_model(self, run: dict) -> None:
        run["locks"]["cameraToScreen"]["state"] = cb.MODEL_LOCK_VALIDATED
        run["locks"]["cameraToScreen"]["state"] = cb.MODEL_LOCK_LOCKED
        run["locks"]["cameraToScreen"]["version"] = \
            int(run["locks"]["cameraToScreen"].get("version", 0)) + 1
        run["locks"]["cameraToScreen"]["lockedAt"] = _now()
        run["locks"]["cameraToScreen"]["source"] = AUTO_CAMERA_SOURCE
        run["h"]["version"] = run["locks"]["cameraToScreen"]["version"]
        run["h"]["lockedAt"] = _now()
        run["modelVersion"] += 1
        self._auto_phase_set(run, "H_LOCKED")
        self._auto_event(run, "h_locked", {"version": run["h"]["version"],
                                           "rms": run["h"]["rms"]})
        cb.save_candidate(run, self.host.data_root)

    # ----- Bootstrap Check（§6） -----

    def _auto_bootstrap_check(self, run: dict) -> None:
        self._auto_phase_set(run, "BOOTSTRAP_CHECK")
        seed = run.get("armSeed") or {}
        model = seed.get("robotToScreen")
        if not model or not cb.affine_invertible(model):
            raise AutoRunError("bootstrap_required",
                               "活动档案没有可用的 xy.robotToScreen seed：必须先通过"
                               "人工 C 流程建立初始 Arm 映射（不得“先点击再学习”）",
                               state=AUTO_STATE_BOOTSTRAP_REQUIRED, park=False)
        mpos = self.host._read_position()
        results = []
        for c in run["h"]["correspondences"]:
            px, py = cb.apply_homography(run["h"]["homography"],
                                         c["cameraX"], c["cameraY"])
            if not self._auto_screen_safe(run, px, py):
                raise AutoRunError("bootstrap_required",
                                   f"标定点 {c['targetId']} 的 H 投影落在屏幕安全区外",
                                   state=AUTO_STATE_BOOTSTRAP_REQUIRED, park=False)
            rx, ry = cb.robot_from_screen(model, px, py)
            if not (math.isfinite(rx) and math.isfinite(ry)) \
                    or not self._auto_workspace_ok(run, rx, ry):
                raise AutoRunError("bootstrap_required",
                                   f"标定点 {c['targetId']} 的预测机械坐标越界/无效",
                                   state=AUTO_STATE_BOOTSTRAP_REQUIRED, park=False)
            displacement = ((rx - float(mpos["x"])) ** 2
                            + (ry - float(mpos["y"])) ** 2) ** 0.5
            if displacement > 300.0:
                raise AutoRunError("bootstrap_required",
                                   f"标定点 {c['targetId']} 距当前 MPos {displacement:.0f}mm "
                                   f"超 300mm：seed 不足以安全预测",
                                   state=AUTO_STATE_BOOTSTRAP_REQUIRED, park=False)
            results.append({"targetId": c["targetId"],
                            "screen": [round(px, 1), round(py, 1)],
                            "arm": [round(rx, 2), round(ry, 2)],
                            "displacementMm": round(displacement, 1)})
        # 旧/新模型对比（仅当旧 H 是已验证基线时才生效，§6.3.5）
        if seed.get("seedHUsable") and seed.get("seedH"):
            try:
                shift = 0.0
                for c in run["h"]["correspondences"]:
                    old_px, old_py = cb.apply_homography(seed["seedH"],
                                                         c["cameraX"], c["cameraY"])
                    new_rx, new_ry = cb.robot_from_screen(
                        model, *cb.apply_homography(run["h"]["homography"],
                                                    c["cameraX"], c["cameraY"]))
                    old_rx, old_ry = cb.robot_from_screen(model, old_px, old_py)
                    shift = max(shift, ((new_rx - old_rx) ** 2
                                        + (new_ry - old_ry) ** 2) ** 0.5)
                if shift > float(run["config"]["model_shift_max_mm"]):
                    raise AutoRunError(
                        "bootstrap_required",
                        f"新 H 与已验证旧 H 的 9 点预测差异 {shift:.1f}mm 超过 "
                        f"{run['config']['model_shift_max_mm']}mm：需人工确认",
                        state=AUTO_STATE_BOOTSTRAP_REQUIRED, park=False)
            except ValueError:
                pass
        run["bootstrap"] = {"checkedAt": _now(), "points": results, "passed": True}
        self._auto_event(run, "bootstrap_passed")

    # ----- First Click Gate（§7） -----

    def _auto_first_click_gate(self, run: dict) -> dict:
        # 口令门禁已按用户拍板移除：操作员点「启动自动校准」即确认首次中心点击。
        # 语义（用户拍板 2026-09-03）：链路验证 + 偏移自校正——安全条件不满足才中止；
        # 超差时把触点-预测的平移偏移修正进种子映射后继续走完全部标定点。
        self._auto_phase_set(run, "FIRST_CLICK_GATE")
        name, u, v = AUTO_FIRST_CLICK_POINT
        attempts = 0
        while True:
            try:
                sample = self._auto_click_point(run, name, u, v,
                                                "FIRST_CLICK_GATE", use_camera=True)
            except AutoRunError as exc:
                if exc.code in self._AUTO_RESAMPLE_CODES:
                    attempts += 1
                    if attempts < int(run["config"]["max_resample"]):
                        continue  # 检测抖动：重试中心点（新布防/新 sampleId）
                raise
            break
        if not sample.get("eligible"):
            raise AutoRunError(
                "first_click_unsafe",
                f"中心点链路验证失败（{sample.get('rejectedReason')} / 分类 "
                f"{sample.get('classification')}）：触点未落在屏幕内或匹配失败，"
                f"停止自动校准",
                classification=sample.get("classification") or "ambiguous",
                state=AUTO_STATE_ABORTED, park=True)
        error = float(sample.get("touchTargetErrorPx") or 0.0)
        tolerance = float(run["config"]["target_tolerance_px"])
        if error > tolerance:
            # 用户拍板 2026-09-03：中心点超差不中止——把「触点−预测」的平移偏移
            # 修正到种子映射（典型成因：重连后 G92 锚点归零的种子平移偏差），
            # Phase 1 用校正后的模型走完全部 9 点并全量重拟合。
            offset = [
                sample["actualTouch"][0] - sample["cameraPredictedScreen"][0],
                sample["actualTouch"][1] - sample["cameraPredictedScreen"][1],
            ]
            seed = run["armSeed"].get("robotToScreen") or {}
            # 用户拍板 2026-09-29：误差 >40px 说明种子形状（比例）已随坐标
            # 基准失效——平移修正只救中心、远点仍会出屏（真机实测
            # calibration-3 出屏导致连续触点超时）。用两个已确认点击对
            # （z_confirm 锚 + 本次门触点，均为操作员确认过的真实触点）
            # 解每轴「平移+比例」两点标定，重建种子线性映射。
            two_point_applied = False
            anchor = (self.host.state.get("z") or {}).get("tapAnchor") or {}
            gate_machine = sample.get("postMoveMPos") or {}
            if (error > 40.0
                    and anchor.get("robotX") is not None
                    and anchor.get("screenX") is not None
                    and anchor.get("daemonSession") == run.get("daemonSession")
                    and isinstance(gate_machine.get("x"), (int, float))
                    and isinstance(gate_machine.get("y"), (int, float)) and sample.get("actualTouch")):
                mx1, my1 = float(anchor["robotX"]), float(anchor["robotY"])
                sx1, sy1 = float(anchor["screenX"]), float(anchor["screenY"])
                mx2 = float(gate_machine["x"])
                my2 = float(gate_machine.get("y"))
                sx2 = float(sample["actualTouch"][0])
                sy2 = float(sample["actualTouch"][1])
                if abs(mx2 - mx1) > 5.0 and abs(my2 - my1) > 5.0:
                    a = (sx2 - sx1) / (mx2 - mx1)
                    c = sx1 - a * mx1
                    e = (sy2 - sy1) / (my2 - my1)
                    f = sy1 - e * my1
                    run["armSeed"]["robotToScreen"] = {
                        "a": round(a, 6), "b": 0.0, "c": round(c, 6),
                        "d": 0.0, "e": round(e, 6), "f": round(f, 6)}
                    two_point_applied = True
                    self._auto_event(run, "seed_rebuilt_two_point", {
                        "pairs": [{"robot": [mx1, my1], "screen": [sx1, sy1]},
                                  {"robot": [mx2, my2], "screen": [sx2, sy2]}],
                        "robotToScreen": run["armSeed"]["robotToScreen"]})
            seed = run["armSeed"].get("robotToScreen") or {}
            if not two_point_applied:
                seed["c"] = float(seed.get("c", 0.0)) + offset[0]
                seed["f"] = float(seed.get("f", 0.0)) + offset[1]
            run["firstClickCorrection"] = {
                "errorPx": round(error, 2),
                "offsetPx": [round(o, 2) for o in offset],
                "twoPointReseed": two_point_applied,
                "appliedAt": _now(),
                "note": ("两点标定已重建种子映射（平移+比例）"
                         if two_point_applied else
                         "平移偏移已应用到种子映射，Phase 1 将全量重拟合")}
            self._auto_event(run, "first_click_offset_correction",
                             {"errorPx": round(error, 2), "offsetPx": offset,
                              "twoPointReseed": two_point_applied})
        else:
            self._auto_event(run, "first_click_passed", {"touchTargetErrorPx": error})
        return sample

    # ----- Phase 1：Arm Affine（§10，只用 Effective Robot XY ↔ Actual Touch） -----

    _AUTO_RESAMPLE_CODES = {"camera_target_unstable", "camera_target_ambiguous"}

    def _auto_phase1_arm(self, run: dict) -> None:
        self._auto_phase_set(run, "PHASE1_ARM")
        for point in run["pointPlan"]:
            self._auto_phase1_point(run, point)

    def _auto_phase1_point(self, run: dict, point: dict) -> dict:
        """采集单个 Phase 1 样本：按已知标定屏幕坐标预测（不经相机），走位→TAP→触点。

        相机只在布防标靶显示后做一次稳定检测留证（资格判定用），不进入拟合
        （补充文档 §3：不得让 Camera Homography 的误差进入 Arm Affine）。
        """
        attempts = 0
        timeouts = 0
        while True:
            try:
                sample = self._auto_click_point(
                    run, point["id"], point["u"], point["v"],
                    "PHASE1_ARM", use_camera=False)
            except AutoRunError as exc:
                if exc.code in self._AUTO_RESAMPLE_CODES:
                    attempts += 1
                    if attempts < int(run["config"]["max_resample"]):
                        continue
                raise
            except TouchReportTimeout:
                timeouts += 1
                if timeouts <= int(run["config"]["touch_retry"]):
                    continue
                raise AutoRunError("touch_report_timeout",
                                   f"标定点 {point['id']} 触点超时重试用尽",
                                   classification="touch_feedback_error", park=True)
            if sample.get("eligible"):
                # 学习阶段：安全合格的样本一律采集（超差正是要学的数据）；
                # 拟合质量在 _auto_update_arm_candidate 统一把关
                return sample
            attempts += 1
            if attempts >= int(run["config"]["max_resample"]):
                raise AutoRunError(
                    "phase1_sample_failed",
                    f"Phase 1 标定点 {point['id']} 采样重试用尽"
                    f"（{sample.get('rejectedReason')}）",
                    classification=sample.get("classification") or "ambiguous",
                    park=True)

    def _auto_update_arm_candidate(self, run: dict) -> None:
        """全量重拟合（等权）：R_eff ↔ Actual Touch；相机数据绝不进入拟合（§10/§13.3）。

        2026-09-04 方案 A：样本 ≥12 时升级二次模型（倾斜触点滑移补偿），
        仅当残差不劣于仿射才采用（嵌套模型，正常必不劣；劣化=数值病态→仿射）。
        """
        from adapters.screen_ime.geometry import (
            CalibrationMathError,
            fit_affine,
            fit_quadratic,
        )

        samples = [s for s in run["samples"]
                   if s.get("phase") == "PHASE1_ARM" and s.get("eligible")
                   and s.get("effectiveRobotXY") and s.get("actualTouch")]
        if len(samples) < 3:
            raise AutoRunError("arm_samples_insufficient",
                               f"合格 Arm 样本只有 {len(samples)} 个（需 ≥3）",
                               classification="ambiguous", park=True)
        previous = run.get("arm")
        pairs = [{"robotX": s["effectiveRobotXY"][0], "robotY": s["effectiveRobotXY"][1],
                  "screenX": s["actualTouch"][0], "screenY": s["actualTouch"][1]}
                 for s in samples]
        try:
            fit = fit_affine(pairs)
        except CalibrationMathError as exc:
            raise AutoRunError("arm_fit_failed", exc.message,
                               classification="arm_mapping_error",
                               routed_to="ARM_RECALIBRATION", park=True) from exc
        if len(pairs) >= 12:
            try:
                quad = fit_quadratic(pairs)
                if quad["residualRms"] <= fit["residualRms"] + 1e-9:
                    fit = quad
            except CalibrationMathError:
                pass  # 样本退化（共线/奇异）→ 保持仿射
        rms, worst = fit["residualRms"], max(fit["residuals"])
        if rms > float(run["config"]["arm_rms_target_px"]) \
                or worst > float(run["config"]["arm_max_error_px"]):
            raise AutoRunError("arm_quality_below_target",
                               f"Arm 拟合 RMS {rms:.2f}px / 最大 {worst:.2f}px 未达标",
                               classification="arm_mapping_error",
                               routed_to="ARM_RECALIBRATION", park=True)
        candidate_model = {k: round(float(fit[k]), 6)
                           for k in ("a", "b", "c", "d", "e", "f",
                                     "qx2", "qxy", "qy2", "px2", "pxy", "py2")
                           if k in fit}
        if previous and previous.get("robotToScreen"):
            sample_set_grew = len(samples) > int(previous.get("sampleCount") or 0)
            if not sample_set_grew and rms > float(previous["residualRmsPx"]) \
                    + float(run["config"]["rms_regression_tol_px"]):
                raise AutoRunError("arm_model_regression",
                                   f"新 RMS {rms:.2f}px 比上一稳定 candidate "
                                   f"{previous['residualRmsPx']}px 明显变差：rollback",
                                   classification="arm_mapping_error", park=True)
            shift = self._auto_model_shift_mm(previous["robotToScreen"], candidate_model,
                                              run)
            if shift > float(run["config"]["model_shift_max_mm"]):
                raise AutoRunError("arm_model_shift_exceeded",
                                   f"新旧模型 9 点预测差异 {shift:.1f}mm 超上限：拒绝",
                                   classification="arm_mapping_error", park=True)
        run["arm"] = {
            "robotToScreen": candidate_model,
            "residualRmsPx": round(rms, 3), "maxResidualPx": round(worst, 3),
            "sampleCount": len(samples), "iteration": run["iteration"],
            "fittedAt": _now(),
            "residualsByPoint": [{"targetId": s["targetId"],
                                  "residualPx": round(r, 2)}
                                 for s, r in zip(samples, fit["residuals"])],
        }
        run["locks"]["armAffine"]["state"] = cb.MODEL_LOCK_CANDIDATE
        run["armHistory"].append({"iteration": run["iteration"],
                                  "residualRmsPx": round(rms, 3), "at": _now()})
        self._auto_event(run, "arm_refit", {"rms": round(rms, 3),
                                            "samples": len(samples)})

    def _auto_model_shift_mm(self, old_model: dict, new_model: dict, run: dict) -> float:
        w, h = self._auto_screen_size(run)
        shift = 0.0
        for point in run["pointPlan"]:
            sx, sy = point["u"] * w, point["v"] * h
            try:
                old_r = cb.robot_from_screen(old_model, sx, sy)
                new_r = cb.robot_from_screen(new_model, sx, sy)
            except ValueError:
                return float("inf")
            shift = max(shift, ((old_r[0] - new_r[0]) ** 2
                                + (old_r[1] - new_r[1]) ** 2) ** 0.5)
        return shift

    def _auto_lock_arm_model(self, run: dict) -> None:
        run["locks"]["armAffine"]["state"] = cb.MODEL_LOCK_LOCKED
        run["locks"]["armAffine"]["version"] = \
            int(run["locks"]["armAffine"].get("version", 0)) + 1
        run["locks"]["armAffine"]["lockedAt"] = _now()
        run["arm"]["version"] = run["locks"]["armAffine"]["version"]
        run["arm"]["lockedAt"] = _now()
        run["modelVersion"] += 1
        self._auto_phase_set(run, "ARM_LOCKED")
        self._auto_event(run, "arm_locked", {"version": run["arm"]["version"],
                                             "rms": run["arm"]["residualRmsPx"]})
        cb.save_candidate(run, self.host.data_root)

    # ----- Phase 2：闭环验证（只验证，不学习，§13.1/§13.4） -----

    def _auto_phase2_validate(self, run: dict, resume: dict | None = None) -> None:
        self._auto_phase_set(run, "PHASE2_VALIDATE")
        resume_target = (resume or {}).get("targetId") \
            if (resume or {}).get("phase") == "PHASE2_VALIDATE" else None
        soft_failing: list[str] = []
        for point in run["pointPlan"]:
            if point["id"] in run.get("phase2", {}) and point["id"] != resume_target:
                continue  # 已通过的点不重复
            attempts = 0
            timeouts = 0
            while True:
                try:
                    sample = self._auto_click_point(
                        run, point["id"], point["u"], point["v"],
                        "PHASE2_VALIDATE", use_camera=True)
                except AutoRunError as exc:
                    if exc.code in self._AUTO_RESAMPLE_CODES:
                        attempts += 1
                        if attempts < int(run["config"]["max_resample"]):
                            continue
                    raise
                except TouchReportTimeout:
                    timeouts += 1
                    if timeouts <= int(run["config"]["touch_retry"]):
                        continue
                    raise AutoRunError("touch_report_timeout",
                                       f"Phase 2 点 {point['id']} 触点超时重试用尽",
                                       classification="touch_feedback_error", park=True)
                level = sample.get("errorLevel")
                if level == "PASS" and sample.get("eligible"):
                    run["phase2"][point["id"]] = {
                        "touchTargetErrorPx": sample["touchTargetErrorPx"],
                        "cameraProjectionErrorPx": sample["cameraProjectionErrorPx"],
                        "cameraTouchGapPx": sample["cameraTouchGapPx"],
                        "classification": sample["classification"], "at": _now(),
                    }
                    break
                if level in ("HARD_FAIL", "HARD_ABORT"):
                    classification = sample.get("classification") or "ambiguous"
                    raise AutoRunError(
                        "hard_fail",
                        f"Phase 2 点 {point['id']} 误差 "
                        f"{sample.get('touchTargetErrorPx')}px 触发安全停止边界",
                        classification=classification, state=AUTO_STATE_ABORTED,
                        routed_to=self._auto_route_for(classification), park=True)
                attempts += 1
                if attempts >= int(run["config"]["max_resample"]):
                    # SOFT 用尽：不暂停不报错——记为失败点，交由迭代循环
                    # 自动重采该点 + 全量重拟合 + 复验（用户拍板 2026-09-03：
                    # 校准过程应自动计算收敛，只有 HARD/安全事件才停）
                    soft_failing.append(point["id"])
                    run.setdefault("phase2SoftFails", {})[point["id"]] = {
                        "touchTargetErrorPx": sample.get("touchTargetErrorPx"),
                        "errorLevel": level, "tries": attempts, "at": _now(),
                    }
                    break
        if soft_failing:
            self._auto_event(run, "phase2_soft_fails", {"points": soft_failing})
            return soft_failing
        self._auto_event(run, "phase2_passed",
                         {"maxPx": self._auto_phase2_max(run)})
        return []

    def _auto_phase2_max(self, run: dict) -> float | None:
        values = [entry.get("touchTargetErrorPx")
                  for entry in (run.get("phase2") or {}).values()]
        return max(values) if values else None

    def _auto_check_convergence(self, run: dict) -> bool:
        """全局收敛（§14.3）：H/Arm 双锁定 + Phase 2 全点达标 + 独立验证随后执行。"""
        if run["locks"]["cameraToScreen"].get("state") != cb.MODEL_LOCK_LOCKED \
                or run["locks"]["armAffine"].get("state") != cb.MODEL_LOCK_LOCKED:
            return False
        tolerance = float(run["config"]["target_tolerance_px"])
        entries = run.get("phase2") or {}
        if len(entries) < len(run["pointPlan"]):
            return False
        for entry in entries.values():
            error = entry.get("touchTargetErrorPx")
            if error is None or float(error) > tolerance:
                return False
        history = run.get("armHistory") or []
        if len(history) >= 2:
            older, newer = history[-2], history[-1]
            if newer["residualRmsPx"] > older["residualRmsPx"] \
                    + float(run["config"]["rms_regression_tol_px"]):
                raise AutoRunError("arm_model_regression",
                                   "连续两轮 RMS 变差：停止迭代并回退上一稳定模型",
                                   classification="arm_mapping_error", park=True)
        return True

    # ----- 独立验证（Appendix A 5 点，不参与拟合） -----

    def _auto_validate_independent(self, run: dict) -> None:
        self._auto_phase_set(run, "INDEPENDENT_VALIDATE")
        points = []
        for point in run["validationPlan"]:
            attempts = 0
            while True:
                try:
                    sample = self._auto_click_point(
                        run, point["id"], point["u"], point["v"],
                        "INDEPENDENT_VALIDATE", use_camera=True)
                except AutoRunError as exc:
                    if exc.code in self._AUTO_RESAMPLE_CODES:
                        attempts += 1
                        if attempts < int(run["config"]["max_resample"]):
                            continue
                    raise
                except TouchReportTimeout:
                    raise AutoRunError(
                        "independent_validation_failed",
                        f"独立验证点 {point['id']} 触点超时",
                        classification="touch_feedback_error",
                        routed_to="HARDWARE_INVESTIGATION", park=True)
                if sample.get("errorLevel") != "PASS" or not sample.get("eligible"):
                    classification = sample.get("classification") or "ambiguous"
                    # 失败也先落盘该点成绩（诊断要看全部 5 点的分布）
                    points.append({
                        "targetId": point["id"], "u": point["u"], "v": point["v"],
                        "touchTargetErrorPx": sample.get("touchTargetErrorPx"),
                        "cameraProjectionErrorPx": sample.get("cameraProjectionErrorPx"),
                        "cameraTouchGapPx": sample.get("cameraTouchGapPx"),
                        "passed": False, "at": _now(),
                    })
                    run["validationPoints"] = points
                    cb.save_candidate(run, self.host.data_root)
                    raise AutoRunError(
                        "independent_validation_failed",
                        f"独立验证点 {point['id']} 误差 "
                        f"{sample.get('touchTargetErrorPx')}px 超过 "
                        f"{run['config']['target_tolerance_px']}px（独立验证不参与拟合，"
                        f"不允许重采样证明）",
                        classification=classification,
                        routed_to=self._auto_route_for(classification), park=True)
                points.append({
                    "targetId": point["id"], "u": point["u"], "v": point["v"],
                    "touchTargetErrorPx": sample["touchTargetErrorPx"],
                    "cameraProjectionErrorPx": sample["cameraProjectionErrorPx"],
                    "cameraTouchGapPx": sample["cameraTouchGapPx"],
                    "passed": True, "at": _now(),
                })
                break
        run["validationPoints"] = points
        run["validation"] = {
            "points": points,
            "contactVsNominalMaxPx": max(p["touchTargetErrorPx"] for p in points),
            "contactVsCameraMaxPx": max(p["cameraTouchGapPx"] for p in points),
            "passedCount": len(points), "passed": True, "checkedAt": _now(),
        }
        self._auto_event(run, "independent_validation_passed")

    # ----- E 验收（复用现有 debug/wechat 验收，§3.3/§43） -----

    def _auto_acceptance_done(self, run: dict) -> bool:
        """E 验收完成条件（2026-09-04 用户拍板：微信测试另说）。

        调试台两项（输入法下发 + 删除键清空）✓ 即可提升 ready。微信四项
        仅保留为历史兼容记录，不参与机械臂校准、ready 或发送门禁；真实发送
        由 WX/插件按每次任务的目标、正文和发送后效果证据校验。
        """
        acceptance = run.get("acceptance") or {}
        recorded = acceptance.get("debug") or {}
        return all((recorded.get(check) or {}).get("ok")
                   for check in DEBUG_ACCEPTANCE_CHECKS)

    def _auto_e_acceptance(self, run: dict) -> None:
        self._auto_phase_set(run, "E_ACCEPTANCE")
        deadline = time.time() + float(run["config"]["acceptance_timeout_s"])
        # 逐项等待人工确认，避免输入结果还没看清就被下一项删除。
        for check in DEBUG_ACCEPTANCE_CHECKS:
            self._auto_check_cancel()
            try:
                self.auto_acceptance_execute(run["runId"], check)
            except (_AutoCancelled, AutoRunError):
                raise
            except Exception as exc:
                self._auto_event(run, "acceptance_execute_failed",
                                 {"check": check, "error": str(exc)})
                code = ("input_acceptance_unavailable"
                        if "device_unavailable" in str(exc)
                        else "acceptance_execution_failed")
                raise AutoRunError(
                    code, f"E 验收动作 {check} 未执行完成：{exc}",
                    classification="input_unavailable", park=True) from exc
            while not ((run.get("acceptance") or {}).get("debug") or {}).get(
                    check, {}).get("ok"):
                self._auto_check_cancel()
                if time.time() > deadline:
                    raise AutoRunError("acceptance_timeout",
                                       "等待操作员完成 E 验收超时", park=True)
                time.sleep(0.5)
        self._auto_event(run, "acceptance_done")

    # ----- PROMOTE（§21.4/§28） -----

    def _auto_promote_candidate(self, run: dict) -> None:
        self._auto_phase_set(run, "PROMOTE")
        # R2-09（FIX-07）：提升前再核对——当前受信绑定事实（含安装 UUID/
        # 来源几何身份）必须仍与本 run 冻结事实一致；任何变化拒绝提升，
        # 不把旧身份写进 v3（候选保留，另行重校准）。
        try:
            current_facts = self.host._read_binding_facts()
        except (WizardError, RuntimeError) as exc:
            raise AutoRunError("camera_binding_unavailable", str(exc),
                               state=AUTO_STATE_INVALIDATED,
                               classification="camera_measurement_error",
                               routed_to="CAMERA_RECALIBRATION", park=False) from exc
        run_binding = run.get("binding") or {}
        for key in ("cameraId", "cameraDeviceUniqueId", "bindingRevision",
                    "daemonAddr", "frameWidth", "frameHeight",
                    "cameraBindingId", "sourceGeometryId"):
            if str((current_facts or {}).get(key)) != str(run_binding.get(key)):
                raise AutoRunError(
                    "camera_binding_changed",
                    f"提升前绑定事实已变化（{key}）：拒绝提升，请重新校准",
                    state=AUTO_STATE_INVALIDATED,
                    classification="camera_measurement_error",
                    routed_to="CAMERA_RECALIBRATION", park=False)
        if run["locks"]["cameraToScreen"].get("state") != cb.MODEL_LOCK_LOCKED \
                or run["locks"]["armAffine"].get("state") != cb.MODEL_LOCK_LOCKED:
            raise AutoRunError("promote_locks_invalid", "H/Arm 未双锁定：拒绝提升")
        if not (run.get("validation") or {}).get("passed"):
            raise AutoRunError("promote_validation_missing", "独立验证未通过：拒绝提升")
        deferred = run.get("deferInputAcceptance") is True
        if deferred:
            self._auto_guard(run)
            if (run.get("z", {}).get("validated") is not True
                    or run["z"].get("daemonSession") != run.get("daemonSession")):
                raise AutoRunError("promote_motion_invalid", "本轮 Z 触点未验证，拒绝保存机械资格")
            if (run["validation"].get("passed") is not True
                    or run.get("validationPoints") != run["validation"].get("points")):
                raise AutoRunError("promote_motion_invalid", "独立验证记录不一致，拒绝保存机械资格")
        if not deferred and not self._auto_acceptance_done(run):
            raise AutoRunError("promote_acceptance_missing", "E 验收未完成：拒绝提升")
        base = cb.load_bundle(self.host.device, self.host.data_root)
        if base is None:
            raise AutoRunError("promote_base_missing", "活动档案缺失：拒绝提升")
        # P08：读取后立即捕获字节摘要——后续所有校验与写入都以这份字节
        # 为并发条件，主档被其他写入改动即拒绝覆盖。
        base_digest = cb.bundle_file_digest(self.host.device, self.host.data_root)
        if cb.profile_hash(base, keys=AUTO_PROMOTE_GUARD_KEYS) != run["baseGeometryHash"]:
            raise AutoRunError("promote_base_changed",
                               "活动档案的几何/绑定基础在 run 期间被修改：拒绝提升"
                               "（candidate 保留）")
        if cb.profile_hash(base) != run["baseProfileHash"]:
            self._auto_event(run, "promote_base_non_geometry_delta",
                             {"note": "run 期间有非几何字段改动（如目标标记），"
                                      "几何基础未变，放行提升"})
        run["baseStatus"] = base.get("status")
        promoted = json.loads(json.dumps(base, ensure_ascii=False))
        # 独立机械资格（2026-09-29 拍板）：E 输入/删除验收延后时提升为
        # motion_ready（机械可执行、输入法待验）；E 全过后才是 ready。
        # 发送/输入门禁由 validate_calibration_bundle 的
        # input_acceptance_deferred 检查承担，不因状态降级放行。
        promoted["status"] = cb.MOTION_READY_STATUS if deferred else "ready"
        promoted["daemonSession"] = run.get("daemonSession")
        # FIX-07：自动校准产出 v3——安装 UUID/来源几何身份取自 run 绑定事实
        # （可信绑定），机械模型完整保留；身份缺失保持 v2，不得自称 v3。
        run_binding = run.get("binding") or {}
        for key in ("cameraBindingId", "sourceGeometryId"):
            value = run_binding.get(key)
            if isinstance(value, str) and value.strip():
                promoted[key] = value.strip()
        if all(str(promoted.get(key) or "").strip()
               for key in cb.V3_REQUIRED_IDENTITY_FIELDS):
            promoted["schemaVersion"] = cb.BUNDLE_SCHEMA_VERSION_V3
        # 第七轮 OCR 审查 F2：PROMOTE 时写入恢复几何哈希（键集剔除
        # daemonSession/status——会话重置正是恢复的场景，哈希必须跨会话
        # 稳定），否则 attempt_recovery 的 geometry_changed 守卫是死代码，
        # 且重标定后旧哈希残留会把一键恢复永久锁死。
        promoted["recoveryGeometryHash"] = cb.profile_hash(
            promoted,
            keys=tuple(k for k in AUTO_PROMOTE_GUARD_KEYS
                       if k not in ("daemonSession", "status")))
        screen = dict(promoted.get("screen") or {})
        screen.update(run.get("screen") or {})
        # 屏幕边缘基准以本轮 run 实测为准（2026-09-04 修复：原来只在档案
        # 没有旧值时才写——重夹手机后再校准，提升仍保留重夹前的旧四角，
        # 门禁 screen_edge_changed 47.8px 误报）。同时同步向导 state，
        # 保证 gate（读 state 构建的档案视图）与磁盘档案一致。
        if run.get("edge"):
            screen["edgeModel"] = {"corners": run["edge"]["corners"],
                                   "tolerancePx": cb.DEFAULT_EDGE_TOLERANCE_PX,
                                   "safeInsetPx": cb.DEFAULT_SAFE_INSET_PX,
                                   "capturedAt": run["edge"]["capturedAt"],
                                   "source": "auto_calibration"}
        promoted["screen"] = screen
        from copy import deepcopy

        z = deepcopy(run["z"])
        # 删除键/停留参数固化：与 _execute_acceptance_debug 实际使用的来源一致
        # （state delete → autoSafe → 默认）。纯自动流程没走过手动 B 阶段时，
        # 不固化这些键会让 ready 档案过不了 z_parameters 门禁。
        z_state_cfg = self.host.state.get("z") or {}
        _auto_alt = z_state_cfg.get("autoSafe") or {}
        _delete_cfg = z_state_cfg.get("delete") or {}
        _tap_cfg = z_state_cfg.get("tap") or {}
        z.update({"zSafe": round(float(run["z"]["zSafe"]), 3),
                  "hoverZ": round(float(run["z"]["zSafe"]), 3),  # 兼容字段与 zSafe 一致
                  "tapDepth": round(float(run["z"]["tapDepth"]), 3),
                  "tapFeed": float(run["z"].get("tapFeed") or DEFAULT_TAP_FEED),
                  "tapDwellMs": int(_tap_cfg.get("dwellMs", 0)),
                  "deleteHoldMs": int(_delete_cfg.get("holdMs", 2000)),
                  "deleteDepth": float(_delete_cfg.get(
                      "depth", _auto_alt.get("tapDepth", 3.0))),
                  "deleteFeed": float(_delete_cfg.get(
                      "feed", _auto_alt.get("tapFeed", DEFAULT_TAP_FEED))),
                  "validated": True})
        delete_ok = ((run["acceptance"].get("debug") or {})
                     .get("ime_delete_clear") or {}).get("ok")
        if delete_ok is not None:
            z["deleteValidated"] = bool(delete_ok)
        if deferred:
            z["deleteValidated"] = False
        promoted["z"] = z
        promoted["safePark"] = dict(run.get("safePark") or {})
        xy_samples = [{"targetId": s["targetId"],
                       "effectiveRobotXY": s["effectiveRobotXY"],
                       "actualTouch": s["actualTouch"],
                       "sampleId": s["sampleId"], "phase": s["phase"]}
                      for s in run["samples"] if s.get("phase") == "PHASE1_ARM"
                      and s.get("eligible")]
        promoted["xy"] = {
            "robotToScreen": dict(run["arm"]["robotToScreen"]),
            "residualRmsPx": run["arm"]["residualRmsPx"],
            "maxResidualPx": run["arm"]["maxResidualPx"],
            "samples": xy_samples,
            "validation": [dict(p) for p in run["validationPoints"]],
            "validated": True,
        }
        promoted["camera"] = {
            "cameraToScreen": {"homography": run["h"]["homography"],
                               "reprojectionRmsPx": run["h"]["rms"],
                               "source": AUTO_CAMERA_SOURCE,
                               "inlierCount": run["h"]["correspondences"]
                               and len(run["h"]["correspondences"]),
                               "maxErrorPx": run["h"]["maxError"]},
            "reprojectionRmsPx": run["h"]["rms"],
            "validated": True,
            "visualVerify": {
                "checkedAt": run["validation"]["checkedAt"],
                "contactVsNominalMaxPx": run["validation"]["contactVsNominalMaxPx"],
                "contactVsCameraMaxPx": run["validation"]["contactVsCameraMaxPx"],
                "passedCount": run["validation"]["passedCount"],
                "passed": True,
                "note": "v1.1 自动校准 5 点独立验证（validation 点，不参与拟合）",
            },
        }
        promoted["modelLocks"] = {
            "cameraToScreen": {"state": cb.MODEL_LOCK_LOCKED,
                               "version": run["locks"]["cameraToScreen"]["version"],
                               "lockedAt": run["locks"]["cameraToScreen"]["lockedAt"],
                               "source": AUTO_CAMERA_SOURCE},
            "armAffine": {"state": cb.MODEL_LOCK_LOCKED,
                          "version": run["locks"]["armAffine"]["version"],
                          "lockedAt": run["locks"]["armAffine"]["lockedAt"],
                          "source": "effective_mpos_actual_touch"},
        }
        promoted["validation"] = {
            "points": [dict(p) for p in run["validationPoints"]],
            "passed": True, "checkedAt": run["validation"]["checkedAt"],
            "runId": run["runId"],
            "tolerancePx": run["config"]["target_tolerance_px"],
        }
        # 验收标志以 RUN 的 ✓ 记录为准（手动流程的 state 记录兜底取或）。
        # 应用四项仅作历史兼容记录，不参与 ready 或机械臂运行门禁。
        run_acceptance = run.get("acceptance") or {}

        def _run_kind_done(kind, checks_tuple):
            recorded = run_acceptance.get(kind) or {}
            return all((recorded.get(c) or {}).get("ok") for c in checks_tuple)

        promoted["acceptance"] = {
            "debug": not deferred and bool(_run_kind_done("debug", DEBUG_ACCEPTANCE_CHECKS)
                                          or self.host._acceptance_done("debug")),
            "wechat": bool(_run_kind_done("wechat", WECHAT_ACCEPTANCE_CHECKS)
                           or self.host._acceptance_done("wechat")),
            "lastRunAt": _now(),
            "deferred": {"debug": deferred,
                         "wechat": not (_run_kind_done("wechat", WECHAT_ACCEPTANCE_CHECKS)
                                        or self.host._acceptance_done("wechat"))},
        }
        promoted["autoCalibration"] = {
            "runId": run["runId"], "state": AUTO_STATE_PROMOTED,
            "phase": "PROMOTE", "iteration": run["iteration"],
            "modelVersion": run["modelVersion"], "lastFailure": None,
            "promotedAt": _now(),
            "deferInputAcceptance": deferred,
        }
        if deferred:
            facts = run["binding"]
            checks = cb.validate_calibration_bundle(
                promoted, device_id=self.host.device, daemon_session=run["daemonSession"],
                camera_id=facts.get("cameraId"),
                camera_device_unique_id=facts.get("cameraDeviceUniqueId"),
                binding_revision=facts.get("bindingRevision"), daemon_addr=facts.get("daemonAddr"),
                frame_width=facts.get("frameWidth"), frame_height=facts.get("frameHeight"))
            if checks:
                raise AutoRunError("promote_motion_invalid",
                                   "机械独立资格未通过：" +
                                   "；".join(item["message"] for item in checks))
        # P08：候选原子提升——以读取时的档案字节摘要为并发条件，主档
        # 在 run 期间/校验后被其他写入改动即拒绝覆盖，绝不混合字段。
        try:
            cb.save_bundle(promoted, self.host.data_root, expected_digest=base_digest)
        except cb.BundleConcurrentWrite as exc:
            raise AutoRunError("promote_base_changed",
                               f"{exc}（candidate 保留）") from exc
        run["qualificationStatus"] = promoted["status"]
        if run.get("edge"):
            geometry = dict(self.host.state.get("geometry") or {})
            geometry["edge"] = {"corners": run["edge"]["corners"],
                                "capturedAt": run["edge"]["capturedAt"],
                                "source": "auto_calibration"}
            self.host.state["geometry"] = geometry
        # 坐标桥旁档尽力写入（第 4.2/22.4 节）：失败只向本 run 留痕，
        # 不影响已保存的提升结果
        self.host._save_gesture_coordinate_frame(promoted)
        # 验收期登记（bindPending）的固定目标绑定快照随最终档案生效；
        # 失败不阻断提升（条目保持待生效，业务解析会拒绝，现场重新标记即可）
        try:
            ui_targets.rebind_pending(self.host.device, promoted,
                                      data_root=self.host.data_root,
                                      operator="auto_promote")
        except Exception as exc:
            self._auto_event(run, "ui_target_rebind_failed", {"error": str(exc)})
        with self.host._lock:
            if deferred:
                self.host.state["acceptance"]["debug"] = {}
                self.host.state["acceptance"]["deferred"] = {"debug": True}
                self.host.state["geometry"]["homography"] = {
                    "homography": run["h"]["homography"],
                    "frameW": promoted["frame"]["width"],
                    "frameH": promoted["frame"]["height"],
                    "reprojectionRmsPx": run["h"]["rms"],
                }
                self.host.state["xy"]["fit"] = {
                    **run["arm"]["robotToScreen"],
                    "residualRmsPx": run["arm"].get("residualRmsPx"),
                    "maxResidualPx": run["arm"].get("maxResidualPx")}
                self.host.state["z"]["hoverZ"] = z["hoverZ"]
                self.host.state["z"]["tap"] = {
                    "depth": z["tapDepth"], "feed": z["tapFeed"],
                    "dwellMs": z["tapDwellMs"], "validated": True}
                self.host.state["z"]["delete"] = {
                    "depth": z["deleteDepth"], "feed": z["deleteFeed"],
                    "holdMs": z["deleteHoldMs"], "validated": False}
            # 延后提升停在 E 验收阶段（机械资格已保存、输入法待验），
            # 操作员补完输入/删除验收后 finish() 才置 ready（补记解锁）。
            self.host.state["stage"] = STAGE_DEBUG_ACCEPTANCE if deferred else STAGE_READY
            self.host._save_state()
        self._auto_event(run,
                         "promoted_to_motion_ready" if deferred
                         else "promoted_to_ready")



def _point_in_convex_quad(px: float, py: float, corners: list) -> bool:
    """点是否在顺时针凸四边形内（叉积同号法；corners: TL,TR,BR,BL）。"""
    crosses = []
    for (x1, y1), (x2, y2) in zip(corners, list(corners[1:]) + [corners[0]]):
        crosses.append((x2 - x1) * (py - y1) - (y2 - y1) * (px - x1))
    return (all(c >= -1e-6 for c in crosses) or all(c <= 1e-6 for c in crosses))
