"""测试辅助：脚本化探针插件（非业务插件，仅测试用）与运行时工厂。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tasks.plugin_api import TaskPlugin, make_step

PROBE_MANIFEST = {
    "plugin_id": "test-scripted-probe",
    "name_display": "脚本探针插件（测试专用）",
    "version": "1.0.0",
    "vendor": "平台测试",
    "description": "按脚本执行步骤的测试插件，用于运行时核心验收，不含任何业务流程",
    "entry_module": "plugin.py",
    "task_types": [
        {
            "task_type": "script_probe",
            "name_display": "脚本探针任务",
            "confirmed_display": "已完成",
            "capability": "probe.script",
            "input_schema": {"type": "object"},
            "description": "测试用脚本任务",
            "entry_conditions": "仅测试环境",
            "exit_conditions": "脚本步骤执行完毕",
            "failure_handling": "按步骤结果返回明确失败或结果未知",
            "risk_level": "低",
        }
    ],
    "capabilities": ["arm.stop", "arm.home", "camera.capture"],
    "resources": {
        "devices": {"mode": "assigned", "description": "由任务指定设备"},
        "camera": True,
        "input": False,
        "arm": True,
        "external_services": [],
        "data_scope": {"reads_customer_message_text": False, "writes_customer_data": False},
    },
    "evidence": {"screenshot_required": True, "home_check_required": True},
    "limits": {"max_actions_per_task": 50},
}


class ScriptedProbePlugin(TaskPlugin):
    """按任务 payload.steps 脚本执行的探针插件（仅测试）。"""

    plugin_id = PROBE_MANIFEST["plugin_id"]
    manifest = PROBE_MANIFEST

    def __init__(self):
        self.executed_tasks: list[str] = []
        self.step_executions: list[tuple[str, int]] = []
        self.cancel_called = False

    async def validate(self, task, device_context):
        return {"ok": True}

    async def plan(self, task, device_context):
        steps = task["payload"].get("steps", [{"action_display": "默认步骤"}])
        plan = []
        for index, spec in enumerate(steps):
            plan.append(
                make_step(
                    index + 1,
                    f"probe_{index + 1}",
                    spec.get("action_display", f"探针步骤{index + 1}"),
                    is_action=spec.get("is_action", False),
                    required_capabilities=["arm.stop"],
                    timeout_seconds=spec.get("timeout_seconds", 5),
                    evidence_required=False,
                )
            )
        return plan

    async def execute_step(self, step, ctx):
        if ctx.task["task_id"] not in self.executed_tasks:
            self.executed_tasks.append(ctx.task["task_id"])
        self.step_executions.append((ctx.task["task_id"], step["step_no"]))
        spec = ctx.task["payload"]["steps"][step["step_no"] - 1]
        if spec.get("sleep"):
            await asyncio.sleep(spec["sleep"])
        if spec.get("wait_event"):
            await spec["wait_event"].wait()
        outcome = spec.get("outcome", "success")
        if outcome == "failed":
            return {"outcome": "failed", "error_category": spec.get("error_category", "driver_error"),
                    "message": spec.get("message", "探针步骤明确失败")}
        if outcome == "unknown":
            return {"outcome": "unknown", "message": spec.get("message", "探针步骤结果未知")}
        return {"outcome": "success", "message": "成功", "detail": spec.get("detail", {})}

    async def cancel(self, ctx):
        self.cancel_called = True
        return {"stopped": True, "message": "探针已停止"}


def make_device_configs(n: int = 1, prefix: str = "arm", **extra) -> list[dict]:
    configs = []
    for i in range(1, n + 1):
        cfg = {
            "device_id": f"{prefix}-{i:02d}",
            "name_display": f"设备 {prefix}-{i:02d}",
            "arm_slot": i,
            "camera_id": f"cam-{i:02d}",
            "wechat_account_id": f"sim-acc-{i:02d}",
        }
        cfg.update(extra)
        configs.append(cfg)
    return configs


def write_ten_device_root(root: Path) -> Path:
    """隔离的十设备绑定表；仅供仿真，不读取或修改现场配置。"""
    configs = make_device_configs(10)
    configs[0].update({
        "driver_mode": "daemon", "serial_port": "/dev/test-arm-01",
        "daemon_addr": "127.0.0.1:1", "camera_device_name": "Test Camera",
        "expected_frame_width": 1280, "expected_frame_height": 720,
    })
    (root / "configs").mkdir(parents=True, exist_ok=True)
    (root / "configs" / "devices.json").write_text(
        json.dumps({"devices": configs}, ensure_ascii=False), encoding="utf-8")
    return root


def make_runtime(tmp_path: Path, n_devices: int = 1, plugin=None, manifest=None):
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime

    config = PlatformConfig(
        {
            "db_path": str(tmp_path / "fleet.db"),
            "evidence_dir": str(tmp_path / "evidence"),
            "plugin_dir": str(tmp_path / "plugins"),
            "hardware_mode": "simulation",
            "heartbeat_interval_seconds": 1,
            "camera_check_interval_seconds": 2,
            "view_session_ttl_seconds": 60,
            "camera_frame_interval_seconds": 0.05,
        },
        root=tmp_path,
    )
    runtime = ArmFleetRuntime(config)
    runtime.plugins.register_instance_for_tests(plugin or ScriptedProbePlugin(), manifest or PROBE_MANIFEST)
    return runtime


async def wait_terminal(runtime, task_id: str, timeout: float = 8.0) -> dict:
    """等待任务进入终态。"""
    terminal = {"confirmed", "failed", "unknown", "cancelled", "expired"}
    waited = 0.0
    while waited < timeout:
        task = runtime.get_task(task_id)
        if task["status"] in terminal:
            return task
        await asyncio.sleep(0.05)
        waited += 0.05
    return runtime.get_task(task_id)


def enable_input_switch(runtime, tmp_path, device_ids=None):
    """A-开关测试助手（2026-10-03 规格 §4）：为设备写「允许输入与发送」选择。

    新契约默认关=阻断；成功路径套件显式开启。按当前 revision 幂等推进。
    """
    from runtime import input_settings as iset
    import uuid
    if device_ids is None:
        device_ids = [d["device_id"] for d in runtime.devices.all()]
    for device_id in device_ids:
        try:
            revision = int(iset.load_settings(
                device_id, data_root=tmp_path / "data").get("revision") or 0)
        except iset.InputSettingsError:
            revision = 0
        iset.update_settings(
            device_id, data_root=tmp_path / "data", enabled=True,
            ime_device_id="k40-test", expected_revision=revision,
            operator_id="操作员", reason="测试允许",
            client_request_id=f"allow-{uuid.uuid4().hex[:8]}",
            calibration_ready=True)


def op_context(operator_id: str = "操作员甲", role: str = "operator", reason: str = "测试操作",
               confirmation_token: str | None = None) -> dict:
    import uuid

    context = {
        "operator_id": operator_id,
        "role": role,
        "reason": reason,
        "correlation_id": f"corr-{uuid.uuid4().hex[:8]}",
        "client_request_id": f"req-{uuid.uuid4().hex[:8]}",
    }
    if confirmation_token is not None:
        context["confirmation_token"] = confirmation_token
    return context


def probe_payload(steps: list[dict], *, device_id: str = "arm-01", key: str | None = None) -> dict:
    import uuid

    return {
        "task_type": "script_probe",
        "idempotency_key": key or f"probe-{uuid.uuid4().hex[:10]}",
        "device_id": device_id,
        "expires_at": None,
        "payload": {"steps": steps},
        "correlation_id": f"corr-{uuid.uuid4().hex[:8]}",
    }
