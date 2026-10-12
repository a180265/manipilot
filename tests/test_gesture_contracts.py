"""D1：自动手势闭环新契约与设计常量。

按《自动手势闭环与WX导航完整实施规范-2026-09-15》第 5/6/12/22 节：
- 契约文件成对存在（.md + .schema.json），Draft 2020-12 合法；
- 关键对象 additionalProperties=false，非法示例必须被拒绝；
- configs/gesture_defaults.json 冻结第 8/9/10 节设计常量。
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")


CONTRACTS = Path(__file__).resolve().parents[1] / "api" / "contracts"
DEFAULTS = Path(__file__).resolve().parents[1] / "configs" / "gesture_defaults.json"

EXPECTED_CONTRACTS = (
    "gesture-capture.v1",
    "gesture-program.v1",
    "gesture-validation.v1",
    "wx-auto-navigation.v1",
    "automatic-gesture-profile.v1",
    "navigation-template.v1",
    "probe-path-observation.v1",
)


def load_schema(name: str) -> dict:
    return json.loads((CONTRACTS / f"{name}.schema.json").read_text(encoding="utf-8"))


def validator(name: str, fragment: str | None = None):
    schema = load_schema(name)
    if fragment:
        schema = {"$ref": f"#/$defs/{fragment}", "$defs": schema["$defs"]}
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


@pytest.mark.parametrize("name", EXPECTED_CONTRACTS)
def test_contract_files_exist(name: str):
    for suffix in (".md", ".schema.json"):
        assert (CONTRACTS / f"{name}{suffix}").exists(), f"缺少契约文件 {name}{suffix}"


@pytest.mark.parametrize("name", EXPECTED_CONTRACTS)
def test_contract_schema_is_valid_draft202012(name: str):
    jsonschema.Draft202012Validator.check_schema(load_schema(name))


# ---------- gesture-capture.v1 ----------

def _page_metadata(**override):
    page = {
        "pageId": "gesture-capture",
        "pageGeneration": 3,
        "resumed": True, "hasFocus": True,
        "rotation": 0,
        "rootWidthPx": 1080, "rootHeightPx": 2280,
        "displayWidthPx": 1080, "displayHeightPx": 2400,
        "rootOriginDisplayPx": [0, 120],
        "density": 3.0,
        "systemGestureInsets": {"left": 20, "top": 40, "right": 20, "bottom": 60},
        "systemBarInsets": {"left": 0, "top": 80, "right": 0, "bottom": 60},
        "legacyCalibrationSessionId": None,
        "coordinateSpaceVersion": 2,
    }
    page.update(override)
    return page


def _client_hello(**override):
    hello = {
        "contractVersion": "gesture-capture.v1",
        "deviceId": "ime-01",
        "clientInstanceId": "0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001",
        "heartbeatSeq": 1,
        "apkVersionCode": 7,
        "capabilities": ["gesture_trace.v1", "scroll_fixture.v1"],
        "page": _page_metadata(),
    }
    hello.update(override)
    return hello


class TestGestureCapture:
    def test_client_hello_accepts_legal(self):
        validator("gesture-capture.v1", "ClientHello").validate(_client_hello())

    def test_client_hello_rejects_unknown_field(self):
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-capture.v1", "ClientHello").validate(
                _client_hello(extra=1))

    def test_client_hello_rejects_bad_capability(self):
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-capture.v1", "ClientHello").validate(
                _client_hello(capabilities=["mind_reading.v1"]))

    def test_page_metadata_requires_legacy_fields(self):
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-capture.v1", "PageMetadata").validate({
                key: value for key, value in _page_metadata().items()
                if key not in ("legacyCalibrationSessionId", "coordinateSpaceVersion")})

    def test_trace_consts_and_end_reason(self):
        base = {
            "contractVersion": "gesture-capture.v1",
            "sessionId": "6b9f7c2e-1f4a-4d5e-9c01-2f2f2e08c002",
            "trialId": "7c0f8d3f-2a5b-4e6f-8d12-2f2f2e08c003",
            "clientInstanceId": "0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001",
            "nonce": "a" * 64,
            "traceId": "8d1f9e4a-3b6c-4f70-9e23-2f2f2e08c004",
            "pageGeneration": 3,
            "resetId": "9e2faf5b-4c7d-4a81-8f34-2f2f2e08c005",
            "coordinateSpace": "calibration_root_px.v1",
            "clockDomain": "android_uptime_ms",
            "events": [
                {"index": 0, "contactIndex": 0, "action": "down", "pointerId": 0,
                 "tMs": 0, "xPx": 540, "yPx": 1710, "historical": False},
                {"index": 1, "contactIndex": 0, "action": "move", "pointerId": 0,
                 "tMs": 40, "xPx": 540, "yPx": 900, "historical": False},
                {"index": 2, "contactIndex": 0, "action": "up", "pointerId": 0,
                 "tMs": 120, "xPx": 540, "yPx": 684, "historical": False},
            ],
            "endReason": "up",
            "droppedSampleCount": 0,
            "scroll": {
                "beforeY": 1200, "afterY": 200, "minY": 0, "maxY": 4480,
                "contentHeightPx": 6400, "viewportHeightPx": 1920, "rowHeightPx": 192,
                "firstVisibleRowBefore": 6, "firstVisibleRowAfter": 1,
                "rowOffsetBeforePx": 48, "rowOffsetAfterPx": 8,
                "settled": True, "settleWindowMs": 300,
                "resetCounterBefore": 1, "resetCounterAfter": 1,
                "boundaryBefore": "neither", "boundaryAfter": "neither",
            },
            "recognition": None,
        }
        validator("gesture-capture.v1").validate(base)
        bad_reason = {**base, "endReason": "just_felt_like_it"}
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-capture.v1").validate(bad_reason)
        bad_action = {**base, "events": [{**base["events"][0], "action": "wiggle"}]}
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-capture.v1").validate(bad_action)

    def test_recognition_structure(self):
        trace = {
            "contractVersion": "gesture-capture.v1",
            "sessionId": "6b9f7c2e-1f4a-4d5e-9c01-2f2f2e08c002",
            "trialId": "7c0f8d3f-2a5b-4e6f-8d12-2f2f2e08c003",
            "clientInstanceId": "0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001",
            "nonce": "a" * 64,
            "traceId": "8d1f9e4a-3b6c-4f70-9e23-2f2f2e08c004",
            "pageGeneration": 3,
            "resetId": "9e2faf5b-4c7d-4a81-8f34-2f2f2e08c005",
            "coordinateSpace": "calibration_root_px.v1",
            "clockDomain": "android_uptime_ms",
            "events": [],
            "endReason": "timeout",
            "droppedSampleCount": 0,
            "scroll": None,
            "recognition": {
                "kind": "double_tap", "recognized": True,
                "firstUpMs": 40, "secondDownMs": 90,
                "systemDoubleTapTimeoutMs": 300, "systemDoubleTapSlopPx": 100,
            },
        }
        validator("gesture-capture.v1").validate(trace)
        with pytest.raises(jsonschema.ValidationError):
            bad = {**trace, "recognition": {**trace["recognition"], "extra": 1}}
            validator("gesture-capture.v1").validate(bad)

    def test_command_kinds_are_closed(self):
        command = {
            "sessionId": "6b9f7c2e-1f4a-4d5e-9c01-2f2f2e08c002",
            "trialId": "7c0f8d3f-2a5b-4e6f-8d12-2f2f2e08c003",
            "commandId": "a4b0c1d2-3e4f-4a56-8b78-2f2f2e08c006",
            "commandSeq": 1,
            "clientInstanceId": "0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001",
            "calibrationFingerprint": "a" * 64,
            "coordinateFrame": {
                "schema_version": 1,
                "binding": {"device_id": "arm-01", "daemon_session": "s",
                            "camera_id": "cam-01", "camera_device_unique_id": "u",
                            "binding_revision": 1, "calibration_fingerprint": "a" * 64,
                            "frame_width": 1280, "frame_height": 720},
                "calibration_run_id": "run-abc123",
                "root_width_px": 1080, "root_height_px": 2280,
                "display_width_px": 1080, "display_height_px": 2400,
                "root_origin_display_px": [0, 120],
                "rotation": 0, "density": 3.0,
                "coordinate_space_version": 2,
                "source_client_instance_id": "0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001",
                "coordinate_frame_hash": "b" * 64,
            },
            "nonce": "c" * 64,
            "kind": "reset_list",
            "payload": {"offsetFraction": 0.5},
            "issuedAt": "2026-09-15T10:00:00Z",
            "expiresAt": "2026-09-15T10:00:10Z",
        }
        validator("gesture-capture.v1", "Command").validate(command)
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-capture.v1", "Command").validate(
                {**command, "kind": "open_wechat"})

    def test_page_event_kinds_are_closed(self):
        event = {
            "sessionId": "6b9f7c2e-1f4a-4d5e-9c01-2f2f2e08c002",
            "trialId": "7c0f8d3f-2a5b-4e6f-8d12-2f2f2e08c003",
            "clientInstanceId": "0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001",
            "nonce": "a" * 64,
            "eventId": "b5c1d2e3-4f56-4a78-9c90-2f2f2e08c007",
            "eventSeq": 1,
            "pageGeneration": 3,
            "kind": "back_committed",
            "pageId": "gesture-child",
            "uptimeMs": 4567,
            "callbackSource": "on_back_invoked",
            "committed": True,
        }
        validator("gesture-capture.v1", "PageEvent").validate(event)
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-capture.v1", "PageEvent").validate(
                {**event, "kind": "app_clicked"})
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-capture.v1", "PageEvent").validate(
                {**event, "callbackSource": "magic"})


# ---------- gesture-program.v1 ----------

class TestGestureProgram:
    def test_legal_single_stroke(self):
        program = {
            "program_version": "gesture-program.v1",
            "coordinate_space": "calibration_root_normalized.v1",
            "strokes": [
                {"points": [{"x": 0.5, "y": 0.72, "t_ms": 0},
                            {"x": 0.5, "y": 0.35, "t_ms": 900}],
                 "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0},
            ],
        }
        validator("gesture-program.v1").validate(program)

    def test_rejects_more_than_four_strokes(self):
        stroke = {"points": [{"x": 0.5, "y": 0.5, "t_ms": 0}],
                  "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0}
        program = {"program_version": "gesture-program.v1",
                   "coordinate_space": "calibration_root_normalized.v1",
                   "strokes": [dict(stroke) for _ in range(5)]}
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-program.v1").validate(program)

    def test_rejects_sixty_five_points(self):
        points = [{"x": 0.5, "y": 0.5, "t_ms": index} for index in range(65)]
        program = {"program_version": "gesture-program.v1",
                   "coordinate_space": "calibration_root_normalized.v1",
                   "strokes": [{"points": points, "hold_before_ms": 0,
                                "hold_after_ms": 0, "gap_after_ms": 0}]}
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-program.v1").validate(program)

    def test_rejects_out_of_range_coordinate(self):
        program = {"program_version": "gesture-program.v1",
                   "coordinate_space": "calibration_root_normalized.v1",
                   "strokes": [{"points": [{"x": 1.5, "y": 0.5, "t_ms": 0}],
                                "hold_before_ms": 0, "hold_after_ms": 0,
                                "gap_after_ms": 0}]}
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-program.v1").validate(program)

    def test_rejects_forbidden_fields(self):
        program = {"program_version": "gesture-program.v1",
                   "coordinate_space": "calibration_root_normalized.v1",
                   "strokes": [{"points": [{"x": 0.5, "y": 0.5, "t_ms": 0}],
                                "hold_before_ms": 0, "hold_after_ms": 0,
                                "gap_after_ms": 0, "tapDepth": 2.0}]}
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-program.v1").validate(program)
        program2 = {**program, "strokes": [{**program["strokes"][0], "tapDepth": None}], "z": 1}
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-program.v1").validate(program2)

    def test_rejects_gap_over_limit(self):
        program = {"program_version": "gesture-program.v1",
                   "coordinate_space": "calibration_root_normalized.v1",
                   "strokes": [{"points": [{"x": 0.5, "y": 0.5, "t_ms": 0}],
                                "hold_before_ms": 0, "hold_after_ms": 0,
                                "gap_after_ms": 1001}]}
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-program.v1").validate(program)


# ---------- wx-auto-navigation.v1（任务契约） ----------

NAVIGATION_TASK = {
    "task_id": "nav-fixture-task-001",
    "task_type": "wx_auto_navigate",
    "contract_version": "wx-auto-navigation.v1",
    "device_id": "arm-fixture-01",
    "idempotency_key": "nav-fixture-step-001",
    "expires_at": "2026-09-01T00:01:00Z",
    "correlation_id": "nav-fixture-run-001",
    "payload": {
        "navigation_id": "f2f2e08c-3c43-4dca-86b3-6bb46d16c001",
        "navigation_profile": {
            "profile_id": "wx_chat_list_auto",
            "version": 1,
            "contract_version": "automatic-gesture-profile.v1",
        },
        "binding": {
            "device_id": "arm-fixture-01",
            "daemon_session": "fixture-session",
            "camera_id": "cam-fixture-01",
            "camera_device_unique_id": "fixture-camera",
            "binding_revision": 1,
            "calibration_fingerprint": "a" * 64,
            "frame_width": 1280,
            "frame_height": 720,
            "coordinate_frame_hash": "b" * 64,
            "gesture_profile_version": 1,
            "recipe_version": "gesture-rules.v1",
        },
        "action": "search_next",
        "step_index": 1,
    },
}


class TestWxAutoNavigation:
    def test_spec_22_5_example_validates(self):
        validator("wx-auto-navigation.v1").validate(NAVIGATION_TASK)

    def test_target_only_for_search(self):
        payload = {**NAVIGATION_TASK["payload"], "action": "return_top",
                   "target": {"visible_name": "客户甲",
                              "external_conversation_id": "ext-c1"}}
        with pytest.raises(jsonschema.ValidationError):
            validator("wx-auto-navigation.v1").validate(
                {**NAVIGATION_TASK, "payload": payload})

    def test_search_with_target_validates(self):
        payload = {**NAVIGATION_TASK["payload"],
                   "target": {"visible_name": "客户甲",
                              "external_conversation_id": "ext-c1"}}
        validator("wx-auto-navigation.v1").validate(
            {**NAVIGATION_TASK, "payload": payload})

    def test_session_ref_only_for_back_to_list(self):
        payload = {**NAVIGATION_TASK["payload"], "action": "home",
                   "session_ref": {"session_id": "session-1",
                                   "control_mode": "automatic",
                                   "external_conversation_id": "ext-c1"}}
        with pytest.raises(jsonschema.ValidationError):
            validator("wx-auto-navigation.v1").validate(
                {**NAVIGATION_TASK, "payload": payload})

    def test_back_to_list_requires_session_ref(self):
        payload = {**NAVIGATION_TASK["payload"], "action": "back_to_list"}
        with pytest.raises(jsonschema.ValidationError):
            validator("wx-auto-navigation.v1").validate(
                {**NAVIGATION_TASK, "payload": payload})

    def test_expires_at_must_be_valid_datetime(self):
        with pytest.raises(jsonschema.ValidationError):
            validator("wx-auto-navigation.v1").validate(
                {**NAVIGATION_TASK, "expires_at": None})

    def test_step_index_must_be_positive(self):
        payload = {**NAVIGATION_TASK["payload"], "step_index": 0}
        with pytest.raises(jsonschema.ValidationError):
            validator("wx-auto-navigation.v1").validate(
                {**NAVIGATION_TASK, "payload": payload})


# ---------- gesture-validation.v1（自检任务契约） ----------

class TestGestureValidation:
    def _task(self, **payload_override):
        payload = {
            "binding": {**NAVIGATION_TASK["payload"]["binding"],
                        "gesture_profile_version": 0},
            "actions": ["scroll_up", "scroll_down"],
            "rules_version": "gesture-rules.v1",
        }
        payload.update(payload_override)
        return {**NAVIGATION_TASK, "task_type": "gesture_self_test",
                "contract_version": "gesture-validation.v1",
                "idempotency_key": "selftest-001", "payload": payload}

    def test_legal_self_test(self):
        validator("gesture-validation.v1").validate(self._task())

    def test_actions_closed_set(self):
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-validation.v1").validate(
                self._task(actions=["scroll_up", "pinch_zoom"]))

    def test_actions_unique(self):
        with pytest.raises(jsonschema.ValidationError):
            validator("gesture-validation.v1").validate(
                self._task(actions=["scroll_up", "scroll_up"]))

    def test_profile_version_zero_allowed_only_here(self):
        # gesture_program / wx_auto_navigate 的 binding 不允许 gesture_profile_version=0
        zero_binding = {**NAVIGATION_TASK["payload"]["binding"],
                        "gesture_profile_version": 0}
        with pytest.raises(jsonschema.ValidationError):
            validator("wx-auto-navigation.v1").validate(
                {**NAVIGATION_TASK,
                 "payload": {**NAVIGATION_TASK["payload"], "binding": zero_binding}})


# ---------- automatic-gesture-profile.v1 ----------

class TestAutomaticGestureProfile:
    def _navigation_body(self, return_top_entry: dict) -> dict:
        def entry(usable: bool = True):
            return {"usable": usable, "blocking_reasons": []}
        return {
            "contract_version": "automatic-gesture-profile.v1",
            "device_id": "arm-01",
            "binding": None,
            "actions": {
                "search_next": entry(), "search_previous": entry(),
                "back_to_list": entry(), "home": entry(),
                "return_top": return_top_entry,
            },
            "blocking_reasons": [],
            "profile_id": "wx_chat_list_auto",
            "version": 1,
            "owner_plugin": "wx-wechat-adapter",
            "layout_version": "wechat-list-layout.v1",
            "navigation_limits": {"max_steps": 8, "max_physical_actions": 8,
                                  "timeout_ms": 90000, "max_boundary_probes": 2},
        }

    def test_top_proof_available_required_for_return_top(self):
        missing = {"usable": False,
                   "blocking_reasons": [{"code": "top_not_confirmed", "message": "缺少顶部证明"}]}
        with pytest.raises(jsonschema.ValidationError):
            validator("automatic-gesture-profile.v1", "navigationResponse").validate(
                self._navigation_body(missing))
        provided = {**missing, "top_proof_available": False}
        validator("automatic-gesture-profile.v1", "navigationResponse").validate(
            self._navigation_body(provided))

    def test_state_enums_closed(self):
        entry = {
            "gesture_id": "scroll_up", "version": 1,
            "state": "probably_fine", "state_display": "大概可以",
            "calculated": True, "controller_supported": True,
            "contact_validated": True, "effect_validated": True,
            "usable": False, "blocking_reasons": [],
        }
        with pytest.raises(jsonschema.ValidationError):
            validator("automatic-gesture-profile.v1", "actionEntry").validate(entry)


# ---------- navigation-template.v1 ----------

def _evidence_ref(seq: int) -> dict:
    return {"kind": "image", "evidence_id": f"ev-{seq}",
            "sha256": "c" * 64, "captured_at": "2026-09-15T10:00:00Z",
            "camera_id": "cam-01", "frame_seq": seq,
            "calibration_fingerprint": "a" * 64}


class TestNavigationTemplate:
    def test_preview_and_registration(self):
        validator("navigation-template.v1", "previewResponse").validate({
            "preview_id": "e6f7a8b9-0c1d-4e2f-8a43-2f2f2e08c008",
            "device_id": "arm-01",
            "binding": NAVIGATION_TASK["payload"]["binding"],
            "kind": "launcher",
            "frame_evidence": [_evidence_ref(10), _evidence_ref(11), _evidence_ref(12)],
            "candidates": [{"left_px": 100, "top_px": 200, "width_px": 60,
                            "height_px": 60}],
            "expires_at": "2026-09-15T10:01:00Z",
        })
        validator("navigation-template.v1", "registrationRequest").validate({
            "expected_version": 0,
            "preview_id": "e6f7a8b9-0c1d-4e2f-8a43-2f2f2e08c008",
            "confirmed": True,
            "anchor_regions": [
                {"left_px": 100, "top_px": 200, "width_px": 60, "height_px": 60},
                {"left_px": 300, "top_px": 200, "width_px": 60, "height_px": 60},
                {"left_px": 500, "top_px": 200, "width_px": 60, "height_px": 60},
            ],
            "restore_target": {
                "kind": "ocr_text",
                "visible_text": "K40输入法",
                "region": {"x": 0.3, "y": 0.8, "width": 0.4, "height": 0.1},
                "expected_page_id": "gesture-parent",
            },
        })

    def test_kind_closed(self):
        # kind 枚举闭合由 previewRequest.enum 拒绝（原用例给 registrationRequest
        # 塞 kind 字段，实际被 additionalProperties 拒——错因通过）
        with pytest.raises(jsonschema.ValidationError):
            validator("navigation-template.v1", "previewRequest").validate(
                {"kind": "drawer"})
        # 登记请求同样不接受客户端自带 kind（登记端 kind 由 template_id 推导）
        with pytest.raises(jsonschema.ValidationError):
            validator("navigation-template.v1", "registrationRequest").validate({
                "expected_version": 0, "preview_id": "x", "confirmed": True,
                "anchor_regions": [], "restore_target": None, "kind": "drawer"})


# ---------- probe-path-observation.v1 ----------

def test_probe_path_observation_contract():
    observation = {
        "contract_version": "probe-path-observation.v1",
        "plan_id": "plan-001",
        "binding": NAVIGATION_TASK["payload"]["binding"],
        "frame_seqs": [10, 11, 12, 13],
        "points": [
            {"frame_seq": 10, "x_camera_px": 640.0, "y_camera_px": 360.0, "at_ms": 0},
            {"frame_seq": 11, "x_camera_px": 640.0, "y_camera_px": 350.0, "at_ms": 120},
            {"frame_seq": 12, "x_camera_px": 640.0, "y_camera_px": 340.0, "at_ms": 240},
        ],
        "verified": True,
        "reason_code": None,
    }
    validator("probe-path-observation.v1").validate(observation)
    with pytest.raises(jsonschema.ValidationError):
        validator("probe-path-observation.v1").validate(
            {**observation, "points": []})


# ---------- configs/gesture_defaults.json ----------

class TestGestureDefaults:
    @pytest.fixture(scope="class")
    def defaults(self):
        return json.loads(DEFAULTS.read_text(encoding="utf-8"))

    def test_file_exists_and_has_rules_version(self, defaults):
        assert defaults["gesture_rules_version"] == "gesture-rules.v1"

    def test_scroll_candidates(self, defaults):
        candidates = defaults["scroll_self_test"]["candidates"]
        assert [(c["x"], c["y_start"], c["y_end"], c["feed"]) for c in candidates] == [
            (0.50, 0.75, 0.30, 2250),
            (0.50, 0.75, 0.30, 1500),
            (0.50, 0.75, 0.30, 3000),
        ]

    def test_list_pass_thresholds(self, defaults):
        thresholds = defaults["scroll_self_test"]["pass_thresholds"]
        assert thresholds["endpoint_tolerance_factor"] == 2
        assert thresholds["endpoint_tolerance_min_px"] == 12
        assert thresholds["net_displacement_min_ratio"] == 0.80
        assert thresholds["reverse_max_ratio"] == 0.10
        assert thresholds["scroll_min_ratio"] == 0.15

    def test_budgets(self, defaults):
        budgets = defaults["self_test_budget"]
        assert budgets["timeout_ms"] == 600000
        assert budgets["max_physical_actions"] == 36
        assert budgets["consecutive_passes_required"] == 3

    def test_navigation_limits(self, defaults):
        limits = defaults["navigation_limits"]
        assert limits == {"max_steps": 8, "max_physical_actions": 8,
                          "timeout_ms": 90000, "max_boundary_probes": 2}

    def test_trace_limits(self, defaults):
        trace = defaults["phone_trace_limits"]
        assert trace["max_events_per_trace"] == 512
        assert trace["max_contact_ms"] == 8000
        assert trace["max_body_bytes"] == 65536
        assert trace["min_move_sample_interval_ms"] == 16

    def test_command_and_heartbeat(self, defaults):
        timing = defaults["command_timing"]
        assert timing["expiry_ms"] == 10000
        assert timing["heartbeat_interval_ms"] == 500
        # N-2（R2 复审 2026-09-30）：心跳新鲜度唯一来源=代码常量
        # HEARTBEAT_FRESHNESS_S（WiFi 抖动放宽到 6.0s）；配置里不再有
        # 第二/第三个说法（旧 command_timing.heartbeat_freshness_ms=2000
        # 与行为矛盾，已删）。
        from adapters.screen_ime import gesture_capture as gc
        assert "heartbeat_freshness_ms" not in timing
        assert "heartbeat_freshness_ms" not in defaults
        assert gc.HEARTBEAT_FRESHNESS_S == 6.0

    def test_layout_rules(self, defaults):
        layout = defaults["wechat_list_layout"]
        assert layout["title_region"] == [0, 0, 1, 0.18]
        assert layout["tab_region"] == [0, 0.78, 1, 1]
        assert layout["min_tab_confidence"] == 0.85
        assert layout["list_top_gap_ratio"] == 0.03
        assert layout["list_bottom_gap_ratio"] == 0.04
        assert layout["list_left_ratio"] == 0.05
        assert layout["list_right_ratio"] == 0.95
        assert layout["min_list_height_ratio"] == 0.45

    def test_edge_gesture_candidates(self, defaults):
        back = defaults["system_back"]
        assert back["y_candidates"] == [0.50, 0.40, 0.60]
        assert back["inward_lengths"] == [0.20, 0.30]
        assert back["max_candidates"] == 6
        home = defaults["system_home"]
        assert home["x_candidates"] == [0.50, 0.35, 0.65]
        assert home["inward_lengths"] == [0.18, 0.28]

    def test_launcher_template_thresholds(self, defaults):
        launcher = defaults["launcher_template"]
        assert launcher["min_anchor_correlation"] == 0.92
        assert launcher["best_vs_second_margin"] == 0.05
        assert launcher["position_tolerance_ratio"] == 0.02
        assert launcher["search_window_ratio"] == 0.03
        assert launcher["min_anchor_size_px"] == 24
        assert launcher["min_anchor_stddev"] == 10
        assert launcher["stable_frames"] == 3
        assert launcher["frame_interval_ms"] == 300

    def test_optical_flow_params(self, defaults):
        flow = defaults["scroll_observation"]
        assert flow["min_inliers"] == 12
        assert flow["min_retained_ratio"] == 0.70
        assert flow["max_forward_backward_error_px"] == 1.5
        assert flow["min_displacement_ratio"] == 0.04
        assert flow["max_horizontal_ratio"] == 0.03
        assert flow["max_dy_mad_ratio"] == 0.02

    def test_business_adjustment_limits(self, defaults):
        adjust = defaults["business_scroll_adjustment"]
        assert adjust["max_length_multiplier"] == 1.2
        assert adjust["max_length_ratio_of_list"] == 0.55
        assert adjust["allowed_adjustments"] == 1

    def test_device_flags_removed(self, defaults):
        """开关已删（2026-09-30 拍板）：默认配置与运行时设置都不再含开关。"""
        from runtime import automatic_gestures as ag
        assert "device_flags_default" not in defaults
        for key in ("automatic_gestures_enabled", "auto_validate_after_calibration",
                    "automatic_navigation_enabled", "gesture_program_enabled"):
            assert key not in ag.DEFAULT_SETTINGS


def test_hold_maximum_bound_to_daemon_constant():
    """R2 N8：schema 的 hold 上限与 PATH1_HOLD_MS_MAX 双源硬编码，
    用契约测试绑定，漂移即在测试期暴露。"""
    import json
    from pathlib import Path
    from drivers.daemon_protocol import PATH1_HOLD_MS_MAX

    schema = json.loads((Path(__file__).parents[1] / "api" / "contracts"
                         / "gesture-program.v1.schema.json").read_text())
    stroke_props = schema["properties"]["strokes"]["items"]["properties"]
    for key in ("hold_before_ms", "hold_after_ms"):
        assert stroke_props[key]["maximum"] == PATH1_HOLD_MS_MAX, key
