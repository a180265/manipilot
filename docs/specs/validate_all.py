"""Run documentation checks only; never import runtime code or contact devices."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
CHECKS = (
    "validate_spec.py",
    "input-control/validate_contracts.py",
    "camera-binding/validate.py",
    "calibrated-scroll/validate_fixtures.py",
    "layout-reuse/validate.py",
    "wx-navigation/validate_examples.py",
)


def read(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def cross_package_checks():
    schemas = {
        path: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(ROOT.rglob("*.schema.json"))
    }
    registered = {schema["$id"]: schema for schema in schemas.values()}
    assert len(registered) == len(schemas), "Duplicate schema ID across packages"
    references = 0
    for path, schema in schemas.items():
        for node in walk(schema):
            # Bare object constraints inside allOf only narrow a closed referenced shape.
            if node.get("type") == "object" and ("properties" in node or "patternProperties" in node):
                assert node.get("additionalProperties") is False, f"Open object: {path}"
            assert node.get("additionalProperties") is not True, f"Unrestricted fields: {path}"
            if "$ref" not in node:
                continue
            uri, _, fragment = node["$ref"].partition("#")
            target = registered[uri] if uri else schema
            if fragment:
                assert fragment.startswith("/"), f"Unsupported schema anchor: {node['$ref']}"
                for part in fragment[1:].split("/"):
                    target = target[part.replace("~1", "/").replace("~0", "~")]
            references += 1

    camera = read("camera-binding/contracts.schema.json")["definitions"]
    scroll = read("calibrated-scroll/observation.schema.json")["definitions"]
    navigation = read("wx-navigation/contracts.schema.json")["definitions"]
    layout = read("layout-reuse/common.schema.json")["definitions"]
    assert set(camera["source_frame"]["required"]) == set(scroll["source_frame"]["required"]), (
        "Camera producer and scroll consumer disagree on source metadata"
    )
    identity = {"camera_binding_id", "source_geometry_id", "binding_revision"}
    assert identity <= set(scroll["binding"]["required"])
    assert identity <= set(navigation["Binding"]["required"])
    assert "camera_binding_id" in layout["binding"]["required"]
    assert navigation["SourceFrameProof"]["$ref"] == (
        "urn:wx:design:camera-binding:v2#/definitions/source_frame_view"
    )
    assert camera["calibration_identity"]["properties"]["schemaVersion"]["const"] == 3
    assert camera["binding_file"]["properties"]["schemaVersion"]["const"] == 2
    print(f"PASS: {len(schemas)} locally registered schemas; {references} resolved references; "
          "camera/scroll/WX/layout identity contracts agree.", flush=True)


def main():
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    for relative in CHECKS:
        print(f"\nCHECK {relative}", flush=True)
        subprocess.run(
            [sys.executable, "-B", str(ROOT / relative)],
            cwd=ROOT.parents[2],
            env=environment,
            check=True,
        )
    cross_package_checks()
    print("\nPASS: all documentation checks completed.", flush=True)
    print("Not runtime/parser integration, phone, camera, motion or sending acceptance.", flush=True)


if __name__ == "__main__":
    main()
