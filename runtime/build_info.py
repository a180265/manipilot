"""Immutable startup diagnostics; never a hardware or deployment authorization."""

from datetime import datetime, timezone
import hashlib
import importlib
import json
import marshal
import os
from pathlib import Path
import sys
import types


MODULE_NAMES = (
    "api.app", "runtime.runtime", "runtime.devices", "runtime.build_info",
    "runtime.config", "runtime.errors", "drivers.daemon_arm",
    "drivers.screen_edge", "drivers.real_camera", "drivers.camera_broker",
    "drivers.screen_proof", "runtime.layout_region", "runtime.layout_matching",
    "runtime.layout_geometry", "runtime.layout_drafts",
    "runtime.layout_adaptation", "runtime.layout_observation",
)


def snapshot_modules(modules, root: Path) -> dict:
    records = {}
    for module in modules:
        functions = {}
        for name, value in vars(module).items():
            if getattr(value, "__module__", None) != module.__name__:
                continue
            if isinstance(value, types.FunctionType):
                functions[name] = value
            elif isinstance(value, type):
                for member_name, member in vars(value).items():
                    if isinstance(member, (classmethod, staticmethod)):
                        member = member.__func__
                    if isinstance(member, property):
                        for accessor in ("fget", "fset", "fdel"):
                            function = getattr(member, accessor)
                            if function is not None:
                                functions[f"{name}.{member_name}.{accessor}"] = function
                    elif isinstance(member, types.FunctionType):
                        functions[f"{name}.{member_name}"] = member
        codes = {
            # v3+ encodes object-sharing/refcount flags that can change while
            # these same functions execute; v2 hashes the code content instead.
            name: hashlib.sha256(marshal.dumps(function.__code__, 2)).hexdigest()
            for name, function in sorted(functions.items())
        }
        path = Path(module.__file__).resolve()
        relative = str(path.relative_to(root))
        source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        records[relative] = {
            "source_sha256_at_start": source_hash,
            "loaded_functions_sha256": hashlib.sha256(
                json.dumps(codes, sort_keys=True).encode()).hexdigest(),
            "function_count": len(codes),
        }
    return records


def capture_runtime_build() -> dict:
    root = Path(__file__).resolve().parents[1]
    records = snapshot_modules(
        [importlib.import_module(name) for name in MODULE_NAMES], root)
    return {
        "contract_version": "runtime-build.v1",
        "scope_display": "启动源码快照与关键模块已加载函数摘要；不代表校准或运行验收",
        "process_id": os.getpid(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version.split()[0],
        "code_serialization": "marshal-v2",
        "modules": records,
        "manifest_sha256": hashlib.sha256(
            json.dumps(records, sort_keys=True).encode()).hexdigest(),
    }
