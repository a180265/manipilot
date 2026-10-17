"""直接候选通过正式 HTTP 接纳、领取、开始输入；物理 IO 仅使用仿真设备。"""
from __future__ import annotations

import asyncio
import contextlib
import threading

import pytest

from runtime.input_control import record_id_for_effect, record_id_for_owner
from tests.test_close_d_helpers import LoopbackFleet, enable_input, pair_phone


@pytest.fixture
def direct_phone(tmp_path):
    fleet = LoopbackFleet(tmp_path, n_devices=2)
    with fleet.serving():
        with fleet.client() as client:
            activation, token = pair_phone(client, instance="direct-ime-test")
            ime = activation["ime_device_id"]
            fleet.runtime.devices._devices["arm-01"]["ime_device_id"] = ime
            headers = {"x-ime-phone-token": token}
            beat = client.post(f"/api/v1/devices/{ime}/heartbeat", headers=headers,
                               json={"appVersion": "direct-test", "keyboardActive": True,
                                     "inputTaskControlVersion": 1})
            assert beat.status_code == 200, beat.text
            enable_input(client, ime, request_id="enable-direct")
            yield fleet, client, ime, headers


def _body(ime, task_id="direct-1"):
    return {"taskId": task_id, "deviceId": ime, "targetUser": "验收文本框",
            "content": "中文输入验收", "typingSpeed": "normal"}


def test_direct_candidate_can_be_delivered_and_typed_once_without_robot_click(direct_phone):
    fleet, client, ime, headers = direct_phone
    actions_before = list(fleet.arm().action_log)
    body = _body(ime)
    created = client.post("/api/v1/tasks", json=body)
    assert created.status_code == 201, created.text
    delivered = client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0", headers=headers)
    assert delivered.status_code == 200, delivered.text
    assert delivered.json()["content"] == body["content"]
    for status, count in (("displayed", 0), ("typing", 0), ("inserted", 6)):
        update = client.post("/api/v1/tasks/direct-1/status", headers=headers,
                             json={"deviceId": ime, "status": status,
                                   "typedCharacterCount": count})
        assert update.status_code == 200, update.text
        assert update.json()["status"] == status
        if status == "typing":
            duplicate = client.post("/api/v1/tasks/direct-1/status", headers=headers,
                                    json={"deviceId": ime, "status": "typing"})
            assert duplicate.status_code == 409
            assert duplicate.json()["error"] == "input_start_already_claimed"
    replay = client.post("/api/v1/tasks", json=body)
    assert replay.status_code == 200
    assert replay.json()["status"] == "inserted"
    assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                      headers=headers).status_code == 204
    assert fleet.arm().action_log == actions_before
    owner = _owner(fleet)
    assert owner["owner_type"] == "direct_ime"
    assert owner["ime_device_id"] == ime
    assert owner["settings_revision"] == 1
    for kind, state in (("ime_create", "completed"), ("ime_deliver", "claimed"),
                        ("ime_start", "claimed")):
        assert _effect(fleet, kind)["state"] == state


def _owner(fleet, task_id="direct-1"):
    return fleet.runtime.store.input_record_get(record_id_for_owner("direct_ime", task_id))


def _effect(fleet, kind, task_id="direct-1"):
    return fleet.runtime.store.input_record_get(record_id_for_effect(
        record_id_for_owner("direct_ime", task_id), ["ime:" + task_id, 0, kind]))


def _close_input(client):
    response = client.put("/v1/devices/arm-01/input-settings", json={
        "contract_version": "device-input-settings.v1", "operator_id": "操作员甲",
        "role": "operator", "reason": "直发取消验收", "correlation_id": "direct-close",
        "client_request_id": "direct-close", "enabled": False, "expected_revision": 1})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("blocker, error", [
    ("disabled", "input_switch_disabled"),
    ("calibration", "runtime_eligibility_blocked"),
    ("no_binding", "input_binding_not_unique"),
    ("duplicate_binding", "input_binding_not_unique"),
    ("guard", "input_guard_open"),
    ("pending_settings", "input_settings_commit_pending"),
    ("busy", "runtime_eligibility_blocked"),
])
def test_unavailable_input_rejects_before_candidate_or_owner(direct_phone, blocker, error):
    fleet, client, ime, _headers = direct_phone
    runtime = fleet.runtime
    if blocker == "disabled":
        _close_input(client)
    elif blocker == "calibration":
        runtime.devices._devices["arm-01"]["calibration_status"] = "due"
    elif blocker == "busy":
        runtime.devices._devices["arm-01"]["status"] = "busy"
    elif blocker == "no_binding":
        runtime.devices._devices["arm-01"]["ime_device_id"] = None
    elif blocker == "duplicate_binding":
        runtime.devices._devices["arm-02"]["ime_device_id"] = ime
    elif blocker == "guard":
        runtime.input_control.open_guard_issue("arm-01", code="editor_review_required")
    elif blocker == "pending_settings":
        intent = runtime.input_control.build_settings_write(
            device_id="arm-01", client_request_id="pending-direct-test",
            call_context={"origin": "human_put", "operator_id": "操作员甲",
                          "role": "operator", "operation_id": "input_settings_update"},
            normalized_request={"enabled": False}, before={"revision": 1},
            after={"revision": 2}, proposed_file={"revision": 2})
        runtime.input_control.insert_settings_write(intent)
    result = client.post("/api/v1/tasks", json=_body(ime))
    assert result.status_code == 409, result.text
    assert result.json()["error"] == error
    assert client.get("/api/v1/tasks/direct-1").status_code == 404
    assert _owner(fleet) is None
    assert _effect(fleet, "ime_create") is None


def test_close_after_delivery_cancels_and_accepts_original_phone_ack(direct_phone):
    fleet, client, ime, headers = direct_phone
    assert client.post("/api/v1/tasks", json=_body(ime)).status_code == 201
    assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                      headers=headers).status_code == 200
    original_owner = _owner(fleet)
    _close_input(client)
    fleet.screen_ime.tasks.sweep_cancellations()
    command = client.get(f"/api/v1/tasks/direct-1/control/next?deviceId={ime}&waitSeconds=0",
                         headers=headers)
    assert command.status_code == 200, command.text
    assert command.json()["command"] == "cancel"
    start = client.post("/api/v1/tasks/direct-1/status", headers=headers,
                        json={"deviceId": ime, "status": "typing"})
    assert start.status_code == 409, start.text
    assert start.json()["error"] == "input_authorization_revoked"
    assert _effect(fleet, "ime_start") is None
    ack = client.post("/api/v1/tasks/direct-1/control/ack", headers=headers,
                      json={"deviceId": ime, "commandId": command.json()["commandId"],
                            "typedCharacterCount": 0})
    assert ack.status_code == 200, ack.text
    assert client.get("/api/v1/tasks/direct-1").json()["task"]["status"] == "cancelled"
    assert _effect(fleet, "ime_create")["cancel_state"] == "confirmed"
    replay = client.post("/api/v1/tasks", json=_body(ime))
    assert replay.status_code == 200
    assert replay.json()["status"] == "cancelled"
    assert _owner(fleet)["settings_revision"] == original_owner["settings_revision"]
    assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                      headers=headers).status_code == 204


def test_reopen_never_renews_old_candidate_but_accepts_new_id(direct_phone):
    fleet, client, ime, headers = direct_phone
    assert client.post("/api/v1/tasks", json=_body(ime)).status_code == 201
    _close_input(client)
    fleet.screen_ime.tasks.sweep_cancellations()
    enable_input(client, ime, request_id="reopen-direct", expected_revision=2)
    replay = client.post("/api/v1/tasks", json=_body(ime))
    assert replay.status_code == 200, replay.text
    assert replay.json()["status"] == "cancelled"
    assert _owner(fleet)["settings_revision"] == 1
    assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                      headers=headers).status_code == 204
    assert client.post("/api/v1/tasks", json=_body(ime, "direct-new")).status_code == 201
    assert _owner(fleet, "direct-new")["settings_revision"] == 3
    assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                      headers=headers).json()["taskId"] == "direct-new"


def test_payload_change_conflicts_and_random_speed_replay_keeps_original_owner(direct_phone):
    fleet, client, ime, _headers = direct_phone
    body = {**_body(ime), "typingSpeed": "random"}
    first = client.post("/api/v1/tasks", json=body)
    assert first.status_code == 201
    owner = _owner(fleet)
    replay = client.post("/api/v1/tasks", json=body)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert _owner(fleet) == owner
    changed = client.post("/api/v1/tasks", json={**body, "content": "更改后的中文"})
    assert changed.status_code == 409
    assert changed.json()["error"] == "task_conflict"


@pytest.mark.parametrize("stage", ["deliver", "typing"])
def test_duplicate_binding_after_admission_rejects_new_input(direct_phone, stage):
    fleet, client, ime, headers = direct_phone
    assert client.post("/api/v1/tasks", json=_body(ime)).status_code == 201
    if stage == "typing":
        assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                          headers=headers).status_code == 200
        assert client.post("/api/v1/tasks/direct-1/status", headers=headers,
                           json={"deviceId": ime, "status": "displayed"}).status_code == 200
    fleet.runtime.devices._devices["arm-02"]["ime_device_id"] = ime
    if stage == "deliver":
        assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                          headers=headers).status_code == 204
        assert _effect(fleet, "ime_deliver") is None
    else:
        start = client.post("/api/v1/tasks/direct-1/status", headers=headers,
                            json={"deviceId": ime, "status": "typing"})
        assert start.status_code == 409, start.text
        assert start.json()["error"] == "input_binding_not_unique"
        assert _effect(fleet, "ime_start") is None


@pytest.mark.parametrize("stage", ["deliver", "typing"])
def test_binding_becomes_ambiguous_before_final_claim(direct_phone, stage, monkeypatch):
    fleet, client, ime, headers = direct_phone
    assert client.post("/api/v1/tasks", json=_body(ime)).status_code == 201
    if stage == "typing":
        assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                          headers=headers).status_code == 200
        assert client.post("/api/v1/tasks/direct-1/status", headers=headers,
                           json={"deviceId": ime, "status": "displayed"}).status_code == 200
    control = fleet.runtime.input_control
    insert = control.insert_effect

    def insert_then_change_binding(effect):
        result = insert(effect)
        fleet.runtime.devices._devices["arm-02"]["ime_device_id"] = ime
        return result

    monkeypatch.setattr(control, "insert_effect", insert_then_change_binding)
    if stage == "deliver":
        assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                          headers=headers).status_code == 204
        kind = "ime_deliver"
    else:
        start = client.post("/api/v1/tasks/direct-1/status", headers=headers,
                            json={"deviceId": ime, "status": "typing"})
        assert start.status_code == 409, start.text
        assert start.json()["error"] == "input_binding_not_unique"
        kind = "ime_start"
    assert _effect(fleet, kind)["state"] == "prepared"


@pytest.mark.parametrize("stage", ["deliver", "typing"])
def test_device_busy_after_admission_blocks_direct_input(direct_phone, stage):
    fleet, client, ime, headers = direct_phone
    assert client.post("/api/v1/tasks", json=_body(ime)).status_code == 201
    if stage == "typing":
        assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                          headers=headers).status_code == 200
        assert client.post("/api/v1/tasks/direct-1/status", headers=headers,
                           json={"deviceId": ime, "status": "displayed"}).status_code == 200
    fleet.runtime.devices._devices["arm-01"]["status"] = "busy"
    if stage == "deliver":
        assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                          headers=headers).status_code == 204
        kind = "ime_deliver"
    else:
        start = client.post("/api/v1/tasks/direct-1/status", headers=headers,
                            json={"deviceId": ime, "status": "typing"})
        assert start.status_code == 409, start.text
        assert start.json()["error"] == "runtime_eligibility_blocked"
        kind = "ime_start"
    assert _effect(fleet, kind)["state"] == "prepared"


def test_direct_admission_waits_off_event_loop_without_condition_lock(direct_phone, monkeypatch):
    fleet, client, ime, _headers = direct_phone
    control = fleet.runtime.input_control
    lock = control.device_lock
    observations = []

    @contextlib.contextmanager
    def observe_lock(device_id):
        observations.append((threading.get_ident(), fleet.screen_ime.tasks._condition.locked()))
        with lock(device_id):
            yield

    monkeypatch.setattr(control, "device_lock", observe_lock)
    assert client.post("/api/v1/tasks", json=_body(ime)).status_code == 201
    assert observations
    assert all(thread != fleet._thread.ident and not held for thread, held in observations)


@pytest.mark.parametrize("candidate_exists", [False, True])
def test_restart_of_claimed_direct_create_requires_review(direct_phone, candidate_exists):
    fleet, _client, ime, headers = direct_phone
    control = fleet.runtime.input_control
    owner = control.build_owner(
        owner_type="direct_ime", owner_ref="direct-1", device_id="arm-01",
        ime_device_id=ime, settings_revision=1,
        binding_revision=int(fleet.runtime.devices.binding_view("arm-01")
                             .get("binding_revision") or 0), payload=_body(ime))
    control.admit_owner(owner)
    effect, _ = control.insert_effect(control.build_effect(
        owner_id=owner["record_id"], device_id="arm-01",
        slot_key=["ime:direct-1", 0, "ime_create"], payload=_body(ime), ime_task_id="direct-1"))
    control.claim_effect(effect["record_id"], expected_revision=effect["revision"])
    if candidate_exists:
        # 构造进程在 IME commit 后、Fleet finish 前消失的真实持久边界。
        asyncio.run(fleet.screen_ime.tasks.create(_body(ime)))
    fleet._stop()
    restarted = LoopbackFleet(fleet.tmp_path, n_devices=2)
    restarted.app.state.device_configs = restarted.configs
    # 本例走真实 startup 注册/恢复；共享夹具通常预注册并跳过该阶段。
    restarted.configs = []
    with restarted.serving():
        with restarted.client() as client:
            restarted.runtime.devices._devices["arm-01"].update({
                "ime_device_id": ime, "status": "ready", "calibration_status": "validated"})
            beat = client.post(f"/api/v1/devices/{ime}/heartbeat", headers=headers,
                               json={"keyboardActive": True, "inputTaskControlVersion": 1})
            assert beat.status_code == 200, beat.text
            assert _effect(restarted, "ime_create")["state"] == "unknown"
            assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                              headers=headers).status_code == 204
            if candidate_exists:
                restarted.screen_ime.tasks.sweep_cancellations()
                assert client.get("/api/v1/tasks/direct-1").json()["task"]["status"] == "cancelled"
            assert _owner(restarted)["settings_revision"] == 1
            new_candidate = client.post("/api/v1/tasks", json=_body(ime, "direct-after-crash"))
            assert new_candidate.status_code == 409, new_candidate.text
            assert new_candidate.json()["error"] == "input_guard_open"


def test_live_create_is_not_cancelled_between_candidate_commit_and_receipt(direct_phone, monkeypatch):
    fleet, client, ime, headers = direct_phone
    control = fleet.runtime.input_control
    finish = control.finish_effect
    observed = []

    def finish_after_sweep(record_id, **kwargs):
        fleet.screen_ime.tasks.sweep_cancellations()
        observed.append(fleet.screen_ime.tasks.detail("direct-1")["task"]["status"])
        return finish(record_id, **kwargs)

    monkeypatch.setattr(control, "finish_effect", finish_after_sweep)
    assert client.post("/api/v1/tasks", json=_body(ime)).status_code == 201
    assert observed == ["pending"]
    assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                      headers=headers).status_code == 200


@pytest.mark.parametrize("stage", ["pending", "displayed", "typing", "inserted"])
def test_restart_cancels_unfinished_direct_task_and_preserves_completed_history(direct_phone, stage):
    fleet, client, ime, headers = direct_phone
    assert client.post("/api/v1/tasks", json=_body(ime)).status_code == 201
    if stage != "pending":
        assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                          headers=headers).status_code == 200
        for status in ("displayed", "typing", "inserted"):
            response = client.post("/api/v1/tasks/direct-1/status", headers=headers,
                                   json={"deviceId": ime, "status": status,
                                         "typedCharacterCount": 6 if status == "inserted" else 0})
            assert response.status_code == 200, response.text
            if status == stage:
                break
    original = _owner(fleet)
    fleet._stop()
    restarted = LoopbackFleet(fleet.tmp_path, n_devices=2)
    restarted.app.state.device_configs = restarted.configs
    restarted.configs = []
    with restarted.serving():
        with restarted.client() as restarted_client:
            restarted.runtime.devices._devices["arm-01"].update({
                "ime_device_id": ime, "status": "ready", "calibration_status": "validated"})
            assert restarted_client.post(
                f"/api/v1/devices/{ime}/heartbeat", headers=headers,
                json={"keyboardActive": True, "inputTaskControlVersion": 1}).status_code == 200
            restarted.screen_ime.tasks.sweep_cancellations()
            assert restarted_client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                                        headers=headers).status_code == 204
            saved = restarted_client.get("/api/v1/tasks/direct-1").json()["task"]
            if stage == "inserted":
                assert saved["status"] == "inserted"
                assert _owner(restarted) == original
                assert restarted_client.post("/api/v1/tasks", json=_body(ime, "fresh")).status_code == 201
            else:
                expected = "cancelled" if stage == "pending" else "cancel_requested"
                assert saved["status"] == expected
                assert _owner(restarted)["revocation"] == "revoked"
                start = restarted_client.post(
                    "/api/v1/tasks/direct-1/status", headers=headers,
                    json={"deviceId": ime, "status": "typing"})
                assert start.status_code == 409
                assert start.json()["error"] == "input_authorization_revoked"
                assert _owner(restarted)["settings_revision"] == original["settings_revision"]
                if stage == "pending":
                    assert restarted_client.post(
                        "/api/v1/tasks", json=_body(ime, "fresh")).status_code == 201
                else:
                    blocked = restarted_client.post("/api/v1/tasks", json=_body(ime, "fresh"))
                    assert blocked.status_code == 409
                    assert blocked.json()["error"] == "input_guard_open"
                    command = restarted_client.get(
                        f"/api/v1/tasks/direct-1/control/next?deviceId={ime}&waitSeconds=0",
                        headers=headers)
                    assert command.status_code == 200, command.text
                    ack = restarted_client.post(
                        "/api/v1/tasks/direct-1/control/ack", headers=headers,
                        json={"deviceId": ime, "commandId": command.json()["commandId"],
                              "typedCharacterCount": 2 if stage == "typing" else 0})
                    assert ack.status_code == 200, ack.text
                    assert _effect(restarted, "ime_deliver")["cancel_state"] == "confirmed"
                    if stage == "typing":
                        assert _effect(restarted, "ime_start")["cancel_state"] == "confirmed"
                        assert _effect(restarted, "ime_start")["state"] == "claimed"
                    guard = restarted.runtime.input_control.ensure_guard("arm-01")
                    reviewed = restarted_client.post("/v1/devices/arm-01/input-recovery/review", json={
                        "contract_version": "input-recovery-review.v1",
                        "expected_recovery_revision": guard["revision"], "ime_device_id": ime,
                        "issue_ids": [issue["issue_id"] for issue in guard["issues"]
                                      if issue["state"] == "open"],
                        "candidate_removed": True, "editor_checked": True,
                        "operator_id": "操作员甲", "role": "operator", "reason": "仿真现场核对",
                        "correlation_id": "direct-restart-review", "client_request_id": "review-1"})
                    assert reviewed.status_code == 200, reviewed.text
                    assert restarted_client.post("/api/v1/tasks", json=_body(ime, "fresh")).status_code == 201
                    old_start = restarted_client.post(
                        "/api/v1/tasks/direct-1/status", headers=headers,
                        json={"deviceId": ime, "status": "typing"})
                    assert old_start.status_code == 409


def test_legacy_orphan_replay_does_not_retroactively_admit_owner(direct_phone):
    fleet, client, ime, headers = direct_phone
    asyncio.run(fleet.screen_ime.tasks.create(_body(ime)))
    replay = client.post("/api/v1/tasks", json=_body(ime))
    assert replay.status_code == 200
    assert _owner(fleet) is None
    assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                      headers=headers).status_code == 204


def test_direct_id_cannot_borrow_existing_fleet_owner(direct_phone):
    fleet, client, ime, _headers = direct_phone
    control = fleet.runtime.input_control
    owner = control.build_owner(
        owner_type="fleet_task", owner_ref="direct-1", device_id="arm-01",
        ime_device_id=ime, settings_revision=1,
        binding_revision=int(fleet.runtime.devices.binding_view("arm-01")
                             .get("binding_revision") or 0), payload={"text": "平台任务"})
    control.admit_owner(owner)
    result = client.post("/api/v1/tasks", json=_body(ime))
    assert result.status_code == 409, result.text
    assert result.json()["error"] == "input_owner_conflict"
    assert client.get("/api/v1/tasks/direct-1").status_code == 404
    assert _owner(fleet) is None


def test_candidate_write_failure_preserves_claim_without_replaying(direct_phone, monkeypatch):
    fleet, client, ime, headers = direct_phone
    repo = fleet.screen_ime.tasks
    execute = repo._execute

    def fail_candidate(sql, params=()):
        if sql.startswith("INSERT INTO tasks("):
            raise OSError("injected IME persistence failure")
        return execute(sql, params)

    monkeypatch.setattr(repo, "_execute", fail_candidate)
    response = client.post("/api/v1/tasks", json=_body(ime))
    assert response.status_code == 503, response.text
    assert response.json()["error"] == "input_create_uncertain"
    assert client.get("/api/v1/tasks/direct-1").status_code == 404
    assert _effect(fleet, "ime_create")["state"] == "unknown"
    owner = _owner(fleet)
    monkeypatch.setattr(repo, "_execute", execute)
    replay = client.post("/api/v1/tasks", json=_body(ime))
    assert replay.status_code == 409
    assert replay.json()["error"] == "input_guard_open"
    assert _owner(fleet) == owner
    assert client.get(f"/api/v1/devices/{ime}/tasks/next?waitSeconds=0",
                      headers=headers).status_code == 204
