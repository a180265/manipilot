"""插件管理器：外部插件的安装、启用、停用、卸载、升级、回滚与越权拦截。

平台核心不内置任何具体应用业务插件；插件必须通过清单声明：
任务类型（含中文名称）、使用的基础能力、资源范围、数据范围、证据声明和上限。
运行时按声明执行；未声明的能力/资源一律拒绝并审计。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import jsonschema

from runtime.errors import ApiError
from runtime.util import iso, new_id, sha256_text, utc_now

# 插件清单必填字段
MANIFEST_REQUIRED = (
    "plugin_id",
    "name_display",
    "version",
    "vendor",
    "description",
    "entry_module",
    "task_types",
    "capabilities",
    "resources",
    "evidence",
)

TASK_TYPE_REQUIRED = ("task_type", "name_display", "confirmed_display", "input_schema", "capability")


class PluginManager:
    def __init__(self, store, audit, events, plugin_dir: Path):
        self._store = store
        self._audit = audit
        self._events = events
        self._dir = Path(plugin_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        # 已加载插件实例：plugin_id -> TaskPlugin
        self._instances: dict[str, object] = {}

    # ---------- 清单校验 ----------
    def validate_manifest(self, manifest: dict) -> None:
        missing = [field for field in MANIFEST_REQUIRED if not manifest.get(field)]
        if missing:
            raise ApiError("plugin_manifest_invalid", f"插件清单缺少必填声明：{missing}")
        if not isinstance(manifest["task_types"], list) or not manifest["task_types"]:
            raise ApiError("plugin_manifest_invalid", "插件必须至少注册一个任务类型")
        guards = manifest.get("screen_context_guards", [])
        if (not isinstance(guards, list) or any(
                not isinstance(guard, dict) or set(guard) != {"plugin_id", "state_key"}
                or any(not isinstance(value, str) or not value.strip() for value in guard.values())
                for guard in guards)):
            raise ApiError("plugin_manifest_invalid", "屏幕执行上下文约束声明无效")
        for entry in manifest["task_types"]:
            if "request_schema" in entry:
                if not isinstance(entry["request_schema"], dict):
                    raise ApiError("plugin_manifest_invalid", "任务请求外壳约束必须为对象")
                try:
                    jsonschema.Draft202012Validator.check_schema(entry["request_schema"])
                except jsonschema.SchemaError as exc:
                    raise ApiError("plugin_manifest_invalid", "任务请求外壳约束无效") from exc
            if "read_only_task" in entry and type(entry["read_only_task"]) is not bool:
                raise ApiError("plugin_manifest_invalid", "只读任务声明必须为布尔值")
            if "focuses_input_before_typing" in entry:
                if (type(entry["focuses_input_before_typing"]) is not bool
                        or (entry["focuses_input_before_typing"] is True
                            and (entry.get("requires_input") is not True
                                 or entry.get("requires_keyboard_active") is not False))):
                    raise ApiError("plugin_manifest_invalid", "输入聚焦声明必须为布尔值，且仅用于先聚焦的输入任务")
            fields = entry.get("context_fields", [])
            if (not isinstance(fields, list) or any(not isinstance(field, str) or not field.strip()
                                                  for field in fields)
                    or len(set(fields)) != len(fields)):
                raise ApiError("plugin_manifest_invalid", "任务附加上下文字段必须为不重复的非空名称列表")
            for field in TASK_TYPE_REQUIRED:
                if not entry.get(field):
                    raise ApiError(
                        "plugin_manifest_invalid",
                        f"任务类型 {entry.get('task_type')} 缺少 {field}（含中文名称登记）",
                    )
        resources = manifest["resources"]
        for key in ("devices", "camera", "input", "arm", "external_services", "data_scope"):
            if key not in resources:
                raise ApiError("plugin_manifest_invalid", f"插件资源声明缺少 {key}")
        evidence = manifest["evidence"]
        if not evidence.get("screenshot_required"):
            raise ApiError("plugin_manifest_invalid", "插件必须声明截图取证（所有插件都必须留证）")
        if not isinstance(manifest["capabilities"], list):
            raise ApiError("plugin_manifest_invalid", "capabilities 必须是能力编号列表")

    # ---------- 安装 / 生命周期 ----------
    def install(self, source_dir: Path, operator_id: str, reason: str) -> dict:
        source_dir = Path(source_dir)
        manifest_path = source_dir / "manifest.json"
        if not manifest_path.exists():
            raise ApiError("plugin_manifest_invalid", f"缺少 manifest.json：{source_dir}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.validate_manifest(manifest)
        plugin_id = manifest["plugin_id"]
        existing = self._store.get_plugin(plugin_id)
        if existing:
            raise ApiError("plugin_already_installed", f"{plugin_id} 已安装，升级请使用 upgrade 接口")

        target = self._dir / plugin_id / manifest["version"]
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_dir, target, ignore=shutil.ignore_patterns("__pycache__"))

        record = {
            "plugin_id": plugin_id,
            "name_display": manifest["name_display"],
            "version": manifest["version"],
            "manifest": manifest,
            "status": "installed",
            "status_display": "已安装",
            "installed_at": iso(utc_now()),
            "activated_at": None,
            "install_dir": str(target.relative_to(self._dir.parent)) if target.is_relative_to(self._dir.parent) else str(target),
            "history": [{"action": "安装", "version": manifest["version"], "at": iso(utc_now())}],
        }
        self._store.save_plugin(record)
        self._audit.record("plugin_install", operator_id=operator_id, reason=reason, detail={"plugin_id": plugin_id, "version": manifest["version"]})
        return record

    def activate(self, plugin_id: str, operator_id: str = "system", reason: str = "系统启用") -> dict:
        record = self._require(plugin_id)
        if record["status"] == "activated":
            return record
        instance = self._load_instance(record)
        self._instances[plugin_id] = instance
        self._register_capabilities(record)
        record["status"] = "activated"
        record["status_display"] = "已启用"
        record["activated_at"] = iso(utc_now())
        record["history"].append({"action": "启用", "version": record["version"], "at": iso(utc_now())})
        self._store.save_plugin(record)
        self._audit.record("plugin_activate", operator_id=operator_id, reason=reason, detail={"plugin_id": plugin_id, "version": record["version"]})
        return record

    def deactivate(self, plugin_id: str, operator_id: str = "system", reason: str = "系统停用") -> dict:
        record = self._require(plugin_id)
        if self._running_tasks_for(plugin_id):
            raise ApiError("plugin_tasks_running", f"插件 {plugin_id} 仍有执行中任务，不能停用")
        self._instances.pop(plugin_id, None)
        record["status"] = "deactivated"
        record["status_display"] = "已停用"
        record["history"].append({"action": "停用", "version": record["version"], "at": iso(utc_now())})
        self._store.save_plugin(record)
        self._audit.record("plugin_deactivate", operator_id=operator_id, reason=reason, detail={"plugin_id": plugin_id})
        return record

    def uninstall(self, plugin_id: str, operator_id: str, reason: str) -> dict:
        record = self._require(plugin_id)
        if record["status"] == "activated":
            self.deactivate(plugin_id, operator_id, reason)
        target = self._dir / plugin_id
        if target.exists():
            shutil.rmtree(target)
        self._store._delete("plugins", "plugin_id", plugin_id)
        self._audit.record("plugin_uninstall", operator_id=operator_id, reason=reason, detail={"plugin_id": plugin_id})
        return {"uninstalled": True, "plugin_id": plugin_id}

    def upgrade(self, plugin_id: str, source_dir: Path, operator_id: str, reason: str) -> dict:
        """升级：保留旧版本目录用于回滚。"""
        record = self._require(plugin_id)
        source_dir = Path(source_dir)
        manifest = json.loads((source_dir / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("plugin_id") != plugin_id:
            raise ApiError("plugin_manifest_invalid", "升级包 plugin_id 与已安装插件不一致")
        self.validate_manifest(manifest)
        if manifest["version"] == record["version"]:
            raise ApiError("plugin_already_installed", "新版本号与当前版本相同")
        was_activated = record["status"] == "activated"
        if was_activated:
            self.deactivate(plugin_id, operator_id, reason)
        target = self._dir / plugin_id / manifest["version"]
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_dir, target, ignore=shutil.ignore_patterns("__pycache__"))
        old_version = record["version"]
        record["history"].append({"action": "升级", "version": old_version, "previous_version": old_version, "at": iso(utc_now()), "note": "升级前版本已保留"})
        record["version"] = manifest["version"]
        record["manifest"] = manifest
        record["install_dir"] = (
            str(target.relative_to(self._dir.parent))
            if target.is_relative_to(self._dir.parent) else str(target)
        )
        record["status"] = "installed"
        record["status_display"] = "已安装"
        self._store.save_plugin(record)
        self._audit.record("plugin_upgrade", operator_id=operator_id, reason=reason, detail={"plugin_id": plugin_id, "version": manifest["version"]})
        if was_activated:
            self.activate(plugin_id, operator_id, "升级后自动启用")
        return self._require(plugin_id)

    def rollback(self, plugin_id: str, operator_id: str, reason: str) -> dict:
        """回滚：回到上一个版本目录。"""
        record = self._require(plugin_id)
        upgrades = [h for h in record["history"] if h["action"] == "升级"]
        if not upgrades:
            raise ApiError("plugin_manifest_invalid", "没有可回滚的历史版本")
        was_activated = record["status"] == "activated"
        if was_activated:
            self.deactivate(plugin_id, operator_id, reason)
        previous_version = upgrades[-1].get("previous_version") or self._previous_version_dir(plugin_id, record)
        target = self._dir / plugin_id / previous_version
        if not target.exists():
            raise ApiError("plugin_not_found", f"历史版本目录不存在：{previous_version}")
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        record["history"].append({"action": "回滚", "version": record["version"], "at": iso(utc_now()), "note": f"回滚到 {previous_version}"})
        record["version"] = previous_version
        record["manifest"] = manifest
        record["status"] = "installed"
        record["status_display"] = "已安装"
        self._store.save_plugin(record)
        self._audit.record("plugin_rollback", operator_id=operator_id, reason=reason, detail={"plugin_id": plugin_id, "version": previous_version})
        if was_activated:
            self.activate(plugin_id, operator_id, "回滚后自动启用")
        return self._require(plugin_id)

    def _previous_version_dir(self, plugin_id: str, record: dict) -> str | None:
        versions = sorted(
            (p.name for p in (self._dir / plugin_id).iterdir() if p.is_dir()),
            reverse=True,
        ) if (self._dir / plugin_id).exists() else []
        older = [v for v in versions if v != record["version"]]
        return older[0] if older else None

    # ---------- 加载与查询 ----------
    def _load_instance(self, record: dict):
        plugin_dir = self._dir / record["plugin_id"] / record["version"]
        entry = record["manifest"]["entry_module"]
        module_path = plugin_dir / entry
        if not module_path.exists():
            module_path = plugin_dir / f"{entry}.py"
        if not module_path.exists():
            raise ApiError("plugin_not_found", f"插件入口模块不存在：{module_path}")
        module_name = f"armfleet_plugin_{record['plugin_id']}_{record['version']}".replace(".", "_").replace("-", "_")
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        plugin_cls = getattr(module, "Plugin", None)
        if plugin_cls is None:
            raise ApiError("plugin_manifest_invalid", "插件入口模块必须导出 Plugin 类")
        instance = plugin_cls()
        instance.plugin_id = record["plugin_id"]
        instance.manifest = record["manifest"]
        return instance

    def _register_capabilities(self, record: dict) -> None:
        """把插件注册的任务能力登记进能力注册表（来源标注插件）。"""
        from runtime.capabilities import CapabilityRegistry

        registry = CapabilityRegistry(self._store)
        for entry in record["manifest"]["task_types"]:
            registry.register(
                {
                    "capability_id": entry["capability"],
                    "name_display": entry["name_display"],
                    "version": record["version"],
                    "device_type": "composite",
                    "risk_level": entry.get("risk_level", "中"),
                    "data_scope": json.dumps(record["manifest"]["resources"].get("data_scope", {}), ensure_ascii=False),
                    "interruptible": True,
                    "cleanup_action": "由插件 cleanup 完成",
                    "test_fixture": record["manifest"].get("test_fixture", "插件自带"),
                    "source": f"plugin:{record['plugin_id']}",
                    "input_schema": entry.get("input_schema", {}),
                }
            )

    def _require(self, plugin_id: str) -> dict:
        record = self._store.get_plugin(plugin_id)
        if not record:
            raise ApiError("plugin_not_found", plugin_id)
        return record

    def get(self, plugin_id: str) -> dict:
        return self._require(plugin_id)

    def all(self) -> list[dict]:
        return sorted(self._store.all_plugins(), key=lambda p: p["plugin_id"])

    def instance(self, plugin_id: str):
        return self._instances.get(plugin_id)

    def instance_for_task_type(self, task_type: str):
        """按任务类型查找已启用插件实例。"""
        for record in self.all():
            if record["status"] != "activated":
                continue
            for entry in record["manifest"]["task_types"]:
                if entry["task_type"] == task_type:
                    instance = self._instances.get(record["plugin_id"])
                    if instance is None:  # 已启用但实例丢失（例如进程重启后未重载）
                        instance = self._load_instance(record)
                        self._instances[record["plugin_id"]] = instance
                    return instance, entry
        return None, None

    def task_type_registry(self) -> dict[str, dict]:
        """全部已启用任务类型 -> 登记项（含中文名称与进入条件）。"""
        registry: dict[str, dict] = {}
        for record in self.all():
            if record["status"] != "activated":
                continue
            for entry in record["manifest"]["task_types"]:
                registry[entry["task_type"]] = {
                    "plugin_id": record["plugin_id"],
                    "name_display": entry["name_display"],
                    "confirmed_display": entry["confirmed_display"],
                    "capability": entry["capability"],
                    "input_schema": entry.get("input_schema", {}),
                    "description": entry.get("description", ""),
                    "request_schema": entry.get("request_schema"),
                    "read_only_task": entry.get("read_only_task") is True,
                    "context_fields": list(entry.get("context_fields") or []),
                    "entry_conditions": entry.get("entry_conditions", ""),
                    "exit_conditions": entry.get("exit_conditions", ""),
                    "failure_handling": entry.get("failure_handling", ""),
                }
        return registry

    def _running_tasks_for(self, plugin_id: str) -> list[str]:
        running = []
        for task in self._store.all_tasks():
            if task.get("plugin_id") == plugin_id and task.get("status") in ("queued", "validating", "awaiting_device", "executing"):
                running.append(task["task_id"])
        return running

    # ---------- 测试支持：直接注册实例（不经磁盘安装） ----------
    def register_instance_for_tests(self, instance, manifest: dict) -> None:
        self.validate_manifest(manifest)
        instance.plugin_id = manifest["plugin_id"]
        instance.manifest = manifest
        record = {
            "plugin_id": manifest["plugin_id"],
            "name_display": manifest["name_display"],
            "version": manifest["version"],
            "manifest": manifest,
            "status": "activated",
            "status_display": "已启用",
            "installed_at": iso(utc_now()),
            "activated_at": iso(utc_now()),
            "install_dir": "(in-memory)",
            "history": [{"action": "测试注册", "version": manifest["version"], "at": iso(utc_now())}],
        }
        self._store.save_plugin(record)
        self._instances[manifest["plugin_id"]] = instance
        self._register_capabilities(record)

    # ---------- 插件私有状态 ----------
    def state_get(self, plugin_id: str, device_id: str, key: str):
        import json as _json

        raw = self._store.kv_get(f"plugin_state:{plugin_id}:{device_id}:{key}")
        return _json.loads(raw) if raw is not None else None

    def state_set(self, plugin_id: str, device_id: str, key: str, value) -> None:
        self._store.kv_set(f"plugin_state:{plugin_id}:{device_id}:{key}", json.dumps(value, ensure_ascii=False))
