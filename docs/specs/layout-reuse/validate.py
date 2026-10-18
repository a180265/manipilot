#!/usr/bin/env python3
"""Offline design-contract checks. Does not import WX runtime or contact services."""

import argparse
import base64
import copy
import hashlib
import io
import importlib.util
import json
import math
import re
import sqlite3
import stat
import struct
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path, PurePosixPath
import sys
import tempfile
import zipfile

from jsonschema import Draft7Validator, FormatChecker, validators
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource


ROOT = Path(__file__).resolve().parent
FILES = {
    "common": "common.schema.json",
    "region": "phone-search-region.schema.json",
    "ime": "ime-geometry.schema.json",
    "layout": "layout.schema.json",
    "template": "ui-layout-template.v1.schema.json",
    "adaptation": "device-layout-adaptation.v1.schema.json",
    "api": "api.schema.json",
    "package": "package.schema.json",
    "records": "records.schema.json",
}
IDS = {name: "urn:wx:design:layout-reuse:" + name for name in (*FILES, "wire", "checks")}
STATES = ("wechat_list", "chat_ime_hidden", "chat_ime_empty", "chat_ime_text")
MAX_BYTES = 200 * 1024 * 1024


class ContractError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ContractError(code)


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "json:duplicate_key:" + key)
        result[key] = value
    return result


def invalid_constant(value):
    raise ContractError("json:nonfinite:" + value)


def loads(text):
    result = json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)
    finite(result)
    return result


def read(path):
    return loads(Path(path).read_text(encoding="utf-8"))


def finite(value):
    if isinstance(value, float):
        require(math.isfinite(value), "json:nonfinite")
    elif isinstance(value, str):
        require(not any(ord(char) < 32 for char in value), "json:control_character")
        require(not any(0xD800 <= ord(char) <= 0xDFFF for char in value), "json:surrogate")
    elif isinstance(value, dict):
        for key, child in value.items():
            finite(key)
            finite(child)
    elif isinstance(value, list):
        for child in value:
            finite(child)


def canonical(value):
    finite(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def record_digest(value):
    return digest({key: child for key, child in value.items() if key != "sha256"})


def no_remote(uri):
    raise NoSuchResource(ref=uri)


TYPE_CHECKER = Draft7Validator.TYPE_CHECKER.redefine_many({
    "integer": lambda checker, value: type(value) is int,
    "number": lambda checker, value: type(value) is int or (type(value) is float and math.isfinite(value)),
})
StrictValidator = validators.extend(Draft7Validator, type_checker=TYPE_CHECKER)
SCHEMAS = {name: read(ROOT / filename) for name, filename in FILES.items()}
ROUTES = read(ROOT / "routes.json")
ROUTE_BY_ID = {route["id"]: route for route in ROUTES}


def ref(name):
    family, definition = name.split(":")
    return {"$ref": IDS[family] + "#/definitions/" + definition}


def obj(properties, required=None):
    return {"type": "object", "additionalProperties": False,
            "required": list(properties) if required is None else required, "properties": properties}


def message_schema(name, route):
    if name is None:
        return {"type": "null"}
    family, definition = name.split(":")
    schema = copy.deepcopy(SCHEMAS[family]["definitions"][definition])
    # Local refs must retain their original base when copied into wire.schema.json.
    def absolute(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "$ref" and child.startswith("#"):
                    value[key] = IDS[family] + child
                else:
                    absolute(child)
        elif isinstance(value, list):
            for child in value:
                absolute(child)
    absolute(schema)
    if route["protocol"] == "fleet":
        schema["properties"]["contract_version"] = {"const": route["contract_version"]}
        schema["properties"]["correlation_id"] = ref("common:uuid")
        schema["required"] = sorted(set(schema["required"]) | {"contract_version", "correlation_id"})
    return schema


def build_wire():
    definitions = {}
    write_requests, write_responses = [], []
    for route in ROUTES:
        name = route["id"]
        headers = {} if route["protocol"] == "ime" else {
            "X-Correlation-Id": ref("common:uuid"),
            "X-Layout-Contract-Version": {"const": route["contract_version"]},
        }
        required = list(headers) if route["method"] == "GET" else []
        if route["method"] != "GET":
            headers["Content-Type"] = {"const": "application/json"}
            required.append("Content-Type")
        definitions[name + "_request"] = obj({
            "route_id": {"const": name}, "method": {"const": route["method"]},
            "path": obj({key: ref(value) for key, value in route["path_parameters"].items()}),
            "headers": obj(headers, required), "query": ref(route["query"]),
            "body": message_schema(route["request"], route),
        })
        response_headers = {"Content-Type": {"const": "application/json"}, "Cache-Control": {"const": "no-store"}}
        if route["protocol"] == "fleet":
            response_headers["X-Correlation-Id"] = ref("common:uuid")
            response_headers["X-Layout-Contract-Version"] = {"const": route["contract_version"]}
        if route["response"].startswith("binary:"):
            is_zip = route["response"] == "binary:zip"
            response_headers["Content-Type"] = {"const": "application/zip"} if is_zip else {"enum": ["image/png", "image/jpeg"]}
            response_headers["Content-Length"] = {"type": "integer", "minimum": 1, "maximum": MAX_BYTES}
            response_headers["ETag"] = {"type": "string", "pattern": '^"[0-9a-f]{64}"$'}
            response_headers["Content-Disposition"] = {"const": "attachment; filename=layout-package.zip" if is_zip else "inline"}
            body = ref("api:base64")
        else:
            body = message_schema(route["response"], route)
        definitions[name + "_body"] = body
        response = obj({
            "route_id": {"const": name}, "status": {"const": route["status"], "type": "integer"},
            "headers": obj(response_headers), "body": body,
        })
        if name == "ime_poll":
            empty = obj({"route_id": {"const": name}, "status": {"const": 204, "type": "integer"},
                         "headers": obj({"Cache-Control": {"const": "no-store"}}), "body": {"type": "null"}})
            definitions[name + "_response"] = {"oneOf": [response, empty]}
        else:
            definitions[name + "_response"] = response
        error_headers = {"Content-Type": {"const": "application/json"}, "Cache-Control": {"const": "no-store"}}
        if route["protocol"] == "fleet":
            error_headers.update({"X-Correlation-Id": ref("common:uuid"),
                                  "X-Layout-Contract-Version": {"const": "layout-error.v1"}})
        definitions[name + "_error"] = obj({
            "route_id": {"const": name},
            "status": {"type": "integer", "enum": [401, 403, 409, 422, 503] if route["protocol"] == "ime" else [403, 404, 409, 422, 503]},
            "headers": obj(error_headers),
            "body": ref("api:ime_error" if route["protocol"] == "ime" else "api:error"),
        })
        if route["write"]:
            write_requests.append(ref("wire:" + name + "_request"))
            write_responses.append(ref("wire:" + name + "_response"))
    definitions["write_request"] = {"oneOf": write_requests}
    definitions["write_response"] = {"oneOf": write_responses}
    return {"$schema": "http://json-schema.org/draft-07/schema#", "$id": IDS["wire"],
            "$comment": "Generated by validate.py --write-wire-schema from routes.json and closed payload schemas.",
            "$ref": "#/definitions/write_request", "definitions": definitions}


SCHEMAS["wire"] = build_wire()
FILES["checks"] = "checks.schema.json"
SCHEMAS["checks"] = read(ROOT / FILES["checks"])
REGISTRY = Registry(retrieve=no_remote).with_resources(
    (schema["$id"], Resource.from_contents(schema)) for schema in SCHEMAS.values()
)


def d(value):
    return Decimal(str(value))


def rect(value):
    require(d(value["x"]) + d(value["width"]) <= 1, "rect:x_overflow")
    require(d(value["y"]) + d(value["height"]) <= 1, "rect:y_overflow")


def bounds(value, width, height):
    require(0 <= value["left"] < value["right"] <= width, "bounds:x_order_or_overflow")
    require(0 <= value["top"] < value["bottom"] <= height, "bounds:y_order_or_overflow")


def pixel_rect(value, size):
    rect(value)
    return {
        "left": int((d(value["x"]) * size["width"]).to_integral_value(rounding=ROUND_FLOOR)),
        "top": int((d(value["y"]) * size["height"]).to_integral_value(rounding=ROUND_FLOOR)),
        "right": int(((d(value["x"]) + d(value["width"])) * size["width"]).to_integral_value(rounding=ROUND_CEILING)),
        "bottom": int(((d(value["y"]) + d(value["height"])) * size["height"]).to_integral_value(rounding=ROUND_CEILING)),
    }


def region_geometry(value, size):
    expected = pixel_rect(value["rect"], size)
    require(value["pixel_rect"] == expected, "region:pixel_rect_mismatch")
    require(expected["right"] - expected["left"] >= 4, "region:too_narrow")
    require(expected["bottom"] - expected["top"] >= 4, "region:too_short")


def region_digest(value):
    source = value["source_binding"]
    return digest({
        "device_id": source["device_id"],
        "installation_binding": source["installation_binding"],
        "source_size": source["source_size"],
        "source_geometry_id": source["source_geometry_id"],
        "rect": value["rect"],
        "pixel_rect": value["pixel_rect"],
    })


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def preview(value):
    source = value["source_binding"]
    region_geometry(value, source["source_size"])
    require(value["region_digest"] == region_digest(value), "preview:digest_mismatch")
    created, expires = timestamp(value["created_at"]), timestamp(value["expires_at"])
    require((expires - created).total_seconds() == 120, "preview:lifetime")
    require(timestamp(source["server_received_at"]) <= created, "preview:received_in_future")
    if value["status"] == "verified":
        quad, area = value["detected_quad"], value["pixel_rect"]
        for point in quad:
            require(area["left"] < point["x"] < area["right"], "preview:quad_touches_crop")
            require(area["top"] < point["y"] < area["bottom"], "preview:quad_touches_crop")
        cross = []
        for index in range(4):
            a, b, c = quad[index], quad[(index + 1) % 4], quad[(index + 2) % 4]
            cross.append((b["x"] - a["x"]) * (c["y"] - b["y"]) - (b["y"] - a["y"]) * (c["x"] - b["x"]))
        require(all(item > 0 for item in cross), "preview:quad_not_clockwise_convex")


def publication(value):
    before, after, record = value["before"], value["after"], value["record"]
    request, response, prev = value["request"], value["response"], value["preview"]
    semantic("region", "record", record)
    preview(prev)
    require(before["kind"] == after["kind"] == "phone_search_region", "save:pointer_kind")
    require(before["device_id"] == after["device_id"] == record["device_id"] == prev["source_binding"]["device_id"], "save:device_mismatch")
    require(value["principal_id"] == value["preview_principal_id"] == record["created_by"], "save:principal_mismatch")
    require(request["expected_revision"] == before["revision"], "save:cas_conflict")
    require(request["preview_id"] == prev["preview_id"], "save:preview_mismatch")
    require(request["region_digest"] == prev["region_digest"], "save:digest_mismatch")
    require(value["binding_unchanged"] and value["source_fresh"], "save:binding_or_source")
    now = timestamp(value["now"])
    require(timestamp(prev["created_at"]) <= now < timestamp(prev["expires_at"]), "save:preview_expired")
    require(record["rect"] == prev["rect"] and record["pixel_rect"] == prev["pixel_rect"], "save:rect_mismatch")
    for key in ("installation_binding", "source_size", "source_geometry_id"):
        require(record[key] == prev["source_binding"][key], "save:source_mismatch:" + key)
    require(record["reference_evidence_id"] == prev["source_binding"]["evidence_id"], "save:evidence_mismatch")
    require(timestamp(record["created_at"]) == now, "save:creation_time")
    require(record["version"] > (before["selected_version"] or 0), "save:version_not_new")
    require(not response["replayed"], "save:use_replay_contract_not_new_transition")
    require(request["correlation_id"] == response["correlation_id"], "save:correlation_mismatch")
    require(response["saved_version"] == record["version"], "save:saved_version")
    require(response["revision"] == after["revision"] and response["selected_version"] == after["selected_version"], "save:response_pointer")
    if request["publish"]:
        require(value["device_idle"], "save:device_busy")
        require(prev["status"] == "verified", "save:preview_unverified")
        require(response["status"] == "verified", "save:response_status")
        require(after["revision"] == before["revision"] + 1, "save:revision_step")
        require(after["selected_version"] == record["version"] and after["selected_sha256"] == record["sha256"], "save:selection")
        require(after["operation_id"] == response["operation_id"], "save:operation_mismatch")
        require(timestamp(after["updated_at"]) == now, "save:updated_time")
    else:
        require(before == after, "save:draft_changed_pointer")
        require(response["status"] == "draft_saved", "save:response_status")


def ime_snapshot(value):
    require({item["id"] for item in value["targets"]} == {"ime_candidate_bar", "ime_delete_key"}, "ime:target_ids")
    display, keyboard = value["display"], value["keyboard_bounds"]
    if keyboard is not None:
        bounds(keyboard, display["width_px"], display["height_px"])
    for target in value["targets"]:
        if target["bounds"] is None:
            continue
        box = target["bounds"]
        bounds(box, display["width_px"], display["height_px"])
        require(keyboard is not None, "ime:target_without_keyboard")
        require(keyboard["left"] <= box["left"] < box["right"] <= keyboard["right"], "ime:target_outside_keyboard")
        require(keyboard["top"] <= box["top"] < box["bottom"] <= keyboard["bottom"], "ime:target_outside_keyboard")


def ime_receipt(value):
    if value["request_nonce"] is not None:
        require(value["nonce_expires_at_ms"] == value["nonce_issued_at_ms"] + 2000, "ime:nonce_deadline_changed")
        require(0 <= value["received_monotonic_ms"] - value["nonce_issued_at_ms"] <= 1000, "ime:first_receipt_late")


def ime_acceptance(value):
    snapshot = value["snapshot"]
    ime_snapshot(snapshot)
    require(snapshot["ime_device_id"] == value["authenticated_device_id"], "ime:device_mismatch")
    require(snapshot["runtime_session_id"] == value["active_runtime_session_id"], "ime:session_stale")
    require(snapshot["view_instance_id"] not in value["retired_view_ids"], "ime:view_retired")
    require(value["current_view_instance_id"] not in value["retired_view_ids"], "ime:invalid_context")
    require((value["max_seq"] == 0) == (value["previous_snapshot_sha256"] is None), "ime:sequence_context")
    require(snapshot["seq"] >= value["max_seq"], "ime:sequence_stale")
    if snapshot["seq"] == value["max_seq"]:
        require(digest(snapshot) == value["previous_snapshot_sha256"], "ime:sequence_conflict")
        require(snapshot["view_instance_id"] == value["current_view_instance_id"], "ime:replay_view_changed")
        require(value["first_receipt"] is not None, "ime:replay_receipt_missing")
        require(value["receipt_after"] == value["first_receipt"], "ime:replay_receipt_changed")
    else:
        require(value["first_receipt"] is None, "ime:new_seq_has_receipt")
    require(snapshot["captured_uptime_ms"] >= value["max_captured_uptime_ms"], "ime:uptime_regressed")
    nonce = value["pending_nonce"]
    if snapshot["request_nonce"] is not None:
        require(nonce is not None, "ime:nonce_missing")
        require(snapshot["request_nonce"] == nonce["request_nonce"], "ime:nonce_mismatch")
        require(snapshot["runtime_session_id"] == nonce["runtime_session_id"], "ime:nonce_session")
        require(snapshot["view_instance_id"] == nonce["view_instance_id"] == value["current_view_instance_id"], "ime:nonce_view")
        age = value["now_ms"] - nonce["issued_at_ms"]
        require(0 <= age <= 2000, "ime:nonce_expired")
        if snapshot["seq"] > value["max_seq"]:
            require(age <= 1000, "ime:first_answer_late")
    receipt = value["receipt_after"]
    for key in ("runtime_session_id", "view_instance_id", "seq", "request_nonce"):
        require(receipt[key] == snapshot[key], "ime:receipt_identity")
    require(receipt["snapshot_sha256"] == digest(snapshot), "ime:receipt_digest")
    require(receipt["received_monotonic_ms"] <= value["now_ms"], "ime:receipt_in_future")
    if snapshot["seq"] > value["max_seq"]:
        require(receipt["received_monotonic_ms"] == value["now_ms"], "ime:first_receipt_monotonic")
        require(receipt["server_received_at"] == value["server_now"], "ime:first_receipt_wall_time")
    if snapshot["request_nonce"] is not None:
        require(receipt["nonce_issued_at_ms"] == nonce["issued_at_ms"], "ime:nonce_clock_changed")
    ime_receipt(receipt)
    response = value["report_response"]
    require(response["accepted_seq"] == snapshot["seq"], "ime:ack_seq")
    require(response["geometry_valid"] == snapshot["geometry_valid"], "ime:ack_validity")
    require(response["server_received_at"] == receipt["server_received_at"], "ime:ack_time_refreshed")


def ime_contact(value):
    acceptance, now = value["acceptance"], value["now_ms"]
    ime_acceptance(acceptance)
    snapshot, nonce = acceptance["snapshot"], acceptance["pending_nonce"]
    require(snapshot["geometry_valid"], "ime:geometry_invalid")
    require(snapshot["reason"] == "snapshot" and nonce is not None, "ime:contact_requires_nonce")
    require(acceptance["max_seq"] == snapshot["seq"], "ime:contact_not_current_seq")
    receipt = acceptance["receipt_after"]
    require(now >= acceptance["now_ms"], "ime:contact_before_acceptance")
    require(0 <= now - receipt["received_monotonic_ms"] <= 1000, "ime:snapshot_expired")
    require(receipt["nonce_issued_at_ms"] <= now <= receipt["nonce_expires_at_ms"], "ime:nonce_expired")
    require(value["operation_id"] == nonce["operation_id"], "ime:nonce_operation")
    require(value["camera_matches"], "ime:camera_mismatch")
    require(value["input_claim_valid"], "ime:input_claim_revoked")
    require(value["expected_display"] == snapshot["display"], "ime:display_mismatch")
    require(value["expected_layout_version"] == snapshot["layout_version"], "ime:layout_mismatch")
    target = next(item for item in snapshot["targets"] if item["id"] == value["target_id"])
    require(target["clickable"], "ime:target_not_clickable")
    if value["target_id"] == "ime_candidate_bar":
        require(value["owner_task_id"] is not None and value["owner_task_id"] == snapshot["current_task_id"], "ime:candidate_task_mismatch")


def ime_handshake(value):
    client, session = value["active_client_instance_id"], value["active_runtime_session_id"]
    request, response = value["request"], value["response"]
    require((client is None) == (session is None), "ime:handshake_context")
    require(request["client_instance_id"] not in value["retired_client_instance_ids"], "ime:retired_client")
    if client == request["client_instance_id"]:
        require(response["runtime_session_id"] == session, "ime:handshake_replay_changed_session")
    elif session is not None:
        require(response["runtime_session_id"] != session, "ime:new_client_reused_session")


def asset(value, asset_root=None):
    path = PurePosixPath(value["relative_path"])
    require(not path.is_absolute() and path.parts[0] == "assets", "asset:unsafe_path")
    require(all(part not in (".", "..") for part in path.parts), "asset:unsafe_path")
    require("\\" not in value["relative_path"] and "%" not in value["relative_path"], "asset:unsafe_path")
    suffix = path.suffix
    require((suffix == ".png") == (value["media_type"] == "image/png"), "asset:media_mismatch")
    if asset_root is not None:
        root = Path(asset_root).absolute()
        # Test-only filesystem walk; a production importer needs openat/O_NOFOLLOW
        # or an equivalent race-safe extraction sandbox as well.
        for parent in (root, *root.parents):
            require(not parent.is_symlink(), "asset:symlink")
        current = root
        for part in path.parts:
            current = current / part
            require(not current.is_symlink(), "asset:symlink")
        require(current.is_file(), "asset:file_missing")
        require(root.resolve() in current.resolve().parents, "asset:unsafe_path")
        require(current.stat().st_size <= 200 * 1024 * 1024, "asset:size_limit")
        require(hashlib.sha256(current.read_bytes()).hexdigest() == value["sha256"], "asset:digest_mismatch")


def unique(values, key, code):
    keys = [key(value) for value in values]
    require(len(keys) == len(set(keys)), code)


def four(values):
    states = [value["state"] for value in values]
    require(states[:4] == list(STATES) and len(states) == len(set(states))
            and 4 <= len(states) <= 12, "layout:four_states_order")


def inside(inner, outer):
    return (d(inner["x"]) >= d(outer["x"]) and d(inner["y"]) >= d(outer["y"])
            and d(inner["x"]) + d(inner["width"]) <= d(outer["x"]) + d(outer["width"])
            and d(inner["y"]) + d(inner["height"]) <= d(outer["y"]) + d(outer["height"]))


def overlap(a, b):
    return (max(d(a["x"]), d(b["x"])) < min(d(a["x"]) + d(a["width"]), d(b["x"]) + d(b["width"]))
            and max(d(a["y"]), d(b["y"])) < min(d(a["y"]) + d(a["height"]), d(b["y"]) + d(b["height"])))


def relative_box(anchor, relative):
    return {"x": float(d(anchor["x"]) + d(relative["dx"])),
            "y": float(d(anchor["y"]) + d(relative["dy"])),
            "width": relative["width"], "height": relative["height"]}


def checked_box(value):
    require(0 <= d(value["x"]) < 1 and 0 <= d(value["y"]) < 1, "layout:rect_origin")
    rect(value)


def compatibility(value, complete=False):
    for key, item in value.items():
        if key == "sources":
            continue
        require((item is None) == (value["sources"][key] is None), "layout:unknown_source")
        if complete:
            require(item is not None, "layout:compatibility_unknown")


def compatibility_values(value):
    return {key: item for key, item in value.items() if key != "sources"}


def screen(value):
    width, height, inset = value["width_px"], value["height_px"], value["system_insets"]
    require(height > width, "layout:not_portrait")
    require(inset["left"] + inset["right"] < width and inset["top"] + inset["bottom"] < height, "layout:insets")


def page(value):
    for field in ("anchors", "targets", "regions"):
        unique(value[field], lambda item: item["id"], "layout:duplicate_" + field)
    anchors = {item["id"]: item for item in value["anchors"]}
    required = [item for item in value["anchors"] if item["required"]]
    require(len(required) >= 2, "layout:two_required_anchors")
    require(any(not overlap(a["reference_rect"], b["reference_rect"]) for a in required for b in required), "layout:separate_anchors")
    for anchor in value["anchors"]:
        require(inside(anchor["reference_rect"], anchor["search_region"]), "layout:anchor_search_region")
    resolved = {}
    for field in ("targets", "regions"):
        for item in value[field]:
            require(item["anchor_id"] in anchors, "layout:anchor_reference")
            box = relative_box(anchors[item["anchor_id"]]["reference_rect"], item["relative_rect"])
            checked_box(box)
            resolved[field, item["id"]] = box
    target_ids = {item["id"] for item in value["targets"]}
    purposes = {item["purpose"] for item in value["regions"]}
    text_page = value.get("observation_kind") == "text_regions.v1"
    required_targets = (set() if text_page else {"wechat_chats_tab"}
                        if value["state"] == "wechat_list" else {"back_button", "input_box"})
    if value["state"] == "chat_ime_text":
        required_targets.add("send_button")
        require("send_button_feature" in anchors, "layout:send_feature_missing")
    require(required_targets <= target_ids, "layout:required_target_missing")
    require(len(purposes) == len(value["regions"]), "layout:duplicate_region_purpose")
    required_purposes = ({"input", "content"} if text_page else {"unread", "content"}
                         if value["state"] == "wechat_list" else {"title", "input", "content"})
    require(required_purposes <= purposes, "layout:required_region_missing")
    if value["state"] in ("chat_ime_empty", "chat_ime_text"):
        require("keyboard_boundary" in anchors, "layout:keyboard_boundary_missing")
    regions = [item for item in value["regions"] if item["purpose"] in ("title", "input", "content")]
    for index, a in enumerate(regions):
        for b in regions[index + 1:]:
            require(not overlap(resolved["regions", a["id"]], resolved["regions", b["id"]]), "layout:region_overlap")


def anchor_result(value):
    count = value["candidate_count"]
    if count == 0:
        require(all(value[key] is None for key in ("matched_rect", "best_score", "runner_up_score", "localization_error_bound_px")), "layout:unmeasured_not_null")
    else:
        require(value["matched_rect"] is not None and value["best_score"] is not None, "layout:measurement_missing")
        require((count == 1) == (value["runner_up_score"] is None), "layout:runner_up")
        if count > 1:
            require(value["runner_up_score"] <= value["best_score"], "layout:score_order")
    if value["status"] == "passed":
        require(count > 0 and value["reason_code"] is None and value["localization_error_bound_px"] is not None, "layout:anchor_pass_fields")
        require(value["best_score"] >= 0.90, "layout:score_threshold")
        if count > 1:
            require(d(value["best_score"]) - d(value["runner_up_score"]) >= d("0.08"), "layout:score_margin")
    else:
        require(value["reason_code"] is not None, "layout:failure_reason")


def item_result(value):
    if value["status"] == "passed":
        require(value["resolved_rect"] is not None and value["reason_code"] is None, "layout:item_pass_fields")
    else:
        require(value["reason_code"] is not None and value["safe_rect"] is None, "layout:failed_safe_rect")
    if value["kind"] == "region":
        require(value["safe_rect"] is None, "layout:region_safe_rect")
    if value["safe_rect"] is not None:
        require(value["resolved_rect"] is not None and inside(value["safe_rect"], value["resolved_rect"]), "layout:safe_rect_outside")


def page_check(value):
    unique(value["anchor_results"], lambda item: item["id"], "layout:duplicate_anchor_result")
    unique(value["item_results"], lambda item: (item["kind"], item["id"]), "layout:duplicate_item_result")
    results = value["anchor_results"] + value["item_results"]
    require(all(item["evidence_ids"] == value["evidence_ids"] for item in value["item_results"]), "layout:item_evidence")
    if value["status"] == "passed":
        require(all(item["status"] == "passed" for item in results) and not value["reason_codes"], "layout:page_hides_failure")
    else:
        require(bool(value["reason_codes"]), "layout:page_reason_missing")
        require(any(item["status"] != "passed" for item in results) or any(reason in value["reason_codes"] for reason in ("wrong_page", "unsupported_layout", "compatibility_unknown", "compatibility_mismatch")), "layout:page_failure_missing")
    if value["status"] == "unsupported":
        require("unsupported_layout" in value["reason_codes"], "layout:unsupported_reason")


def identity(record, kind):
    return {"id": record["template_id" if kind == "template" else "device_id"],
            "version": record["version"], "sha256": record["sha256"]}


def passed_keys(checks):
    keys = []
    for check in checks:
        for item in check["anchor_results"]:
            if item["status"] == "passed":
                keys.append({"page_state": check["state"], "item_kind": "anchor", "item_id": item["id"]})
        for item in check["item_results"]:
            if item["status"] == "passed":
                keys.append({"page_state": check["state"], "item_kind": item["kind"], "item_id": item["id"]})
    order = {check["state"]: index for index, check in enumerate(checks)}
    return sorted(keys, key=lambda item: (order[item["page_state"]], item["item_kind"], item["item_id"]))


def configuration(session):
    return digest({key: session[key] for key in (
        "device_id", "template_ref", "installation_region_ref", "binding_snapshot",
        "compatibility_snapshot", "keyboard_geometry_mode")})


def capture(value):
    region_capture = value["owner_kind"] == "search_region"
    require((value["owner_id"] is None) == region_capture, "capture:owner")
    require((value["state"] is None) == region_capture, "capture:state")
    require(len(value["evidence"]) == (1 if region_capture else 2), "capture:frame_count")
    unique(value["evidence"], lambda item: item["evidence_id"], "capture:duplicate_evidence")
    for evidence in value["evidence"]:
        require(evidence["capture_id"] == value["capture_id"] and evidence["page_state"] == value["state"], "capture:evidence_scope")
        require(evidence["device_id"] == value["device_id"] and evidence["principal_id"] == value["principal_id"], "capture:identity")
    require((timestamp(value["expires_at"]) - timestamp(value["created_at"])).total_seconds() == 120, "capture:lifetime")
    if not region_capture:
        a, b = value["evidence"]
        for key in ("device_id", "installation_binding", "source_size", "source_geometry_id", "source_session_id"):
            require(a["source_binding"][key] == b["source_binding"][key], "capture:source_changed")
        require(a["server_instance_id"] == b["server_instance_id"], "capture:clock_changed")
        require(a["timing_proof"]["source_clock_id"] == b["timing_proof"]["source_clock_id"], "capture:clock_changed")
        require(a["source_binding"]["frame_seq"] < b["source_binding"]["frame_seq"], "capture:sequence")
        require(a["timing_proof"]["source_capture_seq"] < b["timing_proof"]["source_capture_seq"], "capture:sampling_sequence")
        interval = d(b["timing_proof"]["source_captured_monotonic_ns"] - a["timing_proof"]["source_captured_monotonic_ns"]) / 1000000
        uncertainty = d(a["timing_proof"]["source_capture_uncertainty_ms"]) + d(b["timing_proof"]["source_capture_uncertainty_ms"])
        require(interval - uncertainty >= 200, "capture:interval")


def layout_semantic(definition, value):
    if definition == "compatibility":
        compatibility(value)
    elif definition in ("screen", "reference_screen"):
        screen(value)
    elif definition == "page":
        page(value)
    elif definition == "template":
        four(value["pages"])
        compatibility(value["compatibility"], True)
        require((value["version"] == 1) == (value["parent_version"] is None), "layout:parent_missing")
        if value["parent_version"] is not None:
            require(value["parent_version"] < value["version"], "layout:parent_order")
        unique(value["assets"], lambda item: item["id"], "layout:duplicate_asset")
        unique(value["assets"], lambda item: item["relative_path"].casefold(), "layout:duplicate_asset_path")
        assets = {item["id"] for item in value["assets"]}
        require(all(anchor["asset_id"] in assets for page_item in value["pages"] for anchor in page_item["anchors"]), "layout:asset_reference")
        require(value["sha256"] == record_digest(value), "layout:digest_mismatch")
    elif definition == "anchor_result":
        anchor_result(value)
    elif definition == "item_result":
        item_result(value)
    elif definition == "page_check":
        page_check(value)
    elif definition in ("page_checks", "colors"):
        four(value)
    elif definition == "override":
        require(not {"tapDepth", "tapDwellMs"}.intersection(value)
                or value["kind"] == "target", "layout:contact_target_only")
    elif definition == "overrides":
        unique(value, lambda item: (item["state"], item["kind"], item["item_id"]), "layout:duplicate_override")
    elif definition == "color_sample":
        require((value["empty_input_asset_ref"] is not None) == (value["state"] in ("chat_ime_hidden", "chat_ime_empty")), "layout:empty_reference_state")
    elif definition == "adaptation":
        require(value["installation_region_ref"]["id"] == value["device_id"], "layout:region_owner")
        compatibility(value["compatibility_observed"], True)
        require(value["sha256"] == record_digest(value), "layout:digest_mismatch")
    elif definition == "evidence":
        require(value["device_id"] == value["source_binding"]["device_id"], "capture:evidence_device")
        require(value["evidence_id"] == value["source_binding"]["evidence_id"], "capture:evidence_id")
        proof = value["timing_proof"]
        require(value["source_binding"]["source_session_id"] == proof["source_generation_id"], "capture:source_generation")
        require(value["source_binding"]["frame_seq"] == proof["source_frame_seq"], "capture:source_sequence")
        require(value["captured_monotonic_ms"] == proof["source_captured_monotonic_ns"] // 1000000, "capture:derived_clock")
        age = d(value["received_monotonic_ms"]) - d(proof["source_captured_monotonic_ns"]) / 1000000
        require(0 <= age and age + d(proof["source_capture_uncertainty_ms"]) <= 1000, "capture:stale_source")
    elif definition == "capture":
        capture(value)
    elif definition == "draft_page":
        if value["definition"] is None:
            require(value["page_check"] is None and value["updated_at"] is None and not value["source_evidence_ids"], "draft:uncaptured_fields")
            require(bool(value["missing_items"]) or value["state"].startswith("app_"), "draft:missing_items")
        else:
            require(value["definition"]["state"] == value["state"], "draft:page_state")
            require(value["updated_at"] is not None, "draft:updated_at")
            if value["page_check"] is not None:
                require(value["page_check"]["state"] == value["state"], "draft:check_state")
                require(value["source_evidence_ids"] == value["page_check"]["evidence_ids"], "draft:evidence")
    elif definition == "draft":
        four(value["pages"])
        unique(value["assets"], lambda item: item["asset"]["id"], "draft:duplicate_asset")
        require(timestamp(value["updated_at"]) >= timestamp(value["created_at"]), "draft:time_order")
    elif definition == "session_page" and value["capture"] is not None:
        require(value["capture"]["check"]["state"] == value["state"] == value["capture"]["color_sample"]["state"], "session:page_state")
    elif definition == "session":
        four(value["pages"])
        require(value["installation_region_ref"]["id"] == value["device_id"], "session:region_owner")
        require((timestamp(value["expires_at"]) - timestamp(value["created_at"])).total_seconds() == 1800, "session:lifetime")
        for entry in value["pages"]:
            if entry["capture"] is not None:
                require(entry["capture"]["configuration_sha256"] == configuration(value), "session:configuration_changed")
                reference = entry["capture"]["color_sample"]["empty_input_asset_ref"]
                if reference is not None:
                    require(any(record["asset"]["id"] == reference["id"] and record["asset"]["sha256"] == reference["sha256"] for record in value["assets"]), "session:color_asset_missing")
        unique(value["assets"], lambda item: item["asset"]["id"], "session:duplicate_asset")
    elif definition == "preview_record":
        require((timestamp(value["expires_at"]) - timestamp(value["created_at"])).total_seconds() == 120, "preview:lifetime")
        require(value["accepted_items"] == passed_keys(value["page_checks"]), "preview:accepted_items")


def graph(value):
    template, adaptation, region = value["template"], value["adaptation"], value["region"]
    require(adaptation["template_ref"] == identity(template, "template"), "graph:template_ref")
    require(adaptation["installation_region_ref"] == identity(region, "region"), "graph:region_ref")
    require(compatibility_values(adaptation["compatibility_observed"]) == compatibility_values(template["compatibility"]), "graph:compatibility")
    calibration = value["calibration"]
    require(calibration["geometry_hash"] == adaptation["screen_descriptor"]["calibration_geometry_hash"], "graph:calibration_hash")
    require(calibration["installation_binding"] == region["installation_binding"] and calibration["source_geometry_id"] == region["source_geometry_id"], "graph:calibration_binding")
    for definition, check in zip(template["pages"], adaptation["page_checks"]):
        require(definition["state"] == check["state"], "graph:state")
        anchors = {item["id"]: item for item in definition["anchors"]}
        actual_anchors = {item["id"]: item for item in check["anchor_results"]}
        require(set(anchors) == set(actual_anchors), "graph:anchor_coverage")
        expected = {(kind, item["id"]): item for kind, field in (("target", "targets"), ("region", "regions")) for item in definition[field]}
        keyboard = definition["state"] in ("chat_ime_empty", "chat_ime_text")
        live = adaptation["keyboard_geometry_mode"] == "live_ime_v1" and keyboard
        if live:
            for name in ("ime_candidate_bar", "ime_delete_key"):
                expected["target", name] = {"mode": "tap", "anchor_id": None}
        elif keyboard:
            require(all(("target", name) in expected for name in ("ime_candidate_bar", "ime_delete_key")), "graph:legacy_targets_missing")
        require(set(expected) == {(item["kind"], item["id"]) for item in check["item_results"]}, "graph:item_coverage")
        for item in check["item_results"]:
            definition_item = expected[item["kind"], item["id"]]
            if item["status"] != "passed":
                continue
            is_tap = item["kind"] == "target" and definition_item["mode"] == "tap"
            require((item["safe_rect"] is not None) == is_tap, "graph:safe_rect_mode")
            anchor_id = definition_item.get("anchor_id")
            error = 0 if anchor_id is None else actual_anchors[anchor_id]["localization_error_bound_px"]
            require(anchor_id is None or actual_anchors[anchor_id]["status"] == "passed", "graph:failed_dependency")
            if is_tap:
                require(error is not None, "graph:error_bound_missing")
                inset = max(2, math.ceil(d(calibration["mapping_error_bound_px"]) + d(error)))
                box, safe, screen_value = item["resolved_rect"], item["safe_rect"], adaptation["screen_descriptor"]
                for axis, extent, pixels in (("x", "width", screen_value["width_px"]), ("y", "height", screen_value["height_px"])):
                    expected_start = float(d(box[axis]) + d(inset) / d(pixels))
                    expected_extent = float(d(box[extent]) - 2 * d(inset) / d(pixels))
                    require(expected_extent > 0 and abs(safe[axis] - expected_start) <= 1e-12 and abs(safe[extent] - expected_extent) <= 1e-12, "graph:safety_inset")
    for override in adaptation["local_overrides"]:
        definition = next(item for item in template["pages"] if item["state"] == override["state"])
        field = {"anchor": "anchors", "target": "targets", "region": "regions"}[override["kind"]]
        require(override["item_id"] in {item["id"] for item in definition[field]}, "graph:override_item_missing")
        require(override["anchor_id"] in {item["id"] for item in definition["anchors"]}, "graph:override_anchor_missing")
        if override["kind"] == "anchor":
            require(override["anchor_id"] == override["item_id"], "graph:anchor_override_origin")
        require(not (adaptation["keyboard_geometry_mode"] == "live_ime_v1" and override["item_id"] in ("ime_candidate_bar", "ime_delete_key")), "graph:live_override_forbidden")
    assets = {(entry["asset"]["id"], entry["asset"]["sha256"]) for entry in value["assets"]}
    for color in adaptation["color_samples"]:
        if color["empty_input_asset_ref"] is not None:
            require((color["empty_input_asset_ref"]["id"], color["empty_input_asset_ref"]["sha256"]) in assets, "graph:color_asset_missing")
        check = next(item for item in adaptation["page_checks"] if item["state"] == color["state"])
        require(check["evidence_ids"] == color["evidence_ids"], "graph:color_evidence")


def session_case(value):
    session, preview_value = value["session"], value["preview"]
    require(value["device_idle"], "session:device_busy")
    require(timestamp(session["created_at"]) <= timestamp(value["now"]) < timestamp(session["expires_at"]), "session:expired")
    require(session["server_instance_id"] == value["server_instance_id"], "session:server_restarted")
    require(0 <= value["now_ms"] - session["created_monotonic_ms"] < 1800000, "session:monotonic_expired")
    require(session["binding_snapshot"] == value["current_binding"], "session:binding_changed")
    require(session["compatibility_snapshot"] == value["current_compatibility"], "session:compatibility_changed")
    require(session["template_ref"] == identity(value["template"], "template"), "session:template_changed")
    require(session["installation_region_ref"] == identity(value["region"], "region"), "session:region_changed")
    require(value["current_calibration"]["geometry_hash"] == session["binding_snapshot"]["screen_descriptor"]["calibration_geometry_hash"], "session:calibration_changed")
    captures = {item["capture_id"]: item for item in value["captures"]}
    require(len(captures) == len(value["captures"]) == len(session["pages"]), "session:duplicate_capture")
    for entry in session["pages"]:
        accepted = entry["capture"]
        require(accepted is not None, "session:page_missing")
        require(accepted["server_instance_id"] == value["server_instance_id"], "session:page_clock")
        require(0 <= (timestamp(value["now"]) - timestamp(accepted["accepted_at"])).total_seconds() <= 900, "session:page_expired")
        require(0 <= value["now_ms"] - accepted["accepted_monotonic_ms"] <= 900000, "session:page_monotonic_expired")
        require(accepted["capture_id"] in captures, "session:capture_missing")
        captured = captures[accepted["capture_id"]]
        require(accepted["accepted_at"] == captured["created_at"], "session:acceptance_refreshed")
        require(captured["device_id"] == session["device_id"] and captured["principal_id"] == session["principal_id"], "session:capture_identity")
        require(captured["owner_kind"] == "session", "session:capture_kind")
        require(accepted["check"]["evidence_ids"] == [item["evidence_id"] for item in captured["evidence"]], "session:page_evidence")
        for evidence in captured["evidence"]:
            for key in ("installation_binding", "source_size", "source_geometry_id"):
                require(evidence["source_binding"][key] == session["binding_snapshot"][key], "session:capture_binding")
        require(accepted["accepted_monotonic_ms"] >= captured["evidence"][-1]["received_monotonic_ms"], "session:acceptance_before_capture")
    require(preview_value["capture_session_id"] == session["capture_session_id"] and preview_value["session_revision"] == session["revision"], "session:preview_revision")
    require(preview_value["principal_id"] == session["principal_id"] and preview_value["device_id"] == session["device_id"], "session:preview_identity")
    require(preview_value["configuration_sha256"] == configuration(session), "session:preview_configuration")
    require(preview_value["binding_snapshot"] == session["binding_snapshot"], "session:preview_binding")
    require(preview_value["page_checks"] == [entry["capture"]["check"] for entry in session["pages"]], "session:preview_checks")
    require(preview_value["accepted_items"] == value["accepted_items"], "session:accepted_items")
    require(preview_value["consumed_by"] is None, "session:preview_consumed")
    require(timestamp(preview_value["created_at"]) <= timestamp(value["now"]) < timestamp(preview_value["expires_at"]), "session:preview_expired")
    require(preview_value["server_instance_id"] == value["server_instance_id"] and 0 <= value["now_ms"] - preview_value["created_monotonic_ms"] < 120000, "session:preview_clock")
    current = value["current_capture"]["evidence"][-1]
    require(current["evidence_id"] == preview_value["current_evidence_id"], "session:current_evidence")
    require(current["device_id"] == session["device_id"], "session:current_device")
    require(current["server_instance_id"] == value["server_instance_id"] and 0 <= value["now_ms"] - current["captured_monotonic_ms"] <= 1000, "session:current_frame_stale")
    for key in ("installation_binding", "source_size", "source_geometry_id"):
        require(current["source_binding"][key] == session["binding_snapshot"][key], "session:current_binding")


def request_hash(value):
    body = {key: child for key, child in value["body"].items() if key not in ("correlation_id", "client_request_id")}
    return digest({"route_id": value["route_id"], "method": value["method"], "path": value["path"], "query": value["query"], "body": body})


def check_scenario(definition, value):
    if definition == "graph":
        graph(value)
    elif definition == "session_case":
        session_case(value)
    elif definition == "page_edit_case":
        before, after = value["before"], value["after"]
        require(after["revision"] == before["revision"] + 1, "edit:revision")
        require({key: item for key, item in before.items() if key not in ("revision", "pages")} == {key: item for key, item in after.items() if key not in ("revision", "pages")}, "edit:session_changed")
        for old, new in zip(before["pages"], after["pages"]):
            if old["state"] != value["changed_state"]:
                require(old == new, "edit:other_page_changed")
    elif definition == "inherit_case":
        before, after = value["before"], value["after"]
        require(configuration(before) == configuration(after), "inherit:configuration")
        require(before["capture_session_id"] != after["capture_session_id"] and after["revision"] == 1, "inherit:new_session")
        require(after["server_instance_id"] == before["server_instance_id"] == value["server_instance_id"], "inherit:server_restarted")
        for old, new in zip(before["pages"], after["pages"]):
            if old["state"] in value["states"]:
                require(old == new and old["capture"] is not None, "inherit:refreshed_page")
                age = (timestamp(value["now"]) - timestamp(old["capture"]["accepted_at"])).total_seconds()
                require(0 <= age <= 900, "inherit:page_expired")
            else:
                require(new["capture"] is None, "inherit:implicit_page")
    elif definition == "crash_case":
        operation, intent = value["operation"], value["intent"]
        require(operation["operation_id"] == intent["operation_id"] and operation["request_hash"] == intent["request_hash"], "crash:intent_identity")
        complete = all(item in value["durable_files"] for item in intent["files"])
        pointer = value["observed_pointer"]
        switched = intent["pointer_after"] is not None and intent["pointer_after"] != intent["pointer_before"] and pointer == intent["pointer_after"]
        if complete and (value["store_committed"] or switched):
            expected = "committed" if pointer == intent["pointer_after"] else "unknown"
        elif not value["store_committed"] and pointer == intent["pointer_before"]:
            expected = "aborted"
        else:
            expected = "unknown"
        require(value["result_phase"] == expected, "crash:wrong_recovery_phase")
        require(value["result_pointer"] == pointer, "crash:replayed_pointer")
    elif definition == "replay_case":
        operation, intent, retry = value["operation"], value["intent"], value["retry_request"]
        require(operation["phase"] == "committed", "replay:not_committed")
        require(operation["principal_id"] == value["principal_id"], "replay:identity")
        require(operation["request_id"] == retry["body"]["client_request_id"], "replay:request_id")
        require(operation["request_hash"] == intent["request_hash"] == request_hash(retry), "replay:request_conflict")
        expected = copy.deepcopy(operation["response"])
        expected["body"]["replayed"] = True
        require(value["response"] == expected, "replay:result_changed")
    elif definition == "pagination_case":
        cursor = value["cursor"]
        for key in ("principal_id", "route_id", "device_id", "limit"):
            require(cursor[key] == value[key], "pagination:scope")
        require(timestamp(cursor["snapshot_at"]) <= timestamp(value["now"]) < timestamp(cursor["expires_at"]), "pagination:expired")
        source = cursor["template_items"] if cursor["route_id"] == "template_list" else cursor["issue_items"]
        start, limit, response = cursor["offset"], cursor["limit"], value["response"]
        require(response["items"] == source[start:start + limit], "pagination:snapshot_changed")
        require((response["next_cursor"] is None) == (start + limit >= len(source)), "pagination:continuation")
        require(response["snapshot_at"] == cursor["snapshot_at"] and response["snapshot_expires_at"] == cursor["expires_at"] and response["limit"] == limit, "pagination:metadata")


def decode64(value):
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise ContractError("binary:base64") from error
    require(base64.b64encode(data).decode("ascii") == value, "binary:noncanonical_base64")
    require(0 < len(data) <= MAX_BYTES, "binary:size_limit")
    return data


def image_bytes(data, declared=None):
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(data)) as image:
            require(image.format in ("PNG", "JPEG"), "image:format")
            require(image.width <= 8192 and image.height <= 8192 and image.width * image.height <= 20000000, "image:pixel_limit")
            require(getattr(image, "n_frames", 1) == 1, "image:animated")
            require(set(image.info) <= {"jfif", "jfif_version", "jfif_unit", "jfif_density", "transparency"}, "image:metadata")
            size = image.size
            media = "image/png" if image.format == "PNG" else "image/jpeg"
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ContractError("image:decode") from error
    if declared is not None:
        require(size == (declared["width"], declared["height"]), "image:dimensions")
        require(media == declared["media_type"], "image:media")
        require(hashlib.sha256(data).hexdigest() == declared["sha256"], "image:digest")
    return size, media


def package_semantic(definition, value):
    if definition == "entry":
        is_record = value["kind"] == "record"
        require((value["resource"] is not None) == is_record, "package:entry_resource")
        require((value["owner"] is None) == is_record and (value["asset"] is None) == is_record, "package:entry_owner")
        resource = value["resource"] if is_record else value["owner"]
        prefix = {"template": "templates", "region": "regions", "adaptation": "adaptations"}[resource["kind"]]
        suffix = "record.json" if is_record else value["asset"]["relative_path"]
        expected = f'{prefix}/{resource["ref"]["id"]}/{resource["ref"]["version"]}/{suffix}'
        require(value["path"] == expected, "package:entry_path")
        if not is_record:
            require(value["sha256"] == value["asset"]["sha256"], "package:asset_digest")
            require(resource["kind"] != "region", "package:region_asset")
    elif definition == "manifest":
        unique(value["entries"], lambda entry: entry["path"].casefold(), "package:duplicate_path")
        require(value["entries"] == sorted(value["entries"], key=lambda entry: entry["path"]), "package:entry_order")
        require(value["file_count"] == len(value["entries"]) + 1, "package:file_count")
        require(value["total_bytes"] == sum(entry["byte_size"] for entry in value["entries"]), "package:total_bytes")
        require(value["total_bytes"] + len(canonical(value)) <= MAX_BYTES, "package:total_limit")
        require(value["sha256"] == record_digest(value), "package:manifest_digest")
        resources = [entry["resource"] for entry in value["entries"] if entry["kind"] == "record"]
        unique(resources, lambda resource: (resource["kind"], resource["ref"]["id"], resource["ref"]["version"]), "package:duplicate_resource")
        require(all(root in resources for root in value["roots"]), "package:root_missing")
    elif definition == "import_preview_response":
        require(value["can_import"] == all(item["status"] != "conflict" for item in value["items"]), "package:conflict_hidden")
        require((timestamp(value["expires_at"]) - timestamp(value["created_at"])).total_seconds() == 120, "preview:lifetime")
        require([item["resource"] for item in value["items"]] == [entry["resource"] for entry in value["manifest"]["entries"] if entry["kind"] == "record"], "package:import_coverage")
    elif definition == "import_response":
        require(all(item["status"] != "conflict" for item in value["items"]), "package:import_conflict")
    elif definition in ("export_response", "export_record"):
        require(value["package_id"] == value["manifest"]["package_id"], "package:identity")
    elif definition == "import_preview_request":
        data = decode64(value["archive_base64"])
        require(hashlib.sha256(data).hexdigest() == value["archive_sha256"], "package:archive_digest")
        check_archive(data)


def resource_key(resource):
    return resource["kind"], resource["ref"]["id"], resource["ref"]["version"], resource["ref"]["sha256"]


def check_archive(data):
    require(0 < len(data) <= MAX_BYTES, "package:compressed_limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ContractError("package:zip_invalid") from error
    with archive:
        infos = archive.infolist()
        require(1 < len(infos) <= 5000, "package:file_count_limit")
        names = [info.filename for info in infos]
        require(len(names) == len(set(name.casefold() for name in names)), "package:duplicate_path")
        require("manifest.json" in names, "package:manifest_missing")
        total = 0
        payloads = {}
        for info in infos:
            name = info.filename
            require(name == info.orig_filename and "\x00" not in name, "package:null_path")
            require(name == "manifest.json" or re.fullmatch(SCHEMAS["package"]["definitions"]["path"]["pattern"], name), "package:unsafe_path")
            require(not any(part in (".", "..") for part in name.split("/")), "package:unsafe_path")
            mode = info.external_attr >> 16
            require(stat.S_IFMT(mode) in (0, stat.S_IFREG), "package:special_file")
            require(not info.is_dir() and not info.flag_bits & 1, "package:encrypted_or_directory")
            require(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), "package:compression")
            require(info.file_size <= MAX_BYTES - total, "package:expanded_limit")
            with archive.open(info) as stream:
                payload = stream.read(MAX_BYTES - total + 1)
            require(len(payload) == info.file_size, "package:size_mismatch")
            total += len(payload)
            require(total <= MAX_BYTES, "package:expanded_limit")
            payloads[name] = payload
    manifest = loads(payloads["manifest.json"].decode("utf-8"))
    validate("package", "manifest", manifest)
    require(payloads["manifest.json"] == canonical(manifest), "package:manifest_canonical")
    require(set(payloads) == {"manifest.json", *(entry["path"] for entry in manifest["entries"])}, "package:unlisted_file")
    records, owned_assets = {}, {}
    for entry in manifest["entries"]:
        payload = payloads[entry["path"]]
        require(len(payload) == entry["byte_size"], "package:entry_size")
        require(hashlib.sha256(payload).hexdigest() == entry["sha256"], "package:entry_digest")
        if entry["kind"] == "record":
            record = loads(payload.decode("utf-8"))
            resource = entry["resource"]
            family = "region" if resource["kind"] == "region" else "layout"
            definition = "record" if resource["kind"] == "region" else resource["kind"]
            validate(family, definition, record)
            require(canonical(record) == payload, "package:record_canonical")
            require(identity(record, resource["kind"]) == resource["ref"], "package:record_ref")
            records[resource_key(resource)] = record
        else:
            image_bytes(payload, entry["asset"])
            owned_assets.setdefault(resource_key(entry["owner"]), []).append(entry["asset"])
    required = set()
    pending = [resource_key(root) for root in manifest["roots"]]
    while pending:
        key = pending.pop()
        if key in required:
            continue
        require(key in records, "package:reference_missing")
        required.add(key)
        record = records[key]
        if key[0] == "adaptation":
            for kind, field in (("template", "template_ref"), ("region", "installation_region_ref")):
                pending.append(resource_key({"kind": kind, "ref": record[field]}))
        elif key[0] == "template" and record["parent_version"] is not None:
            parents = [candidate for candidate in records if candidate[:3] == ("template", key[1], record["parent_version"])]
            require(len(parents) == 1, "package:parent_missing")
            pending.extend(parents)
    require(required == set(records), "package:unselected_record")
    require(set(owned_assets) <= set(records), "package:asset_owner_missing")
    for key, record in records.items():
        assets = owned_assets.get(key, [])
        unique(assets, lambda item: item["id"], "package:duplicate_asset")
        if key[0] == "template":
            require(sorted(assets, key=lambda item: item["id"]) == sorted(record["assets"], key=lambda item: item["id"]), "package:template_assets")
        elif key[0] == "adaptation":
            expected = {(item["empty_input_asset_ref"]["id"], item["empty_input_asset_ref"]["sha256"]) for item in record["color_samples"] if item["empty_input_asset_ref"] is not None}
            require(expected == {(item["id"], item["sha256"]) for item in assets}, "package:adaptation_assets")
        else:
            require(not assets, "package:region_assets")
    return manifest


ERROR_STATUS = {
    "layout_request_invalid": 422, "layout_permission_denied": 403, "layout_resource_missing": 404,
    **{name: 409 for name in ("layout_revision_conflict", "layout_request_conflict", "layout_binding_changed", "layout_device_busy", "layout_preview_expired", "layout_items_unverified")},
    **{name: 503 for name in ("layout_source_unavailable", "layout_archive_invalid", "layout_storage_failed")},
    "ime_geometry_unauthenticated": 401, "ime_geometry_forbidden": 403,
    "ime_geometry_session_stale": 409, "ime_geometry_sequence_conflict": 409,
    "ime_geometry_nonce_expired": 409, "ime_geometry_invalid": 422, "ime_geometry_unavailable": 503,
}

STATE_DEFINITIONS = {
    "draft": ("layout", "draft"), "session": ("layout", "session"),
    "capture": ("layout", "capture"), "evidence": ("layout", "evidence"),
    "region_draft": ("records", "region_draft"), "region_preview": ("records", "region_preview"),
    "adaptation_preview": ("layout", "preview_record"), "rollback_preview": ("records", "rollback_preview"),
    "package_import_preview": ("package", "preview_record"), "package_export": ("package", "export_record"),
    "cursor": ("records", "cursor"), "review_revision": ("records", "review_revision"),
    "adaptation_asset": ("records", "adaptation_asset"), "catalog": ("records", "catalog"),
    "intent": ("records", "intent"),
}
STATE_MUTATIONS = {
    "drafts": "draft", "sessions": "session", "captures": "capture",
    "catalog": "catalog", "exports": "package_export", "region_drafts": "region_draft",
    "review_revisions": "review_revision", "adaptation_assets": "adaptation_asset",
}
CONSUMABLE_STATES = {"capture", "region_preview", "adaptation_preview", "rollback_preview", "package_import_preview"}


def state_key(kind, value):
    scalar = {
        "draft": "draft_id", "session": "capture_session_id", "capture": "capture_id",
        "adaptation_preview": "preview_id", "package_import_preview": "preview_id",
        "package_export": "package_id", "cursor": "cursor_id",
        "review_revision": "device_id", "intent": "operation_id",
    }
    if kind in scalar:
        parts = [value[scalar[kind]]]
    elif kind == "evidence":
        parts = [value["capture_id"], value["evidence_id"]]
    elif kind == "region_draft":
        parts = [value["device_id"], value["ref"]["version"]]
    elif kind in ("region_preview", "rollback_preview"):
        parts = [value["response"]["preview_id"]]
    elif kind == "adaptation_asset":
        parts = [value["owner_ref"]["id"], value["owner_ref"]["version"], value["record"]["asset"]["id"]]
    elif kind == "catalog":
        parts = [value["resource"]["kind"], value["resource"]["ref"]["id"], value["resource"]["ref"]["version"]]
    else:
        raise ContractError("state:unknown_kind")
    return digest([kind, *parts])


def state_owner(kind, value):
    if kind == "region_preview":
        return value["request"]["path"]["device_id"], value["principal_id"]
    if kind in ("package_import_preview", "package_export"):
        return None, value["principal_id"]
    if kind == "review_revision":
        return value["device_id"], None
    if kind == "adaptation_asset":
        return value["owner_ref"]["id"], None
    if kind == "catalog":
        return (None if value["resource"]["kind"] == "template" else value["resource"]["ref"]["id"]), None
    return value["device_id"], value["principal_id"]


def state_row(value):
    kind, payload = value["kind"], value["json"]
    validate(*STATE_DEFINITIONS[kind], payload)
    require(value["state_key"] == state_key(kind, payload), "state:key")
    if kind == "intent":
        # The principal and indirect draft device require the existing operation
        # row; a standalone JSON record cannot manufacture that trusted context.
        require(value["principal_id"] is not None, "state:intent_principal")
        route = payload["request"]
        if "device_id" in route["path"] or "device_id" in route["body"]:
            require(value["device_id"] == route["path"].get("device_id", route["body"].get("device_id")), "state:owner")
        elif route["route_id"] in ("template_publish", "package_export", "package_import"):
            require(value["device_id"] is None, "state:owner")
    else:
        require((value["device_id"], value["principal_id"]) == state_owner(kind, payload), "state:owner")
    if kind == "region_draft":
        require(payload["ref"]["id"] == payload["device_id"], "state:owner")
    if kind in ("draft", "session"):
        revision = payload["revision"]
    elif kind == "review_revision":
        revision = payload["revision"] + 1
    elif kind in CONSUMABLE_STATES:
        revision = 1 if payload["consumed_by"] is None else 2
    else:
        revision = 1
    require(value["revision"] == revision, "state:revision")


def state_cas(value):
    before, after, expected = value["before"], value["after"], value["expected_revision"]
    if before is None:
        require(expected == 0 and after["revision"] == 1, "state:insert_revision")
        return
    require(expected == before["revision"], "state:cas_conflict")
    require(all(before[key] == after[key] for key in ("kind", "state_key", "device_id", "principal_id")), "state:identity_changed")
    kind = before["kind"]
    require(kind in {"draft", "session", "review_revision"} | CONSUMABLE_STATES, "state:immutable")
    if kind in CONSUMABLE_STATES:
        require(before["json"]["consumed_by"] is None and after["json"]["consumed_by"] is not None, "state:already_consumed")
        require({key: item for key, item in before["json"].items() if key != "consumed_by"} ==
                {key: item for key, item in after["json"].items() if key != "consumed_by"}, "state:consumption_changed_payload")
    require(after["revision"] == expected + 1, "state:cas_conflict")


def api_semantic(definition, value):
    if definition == "capture_request":
        is_region = value["owner_kind"] == "search_region"
        require((value["owner_id"] is None) == is_region and (value["page_state"] is None) == is_region, "capture:owner")
        require(value["expected_owner_revision"] == 0 if is_region else value["expected_owner_revision"] > 0, "capture:owner_revision")
    elif definition == "session_create":
        require((value["inherit_session_id"] is None) == (not value["inherit_states"]), "session:inherit_fields")
    elif definition in ("adaptation_previewed", "rollback_previewed"):
        require((timestamp(value["expires_at"]) - timestamp(value["created_at"])).total_seconds() == 120, "preview:lifetime")
        if definition == "adaptation_previewed":
            require(value["accepted_items"] == passed_keys(value["page_checks"]), "preview:accepted_items")
        else:
            require((value["status"] == "verified") == (not value["reason_codes"]), "rollback:status")
    elif definition == "asset_upload":
        image_bytes(decode64(value["content_base64"]), value["asset"])
    elif definition in ("template_list", "issue_list"):
        require(len(value["items"]) <= value["limit"], "pagination:limit")
        require((timestamp(value["snapshot_expires_at"]) - timestamp(value["snapshot_at"])).total_seconds() == 900, "pagination:lifetime")
        if not value["items"]:
            require(value["next_cursor"] is None, "pagination:empty_continuation")
    elif definition == "adaptation_current":
        require((value["revision"] == 0) == (value["selected_ref"] is None), "layout:selection_revision")
        if value["status"] == "verified":
            require(value["selected_ref"] is not None and not value["blocking_reasons"], "layout:verified_selection")
            four(value["page_checks"])
            require(all(check["status"] == "passed" for check in value["page_checks"]), "layout:verified_hides_failure")
        if value["status"] in ("unregistered", "draft"):
            require(value["selected_ref"] is None and not value["page_checks"], "layout:unselected_checks")
        require(value["execution_blocked"] == bool(value["execution_blocking_reasons"]), "layout:execution_projection")
    elif definition == "operation_read":
        phase_fields(value, "original_response")
    elif definition == "rollback_preview":
        is_region = value["kind"] == "phone_search_region"
        require((value["source_evidence_id"] is not None) == is_region, "rollback:evidence")
        require((value["capture_session_id"] is None) == is_region and (value["expected_session_revision"] is None) == is_region, "rollback:session")
    elif definition == "review_result":
        require((value["state"] == "resolved") == (value["reason_code"] is None), "review:result")
    elif definition == "page_projection":
        require((value["status"] == "passed") == (not value["reason_codes"]), "session:projection_reason")
        require((value["status"] == "not_captured") == (value["expires_at"] is None), "session:projection_expiry")


def phase_fields(value, response_field):
    if value["phase"] == "committed":
        require(value[response_field] is not None and value["error"] is None, "operation:committed_fields")
    elif value["phase"] == "aborted":
        require(value[response_field] is None and value["error"] is not None, "operation:aborted_fields")
    else:
        require(value[response_field] is None, "operation:premature_response")
        if value["phase"] == "prepared":
            require(value["error"] is None, "operation:prepared_error")


def records_semantic(definition, value):
    if definition == "state_row":
        state_row(value)
    elif definition == "state_cas_case":
        state_cas(value)
    elif definition == "issue":
        require((value["state"] == "resolved") == (value["resolution"] is not None), "issue:resolution")
        require((value["kind"] == "item") == (value["item"] is not None), "issue:item_scope")
        require(value["resource_ref"]["id"] == value["device_id"], "issue:resource_owner")
        if value["resolution"]:
            require(timestamp(value["resolution"]["reviewed_at"]) >= timestamp(value["opened_at"]), "issue:time_order")
    elif definition == "operation":
        phase_fields(value, "response")
        require((value["device_id"] is None) == (value["resource_kind"] in ("template", "package")), "operation:device_scope")
        require((value["after_revision"] is not None) == (value["phase"] == "committed"), "operation:after_revision")
        require(timestamp(value["updated_at"]) >= timestamp(value["created_at"]), "operation:time_order")
        if value["response"] is not None:
            require(value["response"]["body"]["operation_id"] == value["operation_id"], "operation:response_identity")
        if value["error"] is not None:
            require(value["error"]["operation_id"] == value["operation_id"], "operation:error_identity")
    elif definition == "row":
        expected = {"operation": "operation_id", "issue": "issue_id", "review": "review_id"}[value["kind"]]
        require(value["json"].get(expected) == value["record_id"], "record:kind_or_id")
        require(value["json"]["device_id"] == value["device_id"], "record:device")
    elif definition == "cursor":
        templates = value["route_id"] == "template_list"
        require((value["device_id"] is None) == templates, "pagination:device")
        require(not (value["issue_items"] if templates else value["template_items"]), "pagination:wrong_items")
        require((timestamp(value["expires_at"]) - timestamp(value["snapshot_at"])).total_seconds() == 900, "pagination:lifetime")
        source = value["template_items"] if templates else value["issue_items"]
        require(value["offset"] < len(source), "pagination:offset")
    elif definition == "file_plan":
        path = value["relative_path"]
        require(not path.startswith("/") and all(part not in ("", ".", "..") for part in path.split("/")), "intent:unsafe_path")
    elif definition == "intent":
        require(value["request_hash"] == request_hash(value["request"]), "intent:request_hash")
        require(value["planned_response"]["route_id"] == value["request"]["route_id"], "intent:response_route")
        require(value["planned_response"]["body"]["operation_id"] == value["operation_id"], "intent:operation_id")
        require(not value["planned_response"]["body"]["replayed"], "intent:replayed")
        require(value["planned_response"]["body"]["correlation_id"] == value["request"]["body"]["correlation_id"], "intent:correlation")
        require((value["pointer_before"] is None) == (value["pointer_after"] is None), "intent:pointer_pair")
        if value["pointer_before"] is not None:
            before, after = value["pointer_before"], value["pointer_after"]
            require(before["device_id"] == after["device_id"] and before["kind"] == after["kind"], "intent:pointer_scope")
            if before != after:
                require(after["revision"] == before["revision"] + 1 and after["operation_id"] == value["operation_id"], "intent:pointer_revision")
        unique(value["files"], lambda item: item["relative_path"], "intent:duplicate_file")
        unique(value["mutations"], lambda item: (item["table"], item["key"]), "intent:duplicate_mutation")
    elif definition == "mutation":
        names = {"drafts": ("layout", "draft"), "sessions": ("layout", "session"),
                 "issues": ("records", "issue"), "reviews": ("records", "review"),
                 "captures": ("layout", "capture"), "catalog": ("records", "catalog"),
                 "exports": ("package", "export_record"), "region_drafts": ("records", "region_draft"),
                 "review_revisions": ("records", "review_revision"), "adaptation_assets": ("records", "adaptation_asset")}
        family, name = names[value["table"]]
        validate(family, name, value["value"])
        if value["table"] in STATE_MUTATIONS:
            kind = STATE_MUTATIONS[value["table"]]
            require(value["key"] == state_key(kind, value["value"]), "state:mutation_key")
            device_id, principal_id = state_owner(kind, value["value"])
            state_row({"kind": kind, "state_key": value["key"], "device_id": device_id, "principal_id": principal_id,
                       "revision": value["expected_revision"] + 1, "json": value["value"]})
        else:
            require(value["key"] == value["value"]["issue_id" if value["table"] == "issues" else "review_id"], "state:review_record_key")
    elif definition == "review_revision":
        require((value["revision"] == 0) == (value["updated_at"] is None), "review:revision_time")
        require((value["revision"] == 0) == (value["operation_id"] is None), "review:revision_operation")


def wire_semantic(definition, value):
    if definition in ("write_request", "write_response") or definition.endswith("_body"):
        return
    route = ROUTE_BY_ID[value["route_id"]]
    if definition.endswith("_error"):
        require(value["status"] == ERROR_STATUS[value["body"]["code"]], "http:error_status")
        if route["protocol"] == "fleet":
            require(value["headers"]["X-Correlation-Id"] == value["body"]["correlation_id"], "http:correlation")
        return
    request = definition.endswith("_request")
    body = value["body"]
    if route["protocol"] == "fleet" and isinstance(body, dict):
        if "X-Correlation-Id" in value["headers"]:
            require(value["headers"]["X-Correlation-Id"] == body["correlation_id"], "http:correlation")
        schema_name = route["request"] if request else route["response"]
        if schema_name and not schema_name.startswith("binary:"):
            family, name = schema_name.split(":")
            semantic(family, name, body)
    if request:
        if value["route_id"] == "draft_page":
            require(value["path"]["state"] == body["page_definition"]["state"], "http:page_path")
        if value["route_id"] == "session_page":
            require(all(item["state"] == value["path"]["state"] for item in body["local_overrides"]), "http:override_page")
        if value["route_id"] == "ime_report":
            require(value["path"]["ime_device_id"] == body["ime_device_id"], "http:ime_device")
            ime_snapshot(body)
    elif route["response"].startswith("binary:"):
        data = decode64(body)
        require(len(data) == value["headers"]["Content-Length"], "http:content_length")
        require('"' + hashlib.sha256(data).hexdigest() + '"' == value["headers"]["ETag"], "http:etag")
        if route["response"] == "binary:image":
            _, media = image_bytes(data)
            require(media == value["headers"]["Content-Type"], "http:media")
        else:
            check_archive(data)


def semantic(family, definition, value, asset_root=None):
    if family == "common":
        if definition == "rect":
            rect(value)
        elif definition in ("bounds", "pixel_rect"):
            bounds(value, value["right"], value["bottom"])
        elif definition == "asset":
            asset(value, asset_root)
    elif family == "region":
        if definition == "record":
            region_geometry(value, value["source_size"])
            require(value["sha256"] == record_digest(value), "region:digest_mismatch")
        elif definition == "preview_request":
            rect(value["rect"])
        elif definition == "preview_response":
            preview(value)
        elif definition == "save_response" and value["status"] == "verified":
            require(value["selected_version"] == value["saved_version"], "save:response_version")
        elif definition == "get_response":
            require((value["revision"] == 0) == (value["selected"] is None), "get:selection_revision")
            if value["selected"] is not None and value["pending_draft"] is not None:
                require(value["selected"]["id"] == value["pending_draft"]["id"], "get:draft_device_mismatch")
                require(value["selected"]["version"] != value["pending_draft"]["version"], "get:draft_is_selected")
            if value["status"] == "unregistered":
                require(value["selected"] is None and value["pending_draft"] is None and not value["reasons"], "get:unregistered_has_data")
            elif value["status"] == "verified":
                require(value["selected"] is not None and not value["reasons"], "get:verified_without_selection")
            else:
                require(bool(value["reasons"]), "get:review_without_reason")
        elif definition == "publication_case":
            publication(value)
    elif family == "ime":
        handlers = {"snapshot": ime_snapshot, "receipt": ime_receipt, "acceptance_case": ime_acceptance, "contact_case": ime_contact, "handshake_case": ime_handshake}
        if definition in handlers:
            handlers[definition](value)
    elif family == "layout":
        layout_semantic(definition, value)
    elif family == "api":
        api_semantic(definition, value)
    elif family == "records":
        records_semantic(definition, value)
    elif family == "package":
        package_semantic(definition, value)
    elif family == "wire":
        wire_semantic(definition, value)
    elif family == "checks":
        check_scenario(definition, value)


def semantic_tree(schema, value, family, seen, asset_root=None):
    if "$ref" in schema:
        uri = schema["$ref"]
        base, pointer = uri.split("#")
        target_family = family if not base else next(name for name, identifier in IDS.items() if identifier == base)
        name = pointer.split("/")[-1]
        key = (target_family, name, id(value))
        if key in seen:
            return
        seen.add(key)
        target = SCHEMAS[target_family]["definitions"][name]
        # Children first makes nested integrity errors visible before outer digests.
        semantic_tree(target, value, target_family, seen, asset_root)
        semantic(target_family, name, value, asset_root)
        return
    if isinstance(value, dict):
        for key, child in schema.get("properties", {}).items():
            if key in value:
                semantic_tree(child, value[key], family, seen, asset_root)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for item in value:
            semantic_tree(schema["items"], item, family, seen, asset_root)
    for keyword in ("anyOf", "oneOf"):
        for branch in schema.get(keyword, []):
            # Check a branch with the correct base for its local references.
            scoped = copy.deepcopy(branch)
            def fix(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key == "$ref" and child.startswith("#"):
                            value[key] = IDS[family] + child
                        else:
                            fix(child)
                elif isinstance(value, list):
                    for child in value:
                        fix(child)
            fix(scoped)
            if StrictValidator(scoped, registry=REGISTRY, format_checker=FormatChecker()).is_valid(value):
                semantic_tree(branch, value, family, seen, asset_root)
                break


def validate(family, definition, value, asset_root=None):
    require(family in SCHEMAS and definition in SCHEMAS[family]["definitions"], "schema:unknown_definition")
    finite(value)
    schema = {"$ref": IDS[family] + "#/definitions/" + definition}
    validator = StrictValidator(schema, registry=REGISTRY, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(value), key=lambda item: str(list(item.absolute_path)))
    if errors:
        error = errors[0]
        raise ContractError("schema:" + "/".join(map(str, error.absolute_path)) + ":" + error.message)
    if family in ("common", "region", "ime"):
        semantic(family, definition, value, asset_root)
    else:
        semantic_tree(schema, value, family, set(), asset_root)


def resolve_fixture(value, examples, trail=()):
    if isinstance(value, dict):
        if set(value) == {"$fixture"}:
            name = value["$fixture"]
            require(name not in trail, "fixture:cycle")
            return resolve_fixture(examples[name], examples, (*trail, name))
        return {key: resolve_fixture(child, examples, trail) for key, child in value.items()}
    if isinstance(value, list):
        return [resolve_fixture(child, examples, trail) for child in value]
    return copy.deepcopy(value)


def set_pointer(value, pointer, item, delete=False):
    require(pointer.startswith("/") and pointer != "/", "fixture:bad_pointer")
    parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer[1:].split("/")]
    parent = value
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = int(parts[-1]) if isinstance(parent, list) else parts[-1]
    if delete:
        del parent[key]
    else:
        parent[key] = copy.deepcopy(item)


def materialize(case, examples):
    value = resolve_fixture({"$fixture": case["base"]}, examples)
    for change in case.get("set", []):
        set_pointer(value, change["path"], resolve_fixture(change["value"], examples))
    for pointer in case.get("delete", []):
        set_pointer(value, pointer, None, delete=True)
    return value


def self_checks():
    passed = 0
    for bad in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":"\\ud800"}'):
        try:
            loads(bad)
        except ContractError:
            passed += 1
        else:
            raise ContractError("selfcheck:bad_json_accepted")
    for family, definition in (("common", "positive"), ("common", "uint"), ("common", "rect")):
        value = True if definition != "rect" else {"x": True, "y": 0, "width": 1, "height": 1}
        try:
            validate(family, definition, value)
        except ContractError:
            passed += 1
        else:
            raise ContractError("selfcheck:boolean_number")
    require(canonical({"b": 0.5, "a": "中"}) == b'{"a":"\xe4\xb8\xad","b":0.5}', "selfcheck:canonical")
    passed += 1
    with tempfile.TemporaryDirectory(prefix=".validation-", dir=ROOT) as name:
        root = Path(name)
        (root / "assets").mkdir()
        data = b"contract-test-only-not-an-image"
        sample = root / "assets" / "reference.png"
        sample.write_bytes(data)
        value = {"id": "reference", "relative_path": "assets/reference.png", "sha256": hashlib.sha256(data).hexdigest(), "media_type": "image/png", "width": 1, "height": 1}
        validate("common", "asset", value, root)
        passed += 1
        (root / "assets" / "link.png").symlink_to(sample)
        for bad, code in (
            ({**value, "relative_path": "assets/link.png"}, "asset:symlink"),
            ({**value, "sha256": "0" * 64}, "asset:digest_mismatch"),
            ({**value, "relative_path": "assets/missing.png"}, "asset:file_missing"),
        ):
            try:
                validate("common", "asset", bad, root)
            except ContractError as error:
                require(str(error).startswith(code), "selfcheck:wrong_asset_rejection")
                passed += 1
            else:
                raise ContractError("selfcheck:unsafe_asset_accepted")
    return passed


def state_sql_checks(examples):
    ddl = (ROOT / "typed-state.sql").read_text(encoding="utf-8")
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(ddl)
        columns = connection.execute("PRAGMA table_info(layout_typed_state)").fetchall()
        require([column[1] for column in columns] == ["kind", "state_key", "device_id", "principal_id", "revision", "json"], "state:ddl_columns")
        require([(column[1], column[5]) for column in columns if column[5]] == [("kind", 1), ("state_key", 2)], "state:ddl_primary_key")
        count = 2
        insert = "INSERT INTO layout_typed_state(kind,state_key,device_id,principal_id,revision,json) VALUES (?,?,?,?,?,?)"
        for kind in STATE_DEFINITIONS:
            row = examples["state-row-" + kind]
            values = (row["kind"], row["state_key"], row["device_id"], row["principal_id"], row["revision"], canonical(row["json"]).decode("utf-8"))
            connection.execute(insert, values)
            count += 1
        stored = connection.execute("SELECT kind,state_key,device_id,principal_id,revision,json FROM layout_typed_state").fetchall()
        for kind, key, device_id, principal_id, revision, text in stored:
            validate("records", "state_row", {"kind": kind, "state_key": key, "device_id": device_id, "principal_id": principal_id, "revision": revision, "json": loads(text)})
        count += 1
        row = examples["state-row-draft"]
        for values in (
            ("not_a_kind", "0" * 64, None, None, 1, "{}"),
            ("draft", "bad", None, None, 1, "{}"),
            ("draft", "0" * 64, None, None, 0, "{}"),
            ("draft", "0" * 64, None, None, 1.5, "{}"),
            ("draft", "0" * 64, None, None, 1, ""),
            (row["kind"], row["state_key"], row["device_id"], row["principal_id"], row["revision"], canonical(row["json"]).decode("utf-8")),
        ):
            try:
                connection.execute(insert, values)
            except sqlite3.IntegrityError:
                count += 1
            else:
                raise ContractError("state:ddl_invalid_insert")
        updated = copy.deepcopy(row["json"])
        updated["revision"] += 1
        sql = ("UPDATE layout_typed_state SET revision=revision+1,json=? "
               "WHERE kind=? AND state_key=? AND device_id IS ? AND principal_id IS ? AND revision=?")
        args = (canonical(updated).decode("utf-8"), row["kind"], row["state_key"], row["device_id"], row["principal_id"], row["revision"])
        require(connection.execute(sql, args).rowcount == 1, "state:ddl_cas")
        require(connection.execute(sql, args).rowcount == 0, "state:ddl_stale_cas")
        return count + 2
    finally:
        connection.close()


def all_fixtures():
    examples = read(ROOT / "fixtures" / "examples.json")
    cases = read(ROOT / "fixtures" / "cases.json")
    spec = importlib.util.spec_from_file_location("layout_completion_fixtures", ROOT / "fixtures" / "completion.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    extra_examples, extra_cases = module.build(sys.modules[__name__], examples)
    require(not (set(examples) & set(extra_examples)), "fixture:duplicate_example")
    examples.update(extra_examples)
    cases.extend(extra_cases)
    return examples, cases


def schema_checks():
    count = 0
    def walk(node, family):
        nonlocal count
        if isinstance(node, dict):
            if node.get("type") == "object" and set(node) != {"type"}:
                require(node.get("additionalProperties") is False, "schema:open_object")
                require("properties" in node and "required" in node, "schema:implicit_object")
                require(set(node["required"]) <= set(node["properties"]), "schema:unknown_required")
                count += 1
            require(node.get("additionalProperties") is not True, "schema:any_dict")
            if "$ref" in node:
                uri = node["$ref"]
                base, pointer = uri.split("#")
                target_family = family if not base else next((name for name, identifier in IDS.items() if identifier == base), None)
                require(target_family is not None, "schema:remote_ref")
                require(pointer.startswith("/definitions/") and pointer.split("/")[-1] in SCHEMAS[target_family]["definitions"], "schema:unresolved_ref")
            for child in node.values():
                walk(child, family)
        elif isinstance(node, list):
            for child in node:
                walk(child, family)
    for family, schema in SCHEMAS.items():
        walk(schema, family)
    for route in ROUTES:
        require(set(re.findall(r"\{([a-z_]+)\}", route["path"])) == set(route["path_parameters"]), "route:path_parameters")
    require(len(ROUTES) == len(ROUTE_BY_ID), "route:duplicate_id")
    require(len({(route["method"], route["path"]) for route in ROUTES}) == len(ROUTES), "route:duplicate_path")
    return count


def run_suite():
    for schema in SCHEMAS.values():
        Draft7Validator.check_schema(schema)
    objects = schema_checks()
    wire_path = ROOT / "wire.schema.json"
    require(wire_path.is_file() and read(wire_path) == SCHEMAS["wire"], "schema:wire_stale_run_write_wire_schema")
    examples, cases = all_fixtures()
    require(len({case["name"] for case in cases}) == len(cases), "fixture:duplicate_name")
    positives = negatives = 0
    for case in cases:
        value = materialize(case, examples)
        try:
            validate(case["family"], case["definition"], value)
        except ContractError as error:
            if case["valid"] or not str(error).startswith(case["error"]):
                raise ContractError(case["name"] + ": unexpected rejection: " + str(error)) from error
            negatives += 1
        else:
            require(case["valid"], case["name"] + ": invalid fixture accepted")
            positives += 1
    checked = self_checks()
    state_checked = state_sql_checks(examples)
    coverage = {case["definition"] for case in cases if case["family"] == "wire" and case["valid"]}
    require(all(route["id"] + suffix in coverage for route in ROUTES for suffix in ("_request", "_response", "_error")), "fixture:route_coverage")
    print(f"PASS: {len(SCHEMAS)} Draft07 schemas; {positives} positive / {negatives} negative fixtures; {checked} parser/resource self-checks.")
    print(f"PASS: {len(ROUTES)} routes with request/response/error fixtures; {objects} closed object schemas; all references local.")
    print(f"PASS: {state_checked} in-memory typed-state DDL/CAS checks; {len(STATE_DEFINITIONS)} kinds; existing review-record kinds unchanged.")
    print("Design contracts only. Synthetic PNG/ZIP validation; no application runtime, production data or hardware.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=sorted(IDS))
    parser.add_argument("--definition")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--assets-root", type=Path)
    parser.add_argument("--show-case")
    parser.add_argument("--digests", action="store_true")
    parser.add_argument("--write-wire-schema", action="store_true", help="Mechanically regenerate the full HTTP schema from the route table.")
    parser.add_argument("--show-route-schema")
    parser.add_argument("--check-package", type=Path)
    args = parser.parse_args()
    if args.write_wire_schema:
        (ROOT / "wire.schema.json").write_text(json.dumps(SCHEMAS["wire"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("Wrote design-only wire.schema.json.")
    elif args.show_route_schema:
        require(args.show_route_schema in ROUTE_BY_ID, "route:unknown")
        print(json.dumps({suffix: SCHEMAS["wire"]["definitions"][args.show_route_schema + "_" + suffix] for suffix in ("request", "response", "error")}, ensure_ascii=False, indent=2))
    elif args.check_package:
        require(args.check_package.stat().st_size <= MAX_BYTES, "package:compressed_limit")
        manifest = check_archive(args.check_package.read_bytes())
        print(f'PASS: {manifest["file_count"]} files; complete selected reference closure; pointers unchanged.')
    elif args.show_case:
        examples, cases = all_fixtures()
        case = next((item for item in cases if item["name"] == args.show_case), None)
        require(case is not None, "fixture:case_missing")
        print(json.dumps(materialize(case, examples), ensure_ascii=False, indent=2))
    elif args.digests:
        examples = read(ROOT / "fixtures" / "examples.json")
        for name in ("region", "region_draft", "preview", "ime_snapshot"):
            value = resolve_fixture({"$fixture": name}, examples)
            actual = record_digest(value) if name.startswith("region") else region_digest(value) if name == "preview" else digest(value)
            print(name + ": " + actual)
    elif args.input:
        require(args.family is not None and args.definition is not None, "cli:family_and_definition_required")
        validate(args.family, args.definition, read(args.input), args.assets_root)
        print("PASS: design payload only.")
    else:
        require(args.family is None and args.definition is None and args.assets_root is None, "cli:input_required")
        run_suite()


if __name__ == "__main__":
    try:
        main()
    except (ContractError, ValueError, OSError, KeyError, zipfile.BadZipFile) as error:
        print("FAIL: " + str(error), file=sys.stderr)
        sys.exit(1)
