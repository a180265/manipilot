"""自动手势自检外部插件（gesture-validation.v1，规范第 8/12/22.7/22.10/22.16 节）。

单步任务：构造 GestureValidationRunner（runtime/gesture_validation.py 的状态机），
物理动作全部经 StepContext 能力调用执行（自检内部不直接调驱动绕过动作计数），
run 终态后把有效证书发布到自动档案并按第 3.2/22.10 节映射任务 outcome：

- run passed            -> success（confirmed_display=已完成自检）
- run partial           -> failed（明确失败，detail.action_results 保留已通过动作）
- run failed/cancelled  -> failed
- run unknown           -> unknown（manual_review_required=true，不自动续跑）

detail 严格按第 22.16 节 gesture_self_test 表输出；三层状态按聚合规则归约：
物理任一 unknown -> controller unknown；全部完成 -> completed；未动 -> not_started；
contact 全同值取该值否则 unknown、无采样 not_observed；effect 全满足 verified、
未执行 not_checked、其余 unknown。

插件只使用 ctx 能力与进程内 screen-ime 服务；不连串口、不联网。
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import math
import uuid
from datetime import datetime, timezone
from pathlib import Path

from tasks.plugin_api import TaskPlugin, make_step

# 仓库根：examples/plugins/gesture-validation-adapter/plugin.py -> 仓库根
PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_ROOT = PROJECT_ROOT / "configs"
DATA_ROOT = PROJECT_ROOT / "data"
# 仅保留旧测试/调用方的导入兼容；生产数据与证据只读 ctx 的运行根。

CONTRACT_VERSION = "gesture-validation.v1"
SELF_TEST_ACTIONS = ("scroll_up", "scroll_down", "system_back_left",
                     "system_back_right", "double_tap", "system_home")
# 第 22.7 节：自检任务 timeout=600 秒必须显式设置，不能假设平台默认值
SELF_TEST_TIMEOUT_SECONDS = 600
PHYSICAL_LIMIT = 36
TIMEOUT_MS = 600000
DOUBLE_TAP_INTERVAL_MS = 50
# This is only the compatibility fallback for plans that predate the
# duration-bearing gesture-program contract. Current plans use a duration
# derived from the calibrated motion itself.
SCROLL_FRAME_INTERVAL_SECONDS = 0.12
SCROLL_FRAME_INTERVAL_MIN_SECONDS = 0.08
SCROLL_FRAME_INTERVAL_MAX_SECONDS = 0.5
SCROLL_POST_MOTION_RESERVE_FRAMES = 8

# detail.action_results 每行有且仅有这些键（第 22.16 节）
ACTION_RESULT_KEYS = ("gesture_id", "state", "validation_status", "trial_ids",
                      "controller_status", "contact_status", "effect_status",
                      "reason_code", "evidence_refs")


def load_defaults() -> dict:
    """加载 configs/gesture_defaults.json（软件默认值唯一来源，§22.1）。"""
    return json.loads((CONFIG_ROOT / "gesture_defaults.json")
                      .read_text(encoding="utf-8"))


def _normalize_action_results(rows) -> list[dict]:
    normalized = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        normalized.append({key: row.get(key) for key in ACTION_RESULT_KEYS})
    return normalized


def aggregate_layers(action_results: list[dict]) -> dict:
    """第 22.16 节三层聚合规则（逐动作明细保留原值，不把 partial 写 verified）。"""
    controllers = [row.get("controller_status") for row in action_results]
    if any(status == "unknown" for status in controllers):
        controller = "unknown"
    elif any(status == "completed" for status in controllers):
        # 至少一次实际完成且所有已开始动作完成（剩余均为 skipped 的 not_started）
        controller = "completed"
    else:
        controller = "not_started"

    contacts = {row.get("contact_status") for row in action_results}
    if not contacts or contacts <= {"not_observed", None}:
        contact = "not_observed"
    elif len(contacts - {None}) == 1:
        contact = next(iter(contacts - {None}))
    else:
        contact = "unknown"

    effects = [row.get("effect_status") for row in action_results]
    if not effects or all(effect in {"not_checked", None} for effect in effects):
        effect = "not_checked"
    elif all(effect == "verified" for effect in effects):
        effect = "verified"
    else:
        effect = "unknown"
    return {"controller_status": controller, "contact_status": contact,
            "effect_status": effect}


def _normalize_budget(run: dict) -> dict:
    budget = run.get("budget") or {}
    return {
        "physical_count": max(0, int(budget.get("physical_count", 0) or 0)),
        "physical_limit": PHYSICAL_LIMIT,
        "elapsed_ms": max(0, int(budget.get("elapsed_ms", 0) or 0)),
        "timeout_ms": TIMEOUT_MS,
    }


def _scroll_sample_interval_seconds(plan: dict,
                                    max_motion_frames: int,
                                    captured_frames: int) -> float:
    """Sample the planned stroke densely, reserving frames for driver overhead.

    The plan estimates contact time, while the capability also includes
    positioning and retraction. The latter must not consume the entire
    camera chain before the capability returns.
    """
    if max_motion_frames <= 0:
        return SCROLL_FRAME_INTERVAL_SECONDS
    reserve = min(SCROLL_POST_MOTION_RESERVE_FRAMES, max_motion_frames // 2)
    dense_frames = max(1, max_motion_frames - reserve)
    if captured_frames >= dense_frames:
        return SCROLL_FRAME_INTERVAL_MAX_SECONDS

    duration_ms = plan.get("estimated_duration_ms")
    if duration_ms is None:
        durations = [
            stroke.get("duration_ms")
            for stroke in plan.get("strokes", [])
            if isinstance(stroke, dict)
        ]
        durations = [value for value in durations
                     if isinstance(value, (int, float)) and value > 0]
        duration_ms = max(durations, default=0)
    try:
        duration_seconds = float(duration_ms) / 1000.0
    except (TypeError, ValueError):
        duration_seconds = 0.0
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        return SCROLL_FRAME_INTERVAL_SECONDS

    interval = duration_seconds / dense_frames
    return max(SCROLL_FRAME_INTERVAL_MIN_SECONDS,
               min(SCROLL_FRAME_INTERVAL_MAX_SECONDS, interval))


class _LauncherUnavailable(ValueError):
    def __init__(self, reason_code, message):
        self.reason_code = reason_code
        self.motion_not_started = True
        super().__init__(message)


def _evidence_gray(root, relative, digest):
    """只解码运行根内且哈希一致的证据；绝不访问相机或外部路径。"""
    import cv2
    import numpy as np

    root = Path(root).resolve()
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root):
        raise ValueError("证据路径不属于当前运行根")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("证据哈希不一致")
    image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError("证据图像无法解码")
    return image


class _LauncherProof:
    """本地证据适配：复用模板契约/完整绑定与匹配算法，不负责恢复页面。"""

    def __init__(self, ctx, defaults):
        self.ctx = ctx
        self.defaults = defaults
        self.template = None
        self.last_verified_at = None
        self.check_current()

    def check_current(self):
        import cv2
        import numpy as np
        from adapters.uinav.navigation_templates import load_template_patch
        from drivers import calibration_bundle as cb, gesture_program as gp
        from runtime import automatic_gestures as ag

        ctx = self.ctx
        root = Path(ctx.data_root)
        try:
            bundle = cb.load_bundle(ctx.device_id, root)
            frame = gp.load_coordinate_frame(ctx.device_id, root)
            settings = ag.load_settings(ctx.device_id, root)
            binding, _ = ag._current_binding(
                bundle, frame, settings["gesture_rules_version"])
            payload = ctx.task["payload"]
            live = ctx.device_context
            if (binding is None or binding["device_id"] != ctx.device_id
                    or not ag._certificate_binding_matches(payload["binding"], binding)
                    or payload["rules_version"] != binding["recipe_version"]
                    or self.defaults["gesture_rules_version"] != binding["recipe_version"]
                    or live.get("camera_id") != binding["camera_id"]
                    or live.get("daemon_session") != binding["daemon_session"]):
                raise _LauncherUnavailable("navigation_template_binding_changed",
                                           "桌面验证的当前绑定与任务不一致")
            reason = ag._template_blocking_reason(
                ctx.device_id, "launcher", binding, "wechat-list-layout.v1", root,
                evidence_root=ctx.evidence_root, evidence_get=ctx.get_evidence,
                screen=bundle["screen"])
            if reason:
                raise _LauncherUnavailable(reason["code"], reason["message"])
            path = root / "devices" / ctx.device_id / "navigation_templates/launcher.json"
            template = json.loads(path.read_text(encoding="utf-8"))
            # 对实际读取快照再校验，避免前一个资格读取后的模板替换。
            if (not ag._template_validator().is_valid(template)
                    or template["template_id"] != "launcher"
                    or template["kind"] != "launcher"
                    or template["device_id"] != ctx.device_id
                    or template["layout_version"] != "wechat-list-layout.v1"
                    or not 3 <= len(template["anchor_evidence"]) <= 6):
                raise ValueError("桌面模板身份或结构无效")
            if (not set(binding) <= set(template["binding"])
                    or not ag._certificate_binding_matches(template["binding"], binding)):
                raise _LauncherUnavailable("navigation_template_binding_changed",
                                           "桌面模板完整绑定已失效")
            if self.template is not None and not ag._same_json(self.template, template):
                raise _LauncherUnavailable("navigation_template_binding_changed",
                                           "自检期间桌面模板已变化")
            size = (bundle["screen"]["screenWidth"], bundle["screen"]["screenHeight"])
            matrix = np.asarray(bundle["camera"]["cameraToScreen"]["homography"],
                                dtype=float).reshape(3, 3)
            if (not all(type(v) is int and 100 <= v <= 10000 for v in size)
                    or not np.isfinite(matrix).all() or abs(np.linalg.det(matrix)) < 1e-12):
                raise ValueError("校准平面无效")
            anchors, rectangles = [], []
            for entry in template["anchor_evidence"]:
                region = entry["region"]
                x, y, w, h = (region[k] for k in (
                    "left_px", "top_px", "width_px", "height_px"))
                if (not all(type(v) is int and v >= 0 for v in (x, y, w, h))
                        or x + w > size[0] or y + h > size[1]
                        or any(x < r and x + w > l and y < b and y + h > t
                               for l, t, r, b in rectangles)):
                    raise ValueError("桌面锚点越界或重叠")
                rectangles.append((x, y, x + w, y + h))
                crop = load_template_patch(
                    entry, kind="launcher", device_id=ctx.device_id, binding=binding,
                    evidence_root=ctx.evidence_root, evidence_get=ctx.get_evidence,
                    screen_size=size)
                params = self.defaults["launcher_template"]
                if (crop.shape != (h, w) or min(w, h) < params["min_anchor_size_px"]
                        or float(crop.std()) < params["min_anchor_stddev"]):
                    raise ValueError("桌面裁剪尺寸或纹理无效")
                anchors.append({**region, "template": crop})
            self.template = template
            self.binding, self.matrix, self.size = binding, matrix, size
            self.anchors = anchors
        except _LauncherUnavailable:
            raise
        except (OSError, ValueError, TypeError, KeyError, cv2.error) as exc:
            raise _LauncherUnavailable("navigation_template_invalid",
                                       f"桌面模板或证据不可用：{exc}") from exc

    def verify(self, before, after):
        import cv2
        from adapters.uinav.automatic_navigation import verify_top_template

        try:
            self.check_current()
            binding = self.binding
            if (not before or not after or before["evidence_id"] == after["evidence_id"]
                    or after["device_id"] != self.ctx.device_id
                    or after["task_id"] != self.ctx.task["task_id"]
                    or after["kind"] != "screenshot"
                    # CameraFrameBroker 的现有证据记录 camera_id 可为空；
                    # 其来源由 ctx 能力入口及上面的当前设备绑定负责校验。
                    or after.get("camera_id") not in (None, binding["camera_id"])):
                raise ValueError("当前截图归属或前后帧无效")
            moment = datetime.fromisoformat(after["captured_at"].replace("Z", "+00:00"))
            previous = datetime.fromisoformat(before["captured_at"].replace("Z", "+00:00"))
            if (not 0 <= (datetime.now(timezone.utc) - moment).total_seconds() <= 5
                    or moment <= previous
                    or (self.last_verified_at is not None and moment <= self.last_verified_at)):
                raise ValueError("当前画面过期或未刷新")
            image = _evidence_gray(self.ctx.evidence_root, after["file_path"], after["sha256"])
            if image.shape != (binding["frame_height"], binding["frame_width"]):
                raise ValueError("当前截图尺寸与完整绑定不符")
            result = verify_top_template(
                self.anchors, cv2.warpPerspective(image, self.matrix, self.size),
                defaults=self.defaults, min_anchors=3)
            self.last_verified_at = moment
            # 不匹配只表示桌面未证实，不能据此谎称仍在测试页并试下一候选。
            return {"confirmed": True if result["matched"] else None}
        except (OSError, ValueError, TypeError, KeyError, cv2.error):
            return {"confirmed": None}


class Plugin(TaskPlugin):
    async def validate(self, task, device_context):
        if task.get("contract_version") != CONTRACT_VERSION:
            return {"ok": False, "error_category": "invalid_request",
                    "message": "自检任务契约必须为 gesture-validation.v1"}
        payload = task.get("payload", {})
        # vision_only（2026-09-30 拍板）：可选布尔，视觉优先滑动自检
        allowed = {"binding", "actions", "rules_version", "vision_only"}
        if (not set(payload) <= allowed
                or not isinstance(payload.get("vision_only", False), bool)):
            return {"ok": False, "error_category": "invalid_request",
                    "message": "自检任务参数仅允许 binding/actions/rules_version/vision_only"}
        binding = payload.get("binding")
        if not isinstance(binding, dict) or not binding.get("device_id"):
            return {"ok": False, "error_category": "invalid_request",
                    "message": "缺少第 7 节完整绑定，拒绝自检"}
        actions = payload.get("actions")
        if (not isinstance(actions, list) or not actions
                or len(set(actions)) != len(actions)
                or any(action not in SELF_TEST_ACTIONS for action in actions)):
            return {"ok": False, "error_category": "invalid_request",
                    "message": "actions 必须为内置动作唯一数组"}
        if not isinstance(payload.get("rules_version"), str) or not payload["rules_version"].strip():
            return {"ok": False, "error_category": "invalid_request",
                    "message": "缺少手势规则版本 rules_version"}
        return {"ok": True, "message": "自检契约校验通过"}

    async def plan(self, task, device_context):
        return [make_step(
            1, "gesture_validation", "执行自动手势自检并发布动作证书",
            is_action=True,
            required_capabilities=[
                "device.automatic_gesture_profile", "device.gesture_capture",
                "arm.execute_gesture_program", "arm.test_calibrated_double_tap",
                "camera.capture", "camera.measure_scroll",
            ],
            timeout_seconds=SELF_TEST_TIMEOUT_SECONDS,
            success_criteria={"run_terminal_state": True},
            evidence_required=True,
            cleanup_action="结束采样会话并请求停止，等待运行时归位检查",
        )]

    async def cleanup(self, ctx):
        """自检结束由 runner 收尾采样会话；清理本身不移动机械臂。"""
        return {"cleaned": True,
                "message": "自检插件清理完成；采样会话由自检 run 收尾，平台级归位检查单独执行"}

    async def execute_step(self, step, ctx):
        payload = ctx.task["payload"]
        actions = list(payload["actions"])
        device_id = ctx.device_id
        # 视觉优先（拍板 2026-09-30）：滑动类自检不依赖手势页会话——效果由
        # 相机前后帧光流判定（生产 业务 场景无软件回报）。vision_only 仅对
        # 纯滑动集生效；tap/back/home 仍走页面采样路径。
        scroll_only = bool(actions) and all(
            a in ("scroll_up", "scroll_down") for a in actions)
        vision_only = bool(payload.get("vision_only")) and scroll_only

        try:
            defaults = load_defaults()
        except (OSError, ValueError) as exc:
            return self._failed_detail(run=None, reason="gesture_defaults_missing",
                                       message=f"无法加载手势默认配置：{exc}")

        # 输入法采样设备：优先设备绑定的 ime_device_id，缺省回退任务设备
        device_context = ctx.device_context or {}
        ime_device_id = device_context.get("ime_device_id") or device_id

        from drivers import calibration_bundle as cb
        from drivers import gesture_program as gprog
        data_root = Path(ctx.data_root)
        bundle = cb.load_bundle(device_id, data_root)
        frame = gprog.load_coordinate_frame(device_id, data_root)
        if bundle is None:
            return self._failed_detail(
                run=None, reason="calibration_unavailable",
                message="缺少当前校准档案，未执行任何物理动作；请先完成校准")
        try:
            frame = gprog.frame_for_internal_gesture(bundle, frame)
        except (ValueError, TypeError, KeyError, gprog.GestureProgramError) as exc:
            return self._failed_detail(
                run=None, reason="calibration_unavailable", message=str(exc))

        launcher = None
        if "system_home" in actions and not gprog.is_internal_coordinate_frame(frame):
            try:
                launcher = _LauncherProof(ctx, defaults)
            except _LauncherUnavailable as exc:
                return self._failed_detail(run=None, reason=exc.reason_code,
                                           message=str(exc))

        from adapters.screen_ime.gesture_capture import get_global_coordinator
        coordinator = get_global_coordinator()
        if coordinator is None:
            from adapters.screen_ime.service import get_default_service as _gds
            coordinator = getattr(_gds(), "gesture_capture", None)
        if coordinator is None and not vision_only:
            return self._failed_detail(run=None, reason="capability_unavailable",
                                       message="手势采样协调器未挂载，无法自检")

        from runtime.gesture_validation import GestureValidationRunner
        run_id = f"run-{uuid.uuid4().hex[:12]}"

        scroll_frames = []
        scroll_capture_error = None
        max_motion_frames = max(
            0, int(defaults["scroll_observation"]["max_frame_chain"]) - 2)

        async def execute_swipe(plan: dict):
            nonlocal scroll_capture_error
            if launcher is not None:
                launcher.check_current()
            scroll_frames.clear()
            scroll_capture_error = None
            # Endpoint-only optical flow cannot follow a full-page movement.
            # Keep capture failures separate from the one physical invocation.
            stop_capture = asyncio.Event()
            async def capture_motion():
                nonlocal scroll_capture_error
                while not stop_capture.is_set():
                    if len(scroll_frames) >= max_motion_frames:
                        scroll_capture_error = "camera_frame_budget_exhausted"
                        return
                    try:
                        evidence = await ctx.capture_evidence("自检滑动期间取证")
                        if not evidence or not evidence.get("evidence_id"):
                            raise ValueError("滑动期间摄像头证据缺失")
                        scroll_frames.append(evidence["evidence_id"])
                    except Exception:
                        scroll_capture_error = "camera_capture_failed"
                        return
                    try:
                        await asyncio.wait_for(stop_capture.wait(),
                                               _scroll_sample_interval_seconds(
                                                   plan, max_motion_frames,
                                                   len(scroll_frames)))
                    except asyncio.TimeoutError:
                        pass

            sampler = asyncio.create_task(capture_motion())
            try:
                return await ctx.call_capability("arm.execute_gesture_program",
                                                 {"plan": plan})
            finally:
                stop_capture.set()
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)

        def execute_double_tap(params: dict):
            # 第 22.17 节：参数有且仅有六个键；间隔固定 50ms 自动认证档
            if params["interval_ms"] != DOUBLE_TAP_INTERVAL_MS:
                raise ValueError("双击自检间隔必须为 50ms")
            return ctx.call_capability("arm.test_calibrated_double_tap", {
                "run_id": run_id, "trial_id": "probe", "plan_id": "tap",
                "binding": params["binding"], "point": params["point"],
                "interval_ms": params["interval_ms"]})

        frames = []

        async def capture_frame():
            evidence = await ctx.capture_evidence("自检取证")
            frames.append(evidence)
            del frames[:-2]
            if not evidence or not evidence.get("evidence_id"):
                return None
            return {"evidence_id": evidence["evidence_id"],
                    "sha256": evidence.get("sha256"),
                    "captured_at": evidence.get("captured_at")}

        async def measure_scroll(before, after, region):
            if scroll_capture_error is not None:
                return {"status": "unknown", "reason_code": scroll_capture_error}
            return await ctx.call_capability("camera.measure_scroll", {
                "op": "measure", "before_evidence_id": before["evidence_id"],
                "after_evidence_id": after["evidence_id"],
                "evidence_ids": [before["evidence_id"], *scroll_frames,
                                 after["evidence_id"]],
                "roi_frame": "screen",
                "roi": {"x": region["leftPx"], "y": region["topPx"],
                        "width": region["widthPx"], "height": region["heightPx"]},
            })

        def verify_page(kind):
            # Back 未登记父页模板：保持 Runner 已有手机提交事件通道。
            if kind != "home":
                return None
            return launcher.verify(*frames) if len(frames) == 2 else {"confirmed": None}

        runner = GestureValidationRunner(
            defaults=defaults, bundle=bundle, coordinate_frame=frame,
            device_id=device_id, ime_device_id=ime_device_id,
            coordinator=coordinator,
            execute_swipe=execute_swipe,
            execute_double_tap=execute_double_tap,
            capture_frame=capture_frame,
            measure_scroll=measure_scroll,
            verify_page=verify_page if launcher is not None else None,
            vision_only=vision_only,
            data_root=data_root, run_id=run_id)

        run = await runner.run(actions)

        # 发布证书：唯一写入口是 device.automatic_gesture_profile(op=publish)；
        # base_version 取 payload 绑定的 gesture_profile_version（0 表示自举首版）。
        published_version = None
        publish_error = None
        base_version = (payload.get("binding") or {}).get("gesture_profile_version")
        try:
            published = await ctx.call_capability("device.automatic_gesture_profile", {
                "op": "publish", "run_result": run,
                "base_version": base_version if isinstance(base_version, int)
                and base_version > 0 else None})
            version = (published or {}).get("version")
            if isinstance(version, int) and version > 0:
                published_version = version
        except Exception as exc:  # 发布失败不吞：按是否已动映射结果
            publish_error = getattr(exc, "reason_code", None) or str(exc)

        action_results = _normalize_action_results(run.get("action_results"))
        layers = aggregate_layers(action_results)
        run_state = run.get("state")
        physical_count = _normalize_budget(run)["physical_count"]

        if publish_error is not None:
            message = f"自检证书发布失败（{publish_error}）"
            if physical_count > 0:
                message = f"自检已执行物理动作但证书发布失败（{publish_error}），需人工核对"
                return {"outcome": "unknown", "error_category": "gesture_binding_changed",
                        "message": message,
                        "detail": self._detail(run, action_results, layers, None,
                                               run_state,
                                               run.get("stop_reason")
                                               or "gesture_binding_changed",
                                               message, "none", manual_review=True)}
            return self._failed_detail(run=run, reason="gesture_binding_changed",
                                       message=message)

        if run_state == "passed":
            return {"outcome": "success", "message": "自检通过：全部请求动作均连续验证有效",
                    "detail": self._detail(run, action_results, layers,
                                           published_version, run_state,
                                           None,
                                           "自检通过：全部请求动作均连续验证有效",
                                           "none")}
        if run_state == "partial":
            passed = [row["gesture_id"] for row in action_results
                      if row.get("state") == "valid"]
            message = ("自检部分通过（已有效：" + "、".join(passed) + "）；"
                       "其余动作明确失败或未完成，任务按失败处理")
            return {"outcome": "failed",
                    "error_category": run.get("stop_reason") or "effect_not_observed",
                    "message": message,
                    "detail": self._detail(run, action_results, layers,
                                           published_version, run_state,
                                           run.get("stop_reason") or "effect_not_observed",
                                           message, "test_fixture_next_candidate")}
        if run_state == "unknown":
            message = "自检出现未解决物理不确定性，已停止后续动作，需人工核对"
            return {"outcome": "unknown",
                    "message": message,
                    "detail": self._detail(run, action_results, layers,
                                           published_version, run_state,
                                           run.get("stop_reason") or "effect_unknown",
                                           message, "none", manual_review=True)}
        if run_state == "cancelled":
            message = "自检在物理动作开始后被取消，按失败处理"
            return {"outcome": "failed", "error_category": "gesture_budget_exhausted",
                    "message": message,
                    "detail": self._detail(run, action_results, layers,
                                           published_version, run_state,
                                           run.get("stop_reason") or "gesture_budget_exhausted",
                                           message, "none")}
        if run_state == "failed":
            reason = run.get("stop_reason") or "effect_not_observed"
            message = f"自检未通过（{reason}），已停止，不自动续跑"
            return {"outcome": "failed", "error_category": reason,
                    "message": message,
                    "detail": self._detail(run, action_results, layers,
                                           published_version, run_state, reason,
                                           message, "none")}
        message = f"自检 run 进入未预期状态：{run_state}"
        return {"outcome": "unknown", "message": message,
                "detail": self._detail(run, action_results, layers,
                                       published_version, run_state,
                                       run.get("stop_reason") or "effect_unknown",
                                       message, "none", manual_review=True)}

    # ---------- detail 构造 ----------

    def _detail(self, run, action_results, layers, published_version, run_state,
                reason_code, message, retry_policy, *, manual_review=False) -> dict:
        run = run or {}
        detail = {
            "contract_version": CONTRACT_VERSION,
            "run_id": run.get("run_id"),
            "input_binding": run.get("input_binding"),
            "published_profile_version": published_version,
            "run_state": run_state,
            "action_results": action_results,
            "budget": _normalize_budget(run),
            "controller_status": layers["controller_status"],
            "contact_status": layers["contact_status"],
            "effect_status": layers["effect_status"],
            "reason_code": reason_code,
            "message": message,
            "retry_policy": retry_policy,
            "evidence_refs": [],
        }
        if manual_review:
            # 仅结果未知时携带；§3.2：未知不自动续跑，转人工核对
            detail["manual_review_required"] = True
        return detail

    def _failed_detail(self, *, run, reason, message):
        layers = {"controller_status": "not_started",
                  "contact_status": "not_observed",
                  "effect_status": "not_checked"}
        safe_run = run if isinstance(run, dict) else {
            "run_id": "n/a", "action_results": [], "budget": {
                "physical_count": 0, "physical_limit": 36,
                "elapsed_ms": 0, "timeout_ms": 600000},
            "input_binding": None, "coordinate_frame_hash": None,
            "gesture_rules_version": "gesture-rules.v1",
            "published_profile_version": None}
        return {"outcome": "failed", "error_category": reason, "message": message,
                "detail": self._detail(safe_run, [], layers, None, None, reason,
                                       message, "none")}
