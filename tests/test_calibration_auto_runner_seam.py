"""Auto_CS#8 拆分接缝守卫（ADR-0003）。

锁定三件事：
1. 依赖方向：calibration_auto_runner 不 import calibration_wizard（common 叶模块除外），
   wizard 不再定义 _auto_* 方法——runner 完全自治；
2. 协议成员集：AutoRunner 方法体内 self.<name> 只允许 runner 自有成员或 host.<name>；
3. wizard 名字面：v1.1 常量/异常经 re-export 保持原模块表面（含 runtime/recovery.py
   的 AUTO_PROMOTE_GUARD_KEYS 函数级 import）。
"""
import ast
from pathlib import Path

import drivers.calibration_auto_runner as runner_mod
import drivers.calibration_wizard as wizard_mod

ROOT = Path(__file__).resolve().parents[1]
RUNNER_SRC = (ROOT / "drivers" / "calibration_auto_runner.py").read_text(encoding="utf-8")
WIZARD_SRC = (ROOT / "drivers" / "calibration_wizard.py").read_text(encoding="utf-8")

RUNNER_CLASS_ATTRS = {
    "host", "_auto", "_auto_lock", "_AUTO_RESAMPLE_CODES",
}

FACADES = [
    "auto_run_active", "auto_start", "auto_stop", "auto_retry", "auto_validate",
    "auto_resume_acceptance", "auto_resume_motion_qualification",
    "auto_acceptance_execute", "auto_acceptance_record", "auto_view",
]


def _runner_tree() -> ast.Module:
    return ast.parse(RUNNER_SRC)


def test_runner_never_imports_calibration_wizard():
    for node in ast.walk(_runner_tree()):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("drivers.calibration_wizard"), \
                    f"runner 反向 import {alias.name}（依赖 DAG 禁环，ADR-0003）"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.startswith("drivers.calibration_wizard"):
                assert mod == "drivers.calibration_wizard_common", \
                    f"runner 只许 from common 叶模块 import，见 {mod}"


def test_runner_self_references_are_self_owned_or_host():
    tree = _runner_tree()
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "AutoRunner")
    defined = set(RUNNER_CLASS_ATTRS)
    for n in cls.body:
        if isinstance(n, ast.FunctionDef):
            defined.add(n.name)
    violations = []
    for n in cls.body:
        if not isinstance(n, ast.FunctionDef):
            continue
        for node in ast.walk(n):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"
                    and isinstance(node.ctx, ast.Load)
                    and node.attr not in defined):
                violations.append(f"{n.name}: self.{node.attr}")
    assert not violations, \
        "AutoRunner 引用了既非自有成员也非 host.* 的属性（接缝泄漏）：" + "; ".join(violations[:10])


def test_runner_has_no_wizard_text_dependency():
    # runner 代码不引用 wizard 类名（docstring 提及宿主关系无妨）
    refs = [n.id for n in ast.walk(_runner_tree())
            if isinstance(n, ast.Name) and n.id == "CalibrationWizard"]
    assert not refs, f"runner 代码引用了 CalibrationWizard：{len(refs)} 处"


def test_wizard_defines_no_auto_runner_methods():
    tree = ast.parse(WIZARD_SRC)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "CalibrationWizard")
    leftovers = [n.name for n in cls.body
                 if isinstance(n, ast.FunctionDef) and n.name.startswith("_auto_")]
    assert not leftovers, f"wizard 仍定义 runner 方法：{leftovers}"


def test_wizard_auto_facades_are_pure_delegation():
    tree = ast.parse(WIZARD_SRC)
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "CalibrationWizard")
    bodies = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
    def is_runner_call(call):
        f = call.func
        return (isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Attribute)
                and f.value.attr == "_auto_runner"
                and isinstance(f.value.value, ast.Name)
                and f.value.value.id == "self")

    for name in FACADES:
        node = bodies[name]
        calls = [x for x in ast.walk(node) if isinstance(x, ast.Call)]
        assert calls and all(is_runner_call(c) for c in calls), \
            f"{name} 门面不是纯委托（{len(calls)} 个调用）"
        assert all(c.func.attr == name for c in calls), f"{name} 门面委托目标不一致"


def test_wizard_keeps_runner_container_access_whitelisted():
    # wizard 侧只允许经 self._auto_runner 白盒直达 runner；不允许裸 self._auto（除 _auto_cfg/_auto_runner）
    for i, line in enumerate(WIZARD_SRC.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "self._auto_" not in line:
            continue
        for token in ("self._auto[", "self._auto.", "self._auto_lock", "self._auto_invalidate"):
            assert token not in line, \
                f"wizard L{i} 出现裸 runner 容器访问 {token!r}：应经 self._auto_runner"


def test_wizard_module_surface_reexports_runner_names():
    for name in ("AUTO_PROMOTE_GUARD_KEYS", "AUTO_ACTIVE_STATES", "AUTO_STATE_RUNNING",
                 "AUTO_CONFIG_DEFAULTS", "AUTO_PLAN_POINTS", "AUTO_VALIDATION_POINTS",
                 "AUTO_PHASE_DISPLAY", "AUTO_CAMERA_SOURCE", "AUTO_FIRST_CLICK_POINT"):
        assert getattr(wizard_mod, name) is getattr(runner_mod, name), name
    from drivers import calibration_wizard_common as common_mod
    for name in ("WizardError", "PoseUnlocatable", "TouchReportTimeout", "ImeError",
                 "AutoRunError", "_AutoCancelled", "_AutoPauseRetry", "STAGE_DISPLAY"):
        assert getattr(wizard_mod, name) is getattr(common_mod, name), name


def test_runner_protocol_member_set_is_locked():
    host = runner_mod.AutoRunHost
    expected_attrs = {
        "state", "daemon", "ime", "device", "data_root",
        "_camera_fetch", "_edge_detector", "_target_detector", "_target_candidates",
        "_target_draw_wait_s", "_target_detect_timeout_s", "post_promote_hook",
        "_installation_lock_root", "_position_context", "_real_hardware", "_lock",
        "_auto_cfg",
    }
    annotations = getattr(host, "__annotations__", {})
    for name in expected_attrs:
        assert name in annotations, f"AutoRunHost 缺属性注解 {name}"
    expected_methods = {
        "_acceptance_done", "_bundle", "_bundle_edge_corners", "_camera_status",
        "_clear_session_rebind", "_daemon_tap", "_edge_quad", "_execute_acceptance_debug",
        "_goto_robot", "_mark_session_rebind", "_read_binding_facts", "_read_position",
        "_require_ime_open", "_require_park_session_current", "_require_session",
        "_save_gesture_coordinate_frame", "_save_state", "_tap_with_report", "_target_rect",
    }
    for name in expected_methods:
        assert callable(getattr(host, name, None)), f"AutoRunHost 缺方法 {name}"
