#!/usr/bin/env python3
"""Validate only this pending-design attachment directory; no runtime imports or writes."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys

from jsonschema import Draft7Validator, FormatChecker, validators
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource


ROOT = Path(__file__).resolve().parent
DRAFT = "http://json-schema.org/draft-07/schema#"
MAX_REVISION = 9007199254740991
SAVED = ("enabled", "ime_device_id", "revision", "revoked_through_revision", "updated_at")
RECOVERY = {
    "settings_commit_pending", "input_record_invalid", "input_effect_unknown",
    "ime_cancel_pending", "editor_review_required", "binding_change_pending",
    "legacy_input_review_required", "startup_reconciling",
}
UNREVIEWABLE = {"settings_commit_pending", "input_record_invalid", "binding_change_pending"}
StrictDraft7 = validators.extend(
    Draft7Validator,
    type_checker=Draft7Validator.TYPE_CHECKER.redefine(
        "integer", lambda checker, value: type(value) is int
    ),
)
FORMATS = FormatChecker()


@FORMATS.checks("date-time", raises=(ValueError, TypeError, OverflowError))
def strict_timestamp(value):
    if not isinstance(value, str):
        return True
    return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset() is not None


class ContractFailure(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ContractFailure(code)


def pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "json.duplicate_key")
        result[key] = value
    return result


def invalid_constant(value):
    raise ContractFailure(f"json.nonfinite:{value}")


def read_json(path):
    require(path.resolve().is_relative_to(ROOT), "scope.outside_attachment")
    return json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=pairs_no_duplicates, parse_constant=invalid_constant,
    )


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def request_digest(device, request, context):
    return digest([request["contract_version"], device, request, context])


def saved(value):
    return {key: value[key] for key in SAVED}


def moment(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def chronological(*values):
    times = [moment(value) for value in values if value is not None]
    require(times == sorted(times), "time.order")


def check_context(request, context, origins):
    require(context["origin"] in origins, "context.origin")
    for key in ("operator_id", "role"):
        require(context[key] == request[key], "context.identity")
    for key in ("operator_id", "reason"):
        require(request[key] == request[key].strip(), "request.not_normalized")
    if context["role"] == "system":
        require(request["enabled"] is False, "context.system_open")


def check_saved(value):
    revision, revoked = value["revision"], value["revoked_through_revision"]
    require(not value["enabled"] or revision < MAX_REVISION, "settings.enabled_at_max")
    require(revoked <= revision, "settings.revocation_upper_bound")
    require(
        revoked < revision if value["enabled"] else revoked == revision,
        "settings.revocation_boundary",
    )
    if revision == 0:
        require(value["ime_device_id"] is None and value["updated_at"] is None, "settings.initial")


def check_status(value):
    check_saved(value)
    codes = [item["code"] for item in value["blocking_reasons"]]
    require(len(set(codes)) == len(codes), "status.duplicate_code")
    require(value["effective"] == (not codes), "status.effective_reasons")
    required = {
        "input_switch_disabled": not value["enabled"],
        "input_binding_unavailable": value["current_ime_device_id"] is None,
        "input_binding_mismatch": (
            value["enabled"] and value["current_ime_device_id"] is not None
            and value["ime_device_id"] != value["current_ime_device_id"]
        ),
        "calibration_not_ready": not value["calibration_ready"],
        "ime_offline": not value["ime_online"],
        "ime_keyboard_inactive": not value["keyboard_active"],
        "ime_cancel_pending": value["pending_cancellation_count"] > 0,
    }
    require(all(not needed or code in codes for code, needed in required.items()), "status.missing_reason")
    require(bool(set(codes) & RECOVERY) == value["input_recovery_required"], "status.recovery_aggregate")
    if value["effective"]:
        require(value["ime_device_id"] == value["current_ime_device_id"], "status.binding")
    if value["current_ime_device_id"] is None:
        require(not value["ime_online"] and not value["keyboard_active"], "status.unknown_phone")
    chronological(value["updated_at"], value["observed_at"])


def check_file(value):
    check_saved(value)
    entries = sorted(value["requests"].items(), key=lambda item: item[1]["after"]["revision"])
    baseline = value.get("migration_baseline")
    if baseline is not None:
        check_baseline(baseline)
        previous = baseline["closed_saved_value"]
        old_ids = legacy_ids(baseline)
        require(not old_ids.intersection(value["requests"]), "migration.legacy_id_reused")
        require(not {baseline["migration_id"], baseline["client_request_id"]}.intersection(value["requests"]), "migration.reserved_id_reused")
    else:
        previous = dict(enabled=False, ime_device_id=None, revision=0, revoked_through_revision=0, updated_at=None)
    for key, entry in entries:
        request, context = entry["normalized_request"], entry["call_context"]
        before, after = entry["before"], entry["after"]
        check_context(request, context, {"human_put", "system_binding_change"})
        require(key == request["client_request_id"], "ledger.request_key")
        require(entry["request_digest"] == request_digest(value["device_id"], request, context), "ledger.digest")
        require(before == previous, "ledger.history_gap")
        require(request["expected_revision"] == before["revision"], "ledger.expected_revision")
        require(after["revision"] == before["revision"] + 1, "ledger.revision_step")
        require(after["enabled"] == request["enabled"], "ledger.enabled")
        expected_phone = request["ime_device_id"] if request["enabled"] else before["ime_device_id"]
        require(after["ime_device_id"] == expected_phone, "ledger.close_keeps_authorized_phone")
        expected_revoked = before["revoked_through_revision"] if request["enabled"] else after["revision"]
        require(after["revoked_through_revision"] == expected_revoked, "ledger.revocation")
        audit = {
            "device_id": value["device_id"],
            "requested_ime_device_id": request["ime_device_id"],
            "authorized_ime_device_id": after["ime_device_id"],
            **{key: request[key] for key in ("operator_id", "role", "reason", "correlation_id", "client_request_id")},
            "committed_at": after["updated_at"],
        }
        require(entry["audit"] == audit, "ledger.audit")
        snapshot = entry["original_response"]["settings"]
        require(snapshot["device_id"] == value["device_id"] and saved(snapshot) == after, "ledger.snapshot")
        chronological(before["updated_at"], after["updated_at"])
        previous = after
    require(saved(value) == previous, "ledger.current_value")
    request = entries[-1][1]["normalized_request"] if entries else baseline
    require(request is not None, "ledger.missing_origin")
    require(all(value[key] == request[key] for key in ("operator_id", "reason", "client_request_id")), "ledger.latest_audit")


def check_guard(value):
    require(value["record_id"] == "guard:" + value["device_id"], "guard.identity")
    issues = {issue["issue_id"]: issue for issue in value["issues"]}
    require(len(issues) == len(value["issues"]), "guard.duplicate_issue")
    for key, entry in value["review_requests"].items():
        request, context = entry["normalized_request"], entry["call_context"]
        response = entry["original_response"]
        check_context(request, context, {"human_review"})
        require(key == request["client_request_id"], "review.request_key")
        require(entry["request_digest"] == request_digest(value["device_id"], request, context), "review.digest")
        require(response["device_id"] == value["device_id"], "review.device")
        require(response["issue_ids"] == request["issue_ids"], "review.response_issues")
        require(response["recovery_revision"] == request["expected_recovery_revision"] + 1, "review.revision")
        require(response["recovery_revision"] <= value["revision"], "review.future_revision")
        originals = entry["original_issues"]
        require([item["issue_id"] for item in originals] == request["issue_ids"], "review.original_issues")
        for original in originals:
            issue = issues.get(original["issue_id"])
            require(issue is not None, "review.issue_missing")
            expected = {
                **original, "state": "resolved", "resolved_at": entry["reviewed_at"],
                "resolution": {"method": "human_review", "client_request_id": key, "reviewed_at": entry["reviewed_at"]},
            }
            require(issue == expected, "review.original_fact_changed")
            chronological(original["opened_at"], entry["reviewed_at"], value["updated_at"])
    for issue in issues.values():
        chronological(issue["opened_at"], issue["resolved_at"], value["updated_at"])
        if issue["state"] == "resolved" and issue["resolution"]["method"] == "human_review":
            require(issue["code"] not in UNREVIEWABLE, "review.issue_not_reviewable")
            entry = value["review_requests"].get(issue["resolution"]["client_request_id"])
            require(entry is not None and issue["issue_id"] in entry["normalized_request"]["issue_ids"], "review.audit_missing")
    if value["binding_change"] is not None:
        change = value["binding_change"]
        require(change["old_ime_device_id"] != change["new_ime_device_id"], "binding.same_phone")
        if change["phase"] != "applied":
            require(any(i["state"] == "open" and i["code"] == "binding_change_pending" for i in issues.values()), "binding.guard_missing")


def check_effect(value):
    expected_id = "effect:" + digest([value["owner_id"], value["slot_key"]])
    require(value["record_id"] == expected_id, "effect.identity_digest")
    require(value["slot_key"][2] == value["effect_kind"], "effect.slot_kind")
    if value["effect_kind"] in {"ime_create", "ime_deliver", "ime_start"}:
        require(value["slot_key"] == ["ime:" + value["ime_task_id"], 0, value["effect_kind"]], "effect.ime_slot")
    chronological(value["created_at"], value["prepared_at"], value["claimed_at"], value["finished_at"], value["updated_at"])
    observations = [] if value["receipt"] is None else value["receipt"]["observations"]
    progress = [item["typed_character_count"] for item in observations if item["typed_character_count"] is not None]
    require(progress == sorted(progress), "effect.progress_regressed")
    if progress:
        require(value["typed_character_count"] is not None and value["typed_character_count"] >= progress[-1], "effect.progress_lost")
    if value["state"] == "not_started":
        require(any(item["input_effect_not_started"] and item["event"] in {"not_delivered", "repository_absent"} for item in observations), "effect.not_started_proof")
    if value["cancel_state"] == "not_created":
        require(any(item["event"] == "repository_absent" and item["source"] == "ime_repository" for item in observations), "effect.absence_proof")
    for item in observations:
        chronological(value["prepared_at"], item["recorded_at"], value["updated_at"])


def check_intent(value):
    request, context, proposed = value["normalized_request"], value["call_context"], value["proposed_file"]
    check_context(request, context, {"human_put", "system_binding_change"})
    require(value["record_id"] == "write:" + digest([value["device_id"], request["client_request_id"]]), "intent.identity_digest")
    require(value["after_revision"] == value["before_revision"] + 1, "intent.revision_step")
    require(request["expected_revision"] == value["before_revision"], "intent.expected_revision")
    require(proposed["device_id"] == value["device_id"] and proposed["revision"] == value["after_revision"], "intent.proposed_identity")
    require(value["after_digest"] == digest(proposed), "intent.after_digest")
    if value["before_revision"] == 0:
        require(value["before_digest"] == digest(["input-settings-file-absent.v1"]), "intent.absent_digest")
    else:
        prior = deepcopy(proposed)
        del prior["requests"][request["client_request_id"]]
        if prior["requests"]:
            last = max(prior["requests"].values(), key=lambda item: item["after"]["revision"])
            prior.update(last["after"])
            audit = last["normalized_request"]
        else:
            require("migration_baseline" in prior, "intent.before_history_missing")
            audit = prior["migration_baseline"]
            prior.update(audit["closed_saved_value"])
        prior.update({key: audit[key] for key in ("operator_id", "reason", "client_request_id")})
        require(value["before_digest"] == digest(prior), "intent.before_digest")
    entry = proposed["requests"].get(request["client_request_id"])
    require(entry is not None, "intent.ledger_missing")
    require(entry["normalized_request"] == request and entry["call_context"] == context, "intent.request")
    require(entry["original_response"] == value["original_response"], "intent.snapshot")
    if value["failure"] is not None:
        stage = value["failure"]["stage"]
        key = value["failure"]["response"]["body"]["error"]["key"]
        require(key == ("input_settings_commit_unknown" if stage == "commit_uncertain" else "input_settings_write_failed"), "intent.failure_key")
        if value["state"] == "aborted":
            require(stage != "commit_uncertain", "intent.abort_uncertain")
        if value["state"] == "prepared":
            require(stage == "commit_uncertain", "intent.prepared_failure")
        if stage == "recovery_open_aborted":
            require(request["enabled"], "intent.abort_close")


def legacy_ids(baseline):
    originals = set(baseline["legacy_request_ids"])
    return originals | {key.strip() for key in originals}


def check_legacy_file(value):
    require(not value["enabled"] or value["revision"] < MAX_REVISION, "settings.enabled_at_max")
    entries = sorted(value["requests"].items(), key=lambda item: item[1]["revision"])
    revisions = [entry["revision"] for _, entry in entries]
    require(len(set(revisions)) == len(revisions) and max(revisions) == value["revision"], "legacy.revision_chain")
    aliases = [key.strip() for key, _ in entries]
    require(len(set(aliases)) == len(aliases), "legacy.ambiguous_request_ids")
    last_key, last = entries[-1]
    require(last_key.strip() == value["client_request_id"], "legacy.latest_request")
    for key in ("enabled", "revision", "operator_id"):
        require(last[key] == value[key], "legacy.latest_value")
    require(last["at"] == value["updated_at"], "legacy.latest_time")
    require(last["digest"] == digest({key: value[key] for key in ("enabled", "ime_device_id", "operator_id", "reason")}), "legacy.latest_digest")
    chronological(*(entry["at"] for _, entry in entries))
    for text in [value["operator_id"], value["reason"], *(entry["operator_id"] for _, entry in entries)]:
        require(text == text.strip(), "legacy.audit_not_normalized")


def check_baseline(value):
    floor, closed = value["revision_floor"], value["closed_saved_value"]
    require(floor < MAX_REVISION, "migration.revision_exhausted")
    require(closed["revision"] == floor + 1 and closed["revoked_through_revision"] == floor + 1, "migration.close_revision")
    require(closed["enabled"] is False, "migration.must_close")
    for key in ("operator_id", "reason"):
        require(value[key] == value[key].strip(), "migration.audit_not_normalized")
    aliases = [key.strip() for key in value["legacy_request_ids"]]
    require(len(set(aliases)) == len(aliases), "migration.ambiguous_request_ids")
    require(not {value["migration_id"], value["client_request_id"]}.intersection(legacy_ids(value)), "migration.id_collides_with_legacy")
    require(str(PurePosixPath(value["backup_manifest_ref"])) == value["backup_manifest_ref"] and ".." not in PurePosixPath(value["backup_manifest_ref"]).parts, "migration.manifest_path")


def migration_floor(inventory):
    revisions = [inventory["file_revision"], inventory["ledger_revision"]]
    revisions.extend(item["settings_revision"] for item in inventory["owners"])
    revisions.extend(item["after_revision"] for item in inventory["applied_intents"])
    if inventory["checkpoint"] is not None:
        revisions.append(inventory["checkpoint"]["revision"])
    return max(revisions)


def check_backup_manifest(value):
    inventory = value["version_inventory"]
    revision = inventory["file_revision"]
    require(inventory["ledger_revision"] == revision, "migration.source_ledger_conflict")
    require(all(item["settings_revision"] <= revision for item in inventory["owners"]), "migration.source_owner_conflict")
    for name in ("owners", "applied_intents"):
        ids = [item["record_id"] for item in inventory[name]]
        require(len(set(ids)) == len(ids), "migration.duplicate_source_record")
    checkpoint = inventory["checkpoint"]
    if checkpoint is not None:
        require(checkpoint == {"revision": revision, "digest": value["source_file_semantic_digest"]}, "migration.source_checkpoint_conflict")
    for intent in inventory["applied_intents"]:
        require(intent["after_revision"] <= revision, "migration.source_intent_conflict")
        if intent["after_revision"] == revision:
            require(intent["after_digest"] == value["source_file_semantic_digest"], "migration.source_intent_digest")
    paths = []
    for artifact in value["artifacts"].values():
        for key in ("source_ref", "backup_ref"):
            path = artifact[key]
            require(str(PurePosixPath(path)) == path and ".." not in PurePosixPath(path).parts, "migration.artifact_path")
            paths.append(path)
    require(len(set(paths)) == len(paths), "migration.backup_overwrites_source")


def check_migration_intent(value):
    manifest, proposed = value["backup_manifest"], value["proposed_file"]
    baseline = proposed["migration_baseline"]
    check_baseline(baseline)
    check_backup_manifest(manifest)
    require(value["migration_id"] == manifest["migration_id"] == baseline["migration_id"], "migration.intent_id")
    require(value["device_id"] == manifest["device_id"] == proposed["device_id"], "migration.intent_device")
    require(value["before_digest"] == manifest["source_file_semantic_digest"], "migration.before_digest")
    require(value["after_digest"] == digest(proposed), "migration.after_digest")
    require(value["backup_manifest_sha256"] == baseline["backup_manifest_sha256"] == digest(manifest), "migration.manifest_digest")
    require(value["backup_manifest_ref"] == baseline["backup_manifest_ref"], "migration.manifest_ref")
    require(baseline["revision_floor"] == migration_floor(manifest["version_inventory"]), "migration.revision_floor")
    require(value["backup_manifest_ref"] not in {a[key] for a in manifest["artifacts"].values() for key in ("source_ref", "backup_ref")}, "migration.manifest_overwrite")
    chronological(manifest["created_at"], value["created_at"], baseline["closed_saved_value"]["updated_at"], value["committed_at"], value["updated_at"])


def check_migration_bundle(source, intent):
    manifest, target = intent["backup_manifest"], intent["proposed_file"]
    baseline = target["migration_baseline"]
    require(source["device_id"] == intent["device_id"], "migration.source_device")
    require(digest(source) == intent["before_digest"], "migration.source_changed")
    require(manifest["version_inventory"]["file_revision"] == source["revision"], "migration.source_revision")
    require(baseline["revision_floor"] == migration_floor(manifest["version_inventory"]), "migration.revision_floor")
    check_baseline_source(source, target)
    chronological(source["updated_at"], manifest["created_at"])


def check_baseline_source(source, target):
    baseline = target["migration_baseline"]
    require(source["device_id"] == target["device_id"], "migration.source_device")
    require(baseline["closed_saved_value"]["ime_device_id"] == source["ime_device_id"], "migration.phone_changed")
    require(baseline["legacy_request_ids"] == {key: digest(entry) for key, entry in source["requests"].items()}, "migration.tombstones")


def check_migration_observation(case, fixtures):
    intent = fixtures[case["intent"]][1]
    current = fixtures[case["file"]][1]
    checkpoint = case["checkpoint"]
    proposed = intent["proposed_file"]
    expected_checkpoint = {"revision": proposed["revision"], "digest": intent["after_digest"]}
    before_checkpoint = intent["backup_manifest"]["version_inventory"]["checkpoint"]
    require(case["migration_id"] == intent["migration_id"], "migration.observed_id")
    require(case["source_digest"] == intent["before_digest"], "migration.source_changed")
    require(case.get("backup_manifest_sha256", intent["backup_manifest_sha256"]) == intent["backup_manifest_sha256"], "migration.backup_changed")
    if intent["state"] == "committed":
        require(current.get("migration_baseline") == proposed["migration_baseline"], "migration.committed_file_rollback")
        require(current["revision"] >= proposed["revision"], "migration.committed_revision_rollback")
        require(checkpoint == {"revision": current["revision"], "digest": digest(current)}, "migration.committed_checkpoint")
        require(case["reported_state"] == "committed" and case["action"] == "lookup_only", "migration.committed_reexecution")
        # "normal_checks" is not permission to input; all other G01 gates still apply.
        require(case["startup_gate"] == "normal_checks", "migration.committed_gate")
        return
    require(case["startup_gate"] == "blocked", "migration.pending_must_block")
    require(case["reported_state"] == "maintenance_required", "migration.half_commit_reported_success")
    current_digest = digest(current)
    if current_digest == intent["before_digest"] and checkpoint == before_checkpoint:
        next_action = "write_closed_file"
    elif current_digest == intent["after_digest"] and checkpoint == before_checkpoint:
        next_action = "write_checkpoint"
    elif current_digest == intent["after_digest"] and checkpoint == expected_checkpoint:
        next_action = "commit_intent"
    else:
        next_action = "stop"
    expected_action = next_action if case["mode"] == "resume" else "lookup_only"
    require(case["action"] == expected_action, "migration.resume_action")


def check_migrated_file_transition(before, after):
    require(before["device_id"] == after["device_id"], "migration.transition_device")
    require(before.get("migration_baseline") == after.get("migration_baseline"), "migration.baseline_changed")
    require(after["revision"] == before["revision"] + 1, "migration.transition_revision")
    require(all(after["requests"].get(key) == value for key, value in before["requests"].items()), "migration.ledger_lost")
    added = set(after["requests"]) - set(before["requests"])
    require(len(added) == 1, "migration.new_entry_count")
    require(after["requests"][next(iter(added))]["before"] == saved(before), "migration.transition_chain")


def check_migration_put_gate(case, fixtures):
    file = fixtures[case["file"]][1]
    request = fixtures[case["request"]][1]
    baseline = file["migration_baseline"]
    if request["role"] not in {"operator", "responder"}:
        key = "permission_denied"
    elif request["client_request_id"] in legacy_ids(baseline):
        key = "legacy_request_replay_unavailable"
    elif request["client_request_id"] in {baseline["migration_id"], baseline["client_request_id"]}:
        key = "request_conflict"
    elif request["expected_revision"] != file["revision"]:
        key = "revision_conflict"
    else:
        key = None
    require(case["error_key"] == key, "migration.put_order")
    require(case["http_status"] == (403 if key == "permission_denied" else 409 if key else 200), "migration.put_http")
    require(case["new_input_actions"] == 0, "migration.put_has_actions")


def semantics(value):
    if isinstance(value, str):
        require(not any(0xD800 <= ord(char) <= 0xDFFF for char in value), "unicode.surrogate")
    elif isinstance(value, list):
        for item in value:
            semantics(item)
    elif isinstance(value, dict):
        if set(SAVED).issubset(value):
            check_saved(value)
        if "blocking_reasons" in value:
            check_status(value)
        if "requests" in value:
            if "revoked_through_revision" in value:
                check_file(value)
            else:
                check_legacy_file(value)
        version = value.get("contract_version")
        if version == "input-settings-migration.v1":
            check_baseline(value)
        elif version == "input-settings-migration-backup.v1":
            check_backup_manifest(value)
        elif version == "input-settings-migration-intent.v1":
            check_migration_intent(value)
        if value.get("record_version") == "input-control-record.v1":
            chronological(value["created_at"], value["updated_at"])
            kind = value["kind"]
            if kind == "owner":
                prefix = "owner:task:" if value["owner_type"] == "fleet_task" else "owner:ime:"
                require(value["record_id"] == prefix + value["owner_ref"], "owner.identity")
                chronological(value["created_at"], value["admitted_at"], value["updated_at"])
                if value["revocation"] == "revoked":
                    require(value["revoked_at_revision"] >= value["settings_revision"], "owner.revoked_revision")
            elif kind == "effect":
                check_effect(value)
            elif kind == "device_guard":
                check_guard(value)
            else:
                check_intent(value)
        for key, item in value.items():
            semantics(key)
            semantics(item)


def no_remote(uri):
    raise NoSuchResource(ref=uri)


def schema_audit(node):
    if isinstance(node, dict):
        require(not set(node) & {"$defs", "unevaluatedProperties", "prefixItems", "$dynamicRef"}, "schema.not_draft07")
        if node.get("type") == "object":
            require(node.get("additionalProperties") is False, "schema.open_object")
        for value in node.values():
            schema_audit(value)
    elif isinstance(node, list):
        for value in node:
            schema_audit(value)


def load_validators():
    schemas = {}
    resources = []
    for path in sorted(ROOT.glob("*.schema.json")):
        document = read_json(path)
        require(document["$schema"] == DRAFT, "schema.dialect")
        Draft7Validator.check_schema(document)
        schema_audit(document)
        schemas[path.name] = document
        resources.append((document["$id"], Resource.from_contents(document)))
    require(len({uri for uri, _ in resources}) == len(resources), "schema.duplicate_id")
    registry = Registry(retrieve=no_remote).with_resources(resources)

    def make(reference):
        filename, _, pointer = reference.partition("#")
        require(filename in schemas, "schema.unknown_file")
        document = schemas[filename]
        target = document["$id"] + ("#" + pointer if pointer else "")
        return StrictDraft7({"$ref": target}, registry=registry, format_checker=FORMATS)

    # Resolve every reference, even when an if/then fixture does not visit it.
    def resolve_all(node, resolver):
        if isinstance(node, dict):
            if "$ref" in node:
                resolver.lookup(node["$ref"])
            for item in node.values():
                resolve_all(item, resolver)
        elif isinstance(node, list):
            for item in node:
                resolve_all(item, resolver)

    for document in schemas.values():
        resolve_all(document, registry.resolver(document["$id"]))
    return schemas, make


def edit_value(value, edits, fixtures):
    value = deepcopy(value)
    for edit in edits:
        require(set(edit) <= {"path", "value", "remove", "repeat", "from_fixture", "select"}, "fixture.edit_fields")
        path = edit["path"]
        require(bool(path), "fixture.empty_path")
        target = value
        for component in path[:-1]:
            target = target[component]
        key = path[-1]
        require(sum(field in edit for field in ("value", "remove", "repeat", "from_fixture")) == 1, "fixture.edit_operation")
        if "remove" in edit:
            require(edit["remove"] is True, "fixture.remove")
            del target[key]
        elif "repeat" in edit:
            text, count = edit["repeat"]
            require(type(text) is str and type(count) is int and 0 <= count <= 10001, "fixture.repeat")
            target[key] = text * count
        elif "from_fixture" in edit:
            replacement = fixtures[edit["from_fixture"]][1]
            for part in edit.get("select", []):
                replacement = replacement[part]
            target[key] = deepcopy(replacement)
        else:
            target[key] = deepcopy(edit["value"])
    return value


def check_transition(old, new):
    require(new["record_id"] == old["record_id"] and new["device_id"] == old["device_id"] and new["kind"] == old["kind"], "transition.identity")
    require(new["revision"] == old["revision"] + 1, "transition.cas")
    require(new["created_at"] == old["created_at"], "transition.created_at")
    chronological(old["updated_at"], new["updated_at"])
    if old["kind"] == "owner":
        mutable = {"revision", "updated_at", "revocation", "revoked_at_revision"}
        require(all(new[key] == value for key, value in old.items() if key not in mutable), "transition.owner_immutable")
        if old["revocation"] == "revoked":
            require(new["revocation"] == "revoked" and new["revoked_at_revision"] == old["revoked_at_revision"], "transition.owner_resurrected")
    elif old["kind"] == "effect":
        for key in ("owner_id", "slot_key", "effect_kind", "payload_digest", "prepared_at", "ime_task_id"):
            require(new[key] == old[key], "transition.effect_immutable")
        allowed = {
            "prepared": {"prepared", "claimed", "not_started"},
            "claimed": {"claimed", "completed", "not_started", "unknown"},
            "unknown": {"unknown", "completed", "not_started"},
            "completed": {"completed"}, "not_started": {"not_started"},
        }
        require(new["state"] in allowed[old["state"]], "transition.effect_replay")
        for key in ("claimed_at", "finished_at"):
            if old[key] is not None:
                require(new[key] == old[key], "transition.effect_timestamp")
        previous = old["typed_character_count"]
        if previous is not None:
            require(new["typed_character_count"] is not None and new["typed_character_count"] >= previous, "transition.progress_regressed")
        if old["receipt"] is not None:
            observations = old["receipt"]["observations"]
            require(new["receipt"] is not None and new["receipt"]["observations"][:len(observations)] == observations, "transition.receipt_lost")
        if old["cancel_state"] in {"confirmed", "not_created"}:
            require(new["cancel_state"] == old["cancel_state"], "transition.cancel_regressed")
    elif old["kind"] == "device_guard":
        by_id = {issue["issue_id"]: issue for issue in new["issues"]}
        for previous in old["issues"]:
            require(previous["issue_id"] in by_id, "transition.issue_deleted")
            current = by_id[previous["issue_id"]]
            for key in ("issue_id", "code", "owner_id", "effect_id", "opened_at"):
                require(current[key] == previous[key], "transition.issue_fact_changed")
            if previous["state"] == "resolved":
                require(current == previous, "transition.issue_reopened")
        for key, entry in old["review_requests"].items():
            require(new["review_requests"].get(key) == entry, "transition.review_lost")
        checkpoint = old["settings_checkpoint"]
        if checkpoint is not None:
            current = new["settings_checkpoint"]
            require(current is not None and current["revision"] >= checkpoint["revision"], "transition.checkpoint_rollback")
            if current["revision"] == checkpoint["revision"]:
                require(current == checkpoint, "transition.checkpoint_digest")
    else:
        mutable = {"revision", "updated_at", "state", "failure"}
        require(all(new[key] == item for key, item in old.items() if key not in mutable), "transition.intent_immutable")
        require(old["state"] == "prepared" or old["state"] == new["state"], "transition.intent_replay")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="Print each fixture result.")
    args = parser.parse_args()
    schemas, make_validator = load_validators()
    fixtures = {}
    counts = {"positive": 0, "schema_negative": 0, "semantic_negative": 0, "transition": 0, "relationship": 0, "migration": 0}
    failures = []

    def run(name, reference, value, expect="valid", match=None):
        require(name not in fixtures, "fixture.duplicate_name")
        errors = list(make_validator(reference).iter_errors(value))
        actual, message = "valid", ""
        if errors:
            actual = "schema"
            message = errors[0].message
        else:
            try:
                semantics(value)
            except ContractFailure as exc:
                actual, message = "semantic", str(exc)
        passed = actual == expect and (match is None or match in message)
        if not passed:
            failures.append(f"{name}: expected {expect}/{match}, got {actual}: {message}")
        elif args.verbose:
            print(f"PASS {name} [{actual}]")
        key = {"valid": "positive", "schema": "schema_negative", "semantic": "semantic_negative"}[expect]
        counts[key] += 1
        if expect == "valid" and passed:
            fixtures[name] = (reference, value)

    for filename in ("positive.json", "intent.json", "reviewed.json", "migration.json"):
        for case in read_json(ROOT / "fixtures" / filename):
            require(set(case) == {"name", "schema", "value"}, "fixture.literal_fields")
            run(case["name"], case["schema"], case["value"])
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1

    variants = read_json(ROOT / "fixtures" / "variants.json")
    variants += read_json(ROOT / "fixtures" / "migration-variants.json")
    for case in variants:
        require(set(case) <= {"name", "base", "schema", "select", "edits", "expect", "match"}, "fixture.variant_fields")
        reference, value = fixtures[case["base"]]
        value = deepcopy(value)
        for part in case.get("select", []):
            value = value[part]
        run(case["name"], case.get("schema", reference), edit_value(value, case.get("edits", []), fixtures), case["expect"], case.get("match"))

    for case in read_json(ROOT / "fixtures" / "transitions.json"):
        old = fixtures[case["before"]][1]
        new = fixtures[case["after"]][1]
        message = ""
        try:
            check_transition(old, new)
        except ContractFailure as exc:
            message = str(exc)
        passed = (not message) if case["expect"] == "valid" else message == case["match"]
        if not passed:
            failures.append(f"{case['name']}: transition mismatch: {message or 'valid'}")
        elif args.verbose:
            print(f"PASS {case['name']} [transition]")
        counts["transition"] += 1

    for case in read_json(ROOT / "fixtures" / "migration-checks.json"):
        message = ""
        try:
            if case["check"] == "source_bundle":
                check_migration_bundle(fixtures[case["source"]][1], fixtures[case["intent"]][1])
            elif case["check"] == "baseline_source":
                check_baseline_source(fixtures[case["source"]][1], fixtures[case["file"]][1])
            elif case["check"] == "observation":
                check_migration_observation(case, fixtures)
            elif case["check"] == "file_transition":
                check_migrated_file_transition(fixtures[case["before"]][1], fixtures[case["after"]][1])
            elif case["check"] == "put_gate":
                check_migration_put_gate(case, fixtures)
            elif case["check"] == "intent_transition":
                old, new = fixtures[case["before"]][1], fixtures[case["after"]][1]
                require(all(new[key] == value for key, value in old.items() if key not in {"state", "committed_at", "updated_at"}), "migration.intent_source_changed")
                require(old["state"] == "prepared" and new["state"] == "committed", "migration.intent_state_reversed")
                chronological(old["updated_at"], new["updated_at"])
            else:
                raise ContractFailure("fixture.unknown_migration_check")
        except ContractFailure as exc:
            message = str(exc)
        passed = not message if case["expect"] == "valid" else message == case["match"]
        if not passed:
            failures.append(f"{case['name']}: migration mismatch: {message or 'valid'}")
        elif args.verbose:
            print(f"PASS {case['name']} [migration]")
        counts["migration"] += 1

    # These are relationships between static examples, not a simulated runtime.
    bundle = {name: item[1] for name, item in fixtures.items()}
    settings = bundle["settings-file-open-then-close"]
    owner = bundle["owner-revoked"]
    effect = bundle["effect-unknown-waiting-phone"]
    guard = bundle["guard-reviewed-no-phone-ack"]
    relationships = {
        "checkpoint_matches_file": guard["settings_checkpoint"] == {"revision": settings["revision"], "digest": digest(settings)},
        "effect_owner_matches": effect["owner_id"] == owner["record_id"] and effect["device_id"] == owner["device_id"] == settings["device_id"],
        "old_owner_below_boundary": owner["settings_revision"] <= settings["revoked_through_revision"],
        "replay_is_original_not_current": bundle["success-replayed"]["settings"] == settings["requests"]["请求-甲"]["original_response"]["settings"] and settings["revision"] == 2 and not settings["enabled"],
        "review_does_not_forge_phone_ack": effect["state"] == "unknown" and effect["cancel_state"] == "waiting_ack" and all(item["state"] == "resolved" for item in guard["issues"]),
        "review_snapshot_preserved": guard["review_requests"]["请求-核对"]["original_response"] == bundle["review-response"],
        "cancel_ack_retains_progress": bundle["cancel-ack"]["typedCharacterCount"] == bundle["task-cancelled-progress-retained"]["typedCharacterCount"] == 4,
        "phone_start_grant_is_typing": bundle["task-start-granted"]["status"] == "typing",
    }
    for name, passed in relationships.items():
        counts["relationship"] += 1
        if not passed:
            failures.append(name)
        elif args.verbose:
            print(f"PASS {name} [relationship]")

    for failure in failures:
        print("FAIL " + failure, file=sys.stderr)
    total = sum(counts.values())
    print(f"Schemas: {len(schemas)} draft-07; fixtures: {total - len(failures)}/{total} passed")
    print("; ".join(f"{key}={count}" for key, count in counts.items()))
    print("Scope: attachment JSON only; no production imports, services, hardware, network or existing tests.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ContractFailure, KeyError, TypeError, ValueError, OSError) as exc:
        print(f"FAIL attachment validation: {exc}", file=sys.stderr)
        sys.exit(1)
