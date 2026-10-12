"""Validate documentation-only camera identities without loading runtime or hardware."""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import struct

from jsonschema import Draft7Validator, FormatChecker, validators


ROOT = Path(__file__).resolve().parent
TYPE_CHECKER = Draft7Validator.TYPE_CHECKER.redefine(
    "integer", lambda _, value: type(value) is int
).redefine(
    "number", lambda _, value: type(value) in (int, float) and math.isfinite(value)
)
StrictValidator = validators.extend(Draft7Validator, type_checker=TYPE_CHECKER)
FORMAT = FormatChecker()


def require(condition, code):
    if not condition:
        raise ValueError(code)


def pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "json:duplicate_key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("json:nonfinite:" + value)


def loads(text):
    value = json.loads(text, object_pairs_hook=pairs_no_duplicates, parse_constant=reject_constant)

    def walk(item):
        if isinstance(item, dict):
            for key, child in item.items():
                walk(key)
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, str):
            item.encode("utf-8", errors="strict")
        elif isinstance(item, float):
            require(math.isfinite(item), "json:nonfinite")

    walk(value)
    return value


def read(name):
    return loads((ROOT / name).read_text(encoding="utf-8"))


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def resolve(value, samples, stack=()):
    if isinstance(value, dict) and set(value) == {"$sample"}:
        name = value["$sample"]
        require(name not in stack, "fixture:cycle")
        return resolve(samples[name], samples, stack + (name,))
    if isinstance(value, dict):
        return {key: resolve(child, samples, stack) for key, child in value.items()}
    if isinstance(value, list):
        return [resolve(child, samples, stack) for child in value]
    return value


def changed(value, case):
    result = copy.deepcopy(value)
    for pointer, child in case.get("set", {}).items():
        parent, key = pointer_parent(result, pointer)
        parent[key] = child
    for pointer in case.get("delete", []):
        parent, key = pointer_parent(result, pointer)
        del parent[key]
    return result


def pointer_parent(value, pointer):
    keys = pointer.removeprefix("/").split("/")
    parent = value
    for key in keys[:-1]:
        parent = parent[int(key)] if isinstance(parent, list) else parent[key]
    return parent, int(keys[-1]) if isinstance(parent, list) else keys[-1]


def identity_digest(value):
    keys = ("deviceId", "cameraId", "cameraDeviceUniqueId", "cameraBindingId",
            "sourceGeometryId", "daemonAddr", "frame")
    return digest({key: value[key] for key in keys})


def same_source(left, right):
    keys = ("device_id", "camera_id", "device_unique_id", "camera_binding_id",
            "source_geometry_id", "width", "height")
    return all(left[key] == right[key] for key in keys)


def semantic(definition, value):
    if definition in ("binding_operation", "binding_operation_view"):
        phase, response, error = value["phase"], value["response"], value["error"]
        if phase == "prepared":
            require(response is None and error is None, "operation:prepared_result")
        elif phase == "committed":
            require(error is None, "operation:committed_error")
            require((response is None) == (value["kind"] == "migrate"), "operation:committed_result")
        else:
            require(response is None and error is not None, "operation:failed_result")
    if definition == "source_geometry":
        crop, size = value["crop"], value["input_size"]
        require(0 <= crop["left"] < crop["right"] <= size["width"], "geometry:crop_x")
        require(0 <= crop["top"] < crop["bottom"] <= size["height"], "geometry:crop_y")
        match = re.fullmatch(r"fourcc:[0-9a-f]{8};size:(\d+)x(\d+);pixel-aspect:(\d+)/(\d+)", value["format_id"])
        width, height, numerator, denominator = map(int, match.groups())
        require((width, height) == (size["width"], size["height"]), "geometry:format_size")
        require(math.gcd(numerator, denominator) == 1, "geometry:pixel_aspect_not_reduced")
        width, height = crop["right"] - crop["left"], crop["bottom"] - crop["top"]
        if value["rotation_degrees"] in (90, 270):
            width, height = height, width
        if value["resampler"] == "none":
            require(value["output_size"] == {"width": width, "height": height}, "geometry:output_size")
    elif definition in ("binding_file", "legacy_binding_file"):
        for key in ("cameraId", "deviceUniqueId", "cameraBindingId"):
            if definition == "legacy_binding_file" and key == "cameraBindingId":
                continue
            found = [item[key] for item in value["bindings"]]
            require(len(found) == len(set(found)), "binding:duplicate:" + key)
    elif definition in ("binding_view", "binding_write_response"):
        is_unbound = value["status"] == "unbound"
        require((value["binding"] is None) == is_unbound, "binding:state")
        if value["binding"] is not None:
            require(value["binding"]["cameraId"] == value["camera_id"], "binding:camera")
    elif definition == "binding_operation":
        before, after = value["before"], value["after"]
        semantic("legacy_binding_file" if before["schemaVersion"] == 1 else "binding_file", before)
        semantic("binding_file", after)
        require(digest(before) == value["before_sha256"], "operation:before_hash")
        require(digest(after) == value["after_sha256"], "operation:after_hash")
        if value["kind"] == "migrate":
            require(value["camera_id"] is None and before["schemaVersion"] == 1, "operation:migration_scope")
            require(after["revision"] == before["revision"] + 1, "operation:migration_revision")
            old = {entry["cameraId"]: entry for entry in before["bindings"]}
            new = {entry["cameraId"]: entry for entry in after["bindings"]}
            require(set(old) == set(new), "operation:migration_devices")
            for camera, original in old.items():
                require({key: child for key, child in new[camera].items() if key != "cameraBindingId"} == original,
                        "operation:migration_rewrites_history")
        if value["phase"] == "prepared":
            require(value["response"] is None and value["error"] is None, "operation:prepared_result")
        if value["phase"] == "committed" and value["kind"] != "migrate":
            require(value["response"] is not None and value["error"] is None, "operation:committed_result")
        if value["response"] is not None:
            semantic("binding_write_response", value["response"])
            require(value["response"]["request_id"] == value["request_id"], "operation:response_request")
            require(value["response"]["revision"] == after["revision"], "operation:response_revision")
    elif definition == "calibration_identity":
        require(identity_digest(value) == value["bindingFingerprint"], "calibration:identity_hash")
    elif definition == "debug_context_response":
        if value["readyForMotion"]:
            require(value["readyForRead"] and value["daemonSession"] is not None and not value["checks"], "context:false_ready")
    elif definition == "clock_registration":
        require(value["source_clock_id"] == "mach-absolute:" + value["host_boot_fingerprint"], "clock:boot")
        previous = -1
        for sample in value["samples"]:
            require(sample["before_ns"] <= sample["helper_ns"] <= sample["after_ns"], "clock:bracket")
            require(sample["after_ns"] - sample["before_ns"] <= 20_000_000, "clock:latency")
            require(previous < sample["before_ns"], "clock:order")
            previous = sample["after_ns"]
    elif definition == "capture_session_request":
        semantic("source_geometry", value["geometry"])
    elif definition == "capture_session_view" and value["active_session"] is not None:
        require(value["active_session"]["server_instance_id"] == value["server_instance_id"], "capture:boot")
        require(value["active_session"]["camera_id"] == value["camera_id"], "capture:camera")
    elif definition == "screen_source_response":
        source, observation = value["source"]["source_frame"], value["observation"]
        mapping = {"device_id": "device_id", "camera_id": "camera_id",
                   "camera_device_unique_id": "device_unique_id", "binding_revision": "binding_revision",
                   "captured_at": "source_captured_at", "frame_seq": "source_frame_seq",
                   "frame_width": "width", "frame_height": "height"}
        for field, origin in mapping.items():
            require(observation[field] == source[origin], "source:" + field)
        require(observation["frame_sha256"] == value["source"]["jpeg_sha256"], "source:jpeg_hash")
        try:
            image = base64.b64decode(observation["image_base64"], validate=True)
        except ValueError:
            raise ValueError("image:base64") from None
        require(image.startswith(b"\x89PNG\r\n\x1a\n") and len(image) >= 24, "image:png")
        require(hashlib.sha256(image).hexdigest() == observation["image_sha256"], "image:hash")
        require(struct.unpack(">II", image[16:24]) ==
                (observation["screen_width"], observation["screen_height"]), "image:size")
        require((observation["ocr"]["width"], observation["ocr"]["height"]) ==
                (observation["screen_width"], observation["screen_height"]), "source:ocr_size")


def validate(schema, definition, value):
    validator = StrictValidator({**schema, "$ref": "#/definitions/" + definition}, format_checker=FORMAT)
    require(not list(validator.iter_errors(value)), "schema:" + definition)
    semantic(definition, value)


def relation_checks(values):
    total = 0
    snapshot = values["snapshot"]
    newer = {**snapshot, "binding_revision": snapshot["binding_revision"] + 1}
    require(same_source(snapshot, newer), "relation:global_revision_invalidated_source")
    total += 1
    changes = {
        "device_id": "arm-02", "camera_id": "cam-02", "device_unique_id": "fixture-camera-B",
        "camera_binding_id": "10000000-0000-4000-8000-000000000002",
        "source_geometry_id": "f" * 64, "width": 640, "height": 480,
    }
    for key, value in changes.items():
        require(not same_source(snapshot, {**snapshot, key: value}), "relation:identity_ignored:" + key)
        total += 1
    require(identity_digest(values["calibration_identity"]) ==
            identity_digest({**values["calibration_identity"], "bindingRevision": 999}),
            "relation:global_revision_changed_calibration")
    total += 1
    geo_hash = digest(values["geometry"])
    for actual in (snapshot["source_geometry_id"], values["capture_response"]["source_geometry_id"],
                   values["calibration_identity"]["sourceGeometryId"], values["debug_response"]["sourceGeometryId"]):
        require(actual == geo_hash, "relation:geometry_hash")
        total += 1
    require(values["frame_receipt"]["source"] == snapshot, "relation:receipt_snapshot")
    total += 1
    require(values["capture_request"]["camera_binding_id"] == snapshot["camera_binding_id"] ==
            values["capture_response"]["camera_binding_id"] == values["record"]["cameraBindingId"],
            "relation:session_binding")
    total += 1
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--digests", action="store_true")
    parser.add_argument("--show", help="Print a fully expanded named sample.")
    args = parser.parse_args()
    schema, fixtures = read("contracts.schema.json"), read("examples.json")
    values = {name: resolve(value, fixtures["values"]) for name, value in fixtures["values"].items()}
    if args.show:
        print(json.dumps(values[args.show], ensure_ascii=False, indent=2))
        return
    if args.digests:
        print("geometry:", digest(values["geometry"]))
        print("legacy_file:", digest(values["legacy_file"]))
        print("file:", digest(values["file"]))
        corrected = {**values["calibration_identity"], "sourceGeometryId": digest(values["geometry"])}
        print("calibration_identity:", identity_digest(corrected))
        return
    Draft7Validator.check_schema(schema)
    names = set()
    extra_checks = 0
    for case in fixtures["positive"] + fixtures["negative"]:
        require(case["name"] not in names, "fixture:duplicate_name")
        names.add(case["name"])
        value = changed(values[case["base"]], case)
        try:
            validate(schema, case["definition"], value)
        except ValueError as error:
            require("error" in case and str(error).startswith(case["error"]), case["name"] + ": " + str(error))
        else:
            require("error" not in case, case["name"] + ": invalid sample accepted")
        if "error" not in case:
            for pointer in object_paths(value):
                bad = copy.deepcopy(value)
                target = bad
                for key in pointer:
                    target = target[key]
                target["_unknown_fixture_field"] = True
                validator = StrictValidator(
                    {**schema, "$ref": "#/definitions/" + case["definition"]}, format_checker=FORMAT
                )
                require(bool(list(validator.iter_errors(bad))), "schema:open_object:" + case["name"])
                extra_checks += 1
    relations = relation_checks(values)
    bad_json = ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{"a":1e999}', '{"a":"\\ud800"}')
    for value in bad_json:
        try:
            loads(value)
        except (ValueError, UnicodeError):
            continue
        raise ValueError("parser:bad_json_accepted")
    print(f"PASS: camera binding; {len(fixtures['positive'])} positive / {len(fixtures['negative'])} negative fixtures; "
          f"{relations} cross-record checks; {len(bad_json)} parser checks; {extra_checks} closed-object checks.")
    print("Documentation only. No production files, services, cameras, clocks or hardware tested.")


def object_paths(value, prefix=()):
    if isinstance(value, dict):
        yield prefix
        for key, child in value.items():
            yield from object_paths(child, prefix + (key,))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from object_paths(child, prefix + (index,))


if __name__ == "__main__":
    main()
