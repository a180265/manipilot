"""屏幕输入法适配插件（移植自 AI_demo K40 输入法体系的应用层）。

职责边界：
- 屏幕文字输入任务：文本经内嵌输入法服务下发到手机候选栏，机械臂只点击一次候选，
  输入法按人工节奏逐字写入目标应用；插件核对返回内容与原文一致（协议不改写文本），
  继承 AI_demo 工作区"创建任务必须检查 content、出现乱码立即取消"的实机规则。
- 屏幕校准采样任务：在输入法校准页完成三点不共线采样，拟合 screen = A·robot + b，
  残差超阈值不写入正式映射；映射按 deviceId 保存，夹具/会话改变必须重新校准。
- 屏幕键盘逐字输入任务：不装输入法，机械臂直接在系统键盘上打字——
  校准映射（机械↔屏幕）+ 键盘布局模板（字符→键位）+ 逆变换 → 逐键绝对坐标点击。

安全边界（继承原设计并遵守平台规则）：
- 机械坐标在仿真阶段由任务锚点+步长推算；真机接入后必须改读 GRBL MPos 并配合
  摄像头确认（MPos 不能单独证明真实移动），此前不绕过平台硬件安全门禁。
- 禁止自动归位与 $H 系列；点击动作结束必须回悬停位置；未经人工授权不点击发送/回车。
- 插件只通过 StepContext 门面调用已声明能力；输入法服务为进程内嵌服务，
  在清单 resources.external_services 中声明（screen-ime-service）。
"""
from __future__ import annotations

import asyncio

from adapters.screen_ime.geometry import (
    CalibrationMathError,
    ScreenRobotTransform,
    fit_affine,
)
from adapters.screen_ime.keyboard_layout import (
    KeyboardLayout,
    KeyboardLayoutError,
)
from adapters.screen_ime.service import (
    ScreenImeApiError,
    get_default_service,
    now_iso,
)
from runtime.util import sha256_text
from tasks.plugin_api import TaskPlugin, make_step

DEFAULT_WAIT_SECONDS = 20.0
DEFAULT_MOVE_STEP_MM = 15.0
DEFAULT_RESIDUAL_THRESHOLD_PX = 3.0
DEFAULT_ANCHOR = (150.0, 60.0)  # 仿真锚点（毫米）；真机改读 GRBL MPos
POLL_INTERVAL_SECONDS = 0.2
TYPING_SPEEDS = ("slow", "normal", "fast", "random")

CALIBRATION_SAMPLES = 3


def _fail(category: str, message: str) -> dict:
    return {"ok": False, "error_category": category, "message": message}


class Plugin(TaskPlugin):
    async def validate(self, task, device_context):
        payload = task.get("payload", {})
        if task["task_type"] == "screen_input":
            text = payload.get("text", "")
            if not isinstance(text, str) or not text:
                return _fail("invalid_request", "任务正文为空，禁止下发空输入")
            if len(text) > 10000:
                return _fail("invalid_request", "任务正文超过 10000 字符上限")
            if not str(payload.get("ime_device_id", "")).strip():
                return _fail("invalid_request", "缺少输入法设备编号 ime_device_id")
            expected_hash = payload.get("text_hash")
            if expected_hash and sha256_text(text) != expected_hash:
                return _fail("text_hash_mismatch",
                             "正文哈希与任务不一致，禁止执行（协议不改写文本）")
            speed = payload.get("typing_speed", "random")
            if speed not in TYPING_SPEEDS:
                return _fail("invalid_request",
                             "typing_speed 只允许 slow、normal、fast 或 random")
        elif task["task_type"] == "screen_calibration":
            if not str(payload.get("ime_device_id", "")).strip():
                return _fail("invalid_request", "缺少输入法设备编号 ime_device_id")
            step_mm = payload.get("move_step_mm", DEFAULT_MOVE_STEP_MM)
            if not isinstance(step_mm, (int, float)) or step_mm <= 0:
                return _fail("invalid_request", "move_step_mm 必须为正数")
            threshold = payload.get("residual_threshold_px", DEFAULT_RESIDUAL_THRESHOLD_PX)
            if not isinstance(threshold, (int, float)) or threshold <= 0:
                return _fail("invalid_request", "residual_threshold_px 必须为正数")
        elif task["task_type"] == "screen_type":
            text = payload.get("text", "")
            if not isinstance(text, str) or not text:
                return _fail("invalid_request", "任务正文为空，禁止下发空输入")
            if len(text) > 60:
                return _fail("invalid_request",
                             "逐键打字文本最长 60 字符；长文本请改用 screen_input（输入法候选栏，机械臂只点一次）")
            if not str(payload.get("ime_device_id", "")).strip():
                return _fail("invalid_request", "缺少校准映射设备编号 ime_device_id")
            pace = payload.get("pace_ms", 350)
            if not isinstance(pace, int) or not 100 <= pace <= 3000:
                return _fail("invalid_request", "pace_ms 必须在 100–3000 之间")
            try:
                self._load_layout(payload)
            except KeyboardLayoutError as exc:
                return _fail("invalid_request", f"键盘布局模板无效：{exc.message}")
            mapping = get_default_service().mappings.get(str(payload["ime_device_id"]))
            if mapping is None:
                return _fail("mapping_not_found",
                             "该校准映射不存在，请先完成屏幕校准（screen_calibration）再直接打字")
        return {"ok": True, "message": "校验通过"}

    def _load_layout(self, payload: dict) -> KeyboardLayout:
        spec = payload.get("layout")
        return KeyboardLayout.from_spec(spec) if spec else KeyboardLayout.builtin_qwerty()

    # ---------- 步骤计划 ----------
    async def plan(self, task, device_context):
        if task["task_type"] == "screen_calibration":            return [
                make_step(1, "create_session", "创建屏幕校准会话", is_action=False,
                          required_capabilities=[], timeout_seconds=10,
                          evidence_required=False),
                make_step(2, "wait_page_open", "等待手机校准页打开", is_action=False,
                          required_capabilities=[], timeout_seconds=30,
                          evidence_required=False),
                make_step(3, "sample_points", "三点不共线采样（布防→受控短按→回报像素）",
                          is_action=True,
                          required_capabilities=["arm.click", "arm.stop"],
                          timeout_seconds=90,
                          success_criteria={"sample_count": CALIBRATION_SAMPLES}),
                make_step(4, "fit_and_save", "仿射拟合、残差校验并保存映射", is_action=False,
                          required_capabilities=[], timeout_seconds=10),
                make_step(5, "complete_session", "结束校准会话并取证", is_action=False,
                          required_capabilities=["camera.capture"], timeout_seconds=10),
            ]
        if task["task_type"] == "screen_type":
            text = task["payload"]["text"]
            pace_ms = int(task["payload"].get("pace_ms", 350))
            tap_count = len(text) + sum(1 for ch in text if ch.isupper())
            return [
                make_step(1, "check_mapping", "核对校准映射与键盘布局，生成点击序列", is_action=False,
                          required_capabilities=[], timeout_seconds=10, evidence_required=False),
                make_step(2, "tap_text", "机械臂在系统键盘上逐字打字", is_action=True,
                          required_capabilities=["arm.click", "arm.stop"],
                          timeout_seconds=30 + (tap_count * pace_ms) // 1000 + 20,
                          success_criteria={"tapped": tap_count}),
            ]
        return [
            make_step(1, "check_ime_device", "确认输入法设备在线且键盘激活", is_action=False,
                      required_capabilities=[], timeout_seconds=10, evidence_required=False),
            make_step(2, "dispatch_text", "下发文本到候选栏并核对返回内容", is_action=False,
                      required_capabilities=[], timeout_seconds=10),
            make_step(3, "wait_displayed", "等待输入法显示候选任务", is_action=False,
                      required_capabilities=[], timeout_seconds=30, evidence_required=False),
            make_step(4, "tap_candidate", "机械臂点击候选栏", is_action=True,
                      required_capabilities=["arm.click", "arm.stop"],
                      timeout_seconds=20, success_criteria={"clicked": True}),
            make_step(5, "wait_typed", "等待输入法逐字输入完成并核对字数", is_action=False,
                      required_capabilities=[], timeout_seconds=60),
        ]

    # ---------- 步骤执行 ----------
    async def execute_step(self, step, ctx):
        handler = getattr(self, f"_step_{step['action']}", None)
        if handler is None:
            return {"outcome": "failed", "error_category": "invalid_request",
                    "message": f"未登记的步骤：{step['action']}"}
        try:
            return await handler(ctx)
        except ScreenImeApiError as exc:
            return {"outcome": "failed", "error_category": "invalid_request",
                    "message": f"输入法服务拒绝（{exc.code}）：{exc.message}"}
        except CalibrationMathError as exc:
            return {"outcome": "failed", "error_category": "calibration_residual_exceeded"
                    if exc.code == "calibration_collinear" else "invalid_request",
                    "message": exc.message}

    # ---------- 屏幕文字输入 ----------
    async def _wait_seconds(self, ctx) -> float:
        return float(ctx.task["payload"].get("wait_seconds", DEFAULT_WAIT_SECONDS))

    async def _step_check_ime_device(self, ctx):
        service = get_default_service()
        ime_device_id = ctx.task["payload"]["ime_device_id"]
        devices = service.tasks.list_devices()
        device = next((d for d in devices if d["deviceId"] == ime_device_id), None)
        if device is None:
            return {"outcome": "failed", "error_category": "device_offline",
                    "message": f"输入法设备 {ime_device_id} 从未上报心跳，禁止下发输入任务"}
        if not device["online"] or not device["keyboardActive"]:
            return {"outcome": "failed", "error_category": "device_offline",
                    "message": (f"输入法设备 {ime_device_id} 离线或键盘未激活"
                                f"（最近心跳：{device['lastSeenAt']}），等设备恢复后再下发")}
        return {"outcome": "success",
                "message": f"输入法设备在线且键盘激活：{ime_device_id}"}

    async def _step_dispatch_text(self, ctx):
        service = get_default_service()
        payload = ctx.task["payload"]
        ime_task_id = f"{ctx.task['task_id']}-ime"
        task, created = await service.tasks.create({
            "taskId": ime_task_id,
            "deviceId": payload["ime_device_id"],
            "targetUser": payload.get("target_user", ctx.task.get("task_id", "agent")),
            "content": payload["text"],
            "typingSpeed": payload.get("typing_speed", "random"),
            "source": "api",
        })
        if task["content"] != payload["text"]:
            # 继承 AI_demo 实机规则：返回内容出现改写（如中文被替换为 ?）立即拒绝
            await service.tasks.cancel(ime_task_id)
            return {"outcome": "failed", "error_category": "text_hash_mismatch",
                    "message": "服务端返回内容与原文不一致（疑似乱码改写），已取消任务并禁止点击"}
        ctx.state_set("ime_task_id", ime_task_id)
        ctx.state_set("ime_task_created", created)
        return {"outcome": "success",
                "message": f"文本已下发候选栏（输入法任务 {ime_task_id}，"
                           f"速度档 {task['resolvedTypingSpeed']}），返回内容核对一致",
                "detail": {"ime_task_id": ime_task_id,
                           "resolved_typing_speed": task["resolvedTypingSpeed"]}}

    async def _step_wait_displayed(self, ctx):
        service = get_default_service()
        ime_task_id = ctx.state_get("ime_task_id")
        status = await service.tasks.wait_for_status(
            ime_task_id, {"displayed"}, int(await self._wait_seconds(ctx)))
        if status["status"] == "displayed":
            return {"outcome": "success", "message": "输入法已显示候选任务，可以点击候选栏"}
        return self._ime_failure(status)

    async def _step_tap_candidate(self, ctx):
        await ctx.call_capability("arm.click", {"target_id": "ime_candidate_bar"})
        return {"outcome": "success", "message": "机械臂已点击候选栏，输入法开始逐字输入",
                "detail": {"clicked": True}}

    async def _step_wait_typed(self, ctx):
        service = get_default_service()
        ime_task_id = ctx.state_get("ime_task_id")
        expected_chars = len(ctx.task["payload"]["text"])
        status = await service.tasks.wait_for_status(
            ime_task_id, {"inserted", "typing_failed", "cancelled"},
            max(int(await self._wait_seconds(ctx)), 20))
        if status["status"] == "inserted":
            typed = status["typedCharacterCount"]
            if typed != expected_chars:
                return {"outcome": "unknown",
                        "message": (f"输入法回报已输入，但字数不一致"
                                    f"（回报 {typed}，预期 {expected_chars}），需人工确认屏幕内容")}
            return {"outcome": "success",
                    "message": f"输入法已按原文逐字输入完成（{typed} 个字符），未点击发送",
                    "detail": {"ime_task_id": ime_task_id, "typed_character_count": typed,
                               "text_hash": sha256_text(ctx.task["payload"]["text"])}}
        return self._ime_failure(status)

    def _ime_failure(self, status: dict) -> dict:
        if status["status"] == "typing_failed":
            return {"outcome": "failed", "error_category": "ime_input_failed",
                    "message": (f"输入法回报输入失败（{status.get('failureReason') or '原因未说明'}），"
                                f"已输入 {status['typedCharacterCount']} 个字符，"
                                "内容保持原样，请人工确认后处理"),
                    "detail": {"ime_status": status}}
        if status["status"] in ("cancelled", "cancel_requested"):
            return {"outcome": "failed", "error_category": "ime_task_cancelled",
                    "message": (f"输入法任务已取消（阶段：{status['status']}），"
                                f"已输入 {status['typedCharacterCount']} 个字符，"
                                "请确认屏幕残留内容后决定是否重新下发"),
                    "detail": {"ime_status": status}}
        return {"outcome": "failed", "error_category": "ime_input_failed",
                "message": f"输入法任务进入未预期状态：{status['status']}",
                "detail": {"ime_status": status}}

    # ---------- 屏幕校准采样 ----------
    async def _step_create_session(self, ctx):
        service = get_default_service()
        session = await service.calibrations.create(ctx.task["payload"]["ime_device_id"])
        ctx.state_set("calibration_session_id", session["sessionId"])
        return {"outcome": "success",
                "message": f"校准会话已创建：{session['sessionId']}（等待手机打开校准页）",
                "detail": {"session_id": session["sessionId"]}}

    async def _step_wait_page_open(self, ctx):
        service = get_default_service()
        session_id = ctx.state_get("calibration_session_id")
        deadline = asyncio.get_running_loop().time() + float(await self._wait_seconds(ctx))
        while True:
            session = await service.calibrations.get(session_id)
            if session["status"] != "active":
                return {"outcome": "failed", "error_category": "calibration_page_closed",
                        "message": f"校准会话已结束（{session['status']}），无法继续采样"}
            if session["clientState"] == "open":
                return {"outcome": "success", "message": "手机校准页已打开，可以开始采样"}
            if asyncio.get_running_loop().time() >= deadline:
                return {"outcome": "failed", "error_category": "calibration_page_closed",
                        "message": "等待手机打开校准页超时；请在手机上手动打开输入法校准页后重试"}
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _robot_points(self, ctx) -> list[tuple[float, float]]:
        payload = ctx.task["payload"]
        anchor_x = float(payload.get("anchor_robot_x", DEFAULT_ANCHOR[0]))
        anchor_y = float(payload.get("anchor_robot_y", DEFAULT_ANCHOR[1]))
        step_mm = float(payload.get("move_step_mm", DEFAULT_MOVE_STEP_MM))
        # 三点：锚点、沿 X 一个步长、沿 Y 一个步长（不共线）
        return [(anchor_x, anchor_y), (anchor_x + step_mm, anchor_y), (anchor_x, anchor_y + step_mm)]

    async def _step_sample_points(self, ctx):
        service = get_default_service()
        session_id = ctx.state_get("calibration_session_id")
        points = self._robot_points(ctx)
        wait = float(await self._wait_seconds(ctx))
        collected = ctx.state_get("robot_samples") or []
        start_index = len(collected)
        for index in range(start_index, CALIBRATION_SAMPLES):
            sequence = index + 1
            sample_id = f"sample-{sequence}"
            robot_x, robot_y = points[index]
            await service.calibrations.arm(session_id, sample_id, sequence,
                                           None, None, None, None)
            await ctx.call_capability("arm.click", {"target_id": f"cal_sample_{sequence}"})
            sample = await self._wait_for_touch(ctx, service, session_id, sample_id, wait)
            collected.append({"sampleId": sample_id, "sequence": sequence,
                              "robotX": robot_x, "robotY": robot_y,
                              "screenX": sample["screenX"], "screenY": sample["screenY"],
                              "screenWidth": sample["screenWidth"],
                              "screenHeight": sample["screenHeight"],
                              "orientation": sample["orientation"]})
            ctx.state_set("robot_samples", collected)
        return {"outcome": "success",
                "message": f"已完成 {CALIBRATION_SAMPLES} 点采样（机械坐标与屏幕像素配对）",
                "detail": {"samples": collected}}

    async def _wait_for_touch(self, ctx, service, session_id: str, sample_id: str,
                              wait: float) -> dict:
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            session = await service.calibrations.get(session_id)
            if session["status"] != "active":
                raise ScreenImeApiError(409, "calibration_closed",
                                        "校准会话已结束，采样中止")
            for sample in session["samples"]:
                if sample["sampleId"] == sample_id:
                    return sample
            if asyncio.get_running_loop().time() >= deadline:
                raise ScreenImeApiError(409, "sample_timeout",
                                        f"等待校准页回报触点 {sample_id} 超时；"
                                        "请确认机械臂点击落在校准页标靶上")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _step_fit_and_save(self, ctx):
        service = get_default_service()
        samples = ctx.state_get("robot_samples") or []
        fit = fit_affine(samples)
        threshold = float(ctx.task["payload"].get("residual_threshold_px",
                                                  DEFAULT_RESIDUAL_THRESHOLD_PX))
        if fit["residualRms"] > threshold:
            return {"outcome": "failed", "error_category": "calibration_residual_exceeded",
                    "message": (f"仿射拟合残差 {fit['residualRms']:.2f}px 超过阈值 {threshold}px，"
                                "映射未保存；请检查机械坐标与手机摆放后重新校准"),
                    "detail": {"residual_rms": fit["residualRms"], "threshold_px": threshold}}
        first = samples[0]
        transform = ScreenRobotTransform.from_fit(
            fit, device_id=ctx.task["payload"]["ime_device_id"],
            screen_width=int(first["screenWidth"]), screen_height=int(first["screenHeight"]),
            orientation=first["orientation"], version=1, created_at=now_iso())
        saved = service.mappings.save(transform)
        ctx.state_set("mapping_saved", saved)
        return {"outcome": "success",
                "message": (f"校准映射已保存（版本 {saved['version']}，"
                            f"残差 {fit['residualRms']:.2f}px ≤ {threshold}px）；"
                            "机械臂控制器会话或手机夹具改变后必须重新校准"),
                "detail": {"mapping": saved}}

    async def _step_complete_session(self, ctx):
        service = get_default_service()
        session_id = ctx.state_get("calibration_session_id")
        session = await service.calibrations.finish(session_id, cancel=False)
        return {"outcome": "success", "message": f"校准会话已结束（{session['status']}）",
                "detail": {"session_status": session["status"]}}

    # ---------- 屏幕键盘逐字打字（不装输入法） ----------
    async def _step_check_mapping(self, ctx):
        service = get_default_service()
        payload = ctx.task["payload"]
        mapping = service.mappings.get(str(payload["ime_device_id"]))
        if mapping is None:
            return {"outcome": "failed", "error_category": "mapping_not_found",
                    "message": "校准映射不存在，请先完成屏幕校准（screen_calibration）再直接打字"}
        transform = ScreenRobotTransform.from_dict(mapping)
        layout = self._load_layout(payload)
        text = payload["text"]
        try:
            actions = layout.plan_for_text(text)
        except KeyboardLayoutError as exc:
            return {"outcome": "failed", "error_category": "layout_unknown_char",
                    "message": exc.message}
        # 预计算全部键位的屏幕像素与机械坐标（plan 步骤不含坐标展示，只入插件私有状态）
        width, height = float(mapping["screenWidth"]), float(mapping["screenHeight"])
        clicks = []
        for action in actions:
            px, py = action["u"] * width, action["v"] * height
            rx, ry = transform.robot_from_screen(px, py)
            clicks.append({"key": action["key"], "kind": action["kind"],
                           "robotX": rx, "robotY": ry,
                           "screenX": px, "screenY": py})
        ctx.state_set("screen_type_clicks", clicks)
        return {"outcome": "success",
                "message": (f"映射版本 {mapping['version']} 可用，布局 {layout.layout_id}，"
                            f"共 {len(clicks)} 次点击（含大写切换）"),
                "detail": {"click_count": len(clicks), "layout_id": layout.layout_id,
                           "mapping_version": mapping["version"]}}

    async def _step_tap_text(self, ctx):
        payload = ctx.task["payload"]
        pace_seconds = float(payload.get("pace_ms", 350)) / 1000.0
        clicks = ctx.state_get("screen_type_clicks") or []
        done = 0
        for click in clicks:
            await ctx.call_capability("arm.click", {
                "target_id": f"kb:{click['key']}",
                "robot_x": click["robotX"], "robot_y": click["robotY"],
                "screen_x": click["screenX"], "screen_y": click["screenY"],
            })
            done += 1
            if done < len(clicks):
                await asyncio.sleep(pace_seconds)
        return {"outcome": "success",
                "message": (f"已按布局逐键点击 {done} 次，文本「{payload['text']}」"
                            "已尝试输入（未点击发送/回车确认）"),
                "detail": {"tapped": done,
                           "text_hash": sha256_text(payload["text"])}}

    # ---------- 生命周期 ----------
    async def cancel(self, ctx):
        try:
            await ctx.call_capability("arm.stop")
        except Exception:
            pass
        # 取消平台任务时同步取消内嵌输入法任务与校准布防，避免残留候选栏点击
        try:
            service = get_default_service()
            ime_task_id = ctx.state_get("ime_task_id")
            if ime_task_id:
                await service.tasks.cancel(str(ime_task_id))
            session_id = ctx.state_get("calibration_session_id")
            if session_id:
                session = await service.calibrations.get(str(session_id))
                if session["status"] == "active":
                    await service.calibrations.finish(str(session_id), cancel=True)
        except Exception:
            pass
        return {"stopped": True, "message": "已请求机械臂停止，并同步取消输入法任务/校准会话"}

    async def cleanup(self, ctx):
        return {"cleaned": True, "message": "屏幕输入适配插件清理完成（输入文本不回删、不自动重试）"}
