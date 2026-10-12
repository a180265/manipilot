"""Read-only offline documentation checks; run only with explicit authorization."""
from __future__ import annotations

import copy
from datetime import datetime
import json
import math
from pathlib import Path
import re

from jsonschema import Draft7Validator, FormatChecker, validators
from referencing import Registry, Resource

from semantic_validator import check_semantics, check_transition, digest


ROOT = Path(__file__).resolve().parent
FORMAT_CHECKER = FormatChecker()
SCHEMA_FILES = ("observation.schema.json", "contract.schema.json", "records.schema.json", "routes.schema.json")
StrictValidator = validators.extend(
    Draft7Validator,
    type_checker=Draft7Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int
    ).redefine(
        "number", lambda checker, value: type(value) in (int, float) and math.isfinite(value)
    ),
)
# Keep strict numeric typing when a reference evolves to a Draft-07 resource.
validators.validates("draft7")(StrictValidator)


@FORMAT_CHECKER.checks("date-time", raises=(ValueError, TypeError))
def check_datetime(value):
    if not isinstance(value, str):
        return True
    pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
    if re.fullmatch(pattern, value) is None:
        return False
    return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None


def reject_constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def load_json(name):
    return json.loads((ROOT / name).read_text(encoding="utf-8"),
                      parse_constant=reject_constant, object_pairs_hook=unique_object)


def lookup(value, path):
    for key in path.split(".") if path else []:
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def expand(value, positives):
    if isinstance(value, list):
        return [expand(item, positives) for item in value]
    if not isinstance(value, dict):
        return value
    if "$fixture" in value:
        if set(value) - {"$fixture", "path"}:
            raise ValueError("Unexpected fixture reference fields")
        return copy.deepcopy(lookup(positives[value["$fixture"]], value.get("path", "")))
    if "$sha256" in value:
        if set(value) != {"$sha256"}:
            raise ValueError("Unexpected digest fixture fields")
        return digest(expand(value["$sha256"], positives))
    return {key: expand(item, positives) for key, item in value.items()}


def mutate(base, case, positives):
    record = copy.deepcopy(base)
    for path, value in case.get("set", {}).items():
        keys = path.split(".")
        parent = lookup(record, ".".join(keys[:-1]))
        key = int(keys[-1]) if isinstance(parent, list) else keys[-1]
        parent[key] = expand(value, positives)
    for path in case.get("remove", []):
        keys = path.split(".")
        parent = lookup(record, ".".join(keys[:-1]))
        key = int(keys[-1]) if isinstance(parent, list) else keys[-1]
        del parent[key]
    return record


def object_paths(value, prefix=()):
    if isinstance(value, dict):
        yield prefix
        for key, item in value.items():
            yield from object_paths(item, prefix + (key,))
    elif isinstance(value, list):
        for key, item in enumerate(value):
            yield from object_paths(item, prefix + (key,))


def schema_hygiene(value):
    if isinstance(value, dict):
        if value.get("additionalProperties") is True:
            raise ValueError("Open dictionary in schema")
        if value.get("type") == "object" and "properties" in value:
            if value.get("additionalProperties") is not False:
                raise ValueError("Object must reject extra properties")
        for item in value.values():
            schema_hygiene(item)
    elif isinstance(value, list):
        for item in value:
            schema_hygiene(item)


def main():
    schemas = {name: load_json(name) for name in SCHEMA_FILES}
    for schema in schemas.values():
        Draft7Validator.check_schema(schema)
        schema_hygiene(schema)
    registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
    )

    def validator_for(selector):
        filename, fragment = selector.split("#", 1)
        schema = schemas[filename]
        lookup(schema, fragment.lstrip("/").replace("/", "."))
        return StrictValidator({"$ref": schema["$id"] + "#" + fragment},
                               registry=registry, format_checker=FORMAT_CHECKER)

    def kind_for(selector, record):
        return selector.rsplit("/", 1)[-1] if "#/definitions/" in selector else record.get("record_type")

    routes = load_json("routes.json")
    StrictValidator(schemas["routes.schema.json"], registry=registry).validate(routes)
    route_names = set()
    for route in routes["entries"]:
        if route["id"] in route_names:
            raise ValueError("Duplicate route ID")
        route_names.add(route["id"])
        selectors = [route["response_schema"], route["request_schema"]]
        selectors.extend(error["schema"] for error in route["http_errors"])
        for selector in selectors:
            if selector:
                validator_for(selector)
        if (route["transport"] == "http") != bool(route["http_success"]):
            raise ValueError("HTTP/internal route status mismatch")

    positives, selectors, names = {}, {}, set()
    rejected = {"schema": 0, "semantic": 0}
    extra_property_checks = 0
    suites = [load_json("fixtures.json"), load_json("package-fixtures.json")]
    for suite in suites:
        for case in suite["positive"]:
            if case["name"] in names:
                raise ValueError(f"Duplicate fixture name: {case['name']}")
            names.add(case["name"])
            record = (mutate(positives[case["base"]], case, positives) if "base" in case
                      else expand(case["record"], positives))
            selector = case.get("schema", selectors.get(case.get("base"), "observation.schema.json#"))
            validator = validator_for(selector)
            validator.validate(record)
            check_semantics(record, kind_for(selector, record))
            positives[case["name"]], selectors[case["name"]] = record, selector
            for path in object_paths(record):
                changed = copy.deepcopy(record)
                parent = changed
                for key in path:
                    parent = parent[key]
                parent["unexpected_fixture_field"] = True
                if not list(validator.iter_errors(changed)):
                    raise AssertionError(f"Open object in {case['name']}: {path}")
                extra_property_checks += 1
    for suite in suites:
        for case in suite["negative"]:
            if case["name"] in names:
                raise ValueError(f"Duplicate fixture name: {case['name']}")
            names.add(case["name"])
            record = mutate(positives[case["base"]], case, positives)
            selector = selectors[case["base"]]
            schema_errors = list(validator_for(selector).iter_errors(record))
            if case["stage"] == "schema":
                if not schema_errors:
                    raise AssertionError(f"Expected schema rejection: {case['name']}")
            elif case["stage"] == "semantic":
                if schema_errors:
                    raise AssertionError(f"Wrong rejection stage: {case['name']}: {schema_errors[0].message}")
                try:
                    check_semantics(record, kind_for(selector, record))
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"Expected semantic rejection: {case['name']}")
            else:
                raise ValueError(f"Unknown rejection stage: {case['stage']}")
            rejected[case["stage"]] += 1
    transitions = 0
    for suite in suites:
        for case in suite.get("transitions", []):
            before = positives[case["before"]]
            after = mutate(positives[case["after"]], case, positives)
            validator_for("records.schema.json#/definitions/action_record").validate(after)
            try:
                check_transition(before, after, case["event"])
            except ValueError:
                if case["accept"]:
                    raise
            else:
                if not case["accept"]:
                    raise AssertionError(f"Forbidden transition accepted: {case['name']}")
            transitions += 1
    covered = set(selectors.values())
    required = {route["response_schema"] for route in routes["entries"]}
    required.update(route["request_schema"] for route in routes["entries"] if route["request_schema"])
    required.add("contract.schema.json#/definitions/http_error")
    if required - covered:
        raise AssertionError(f"Route schemas without positive examples: {sorted(required - covered)}")
    print(f"PASS: {len(schemas)} schemas; {len(routes['entries'])} entry mappings; "
          f"{len(positives)} positives; {rejected['schema']} schema negatives; "
          f"{rejected['semantic']} semantic negatives; {transitions} transitions; "
          f"{extra_property_checks} nested extra-property rejections.")
    print("Synthetic documentation checks only. No runtime, database, network, camera or hardware accessed.")


if __name__ == "__main__":
    main()
