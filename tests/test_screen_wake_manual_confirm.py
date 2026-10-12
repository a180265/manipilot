"""W13：人工无姿态唤醒的单次现场确认（规范 §9.3 第 7 条）。

契约、操作员确认路由、原子一次消费与 daemon 侧豁免边界：
- 只豁免绑定任务的实时手机姿态，不豁免新鲜帧/急停/租约；
- 过期、重放、伪造角色、跨设备、参数变化、服务重启均不授予例外；
- 自动来源不可签发；旧 v1 布尔确认不获得豁免（无服务端记录即无例外）。
"""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import re

import httpx
import jsonschema
import pytest

from drivers import calibration_bundle as cb, screen_gestures as sg
from drivers.arm import DriverError
from drivers.wake_screen import resolve_wake_target, wake_target_digest
from runtime.errors import ApiError
from runtime.util import iso, parse_iso, utc_now
from tests.helpers import make_device_configs, make_runtime, wait_terminal
from tests.test_screen_gesture_driver import gesture_bundle, rig
from tests.test_screen_wake_contract import wake_request
from tests.test_screen_wake_driver import prepare_wake, validated_bundle
from tests.test_screen_wake_runtime import PLUGIN_DIR

SCHEMA = Path(__file__).resolve().parents[1] / "api/contracts/screen-wake-manual-confirm.v1.schema.json"


def schema_validator(fragment=None):
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    if fragment:
        schema = {"$ref": f"#/$defs/{fragment}", "$defs": schema["$defs"]}
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker())


def manual_confirm_body(task, prepared, **updates):
    return {
        "contract_version": "screen-wake-manual-confirm.v1",
        "task_id": task["task_id"],
        "idempotency_key": task["idempotency_key"],
        "binding": deepcopy(task["payload"]["binding"]),
        "reference_evidence_id": prepared["reference_evidence_id"],
        "operator_id": "操作员甲",
        "reason": "现场核对手机仍在展示的参考位置、探头与路径安全，仅执行一次双击",
        "correlation_id": "corr-manual-1",
        "client_request_id": "creq-manual-1",
        **updates,
    }


async def manual_runtime(tmp_path, *, devices=1, enabled=True):
    from runtime.screen_wake import confirmations

    runtime = make_runtime(tmp_path)
    runtime.capabilities.bootstrap()
    await runtime.devices.register_all(make_device_configs(devices))
    if enabled:
        runtime.config._data["screen_wake_manual_confirm_enabled"] = True
    bundle = validated_bundle(gesture_bundle())
    cb.save_bundle(bundle, tmp_path / "data")
    binding = sg.binding_snapshot(bundle)
    runtime.plugins.install(PLUGIN_DIR, "tester", "isolated manual confirm test")
    runtime.plugins.activate("screen-wake-adapter", "tester", "isolated manual confirm test")
    device = runtime.devices.get("arm-01")
    device.update(camera_quality="waiting", daemon_session=binding["daemon_session"],
                  camera_device_unique_id=binding["camera_device_unique_id"],
                  camera_binding_revision=binding["binding_revision"],
                  camera_frame_age_ms=0, ime_device_id=None,
                  enforce_input_gate=True, input_acceptance_deferred=True)
    runtime.devices.camera("arm-01").set_quality("waiting")

    async def wake_placeholder(params, *, execution_guard=None, manual_confirmation=None):
        return {"controller_completed": True, "tap_count": 2, "effect_status": "unverified"}

    runtime.devices.driver("arm-01").wake_screen = wake_placeholder
    confirmations(runtime)  # 预挂内存存储，模拟常驻服务
    return runtime, bundle, binding


# ---------- 契约 ----------

def test_contract_accepts_only_the_closed_confirmation_shape():
    body = manual_confirm_body(
        {"task_id": "wake-1", "idempotency_key": "wake-key-1",
         "payload": {"binding": wake_request()["payload"]["binding"]}},
        {"reference_evidence_id": "swmc-ref-" + "0" * 24})
    schema_validator().validate(body)

    for field in ("contract_version", "task_id", "idempotency_key", "binding",
                  "reference_evidence_id", "operator_id", "reason",
                  "correlation_id", "client_request_id"):
        assert not schema_validator().is_valid(
            {key: value for key, value in body.items() if key != field}), field

    # 伪造角色/自带参考坐标/未知字段一律拒绝（additionalProperties=false）。
    assert not schema_validator().is_valid({**body, "role": "admin"})
    assert not schema_validator().is_valid({**body, "reference_pose": {"matrix": []}})
    assert not schema_validator().is_valid({**body, "confirmed": True})
    assert not schema_validator().is_valid({**body,
                                            "reason": " "})  # 文本必须非空
    assert not schema_validator().is_valid({
        **body, "binding": {**body["binding"], "point": {"x": .5, "y": .5}}})


def test_contract_response_carries_only_the_four_fields():
    response = {
        "contract_version": "screen-wake-manual-confirm.v1",
        "confirmation_id": "swmc-" + "0" * 32,
        "task_id": "wake-1",
        "expires_at": iso(utc_now() + timedelta(seconds=60)),
    }
    schema_validator("confirmation").validate(response)
    assert not schema_validator("confirmation").is_valid({**response, "operator_id": "x"})
    assert not schema_validator("confirmation").is_valid(
        {**response, "confirmation_id": "short"})
    assert not schema_validator("confirmation").is_valid(
        {**response, "expires_at": "2026-09-29T00:00:00"})


# ---------- 操作员确认路由 ----------

def test_confirm_route_is_disabled_by_default(tmp_path):
    async def scenario():
        from api.app import create_app
        from runtime.screen_wake import manual_confirm_reference

        runtime, _, binding = await manual_runtime(tmp_path, enabled=False)
        request = wake_request(payload={"binding": binding, "confirmed": True})
        task = runtime.submit_task(request, source="console", context={})
        prepared = manual_confirm_reference(runtime, "arm-01", task)
        app = create_app(runtime)
        app.state.runtime = runtime
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations",
                json=manual_confirm_body(task, prepared))
        assert response.status_code == 403
        assert response.json()["error"]["key"] == "screen_wake_manual_confirm_disabled"
        assert not runtime.store.all_tasks() or all(
            item["task_id"] == task["task_id"] for item in runtime.store.all_tasks())

    asyncio.run(scenario())


def test_confirm_route_creates_and_replays_one_confirmation(tmp_path):
    async def scenario():
        from api.app import create_app
        from runtime.screen_wake import confirmations, manual_confirm_reference

        runtime, _, binding = await manual_runtime(tmp_path)
        request = wake_request(payload={"binding": binding, "confirmed": True})
        task = runtime.submit_task(request, source="console", context={})
        prepared = manual_confirm_reference(runtime, "arm-01", task)
        app = create_app(runtime)
        app.state.runtime = runtime
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations",
                json=manual_confirm_body(task, prepared))
            assert created.status_code == 201, created.text
            body = created.json()
            assert set(body) == {"contract_version", "confirmation_id", "task_id", "expires_at"}
            assert body["contract_version"] == "screen-wake-manual-confirm.v1"
            assert re.fullmatch(r"swmc-[0-9a-f]{32,}", body["confirmation_id"])
            assert 0 < (parse_iso(body["expires_at"]) - utc_now()).total_seconds() <= 60

            replayed = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations",
                json=manual_confirm_body(task, prepared))
            assert replayed.status_code == 200
            assert replayed.json() == body, "重复提交只查原记录，不延长有效期"

            conflict = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations",
                json=manual_confirm_body(task, prepared, reason="不同内容"))
            assert conflict.status_code == 409

            second = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations",
                json=manual_confirm_body(task, prepared, client_request_id="creq-manual-2"))
            assert second.status_code == 409, "同一任务不得叠加第二份未消费确认"

            bad_reference = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations",
                json=manual_confirm_body(task, prepared, client_request_id="creq-3",
                                         reference_evidence_id="swmc-ref-not-displayed"))
            assert bad_reference.status_code == 409

            bad_binding = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations",
                json=manual_confirm_body(
                    task, prepared, client_request_id="creq-4",
                    binding={**binding, "calibration_fingerprint": "b" * 64}))
            assert bad_binding.status_code == 409

            unknown_field = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations",
                json={**manual_confirm_body(task, prepared), "role": "admin"})
            assert unknown_field.status_code == 400

            missing = manual_confirm_body(task, prepared)
            missing.pop("operator_id")
            response = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations", json=missing)
            assert response.status_code == 400

            unknown_task = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations",
                json=manual_confirm_body(
                    {"task_id": "wake-none", "idempotency_key": "key-none",
                     "payload": {"binding": binding, "confirmed": True}}, prepared))
            assert unknown_task.status_code == 404

            cross_device = await client.post(
                "/v1/devices/arm-02/screen-wake/manual-confirmations",
                json=manual_confirm_body(task, prepared))
            assert cross_device.status_code == 409

            expired_request = deepcopy(request)
            expired_request.update(
                task_id="wake-expired", idempotency_key="wake-expired-key",
                expires_at=iso(utc_now() - timedelta(seconds=5)))
            runtime.submit_task(expired_request, source="console", context={})
            expired_task = runtime.store.get_task("wake-expired")
            expired_prepared = manual_confirm_reference(runtime, "arm-01", expired_task)
            expired = await client.post(
                "/v1/devices/arm-01/screen-wake/manual-confirmations",
                json=manual_confirm_body(expired_task, expired_prepared))
            assert expired.status_code == 410

        record = confirmations(runtime)._records["creq-manual-1"]
        assert record["operator_id"] == "操作员甲"
        assert record["target_digest"] == prepared["target_digest"]
        assert record["reference_source"] == "published_calibration_basis"

    asyncio.run(scenario())


# ---------- 原子一次消费与撤销 ----------

def test_confirmation_is_consumed_once_and_revoked_by_expiry_restart_or_change(tmp_path):
    async def scenario():
        from runtime.screen_wake import (
            ManualConfirmationStore, confirmations, consume_manual_confirmation,
            manual_confirm_reference,
        )

        runtime, bundle, binding = await manual_runtime(tmp_path)
        request = wake_request(payload={"binding": binding, "confirmed": True})
        task = runtime.submit_task(request, source="console", context={})
        prepared = manual_confirm_reference(runtime, "arm-01", task)
        store = confirmations(runtime)
        status, payload = store.create(
            runtime, "arm-01", manual_confirm_body(task, prepared))
        assert status == 201

        consumed = consume_manual_confirmation(runtime, task)
        assert consumed["confirmation_id"] == payload["confirmation_id"]
        assert consumed["consumed_at"] is not None
        assert consume_manual_confirmation(runtime, task) is None, "只能原子消费一次"

        # 服务重启：内存确认存储消失，未消费确认自然撤销。
        runtime.screen_wake_confirmations = ManualConfirmationStore()
        request2 = wake_request(
            payload={"binding": binding, "confirmed": True},
            task_id="wake-2", idempotency_key="wake-key-2")
        task2 = runtime.submit_task(request2, source="console", context={})
        prepared2 = manual_confirm_reference(runtime, "arm-01", task2)
        status2, payload2 = confirmations(runtime).create(
            runtime, "arm-01", manual_confirm_body(
                task2, prepared2, client_request_id="creq-manual-2"))
        assert status2 == 201
        runtime.screen_wake_confirmations = ManualConfirmationStore()
        assert consume_manual_confirmation(runtime, task2) is None

        # 过期确认撤销（不授予例外）。
        runtime.screen_wake_confirmations = ManualConfirmationStore()
        request3 = wake_request(
            payload={"binding": binding, "confirmed": True},
            task_id="wake-3", idempotency_key="wake-key-3")
        task3 = runtime.submit_task(request3, source="console", context={})
        prepared3 = manual_confirm_reference(runtime, "arm-01", task3)
        confirmations(runtime).create(
            runtime, "arm-01", manual_confirm_body(
                task3, prepared3, client_request_id="creq-manual-3"))
        record = confirmations(runtime)._records["creq-manual-3"]
        record["expires_at"] = iso(utc_now() - timedelta(seconds=1))
        assert consume_manual_confirmation(runtime, task3) is None

        # 参数/校准变化撤销：确认后换校准 run，摘要不一致即拒绝消费。
        runtime.screen_wake_confirmations = ManualConfirmationStore()
        request4 = wake_request(
            payload={"binding": binding, "confirmed": True},
            task_id="wake-4", idempotency_key="wake-key-4")
        task4 = runtime.submit_task(request4, source="console", context={})
        prepared4 = manual_confirm_reference(runtime, "arm-01", task4)
        confirmations(runtime).create(
            runtime, "arm-01", manual_confirm_body(
                task4, prepared4, client_request_id="creq-manual-4"))
        changed = deepcopy(bundle)
        changed["validation"]["points"] = [
            entry for entry in changed["validation"]["points"]
            if entry["targetId"] != "validation-5"]
        cb.save_bundle(changed, tmp_path / "data")
        assert consume_manual_confirmation(runtime, task4) is None

        # 自动策略任务不是合法人工唤醒任务，不可确认也不可消费。
        automatic = deepcopy(request)
        automatic.update(task_id="wake-auto", idempotency_key="wake-auto-key",
                         policy_context={"automatic": True})
        runtime.store.save_task(automatic)
        with pytest.raises(ApiError):
            manual_confirm_reference(runtime, "arm-01", automatic)
        assert consume_manual_confirmation(runtime, automatic) is None

    asyncio.run(scenario())


def test_runtime_dispatch_passes_consumed_confirmation_to_driver_once(tmp_path, monkeypatch):
    async def scenario():
        from runtime.screen_wake import (
            confirmations, consume_manual_confirmation, manual_confirm_reference,
        )

        runtime, _, binding = await manual_runtime(tmp_path)
        driver = runtime.devices.driver("arm-01")
        received = []

        async def wake(params, *, execution_guard, manual_confirmation=None):
            execution_guard()
            received.append(manual_confirmation)
            return {"controller_completed": True, "tap_count": 2,
                    "effect_status": "unverified",
                    "manual_position_confirmed": manual_confirmation is not None}

        monkeypatch.setattr(driver, "wake_screen", wake)
        request = wake_request(payload={"binding": binding, "confirmed": True})
        task = runtime.submit_task(request, source="console",
                                   context={"operator_id": "tester"})
        prepared = manual_confirm_reference(runtime, "arm-01", task)
        status, payload = confirmations(runtime).create(
            runtime, "arm-01", manual_confirm_body(task, prepared))
        assert status == 201
        await runtime._dispatch_once()
        result = await wait_terminal(runtime, task["task_id"])
        assert result["status"] == "confirmed", result
        assert received and received[0]["confirmation_id"] == payload["confirmation_id"]
        assert received[0]["consumed_at"] is not None
        assert consume_manual_confirmation(runtime, result) is None
        assert result["wake_manual_confirmation_id"] == payload["confirmation_id"]

        # 旧 v1 布尔确认（无服务端记录）不获得豁免：驱动收不到确认记录。
        request2 = wake_request(
            payload={"binding": binding, "confirmed": True},
            task_id="wake-plain", idempotency_key="wake-plain-key")
        task2 = runtime.submit_task(request2, source="console", context={})
        await runtime._dispatch_once()
        result2 = await wait_terminal(runtime, task2["task_id"])
        assert result2["status"] == "confirmed"
        assert received[-1] is None
        assert "wake_manual_confirmation_id" not in result2

    asyncio.run(scenario())


# ---------- daemon 侧消费（豁免实时姿态，不豁免其他门禁） ----------

def manual_record(bundle, *, expires_in=60, consumed=None, **updates):
    target = resolve_wake_target(bundle)
    return {
        "confirmation_id": "swmc-" + "1" * 32,
        "task_id": "wake-manual", "device_id": bundle["deviceId"],
        "idempotency_key": "wake-manual-key",
        "binding": sg.binding_snapshot(bundle),
        "reference_evidence_id": "swmc-ref-" + "2" * 24,
        "reference_source": "published_calibration_basis",
        "reference_pose": None,
        "target_digest": wake_target_digest(target),
        "operator_id": "操作员甲", "reason": "现场确认手机未移动",
        "correlation_id": "corr-1", "client_request_id": "creq-1",
        "confirmed_at": iso(utc_now()),
        "expires_at": iso(utc_now() + timedelta(seconds=expires_in)),
        "consumed_at": consumed,
        **updates,
    }


def test_manual_confirmation_allows_one_frozen_reference_double_tap(rig):
    # 全黑且无姿态（双击后仍暗屏）：人工确认生效时允许用冻结的已验证位置
    # 执行一次双击；只豁免实时姿态，机械完成后效果仍为 unverified。
    daemon, driver, bundle, root, _ = prepare_wake(rig, lit_after=False)
    normal_gate = driver.screen_wake_gate

    async def gate(*, require_pose=False):
        checks, frame, deviation = await normal_gate()
        frame.pop("_screen_pose", None)  # 全黑：姿态不可观测
        return checks, frame, deviation

    driver.screen_wake_gate = gate
    before = cb.bundle_path("arm-01", root).read_bytes()
    result = asyncio.run(driver.wake_screen(
        {"binding": sg.binding_snapshot(bundle), "confirmed": True},
        manual_confirmation=manual_record(bundle)))
    assert result["controller_completed"] is True
    assert result["tap_count"] == 2
    assert result["effect_status"] == "unverified"
    assert result["manual_position_confirmed"] is True
    assert result["position_reference"] == "manual_frozen_reference"
    assert len([cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")]) == 1
    assert daemon.mpos[2] == bundle["z"]["hoverZ"]
    assert cb.bundle_path("arm-01", root).read_bytes() == before
    assert not any(cmd.startswith(("SWIPE", "PATH1", "$H")) for cmd in daemon.commands)


def test_manual_confirmation_keeps_completion_when_lit_frame_has_no_pose(rig):
    # 豁免覆盖本任务的实时姿态要求：双击后亮屏但无姿态 → 机械事实保留、
    # 效果 unverified，不升级为 unknown，也不追加动作（§9.3(6)/W14）。
    daemon, driver, bundle, _, _ = prepare_wake(rig)
    normal_gate = driver.screen_wake_gate

    async def gate(*, require_pose=False):
        checks, frame, deviation = await normal_gate()
        if require_pose:
            frame.pop("_screen_pose", None)  # 亮屏但姿态不可确认
        return checks, frame, deviation

    driver.screen_wake_gate = gate
    result = asyncio.run(driver.wake_screen(
        {"binding": sg.binding_snapshot(bundle), "confirmed": True},
        manual_confirmation=manual_record(bundle)))
    assert result["controller_completed"] is True
    assert result["effect_status"] == "unverified"
    assert result["manual_position_confirmed"] is True
    assert len([cmd for cmd in daemon.commands if cmd.startswith("TAP2 ")]) == 1


@pytest.mark.parametrize("invalid", ["expired", "wrong_binding", "wrong_digest",
                                     "wrong_device"])
def test_invalid_manual_confirmation_still_waits_without_motion(rig, invalid):
    daemon, driver, bundle, _, _ = prepare_wake(rig)
    normal_gate = driver.screen_wake_gate

    async def gate(*, require_pose=False):
        checks, frame, deviation = await normal_gate()
        frame.pop("_screen_pose", None)
        return checks, frame, deviation

    driver.screen_wake_gate = gate
    record = manual_record(bundle)
    if invalid == "expired":
        record = manual_record(bundle, expires_in=-1)
    elif invalid == "wrong_binding":
        record = manual_record(bundle, binding={
            **record["binding"], "calibration_fingerprint": "c" * 64})
    elif invalid == "wrong_digest":
        record = manual_record(bundle, target_digest="0" * 64)
    else:
        record = manual_record(bundle, device_id="arm-02")
    with pytest.raises(DriverError) as error:
        asyncio.run(driver.wake_screen(
            {"binding": sg.binding_snapshot(bundle), "confirmed": True},
            manual_confirmation=record))
    assert error.value.category == "driver_camera_waiting"
    assert error.value.motion_not_started is True
    assert not daemon.commands


def test_manual_confirmation_does_not_exempt_estop(rig):
    daemon, driver, bundle, _, _ = prepare_wake(rig)
    driver._estop_latched = True
    with pytest.raises(DriverError):
        asyncio.run(driver.wake_screen(
            {"binding": sg.binding_snapshot(bundle), "confirmed": True},
            manual_confirmation=manual_record(bundle)))
    assert not daemon.commands
