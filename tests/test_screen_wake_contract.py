"""Manual screen wake is a closed, short-lived, coordinate-free contract."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import jsonschema
import pytest

from runtime.util import iso, utc_now


CONTRACT = Path(__file__).resolve().parents[1] / "api/contracts/screen-wake.v1.schema.json"
BINDING = {
    "device_id": "arm-01", "daemon_session": "session-wake",
    "camera_id": "cam-01", "camera_device_unique_id": "camera-wake",
    "binding_revision": 1, "calibration_fingerprint": "a" * 64,
    "frame_width": 1280, "frame_height": 720,
}


def wake_request(**updates):
    return {
        "task_id": "wake-1", "task_type": "screen_wake",
        "capability": "arm.wake_screen", "contract_version": "screen-wake.v1",
        "device_id": "arm-01", "idempotency_key": "wake-key-1",
        "correlation_id": "wake-correlation-1",
        "expires_at": iso(utc_now() + timedelta(seconds=45)),
        "payload": {"binding": deepcopy(BINDING), "confirmed": True},
        **updates,
    }


def validator(fragment=None):
    schema = json.loads(CONTRACT.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    if fragment:
        schema = {"$ref": f"#/$defs/{fragment}", "$defs": schema["$defs"]}
    return jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())


def test_manual_request_is_valid():
    validator().validate(wake_request())


@pytest.mark.parametrize("field", [
    "task_id", "task_type", "capability", "contract_version", "device_id",
    "idempotency_key", "correlation_id", "expires_at", "payload",
])
def test_stable_identity_and_expiry_are_required(field):
    request = wake_request()
    request.pop(field)
    assert not validator().is_valid(request)


@pytest.mark.parametrize("extra", [
    {"point": {"x": .5, "y": .5}}, {"tapDepth": 8}, {"interval_ms": 50},
    {"unlock": True}, {"retry": 1}, {"confirmed": 1}, {"confirmed": False},
])
def test_payload_cannot_supply_motion_or_automatic_policy(extra):
    request = wake_request()
    request["payload"].update(extra)
    assert not validator().is_valid(request)


@pytest.mark.parametrize("expiry", [None, "", "tomorrow", "2026-09-28T01:00:00"])
def test_expiry_requires_explicit_timezone(expiry):
    assert not validator().is_valid(wake_request(expires_at=expiry))


def test_binding_and_envelope_reject_unknown_fields():
    request = wake_request()
    request["payload"]["binding"]["point"] = {"x": .5, "y": .5}
    assert not validator().is_valid(request)
    assert not validator().is_valid(wake_request(read_only_task=True))
    assert not validator().is_valid(wake_request(capability="arm.double_tap"))


def test_metadata_does_not_claim_an_awake_phone():
    view = {
        "contract_version": "screen-wake.v1", "device_id": "arm-01",
        "binding": BINDING, "usable": True, "blocking_reasons": [],
        "target": {"validation_point_id": "validation-1", "point": {"x": .5, "y": .5}},
        "effect_status": "unverified",
    }
    validator("metadata").validate(view)
    assert not validator("metadata").is_valid({**view, "effect_status": "awake"})
    assert not validator("metadata").is_valid({**view, "target": None})
