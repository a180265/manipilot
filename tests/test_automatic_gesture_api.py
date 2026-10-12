"""自动手势 API、权限、预算与默认关闭（规范第 7.4、12、22.12 节）。

覆盖：GET 资格结构、开关默认 false、写操作权限、发布证书后的 usable、
内置导航档案自举、top_proof_available、validate/auto-validate 去重、
能力分发（含多 stroke 预算）与未注册任务类型的明确失败。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from runtime import automatic_gestures as ag
from runtime.config import PlatformConfig
from runtime.runtime import ArmFleetRuntime
from tasks.plugin_api import TaskPlugin, make_step
from tests.helpers import make_device_configs, op_context, wait_terminal


def _program_fixture(gesture_id="scroll_up"):
    start, end = {
        "scroll_up": ((0.5, 0.8), (0.5, 0.6)),
        "scroll_down": ((0.5, 0.6), (0.5, 0.8)),
        "system_back_left": ((0.05, 0.5), (0.25, 0.5)),
        "system_back_right": ((0.95, 0.5), (0.75, 0.5)),
        "system_home": ((0.5, 0.95), (0.5, 0.65)),
    }[gesture_id]
    return {
        "program_version": "gesture-program.v1",
        "coordinate_space": "calibration_root_normalized.v1",
        "strokes": [{"points": [{"x": start[0], "y": start[1], "t_ms": 0},
                                  {"x": end[0], "y": end[1], "t_ms": 2000}],
                     "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0}],
    }


def _template_fixture(binding, kind="top", *, evidence_root, evidence_store=None,
                      source=None, screen_plane=None, regions=None):
    """Persist real synthetic PNGs and metadata; never bypass integrity checks."""
    import hashlib
    import cv2
    import numpy as np
    from runtime.evidence import EvidenceStore
    from runtime.store import Store

    store = None
    if evidence_store is None:
        store = Store(evidence_root.parent / "fleet.db")
        evidence_store = EvidenceStore(store, evidence_root)
    if source is None:
        source = np.random.default_rng(8).integers(
            0, 256, (binding["frame_height"], binding["frame_width"]), dtype=np.uint8)
    if screen_plane is None:
        screen_plane = source
    data = cv2.imencode(".png", source)[1].tobytes()

    class Camera:
        device_id = binding["camera_id"]

        def capture_sync(self):
            return {"image": data, "sha256": hashlib.sha256(data).hexdigest(),
                    "captured_at": "2026-09-15T10:00:00Z",
                    "width": binding["frame_width"], "height": binding["frame_height"]}

    refs = []
    for seq in (1, 2, 3):
        record = evidence_store.capture_screenshot(
            device_id=binding["device_id"], camera=Camera(), task_id=f"navtpl-{kind}",
            label="合成模板原图")
        refs.append({"kind": "image", "evidence_id": record["evidence_id"],
                     "sha256": record["sha256"], "captured_at": record["captured_at"],
                     "camera_id": binding["camera_id"], "frame_seq": seq,
                     "calibration_fingerprint": binding["calibration_fingerprint"]})
    anchors = []
    regions = regions or [{"left_px": 10 + index * 30, "top_px": 10,
                           "width_px": 20, "height_px": 20}
                          for index in range(1 if kind == "top" else 3)]
    for region in regions:
        x, y, w, h = (region[k] for k in ("left_px", "top_px", "width_px", "height_px"))
        crop = evidence_store.save_file_evidence(
            device_id=binding["device_id"], task_id=f"navtpl-{kind}",
            data=cv2.imencode(".png", screen_plane[y:y + h, x:x + w])[1].tobytes(),
            label="合成模板裁剪", suffix=".png")
        anchors.append({"region": region,
                        "frame_evidence": refs, "crop_sha256": crop["sha256"],
                        "evidence_id": crop["evidence_id"]})
    if store is not None:
        store._conn.close()
    return {
        "contract_version": "navigation-template.v1", "template_id": kind,
        "version": 1, "device_id": "arm-01", "kind": kind,
        "binding": dict(binding), "layout_version": "wechat-list-layout.v1",
        "anchor_evidence": anchors,
        "restore_target": None, "confirmed_at": "2026-09-15T10:00:00Z",
        "confirmation_source": "debug_explicit_confirmation",
    }


def _run_fixture(bundle: dict, data_root: Path | None = None) -> dict:
    from drivers import gesture_program as gp
    from drivers import screen_gestures as sg
    frame = gp.load_coordinate_frame("arm-01", data_root) if data_root else None
    return {
    "run_id": "run-api-1",
    "device_id": "arm-01",
    "gesture_rules_version": "gesture-rules.v1",
    "coordinate_frame_hash": (frame or {}).get("coordinate_frame_hash", "b" * 64),
    "input_binding": sg.binding_snapshot(bundle),
    "action_results": [
        {"gesture_id": "scroll_up", "state": "valid",
         "program": _program_fixture(),
         "validation_status": "three_passes", "trial_ids": ["t1", "t2", "t3"],
         "controller_status": "completed", "contact_status": "continuous",
         "effect_status": "verified", "reason_code": None, "evidence_refs": []},
    ],
}


def _seed_calibration(tmp_path: Path, *, automatic: bool = False) -> dict:
    from drivers import calibration_bundle as cb
    from drivers import gesture_program as gp
    from drivers import screen_gestures as sg
    from tests.test_screen_gesture_driver import gesture_bundle
    bundle = gesture_bundle()
    if automatic:
        bundle["autoCalibration"] = {"state": "PROMOTED", "runId": "run-api-1"}
        bundle["modelLocks"] = {
            key: {"state": cb.MODEL_LOCK_LOCKED, "version": 1}
            for key in ("cameraToScreen", "armAffine")}
        bundle["validation"] = {
            "passed": True, "runId": "run-api-1", "tolerancePx": 10.0,
            "points": [{"targetId": f"validation-{index}", "passed": True,
                        "touchTargetErrorPx": 1.0} for index in range(1, 6)]}
        # ready 档案语义=输入域 E 验收全过（含长按清除）；deleteValidated=False
        # 会触发 delete_parameters_not_validated 门禁令 calibrationReady 恒 False。
        bundle["z"]["deleteValidated"] = True
        bundle["safePark"] = {
            "confirmed": True, "parkX": 0.0, "parkY": 0.0, "parkZ": -8}
    cb.save_bundle(bundle, tmp_path / "data")
    frame = gp.build_coordinate_frame(
        binding=sg.binding_snapshot(bundle), calibration_run_id="run-api-1",
        root_width_px=bundle["screen"]["screenWidth"],
        root_height_px=bundle["screen"]["screenHeight"],
        display_width_px=1080, display_height_px=2400,
        root_origin_display_px=[0, 80], density=3.0,
        source_client_instance_id="0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001")
    gp.save_coordinate_frame("arm-01", frame, data_root=tmp_path / "data")
    return bundle


def _client(tmp_path: Path, *, n_devices: int = 1) -> TestClient:
    config = PlatformConfig({
        "db_path": str(tmp_path / "fleet.db"),
        "evidence_dir": str(tmp_path / "evidence"),
        "plugin_dir": str(tmp_path / "plugins"),
        "hardware_mode": "simulation",
    }, root=tmp_path)
    runtime = ArmFleetRuntime(config)
    app = create_app(runtime)
    app.state.runtime_factory = lambda: runtime
    configs = make_device_configs(
        n_devices, binding_revision=7,
        daemon_addr="127.0.0.1:8795", expected_frame_width=1280,
        expected_frame_height=720)
    # F1 一对一约束：daemon 地址/相机唯一标识不得多设备共享——夹具按序错开，
    # 否则第二台注册即 device_binding_conflict（2026-09-30 修复 98 例 ERROR）
    for index, cfg in enumerate(configs):
        cfg["daemon_addr"] = f"127.0.0.1:{8795 + index}"
        # arm-01 保持 uvc-01 与种子档案一致，其余依次错开
        cfg["camera_device_unique_id"] = f"uvc-{index + 1:02d}"
    app.state.device_configs = configs
    return TestClient(app)


_seeded_bundle = None


@pytest.fixture()
def seeded(tmp_path: Path):
    global _seeded_bundle
    _seeded_bundle = _seed_calibration(tmp_path, automatic=True)
    with _client(tmp_path) as client:
        client.app.state.runtime.devices._devices["arm-01"]["daemon_session"] = (
            _seeded_bundle["daemonSession"])
        yield client, tmp_path


class TestGestureEndpoints:
    def test_fresh_device_profile_missing(self, seeded):
        client, _ = seeded
        response = client.get("/v1/devices/arm-01/automatic-gestures")
        assert response.status_code == 200
        body = response.json()
        assert body["contract_version"] == "automatic-gesture-profile.v1"
        assert body["binding"] is None
        assert body["actions"]["scroll_up"]["usable"] is False
        codes = [r["code"] for r in body["blocking_reasons"]]
        assert "profile_missing" in codes

    def test_settings_have_no_switches(self, seeded):
        """2026-09-30 拍板：开关全部删除，可用性由校准门禁自动决定。"""
        client, _ = seeded
        body = client.get("/v1/devices/arm-01/automatic-gesture-settings").json()
        for key in ("automatic_gestures_enabled", "auto_validate_after_calibration",
                    "automatic_navigation_enabled", "gesture_program_enabled"):
            assert key not in body
        assert body["gesture_rules_version"] == "gesture-rules.v1"
        assert body["calibrationReady"] is True

    def test_switch_writes_rejected_regardless_of_calibration(self, seeded):
        """开关已删（2026-09-30 拍板）：写开关一律 400，无任何旁路；
        calibrationReady 只反映校准事实，不再是开启开关的前置。"""
        from drivers import calibration_bundle as cb
        client, tmp_path = seeded
        bundle = cb.load_bundle("arm-01", tmp_path / "data")
        bundle["validation"]["points"][0]["passed"] = False
        cb.save_bundle(bundle, tmp_path / "data")
        assert client.get("/v1/devices/arm-01/automatic-gesture-settings").json()[
            "calibrationReady"] is False
        for key in ("automatic_gestures_enabled", "automatic_navigation_enabled",
                    "gesture_program_enabled", "auto_validate_after_calibration"):
            response = client.put(
                "/v1/devices/arm-01/automatic-gesture-settings",
                json={"updates": {key: True}, **op_context("尝试写已删除的开关")})
            assert response.status_code in (400, 422), (key, response.text)
        assert ag.load_settings("arm-01", tmp_path / "data").keys()             == {"gesture_rules_version"}
        bundle["validation"]["points"][0]["passed"] = True
        cb.save_bundle(bundle, tmp_path / "data")
        assert client.get("/v1/devices/arm-01/automatic-gesture-settings").json()[
            "calibrationReady"] is True

    def test_legacy_motion_ready_counts_as_calibrated_ready(self, seeded):
        """motion_ready（延后输入验收）即视为校准就绪；开关写入仍被拒绝。"""
        from drivers import calibration_bundle as cb
        client, tmp_path = seeded
        bundle = cb.load_bundle("arm-01", tmp_path / "data")
        bundle["status"] = cb.MOTION_READY_STATUS
        bundle["acceptance"]["deferred"] = {"debug": True}
        cb.save_bundle(bundle, tmp_path / "data")
        assert client.get("/v1/devices/arm-01/automatic-gesture-settings").json()[
            "calibrationReady"] is True
        response = client.put(
            "/v1/devices/arm-01/automatic-gesture-settings",
            json={"updates": {"automatic_navigation_enabled": True},
                  **op_context("旧自动档案资格")})
        assert response.status_code in (400, 422)

    def test_settings_update_requires_context(self, seeded):
        client, _ = seeded
        response = client.put("/v1/devices/arm-01/automatic-gesture-settings",
                              json={"updates": {"automatic_gestures_enabled": True}})
        assert response.status_code in (400, 403)

    def test_settings_update_always_rejected(self, seeded):
        """开关已删：即使带合法操作者上下文，任何 updates 都被拒绝且不落盘。"""
        client, tmp_path = seeded
        response = client.put(
            "/v1/devices/arm-01/automatic-gesture-settings",
            json={"updates": {"automatic_gestures_enabled": True},
                  **op_context("开启自动手势")})
        assert response.status_code in (400, 422)
        stored = ag.load_settings("arm-01", tmp_path / "data")
        assert stored.keys() == {"gesture_rules_version"}

    def test_publish_makes_action_usable_and_creates_navigation(self, seeded):
        client, tmp_path = seeded
        run = _run_fixture(_seeded_bundle, tmp_path / "data")
        ag.publish_run_results("arm-01", run, data_root=tmp_path / "data")
        body = client.get("/v1/devices/arm-01/automatic-gestures").json()
        assert body["actions"]["scroll_up"]["usable"] is True
        assert body["actions"]["scroll_up"]["validation_status"] == "three_passes"
        assert body["actions"]["scroll_down"]["usable"] is False
        assert body["binding"]["coordinate_frame_hash"] == run["coordinate_frame_hash"]
        nav = client.get(
            "/v1/devices/arm-01/automatic-navigation/wx_chat_list_auto")
        assert nav.status_code == 200
        nav_body = nav.json()
        assert nav_body["navigation_limits"] == {
            "max_steps": 8, "max_physical_actions": 8, "timeout_ms": 90000,
            "max_boundary_probes": 2}
        assert nav_body["actions"]["search_next"]["usable"] is True
        assert nav_body["actions"]["search_previous"]["usable"] is False
        assert nav_body["actions"]["return_top"]["top_proof_available"] is False
        missing = client.get(
            "/v1/devices/arm-01/automatic-navigation/other-profile")
        assert missing.status_code == 404

    def test_top_template_enables_top_proof(self, seeded):
        client, tmp_path = seeded
        run = _run_fixture(_seeded_bundle, tmp_path / "data")
        run["action_results"].append(
            {"gesture_id": "scroll_down", "state": "valid",
             "program": _program_fixture("scroll_down"),
             "validation_status": "three_passes", "trial_ids": ["t4", "t5", "t6"],
             "controller_status": "completed", "contact_status": "continuous",
             "effect_status": "verified", "reason_code": None,
             "evidence_refs": []})
        ag.publish_run_results("arm-01", run, data_root=tmp_path / "data")
        top_dir = tmp_path / "data" / "devices" / "arm-01" / "navigation_templates"
        top_dir.mkdir(parents=True, exist_ok=True)
        (top_dir / "top.json").write_text(json.dumps(
            _template_fixture(run["input_binding"], evidence_root=tmp_path / "evidence",
                              evidence_store=client.app.state.runtime.evidence)), "utf-8")
        nav = client.get(
            "/v1/devices/arm-01/automatic-navigation/wx_chat_list_auto").json()
        assert nav["actions"]["return_top"]["top_proof_available"] is True

    def test_validate_without_plugin_registered_fails_cleanly(self, seeded):
        client, _ = seeded
        response = client.post("/v1/devices/arm-01/automatic-gestures/validate",
                               json=op_context("手动自检"))
        assert response.status_code == 400
        assert response.json()["error"]["key"] == "plugin_task_type_unknown"

    def test_auto_validate_switch_removed(self, seeded):
        """2026-09-30 用户拍板：删除「校准后自动测试手势」开关（该手机手势页
        心跳不可用，自动排队只会堆积失败记录）。设置更新拒绝未知键，
        auto-validate 端点整体移除。"""
        client, tmp_path = seeded
        response = client.put(
            "/v1/devices/arm-01/automatic-gesture-settings",
            json={"updates": {"auto_validate_after_calibration": True},
                  **op_context("尝试开启已删除的开关")})
        assert response.status_code in (400, 422)
        assert "auto_validate_after_calibration" not in client.get(
            "/v1/devices/arm-01/automatic-gesture-settings").json()
        # 同日拍板：其余三个开关一并删除——校准完成即可用，无人工开启环节
        assert not hasattr(ag, "auto_validate_eligible")
        assert not hasattr(ag, "auto_validate_release")
        assert ag.DEFAULT_SETTINGS.keys() == {"gesture_rules_version"}
        body = client.get("/v1/devices/arm-01/automatic-gesture-settings").json()
        for key in ("automatic_gestures_enabled", "automatic_navigation_enabled",
                    "gesture_program_enabled"):
            assert key not in body
        assert client.post("/v1/devices/arm-01/automatic-gestures/auto-validate",
                           json=op_context("已删除的端点")).status_code in (404, 405)

    def test_validate_submit_vision_default_for_scroll_only(self, seeded):
        """2026-09-30 拍板：纯滑动自检默认视觉发证（去页面回报依赖）。

        提交端点对纯滑动集默认置 vision_only=true；显式覆盖生效；
        混合动作集保持页面采样路径（false）。
        """
        client, _ = seeded
        rt_ = client.app.state.runtime
        captured = []
        original = rt_.submit_task

        def spy(payload, **kwargs):
            captured.append(payload)
            return original(payload, **kwargs)

        rt_.submit_task = spy
        try:
            client.post("/v1/devices/arm-01/automatic-gestures/validate",
                        json={**op_context("视觉自检"),
                              "actions": ["scroll_up", "scroll_down"]})
            assert captured[-1]["payload"]["vision_only"] is True
            client.post("/v1/devices/arm-01/automatic-gestures/validate",
                        json={**op_context("页面夹具"), "vision_only": False,
                              "actions": ["scroll_up"]})
            assert captured[-1]["payload"]["vision_only"] is False
            client.post("/v1/devices/arm-01/automatic-gestures/validate",
                        json={**op_context("混合默认页面"),
                              "actions": ["scroll_up", "double_tap"]})
            assert captured[-1]["payload"]["vision_only"] is False
        finally:
            rt_.submit_task = original


class SelfTestProbe(TaskPlugin):
    """最小自检插件：走 device.automatic_gesture_profile 读资格。"""

    manifest = {
        "plugin_id": "gesture-validation-adapter",
        "name_display": "手势自检",
        "version": "1.0.0", "vendor": "t", "description": "t",
        "entry_module": "plugin.py",
        "task_types": [{
            "task_type": "gesture_self_test",
            "name_display": "校准后自动手势自检",
            "confirmed_display": "已完成",
            "capability": "gesture.self_test",
            "input_schema": {"type": "object"},
        }],
        "capabilities": ["device.automatic_gesture_profile",
                         "arm.execute_gesture_program"],
        "resources": {"devices": {"mode": "assigned"}, "camera": False,
                      "input": False, "arm": True, "external_services": [],
                      "data_scope": {}},
        "evidence": {"screenshot_required": True, "home_check_required": False},
        "limits": {"max_actions_per_task": 2},
    }

    async def validate(self, task, device_context):
        return {"ok": True}

    async def plan(self, task, device_context):
        return [make_step(1, "probe", "探针", is_action=False,
                          required_capabilities=[], evidence_required=False)]

    async def execute_step(self, step, ctx):
        view = await ctx.call_capability("device.automatic_gesture_profile",
                                        {"op": "read"})
        ctx.state_set("gestures_view", view)
        return {"outcome": "success", "message": "ok",
                "detail": {"contract_version": "gesture-validation.v1",
                           "run_id": "run-x"}}


class TestCapabilityDispatch:
    def _client(self, tmp_path: Path, plugin, manifest) -> TestClient:
        config = PlatformConfig({
            "db_path": str(tmp_path / "fleet.db"),
            "evidence_dir": str(tmp_path / "evidence"),
            "plugin_dir": str(tmp_path / "plugins"),
            "hardware_mode": "simulation",
        }, root=tmp_path)
        runtime = ArmFleetRuntime(config)
        runtime.plugins.register_instance_for_tests(plugin, manifest)
        app = create_app(runtime)
        app.state.runtime_factory = lambda: runtime
        app.state.device_configs = make_device_configs(1)
        return TestClient(app)

    @staticmethod
    def _wait_status(client: TestClient, task_id: str,
                     timeout: float = 6.0) -> dict:
        import time as _time
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            body = client.get(f"/v1/tasks/{task_id}").json()
            if body.get("status") in ("confirmed", "failed", "unknown",
                                      "cancelled", "expired"):
                return body
            _time.sleep(0.05)
        return client.get(f"/v1/tasks/{task_id}").json()

    def _submit(self, client: TestClient, suffix: str) -> dict:
        response = client.post("/v1/tasks", json={
            "task_type": "gesture_self_test",
            "contract_version": "gesture-validation.v1",
            "device_id": "arm-01", "task_id": f"t-{suffix}",
            "idempotency_key": f"k-{suffix}",
            "payload": {"binding": {}, "actions": ["scroll_up"],
                        "rules_version": "gesture-rules.v1"},
            "expires_at": "2099-01-01T00:00:00Z",
            "correlation_id": f"t-{suffix}", "operator_id": "操作员甲",
            "reason": "能力读取探针",
        })
        assert response.status_code == 200, response.text
        return response.json()

    def test_capability_read_via_plugin(self, tmp_path):
        _seed_calibration(tmp_path)
        with self._client(tmp_path, SelfTestProbe(),
                          SelfTestProbe.manifest) as client:
            self._submit(client, "api-1")
            view = self._wait_status(client, "t-api-1")
            assert view["status"] == "confirmed"

    # test_execute_program_blocked_when_disabled 已随开关删除（2026-09-30 拍板）：
    # 执行资格由校准门禁与物理前置检查承担，不再有设置开关拒绝语义。

    def test_multi_stroke_budget_enforced(self, tmp_path):
        from drivers import gesture_program as gp
        _seed_calibration(tmp_path)
        frame = gp.load_coordinate_frame("arm-01", tmp_path / "data")
        from drivers import calibration_bundle as cb
        bundle = cb.load_bundle("arm-01", tmp_path / "data")

        def three_stroke_program():
            points = [{"x": .5, "y": .85 - i * 0.004, "t_ms": i * 150}
                      for i in range(13)]
            stroke = {"points": points, "hold_before_ms": 0,
                      "hold_after_ms": 0, "gap_after_ms": 0}
            return {"program_version": "gesture-program.v1",
                    "coordinate_space": "calibration_root_normalized.v1",
                    "strokes": [dict(stroke) for _ in range(3)]}

        program = three_stroke_program()
        plan = gp.plan_program(program, bundle, frame,
                               limits={"path_feature_available": True})

        captured = {}

        class BudgetProbe(SelfTestProbe):
            async def execute_step(self, step, ctx):
                from tasks.plugin_api import PluginViolationError
                try:
                    await ctx.call_capability("arm.execute_gesture_program",
                                              {"plan": plan})
                except PluginViolationError as exc:
                    captured["violation"] = str(exc)
                    return {"outcome": "failed", "message": str(exc)}
                except Exception as exc:  # 驱动可能尚无 path1 支持
                    captured.setdefault("driver", str(exc))
                    return {"outcome": "failed", "message": str(exc)}
                return {"outcome": "success", "message": "不应到达"}

        with self._client(tmp_path, BudgetProbe(), BudgetProbe.manifest) as client:
            self._submit(client, "api-3")
            view = self._wait_status(client, "t-api-3")
            assert view["status"] == "failed"
        # fake 驱动已支持 path1：预算违反路径是确定的，不再四路 OR 兜底
        assert "动作上限" in captured.get("violation", ""), captured


class TestApiErrorContract:
    """评审 P1-8~11：登记过的错误键必须以 4xx 送达，不得落兜底 500。"""

    def test_unknown_device_returns_404(self, tmp_path):
        with _client(tmp_path) as client:
            resp = client.get("/v1/devices/arm-nope/automatic-gestures")
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["key"] == "device_not_found"

    def test_unusable_calibration_returns_409_registered_key(self, tmp_path):
        from drivers import calibration_bundle as cb
        from tests.test_screen_gesture_driver import gesture_bundle

        with _client(tmp_path) as client:
            broken = gesture_bundle()
            broken["status"] = "fitted"  # 未过验收 → binding_snapshot 拒绝
            cb.save_bundle(broken, tmp_path / "data")
            resp = client.post("/v1/devices/arm-01/automatic-gestures/validate",
                               json=op_context())
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["key"] == "calibration_unavailable"

    def test_close_missing_navigation_run_returns_404(self, tmp_path):
        with _client(tmp_path) as client:
            resp = client.post(
                "/v1/devices/arm-01/navigation-runs/"
                "00000000-0000-4000-8000-000000000000/close",
                json={**op_context(), "reason": "caller_finished"})
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["key"] == "navigation_run_not_found"

    def test_corrupt_settings_returns_409_not_500(self, tmp_path):
        from runtime import automatic_gestures as ag

        with _client(tmp_path) as client:
            data_root = tmp_path / "data"
            settings_file = ag.settings_path("arm-01", data_root)
            settings_file.parent.mkdir(parents=True, exist_ok=True)
            settings_file.write_text("{ 不是 JSON", encoding="utf-8")
            resp = client.put(
                "/v1/devices/arm-01/automatic-gesture-settings",
                json={**op_context(),
                      "updates": {"automatic_gestures_enabled": True}})
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["key"] == "gesture_settings_invalid"


class TestNavigationErrorContract:
    """R2 N1/N2：导航域错误键按语义送达，损坏轮/坏 UUID 不落 internal_error。"""

    RUN_UUID = "00000000-0000-4000-8000-0000000000aa"

    def test_close_invalid_uuid_is_clean_404(self, tmp_path):
        """close 对非法 UUID（必然无文件）走 not_found 404；
        navigation_run_invalid 键登记服务 start 流程（插件可达路径）。"""
        with _client(tmp_path) as client:
            resp = client.post(
                f"/v1/devices/arm-01/navigation-runs/not-a-uuid/close",
                json={**op_context(), "reason": "caller_finished"})
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["key"] == "navigation_run_not_found"

    def test_close_corrupted_run_reports_registered_key_not_internal(self, tmp_path):
        from adapters.uinav import automatic_navigation as nav

        with _client(tmp_path) as client:
            path = nav.navigation_run_path("arm-01", self.RUN_UUID,
                                           tmp_path / "data")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{ 不是 JSON", encoding="utf-8")
            resp = client.post(
                f"/v1/devices/arm-01/navigation-runs/{self.RUN_UUID}/close",
                json={**op_context(), "reason": "caller_finished"})
        assert resp.status_code == 500  # 数据损坏属服务端问题，但错误键必须可读
        assert resp.json()["error"]["key"] == "navigation_run_corrupted"

    def test_missing_navigation_profile_uses_dedicated_404_key(self, tmp_path):
        with _client(tmp_path) as client:
            resp = client.get(
                "/v1/devices/arm-01/automatic-navigation/no-such-profile")
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["key"] == "navigation_profile_not_found"


class TestAutoValidateDedupRelease:
    def test_release_restores_eligibility_after_failed_submit(self, tmp_path):
        """R1 P2：提交失败必须归还去重键，否则该校准的一次资格被吞。"""
        from drivers import calibration_bundle as cb
        from drivers import screen_gestures as sg
        from tests.test_screen_gesture_driver import gesture_bundle

        bundle = gesture_bundle()
        cb.save_bundle(bundle, tmp_path / "data")
        fingerprint = sg.binding_snapshot(bundle)["calibration_fingerprint"]
        data_root = tmp_path / "data"
        # 2026-09-30 拍板：auto_validate 机制整体删除（开关与去重），
        # 自检改为手动/视觉路径触发。
        assert not hasattr(ag, "auto_validate_eligible")
        assert not hasattr(ag, "auto_validate_release")
        assert "auto_validate_after_calibration" not in ag.DEFAULT_SETTINGS


def test_publish_zero_valid_actions_is_visible_not_silent(tmp_path, caplog):
    """C1（2026-10-01 backlog）：v7 空壳成因——失败 run 也递增版本+覆写
    绑定但零动作入证。发布本身是设计（run 结果+绑定快照供下次提交对齐），
    但零入证必须显式留痕（published_actions=0 + warning 日志），不得静默。"""
    import logging
    bundle = _seed_calibration(tmp_path, automatic=True)
    data_root = tmp_path / "data"
    run = _run_fixture(bundle, data_root)
    for row in run["action_results"]:
        row["state"] = "unverified"
    with caplog.at_level(logging.WARNING, logger="runtime.automatic_gestures"):
        profile = ag.publish_run_results("arm-01", run, data_root=data_root,
                                         base_version=0)
    assert profile["published_actions"] == 0
    # 全部动作保留原状态（非 valid 不伪造验收时间）
    assert all((entry or {}).get("validated_at") is None
               for entry in profile["actions"].values())
    assert any("零动作入证" in record.message for record in caplog.records)
