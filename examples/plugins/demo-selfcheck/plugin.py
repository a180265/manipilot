"""设备自检示例插件：演示插件开发接口（硬件无关，不含业务流程）。

插件只能通过 StepContext 调用已声明能力，不得绕过运行时直接操作硬件。
"""
from __future__ import annotations

import asyncio

from tasks.plugin_api import TaskPlugin, make_step


class Plugin(TaskPlugin):
    async def validate(self, task, device_context):
        return {"ok": True, "message": "设备自检任务无业务参数"}

    async def plan(self, task, device_context):
        return [
            make_step(1, "capture", "截图取证", is_action=False,
                      required_capabilities=["camera.capture"], evidence_required=True),
            make_step(2, "identify_ui", "识别界面状态", is_action=False,
                      required_capabilities=["camera.identify_ui_state"], evidence_required=False),
            make_step(3, "go_home", "归位检查", is_action=True,
                      required_capabilities=["arm.home"], evidence_required=True,
                      success_criteria={"position": "home"}),
        ]

    async def execute_step(self, step, ctx):
        if step["action"] == "capture":
            evidence = await ctx.call_capability("camera.capture", {"label": "自检截图"})
            ui = await ctx.call_capability("camera.identify_ui_state", {})
            await ctx.update_observed_ui_state(ui["ui_state"], ui["confidence"])
            return {"outcome": "success", "message": "自检截图完成", "detail": {"ui_state": ui["ui_state_display"]}}
        if step["action"] == "identify_ui":
            ui = await ctx.call_capability("camera.identify_ui_state", {})
            await ctx.update_observed_ui_state(ui["ui_state"], ui["confidence"])
            return {"outcome": "success", "message": f"当前界面：{ui['ui_state_display']}"}
        if step["action"] == "go_home":
            result = await ctx.call_capability("arm.home", {})
            return {"outcome": "success", "message": "归位完成", "detail": {"position": result.get("position")}}
        return {"outcome": "failed", "error_category": "invalid_request", "message": f"未登记的自检步骤：{step['action']}"}

    async def cleanup(self, ctx):
        return {"cleaned": True, "message": "自检无需额外清理"}
