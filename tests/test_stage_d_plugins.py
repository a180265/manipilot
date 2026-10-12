"""阶段 D 验收测试：插件清单、安装/卸载/升级/回滚、资源声明、等效隔离、生命周期接口。

退出条件：未声明能力和资源无法执行；插件不能绕过运行时。
"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from runtime.capabilities import BASE_CAPABILITIES
from runtime.errors import ApiError
from tasks.plugin_api import TaskPlugin, make_step
from tests.helpers import (
    PROBE_MANIFEST,
    ScriptedProbePlugin,
    make_device_configs,
    make_runtime,
    op_context,
    probe_payload,
    wait_terminal,
)

DEMO_SOURCE = Path(__file__).parents[1] / "examples" / "plugins" / "demo-selfcheck"


def test_platform_core_has_no_business_task_types(tmp_path):
    """平台核心纯净性：未装插件时任务类型注册表为空，能力只有平台基础能力。"""

    async def scenario():
        from runtime.config import PlatformConfig
        from runtime.runtime import ArmFleetRuntime

        config = PlatformConfig(
            {"db_path": str(tmp_path / "clean.db"), "evidence_dir": str(tmp_path / "evidence"),
             "plugin_dir": str(tmp_path / "plugins"), "hardware_mode": "simulation"},
            root=tmp_path,
        )
        runtime = ArmFleetRuntime(config)  # 不注册任何插件
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            assert runtime.plugins.task_type_registry() == {}, "平台不得预置业务任务类型"
            sources = {c["source"] for c in runtime.capabilities.all()}
            assert sources == {"platform"}, f"能力注册表只允许平台基础能力，实际：{sources}"
            with pytest.raises(ApiError) as exc_info:
                runtime.submit_task(probe_payload([]), source="console", context={"operator_id": "操作员甲"})
            # 任何未注册的任务类型都必须被拒绝
            assert exc_info.value.key == "plugin_task_type_unknown"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_manifest_validation_rejects_incomplete(tmp_path):
    """清单校验：缺少必填声明必须拒绝。"""

    async def scenario():
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            bad = json.loads((DEMO_SOURCE / "manifest.json").read_text(encoding="utf-8"))
            bad["plugin_id"] = "bad-plugin"
            del bad["evidence"]["screenshot_required"]
            source = tmp_path / "bad-plugin-src"
            source.mkdir()
            (source / "manifest.json").write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
            (source / "plugin.py").write_text("class Plugin:\n  pass\n", encoding="utf-8")
            with pytest.raises(ApiError) as exc_info:
                runtime.plugins.install(source, operator_id="操作员甲", reason="安装测试")
            assert exc_info.value.key == "plugin_manifest_invalid"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_install_activate_execute_uninstall_lifecycle(tmp_path):
    """完整生命周期：安装 → 启用 → 通过运行时执行 → 停用 → 卸载。"""

    async def scenario():
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            record = runtime.plugins.install(DEMO_SOURCE, operator_id="操作员甲", reason="安装示例插件用于验收")
            assert record["status"] == "installed"
            assert record["status_display"] == "已安装"

            runtime.plugins.activate("demo-selfcheck", operator_id="操作员甲", reason="验收启用")
            record = runtime.plugins.get("demo-selfcheck")
            assert record["status"] == "activated"
            registry = runtime.plugins.task_type_registry()
            assert "device_selfcheck" in registry
            assert registry["device_selfcheck"]["name_display"] == "设备自检任务"

            task = runtime.submit_task(
                {"task_type": "device_selfcheck", "idempotency_key": "selfcheck-001",
                 "device_id": "arm-01", "expires_at": None, "payload": {}, "correlation_id": "c-selfcheck"},
                source="console", context={"operator_id": "操作员甲"},
            )
            assert task["task_type_display"] == "设备自检任务"
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed"
            assert done["status_display"] == "已完成"
            assert done.get("evidence_ids"), "插件任务必须留证"

            # 停用与卸载
            runtime.plugins.deactivate("demo-selfcheck", operator_id="操作员甲", reason="验收停用")
            assert runtime.plugins.get("demo-selfcheck")["status_display"] == "已停用"
            with pytest.raises(ApiError) as exc_info:
                runtime.submit_task(
                    {"task_type": "device_selfcheck", "idempotency_key": "selfcheck-002",
                     "device_id": "arm-01", "expires_at": None, "payload": {}, "correlation_id": "c2"},
                    source="console", context={"operator_id": "操作员甲"},
                )
            assert exc_info.value.key == "plugin_task_type_unknown", "停用后任务类型不可再提交"

            runtime.plugins.uninstall("demo-selfcheck", operator_id="操作员甲", reason="验收卸载")
            with pytest.raises(ApiError):
                runtime.plugins.get("demo-selfcheck")
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_undeclared_capability_blocked(tmp_path):
    """越权拦截：插件调用未声明能力必须失败并留审计。"""

    class RoguePlugin(TaskPlugin):
        plugin_id = "rogue-plugin"
        manifest = {
            "plugin_id": "rogue-plugin",
            "name_display": "越权测试插件",
            "version": "1.0.0",
            "vendor": "测试",
            "description": "只声明截图能力，却试图输入文本",
            "entry_module": "plugin.py",
            "task_types": [{
                "task_type": "rogue_task", "name_display": "越权测试任务", "confirmed_display": "已完成",
                "capability": "rogue.composite", "input_schema": {"type": "object"},
            }],
            "capabilities": ["camera.capture"],  # 未声明 input.type_text
            "resources": {
                "devices": {"mode": "assigned"}, "camera": True, "input": False,
                "arm": False, "external_services": [], "data_scope": {},
            },
            "evidence": {"screenshot_required": True},
        }

        async def validate(self, task, device_context):
            return {"ok": True}

        async def plan(self, task, device_context):
            return [make_step(1, "sneak_input", "越权输入", is_action=True, required_capabilities=["input.type_text"])]

        async def execute_step(self, step, ctx):
            await ctx.call_capability("input.type_text", {"text": "不该被允许的输入"})
            return {"outcome": "success", "message": "不应到达这里"}

    async def scenario():
        runtime = make_runtime(tmp_path, 1, plugin=RoguePlugin(), manifest=RoguePlugin.manifest)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            task = runtime.submit_task(
                {"task_type": "rogue_task", "idempotency_key": "rogue-001", "device_id": "arm-01",
                 "expires_at": None, "payload": {}, "correlation_id": "c-rogue"},
                source="console", context={"operator_id": "操作员甲"},
            )
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "failed", f"越权调用必须明确失败，实际 {done['status']}"
            assert done["error_category"] == "capability_not_allowed"
            assert "输入" in done["failure_reason"] or "声明" in done["failure_reason"]
            violations = [e for e in runtime.audit.list_events(limit=100) if e["action"] == "plugin_violation_blocked"]
            assert violations, "越权尝试必须留审计"
            assert runtime.devices.input_driver("arm-01").typed_text() == "", "未声明能力的输入必须没有发生"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_upgrade_and_rollback(tmp_path):
    """升级保留旧版本；回滚恢复旧版本并可用。"""

    async def scenario():
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            runtime.plugins.install(DEMO_SOURCE, operator_id="操作员甲", reason="安装 v1")
            runtime.plugins.activate("demo-selfcheck", operator_id="操作员甲", reason="启用 v1")

            upgraded_src = tmp_path / "demo-v110"
            shutil.copytree(DEMO_SOURCE, upgraded_src)
            manifest = json.loads((upgraded_src / "manifest.json").read_text(encoding="utf-8"))
            manifest["version"] = "1.1.0"
            manifest["description"] = "升级后的示例插件"
            (upgraded_src / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

            runtime.plugins.upgrade("demo-selfcheck", upgraded_src, operator_id="操作员甲", reason="升级到 v1.1.0")
            record = runtime.plugins.get("demo-selfcheck")
            assert record["version"] == "1.1.0"
            assert record["status"] == "activated"

            # 回滚（危险操作语义，由调用方保证权限；管理器内记录审计）
            runtime.plugins.rollback("demo-selfcheck", operator_id="操作员甲", reason="v1.1.0 异常，回滚")
            record = runtime.plugins.get("demo-selfcheck")
            assert record["version"] == "1.0.0"
            assert record["status"] == "activated"

            # 回滚后的插件可正常执行任务
            task = runtime.submit_task(
                {"task_type": "device_selfcheck", "idempotency_key": "selfcheck-rollback",
                 "device_id": "arm-01", "expires_at": None, "payload": {}, "correlation_id": "c-rb"},
                source="console", context={"operator_id": "操作员甲"},
            )
            done = await wait_terminal(runtime, task["task_id"])
            assert done["status"] == "confirmed"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_uninstall_blocked_while_tasks_running(tmp_path):
    """插件仍有执行中任务时禁止卸载/停用。"""

    async def scenario():
        plugin = ScriptedProbePlugin()
        runtime = make_runtime(tmp_path, 1, plugin=plugin)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            task = runtime.submit_task(probe_payload([{"action_display": "长步骤", "sleep": 0.8}]), source="console", context={"operator_id": "操作员甲"})
            await asyncio.sleep(0.2)
            assert runtime.get_task(task["task_id"])["status"] == "executing"
            with pytest.raises(ApiError) as exc_info:
                runtime.plugins.deactivate(PROBE_MANIFEST["plugin_id"], operator_id="操作员甲", reason="有任务时停用")
            assert exc_info.value.key == "plugin_tasks_running"
            await wait_terminal(runtime, task["task_id"])
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_plugin_cannot_bypass_runtime(tmp_path):
    """等效隔离验证：插件实例拿不到驱动对象，唯一硬件入口是 StepContext 门面。"""

    async def scenario():
        runtime = make_runtime(tmp_path, 1)
        await runtime.startup(device_configs=make_device_configs(1))
        try:
            runtime.plugins.install(DEMO_SOURCE, operator_id="操作员甲", reason="安装示例")
            runtime.plugins.activate("demo-selfcheck", operator_id="操作员甲", reason="启用示例")
            instance = runtime.plugins.instance("demo-selfcheck")
            assert instance is not None
            for driver_attr in ("driver", "camera", "input_driver", "store", "devices", "leases"):
                assert not hasattr(instance, driver_attr), f"插件实例不得持有 {driver_attr} 句柄"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())
