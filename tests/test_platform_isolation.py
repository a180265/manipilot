"""平台隔离守卫（2026-10-12 隔离手术的固化断言）。

- 生产核心不得 import adapters.wechat（该包属 WX 业务适配，随 WX 发行，不随 SDK）；
- api/app.py 不得硬编码 WX 导航扩展的端点与契约常量（属 extensions.wx_navigation）；
- 剔除 extensions 包后 create_app 仍可创建、核心端点仍注册（SDK 纯净发行形态）。

create_app 相关验证一律在子进程中执行：主套件 worker 里构造完整 app 有全局
副作用（set_default_service 等），会以分布敏感方式污染同 worker 的其他测试
（2026-10-12 实测教训，勿改回进程内调用）。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CORE_SCAN_DIRS = ["api", "runtime", "drivers", "agent",
                  "adapters/vision", "adapters/uinav", "adapters/screen_ime"]
CORE_SCAN_FILES = ["tasks/plugin_api.py"]

FORBIDDEN_MODULE_PREFIXES = ("adapters.wechat",)

APP_FORBIDDEN_SNIPPETS = (
    "/wx-navigation-capabilities",
    "/wx-navigation-safety",
    "/wx-navigation-review-receipts",
    "WX_NAV_TASK_CONTRACTS",
    "wx_navigation_auth",
    "wx-navigation-review-context",
    "ARMFLEET_WX_NAV_KEYS_FILE",
)

_ISOLATED_ENV = dict(os.environ, ARM_FLEET_SCREEN_IME="0",
                     ARM_FLEET_DAEMON_SUPERVISOR="0")

_RUN_CREATE_APP_CHECKS = (
    "import sys; sys.path.insert(0, %r)\n"
    "from api.app import create_app\n"
    "app = create_app()\n"
    "paths = {getattr(r, 'path', '') for r in app.routes}\n"
)


def _run_isolated(code: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          env=_ISOLATED_ENV, capture_output=True, text=True,
                          timeout=120)


def _iter_core_py_files():
    for rel in CORE_SCAN_DIRS:
        for path in sorted((ROOT / rel).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path
    for rel in CORE_SCAN_FILES:
        yield ROOT / rel


def _imported_modules(tree: ast.AST) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def test_core_has_no_wechat_adapter_imports():
    bad = []
    for path in _iter_core_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in _imported_modules(tree):
            if name.startswith(FORBIDDEN_MODULE_PREFIXES):
                bad.append(f"{path.relative_to(ROOT)}: {name}")
    assert not bad, f"生产核心不得 import adapters.wechat：{bad}"


def test_app_py_has_no_wx_navigation_hardcoding():
    source = (ROOT / "api" / "app.py").read_text(encoding="utf-8")
    hits = [s for s in APP_FORBIDDEN_SNIPPETS if s in source]
    assert not hits, f"app.py 不得硬编码 WX 导航扩展内容：{hits}"


def test_task_contract_registry_is_generic_mechanism():
    """submit_task 的外壳契约校验必须是注册表机制，chat-send 只是一条注册项。"""
    from runtime import runtime as rt_mod

    assert "chat_send" in rt_mod.TASK_CONTRACT_SCHEMAS
    rt_mod.register_task_contract_schema(
        "demo_type", "demo.v1.schema.json", default_contract_version="demo.v1")
    try:
        assert rt_mod.TASK_CONTRACT_SCHEMAS["demo_type"] == "demo.v1.schema.json"
        assert rt_mod.TASK_DEFAULT_CONTRACT_VERSIONS["demo_type"] == "demo.v1"
    finally:
        rt_mod.TASK_CONTRACT_SCHEMAS.pop("demo_type", None)
        rt_mod.TASK_DEFAULT_CONTRACT_VERSIONS.pop("demo_type", None)


def test_service_signing_primitives_standalone(tmp_path):
    from runtime import service_signing as ss

    base = ss.signature_base("GET", "/x?a=b", "2026-10-12T00:00:00Z", "n1", b"")
    assert base.splitlines()[0] == b"GET"
    keys_file = tmp_path / "keys.json"
    keys_file.write_text('{"format_version": 1, "keys": ['
                         '{"key_id": "k1", "secret_hex": "aa", "enabled": true}]}',
                         encoding="utf-8")
    keys = ss.load_keys(str(keys_file))
    assert keys == {"k1": (b"\xaa", True)}



def test_create_app_works_without_extensions():
    """剔除 extensions 包（SDK 纯净发行模拟）：create_app 照常创建，
    核心端点在，WX 导航端点不出现。"""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import importlib.abc\n"
        "class _Block(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'extensions' or fullname.startswith('extensions.'):\n"
        "            raise ImportError('extensions 剔除（SDK 纯净发行模拟）')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        % str(ROOT)
    ) + _RUN_CREATE_APP_CHECKS[(_RUN_CREATE_APP_CHECKS.index("from api.app")):] + (
        "assert '/v1/tasks' in paths\n"
        "assert '/v1/devices' in paths\n"
        "assert '/v1/devices/{device_id}/wx-navigation-capabilities' not in paths\n"
        "assert '/v1/devices/{device_id}/wx-navigation-safety' not in paths\n"
        "assert '/v1/tasks/{task_id}/wx-navigation-review-receipts' not in paths\n"
        "print('OK')\n")
    result = _run_isolated(code)
    assert result.returncode == 0, result.stdout[-800:] + result.stderr[-800:]
    assert "OK" in result.stdout
