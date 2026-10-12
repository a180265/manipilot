"""调试/正式一致性与不回归证明（规范第 13.4、15.2、22.16 节）。

- 同 binding/program 的规划 hash 与命令完全确定（fake 与真机同一函数）。
- 开启新功能开关前后，旧屏幕手势接口行为零变化。
- 自检发布与开关写入对原校准/SAFE PARK 字节零改动。
- 关闭全部新开关时新能力分发被拒（旧路径不受影响）。
- 单设备档案变更不影响另一设备。
- 相同任务参数经调试（console）与 WX 来源得到相同处理与 detail。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")

from fastapi.testclient import TestClient

from api.app import create_app
from runtime import automatic_gestures as ag
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tasks.plugin_api import TaskPlugin, make_step
from tests.helpers import make_device_configs, op_context
from tests.test_automatic_gesture_api import _run_fixture, _seed_calibration


class _NavStub(TaskPlugin):
    manifest = {
        "plugin_id": "wx-wechat-adapter", "name_display": "微信适配",
        "version": "1.7.0", "vendor": "t", "description": "t",
        "entry_module": "plugin.py",
        "task_types": [
            {"task_type": "wx_auto_navigate", "name_display": "自动导航",
             "confirmed_display": "已完成", "capability": "wechat.auto_navigate",
             "input_schema": {"type": "object"}},
        ],
        "capabilities": ["camera.capture"],
        "resources": {"devices": {"mode": "assigned"}, "camera": True,
                      "input": False, "arm": False, "external_services": [],
                      "data_scope": {}},
        "evidence": {"screenshot_required": True, "home_check_required": False},
        "limits": {"max_actions_per_task": 1},
    }

    async def validate(self, task, device_context):
        return {"ok": True}

    async def plan(self, task, device_context):
        return [make_step(1, "automatic_navigation", "自动导航",
                          is_action=True, required_capabilities=[],
                          evidence_required=False,
                          timeout_seconds=10)]

    async def execute_step(self, step, ctx):
        payload = ctx.task.get("payload", {})
        return {"outcome": "success", "message": "stub ok", "detail": {
            "action": "automatic_navigation",
            "contract_version": "wx-auto-navigation.v1",
            "navigation_id": payload.get("navigation_id"),
            "action_name": payload.get("action"),
            "step_index": payload.get("step_index"),
            "controller_status": "completed",
            "contact_status": "not_observed",
            "effect_status": "verified",
        }}


def _client(tmp_path: Path, plugin=None) -> TestClient:
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    if plugin is not None:
        runtime.plugins.register_instance_for_tests(plugin, plugin.manifest)
    app = create_app(runtime)
    app.state.runtime_factory = lambda: runtime
    app.state.device_configs = make_device_configs(2)
    return TestClient(app)


class TestPlanningDeterminism:
    def test_same_program_same_plan_hash(self, tmp_path):
        bundle = _seed_calibration(tmp_path)
        from drivers import gesture_program as gp
        frame = gp.load_coordinate_frame("arm-01", tmp_path / "data")
        program = {
            "program_version": "gesture-program.v1",
            "coordinate_space": "calibration_root_normalized.v1",
            "strokes": [
                {"points": [{"x": 0.5, "y": 0.85, "t_ms": 0},
                            {"x": 0.5, "y": 0.58, "t_ms": 2000}],
                 "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0}],
        }
        first = gp.plan_program(json.loads(json.dumps(program)), bundle, frame)
        second = gp.plan_program(json.loads(json.dumps(program)), bundle, frame)
        # 调试台与正式提交同一 program/binding 必须得到同一规划
        assert first["program_hash"] == second["program_hash"]
        assert first["strokes"][0]["mech_points"] == second["strokes"][0]["mech_points"]
        assert first["strokes"][0]["duration_ms"] == second["strokes"][0]["duration_ms"]
        assert first["execution_mode"] == "swipe"


class TestLegacyUnchanged:
    def test_screen_gestures_endpoint_identical_after_switch_writes_rejected(self, tmp_path):
        """开关已删（2026-09-30）：写开关 400 且 screen-gestures 旧接口零影响。"""
        _seed_calibration(tmp_path)
        with _client(tmp_path) as client:
            before = client.get("/v1/devices/arm-01/screen-gestures")
            assert before.status_code == 200
            baseline = before.json()
            response = client.put("/v1/devices/arm-01/automatic-gesture-settings",
                                  json={"updates": {
                                      "automatic_gestures_enabled": True,
                                      "automatic_navigation_enabled": True,
                                      "gesture_program_enabled": True,
                                      "auto_validate_after_calibration": True},
                                      **op_context("全开")})
            assert response.status_code in (400, 422)
            after = client.get("/v1/devices/arm-01/screen-gestures").json()
            assert after == baseline  # 旧接口同请求相同结果

    def test_calibration_bytes_unchanged_by_new_writes(self, tmp_path):
        bundle = _seed_calibration(tmp_path)
        path = tmp_path / "data" / "devices" / "arm-01" / "calibration_bundle.json"
        before = path.read_bytes()
        ag.publish_run_results("arm-01", _run_fixture(bundle, tmp_path / "data"),
                               data_root=tmp_path / "data")
        assert path.read_bytes() == before  # 自检对原 bundle 零改动

    # test_disabled_switches_block_new_capability 已随开关删除（2026-09-30 拍板）：
    # arm.execute_gesture_program 不再有设置开关拒绝语义，执行资格由校准门禁
    # 与物理前置检查承担（_check_physical_preconditions/plan_program 校验）。


class TestDeviceIsolation:
    def test_publish_one_device_does_not_touch_other(self, tmp_path):
        bundle = _seed_calibration(tmp_path)
        ag.publish_run_results("arm-01", _run_fixture(bundle, tmp_path / "data"),
                               data_root=tmp_path / "data")
        assert ag.load_profile("arm-02", tmp_path / "data") is None
        # 开关已删（2026-09-30 拍板）：其他设备设置只含规则版本，且不被波及
        assert ag.load_settings("arm-02", tmp_path / "data") \
            == {"gesture_rules_version": "gesture-rules.v1"}


class TestConsoleWxParity:
    def test_same_payload_same_detail_console_and_wx(self, tmp_path):
        bundle = _seed_calibration(tmp_path)
        data_root = tmp_path / "data"
        from drivers import gesture_program as gp
        ag.publish_run_results("arm-01", _run_fixture(bundle, data_root),
                               data_root=data_root)
        view = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                                  bundle=bundle,
                                  frame=gp.load_coordinate_frame("arm-01", data_root),
                                  data_root=data_root)
        payload = {
            "task_type": "wx_auto_navigate",
            "contract_version": "wx-auto-navigation.v1",
            "device_id": "arm-01",
            "idempotency_key": "",  # 每次独立生成
            "task_id": "",
            "payload": {
                "navigation_id": "f2f2e08c-3c43-4dca-86b3-6bb46d16c001",
                "navigation_profile": {key: view[key] for key in (
                    "profile_id", "version", "contract_version")},
                "binding": view["binding"],
                "action": "search_next", "step_index": 1,
            },
            "expires_at": "2099-01-01T00:00:00Z",
            "correlation_id": "",
        }
        with _client(tmp_path, _NavStub()) as client:
            runtime = client.app.state.runtime
            details = []
            for index, (source, context) in enumerate((
                    ("console", op_context("调试台提交")),
                    ("wx", op_context("WX 提交", role="wx")))):
                task_payload = json.loads(json.dumps(payload))
                task_payload["task_id"] = f"nav-{index}"
                task_payload["idempotency_key"] = f"nav-{index}"
                task_payload["correlation_id"] = f"nav-{index}"
                task = runtime.submit_task(task_payload, source=source,
                                           context=context)
                import time as _time
                for _ in range(100):
                    stored = runtime.store.get_task(task["task_id"])
                    if stored.get("status") in ("confirmed", "failed",
                                                "unknown"):
                        break
                    _time.sleep(0.05)
                assert stored["status"] == "confirmed"
                step_log = stored.get("step_log") or []
                details.append(step_log[-1].get("detail"))
            assert details[0] == details[1]  # 相同任务参数 → 完全相同 detail
            assert details[0]["contract_version"] == "wx-auto-navigation.v1"
