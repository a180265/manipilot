"""通用手势程序外部插件（gesture-program.v1，规范第 6/3.2/22.16 节）。

单步任务：本地先用 schema + drivers.gesture_program.validate_program 拒绝非法程序
（零动作，failed/invalid_request），再经 arm.execute_gesture_program({"program": ...})
由运行时自动规划执行。结果用 drivers.gesture_program.evaluate_program 归约三层：

- scroll_displacement：camera.measure_scroll 抓前后两帧并测量位移，
  displaced -> verified / stationary -> no_effect / unknown -> unknown；
- manual_only（及本插件没有验证器的 page_transition/fixture_trace）：
  动作已执行、效果待人工确认 -> 任务 unknown，effect=not_checked，
  detail.manual_review_required=true（第 22.16 节）。

detail 严格按第 22.16 节 gesture_program 表；stroke_results 按程序 stroke
逐段输出，未执行的剩余 stroke 也返回 not_started/not_observed/not_checked。
"""
from __future__ import annotations

import json
from pathlib import Path

from tasks.plugin_api import TaskPlugin, make_step

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DATA_ROOT = PROJECT_ROOT / "data"

CONTRACT_VERSION = "gesture-program.v1"
EFFECT_KINDS = ("scroll_displacement", "page_transition", "fixture_trace",
                "manual_only")
# 本插件有自动效果验证器的 kind；其余按无验证器转人工（§3.2 最后一行）
AUTO_VERIFIABLE_KINDS = ("scroll_displacement",)
PROGRAM_TIMEOUT_SECONDS = 120


def _validate_expected_effect(effect) -> str | None:
    """返回中文错误；None 表示通过。"""
    if not isinstance(effect, dict) or set(effect) != {
            "kind", "reference_id", "reference_version"}:
        return "expected_effect 必须且仅有 kind/reference_id/reference_version"
    if effect["kind"] not in EFFECT_KINDS:
        return f"expected_effect.kind 只允许 {'/'.join(EFFECT_KINDS)}"
    reference_id, version = effect["reference_id"], effect["reference_version"]
    if not isinstance(reference_id, str) or type(version) is not int or version < 0:
        return "expected_effect 的 reference_id/reference_version 类型非法"
    if effect["kind"] == "manual_only":
        if reference_id != "" or version != 0:
            return "manual_only 时 reference_id 必须为空字符串且 reference_version=0"
    elif not reference_id.strip() or version < 1:
        return "非 manual_only 效果必须提供非空 reference_id 和正整数 reference_version"
    return None


def _saved_program_path(device_id: str, gesture_id: str) -> Path:
    return (DATA_ROOT / "devices" / device_id / "gesture_programs"
            / f"{gesture_id}.json")


def _check_saved_program_ref(device_id: str, ref, program: dict) -> str | None:
    """saved_program_ref 有且仅有 gesture_id/version；必须与 program 快照 hash 一致。"""
    from drivers.gesture_program import program_hash
    if not isinstance(ref, dict) or set(ref) != {"gesture_id", "version"}:
        return "saved_program_ref 必须且仅有 gesture_id/version"
    gesture_id, version = ref.get("gesture_id"), ref.get("version")
    if (not isinstance(gesture_id, str) or not gesture_id.strip()
            or type(version) is not int or version < 1):
        return "saved_program_ref 的 gesture_id/version 非法"
    path = _saved_program_path(device_id, gesture_id)
    if not path.exists():
        return f"引用的已保存手势程序不存在：{gesture_id}"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"已保存手势程序记录损坏：{exc}"
    if saved.get("version") != version:
        return (f"已保存手势程序版本不一致（登记 {saved.get('version')}，"
                f"引用 {version}）")
    if saved.get("program_hash") != program_hash(program):
        return "program 快照 hash 与已保存手势程序不一致，禁止执行"
    return None


class Plugin(TaskPlugin):
    async def validate(self, task, device_context):
        if task.get("contract_version") != CONTRACT_VERSION:
            return {"ok": False, "error_category": "invalid_request",
                    "message": "手势程序任务契约必须为 gesture-program.v1"}
        payload = task.get("payload", {})
        if not set(payload) <= {"binding", "program", "expected_effect",
                                "saved_program_ref"}:
            return {"ok": False, "error_category": "invalid_request",
                    "message": "任务参数只允许 binding/program/expected_effect/saved_program_ref"}
        for key in ("binding", "program", "expected_effect"):
            if key not in payload:
                return {"ok": False, "error_category": "invalid_request",
                        "message": f"缺少必填参数 {key}"}
        effect_error = _validate_expected_effect(payload["expected_effect"])
        if effect_error:
            return {"ok": False, "error_category": "invalid_request",
                    "message": effect_error}
        from drivers.gesture_program import GestureProgramError, validate_program
        try:
            validate_program(payload["program"])
        except GestureProgramError as exc:
            return {"ok": False, "error_category": "invalid_request",
                    "message": f"手势程序不符合 gesture-program.v1：{exc.message}"}
        if "saved_program_ref" in payload:
            ref_error = _check_saved_program_ref(task["device_id"],
                                                 payload["saved_program_ref"],
                                                 payload["program"])
            if ref_error:
                return {"ok": False, "error_category": "invalid_request",
                        "message": ref_error}
        return {"ok": True, "message": "手势程序契约校验通过"}

    async def plan(self, task, device_context):
        return [make_step(
            1, "gesture_program", "执行一次冻结的手势程序并归约三层结果",
            is_action=True,
            required_capabilities=["arm.execute_gesture_program",
                                   "camera.capture", "camera.measure_scroll"],
            timeout_seconds=PROGRAM_TIMEOUT_SECONDS,
            success_criteria={"controller_completed": True},
            evidence_required=True,
            cleanup_action="请求停止并等待运行时归位检查",
        )]

    async def execute_step(self, step, ctx):
        from drivers.gesture_program import (
            GestureProgramError, evaluate_program, program_hash, validate_program,
        )
        payload = ctx.task["payload"]
        program = payload["program"]
        expected = payload["expected_effect"]
        effect_kind = expected["kind"]

        # 本地拒绝非法程序：零物理动作（第 3.2 节前置失败分支）
        try:
            validate_program(program)
        except GestureProgramError as exc:
            return self._detail_result(
                program=program, plan_id=None, controller="not_started",
                contact="not_observed", effect="not_checked",
                reason_code=exc.reason_code,
                message=f"手势程序本地校验失败：{exc.message}",
                outcome="failed", error_category=exc.reason_code,
                stroke_states=[], evidence_refs=[])
        if "saved_program_ref" in payload:
            ref_error = _check_saved_program_ref(ctx.device_id,
                                                 payload["saved_program_ref"],
                                                 program)
            if ref_error:
                return self._detail_result(
                    program=program, plan_id=None, controller="not_started",
                    contact="not_observed", effect="not_checked",
                    reason_code="invalid_request",
                    message=ref_error, outcome="failed",
                    error_category="invalid_request",
                    stroke_states=[], evidence_refs=[])

        # 位移效果需要动作前帧：动作前先抓测量帧（excluded 时退回普通取证）
        measure_roi = self._measure_roi(program)
        before_measure = None
        evidence_refs: list[dict] = []
        if effect_kind == "scroll_displacement":
            before_measure = await ctx.call_capability("camera.measure_scroll", {
                "op": "grab", "label": "位移测量前帧"})
            evidence_refs.append(self._evidence_ref(before_measure, ctx))

        try:
            result = await ctx.call_capability("arm.execute_gesture_program",
                                               {"program": program})
        except GestureProgramError as exc:
            return self._detail_result(
                program=program, plan_id=None, controller="not_started",
                contact="not_observed", effect="not_checked",
                reason_code=exc.reason_code,
                message=f"手势程序规划被拒绝：{exc.message}",
                outcome="failed", error_category=exc.reason_code,
                stroke_states=[], evidence_refs=evidence_refs)

        # ---------- 效果验证 ----------
        visual = None
        measure_detail = None
        if effect_kind == "scroll_displacement":
            after_measure = await ctx.call_capability("camera.measure_scroll", {
                "op": "grab", "label": "位移测量后帧"})
            evidence_refs.append(self._evidence_ref(after_measure, ctx))
            measure_params = {
                "op": "measure",
                "before_evidence_id": before_measure["evidence_id"],
                "after_evidence_id": after_measure["evidence_id"],
                "roi": measure_roi,
                "roi_frame": "screen_norm",
            }
            visual = await ctx.call_capability("camera.measure_scroll",
                                               measure_params)
            measure_detail = {
                "dx_px": visual.get("dx_px"), "dy_px": visual.get("dy_px"),
                "inlier_ratio": visual.get("inlier_ratio"),
                "matched_ocr_rows": visual.get("matched_ocr_rows"),
            }
        else:
            after_shot = await ctx.capture_evidence("手势程序后取证")
            if after_shot and after_shot.get("evidence_id"):
                evidence_refs.append(self._evidence_ref(after_shot, ctx))

        policy = {
            "effect": effect_kind if effect_kind in AUTO_VERIFIABLE_KINDS else None,
            "min_displacement_px": 0.5,
            "message": "动作已执行，效果待人工确认" if effect_kind not in
                       AUTO_VERIFIABLE_KINDS else "",
        }
        expected_sign = self._scroll_expected_sign(program)
        if effect_kind == "scroll_displacement" and expected_sign is not None:
            policy["expected_sign"] = expected_sign
        verdict = evaluate_program({"program_hash": program_hash(program)},
                                   result, None, visual, policy)
        controller = verdict["controller_status"]
        effect = verdict["effect_status"]
        reason_code = verdict["reason_code"]
        if effect_kind == "scroll_displacement" and effect == "unknown":
            reason_code = (visual or {}).get("reason_code") or reason_code

        stroke_states = self._stroke_states(program, result, effect)
        manual_review = effect_kind not in AUTO_VERIFIABLE_KINDS
        message = self._message(effect_kind, controller, effect, reason_code,
                                measure_detail)

        outcome = "unknown"
        error_category = None
        if controller == "completed" and effect == "verified":
            outcome = "success"
        elif effect == "no_effect":
            outcome, error_category = "failed", "effect_not_observed"
        # manual_only/无验证器（not_checked）与其余未知分支：动作已执行，
        # 效果待人工确认（§3.2/§22.16），统一按结果未知处理

        step_result = self._detail_result(
            program=program, plan_id=(result or {}).get("plan_id"),
            controller=controller, contact=verdict["contact_status"],
            effect=effect, reason_code=reason_code, message=message,
            outcome=outcome, error_category=error_category,
            stroke_states=stroke_states, evidence_refs=evidence_refs,
            binding=payload.get("binding"),
            manual_review=manual_review and outcome == "unknown")
        return step_result

    # ---------- 辅助 ----------

    #: 归一化 ROI 最小边长：垂直/水平滑动包围盒会出现零宽或零高，
    #: 无特征区域光流无法测量，按屏幕比例下限填充
    MIN_ROI_NORM = 0.1

    @classmethod
    def _measure_roi(cls, program: dict) -> dict:
        """程序点包围盒 → 校准根平面归一化 ROI。

        程序坐标域就是校准屏幕归一化平面；measure op 以
        roi_frame=screen_norm 解析，不再乘相机帧尺寸（坐标域错配）。
        """
        xs, ys = [], []
        for stroke in program.get("strokes", []):
            for point in stroke.get("points", []):
                xs.append(float(point["x"]))
                ys.append(float(point["y"]))
        if not xs:
            return {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
        cx = (min(xs) + max(xs)) / 2
        cy = (min(ys) + max(ys)) / 2
        half_w = max((max(xs) - min(xs)) / 2, cls.MIN_ROI_NORM / 2)
        half_h = max((max(ys) - min(ys)) / 2, cls.MIN_ROI_NORM / 2)
        left = max(0.0, cx - half_w)
        right = min(1.0, cx + half_w)
        top = max(0.0, cy - half_h)
        bottom = min(1.0, cy + half_h)
        return {"x": left, "y": top,
                "width": max(0.0, right - left),
                "height": max(0.0, bottom - top)}

    @staticmethod
    def _scroll_expected_sign(program: dict) -> int | None:
        """单笔两点程序的方向意图：手指上滑（y 减小）→ 内容上移（屏幕 dy<0）。

        多笔/多点程序方向意图不明确，不提供（缺省不校验方向）。
        """
        strokes = program.get("strokes") or []
        if len(strokes) != 1:
            return None
        points = strokes[0].get("points") or []
        if len(points) != 2:
            return None
        y0, y1 = float(points[0]["y"]), float(points[1]["y"])
        if abs(y1 - y0) < 1e-6:
            return None
        return -1 if y1 < y0 else 1

    @staticmethod
    def _stroke_states(program: dict, result, effect: str) -> list[dict]:
        """逐 stroke 三层；未执行的剩余 stroke 也返回 not_started/not_observed/not_checked。"""
        strokes = program.get("strokes") or []
        completed = None
        if isinstance(result, dict):
            completed = result.get("strokes_completed")
            if not isinstance(completed, int) or completed < 0:
                from drivers.gesture_verdict import controller_status_from_result
                completed = (len(strokes)
                             if controller_status_from_result(result) == "completed"
                             else 0)
        states = []
        for index in range(len(strokes)):
            executed = index < int(completed or 0)
            states.append({
                "index": index,
                "controller_status": "completed" if executed else "not_started",
                "contact_status": "not_observed",
                "effect_status": effect if executed else "not_checked",
                "reason_code": None,
                "evidence_refs": [],
            })
        return states

    @staticmethod
    def _evidence_ref(record, ctx) -> dict:
        record = record or {}
        return {
            "kind": "image",
            "evidence_id": record.get("evidence_id"),
            "sha256": record.get("sha256"),
            "captured_at": record.get("captured_at"),
            "camera_id": (ctx.device_context or {}).get("camera_id"),
            "frame_seq": None,
            "calibration_fingerprint": None,
        }

    @staticmethod
    def _message(effect_kind, controller, effect, reason_code, measure_detail):
        if effect_kind not in AUTO_VERIFIABLE_KINDS:
            return "动作已执行，效果待人工确认"
        if controller != "completed":
            return f"控制器未完成（{reason_code or controller}），结果未知"
        if effect == "verified":
            dy = (measure_detail or {}).get("dy_px")
            return f"列表位移已验证（dy={dy}px）"
        if effect == "no_effect":
            return "控制器完成，但位移证据明确显示未起效"
        return f"位移测量未知（{reason_code or 'effect_unknown'}），不判成没滚动"

    def _detail_result(self, *, program, plan_id, controller, contact, effect,
                       reason_code, message, outcome, error_category,
                       stroke_states, evidence_refs, binding=None,
                       manual_review=False) -> dict:
        from drivers.gesture_program import program_hash
        detail = {
            "contract_version": CONTRACT_VERSION,
            "program_hash": program_hash(program),
            "plan_id": plan_id,
            "binding": binding,
            "stroke_results": stroke_states,
            "controller_status": controller,
            "contact_status": contact,
            "effect_status": effect,
            "reason_code": reason_code,
            "message": message,
            "retry_policy": "business_next_step" if effect == "verified" else "none",
            "evidence_refs": evidence_refs,
            "manual_review_required": bool(manual_review),
        }
        result = {"outcome": outcome, "message": message, "detail": detail}
        if error_category:
            result["error_category"] = error_category
        return result

    async def cleanup(self, ctx):
        return {"cleaned": True,
                "message": "手势程序插件清理完成；平台级归位检查单独执行"}
