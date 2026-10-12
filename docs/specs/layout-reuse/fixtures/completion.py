"""Deterministic offline examples; --show-case emits ordinary, complete JSON.

Positive digests are calculated once while building the examples. Negative
mutations are applied afterwards by validate.py and never repaired.
"""

import base64
import copy
import hashlib
import io
import struct
import zipfile
import zlib
from datetime import datetime, timedelta, timezone


def build(v, old_examples):
    examples, cases = {}, []
    states = v.STATES
    uid = lambda number: f"a0000000-0000-4000-8000-{number:012d}"
    correlation, request_id, operation_id = uid(1), uid(2), uid(3)
    principal, device, session_id = "operator-01", "arm-01", uid(4)
    draft_id, preview_id, instance = uid(5), uid(6), uid(7)
    start = datetime(2026, 10, 3, tzinfo=timezone.utc)

    def time(seconds):
        return (start + timedelta(seconds=seconds)).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def signed(value):
        value["sha256"] = v.record_digest(value)
        return value

    def add(name, family, definition, value):
        examples[name] = value
        cases.append({"name": name, "base": name, "family": family, "definition": definition, "valid": True})
        return value

    def bad(name, base, path, value, error, delete=False):
        original = next(case for case in cases if case["name"] == base)
        item = {"name": name, "base": base, "family": original["family"], "definition": original["definition"], "valid": False, "error": error}
        item["delete" if delete else "set"] = [path] if delete else [{"path": path, "value": value}]
        cases.append(item)

    def png(width, height):
        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
        rows = b"".join(b"\x00" + bytes(((x // 4 + y // 4) % 2) * 180 + 40 for x in range(width) for _ in range(3)) for y in range(height))
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")

    image, empty_image = png(40, 40), png(650, 120)
    from PIL import Image
    source_output = io.BytesIO()
    with Image.open(io.BytesIO(image)) as synthetic:
        synthetic.resize((1000, 2000)).save(source_output, format="JPEG")
    source_image = source_output.getvalue()
    tile = {"id": "anchor-tile", "relative_path": "assets/anchor-tile.png", "sha256": hashlib.sha256(image).hexdigest(), "media_type": "image/png", "width": 40, "height": 40}
    empty_asset = {"id": "empty-input", "relative_path": "assets/empty-input.png", "sha256": hashlib.sha256(empty_image).hexdigest(), "media_type": "image/png", "width": 650, "height": 120}
    asset_record = lambda asset, data: {"asset": asset, "byte_size": len(data), "privacy_reviewed_by": principal, "privacy_reviewed_at": time(40), "source_evidence_ids": []}
    region = v.resolve_fixture(old_examples["region"], old_examples)
    binding = region["installation_binding"]
    compatibility = {
        "platform": "android", "orientation": "portrait", "wechat_version": "test-8.0",
        "layout_family": "controlled-v1", "font_setting": "normal", "display_scale": "1.0",
        "theme": "light", "system_navigation_mode": "gesture", "ime_package": "com.example.k40ime",
        "ime_layout_version": "keyboard-v1",
    }
    compatibility["sources"] = {key: "operator" for key in compatibility}
    descriptor = {"width_px": 1000, "height_px": 2000, "orientation": "portrait", "system_insets": {"left": 0, "top": 40, "right": 0, "bottom": 40}}
    screen = {**descriptor, "calibration_geometry_hash": "c" * 64}
    snapshot = {"installation_binding": binding, "source_size": region["source_size"], "source_geometry_id": region["source_geometry_id"], "ime_device_id": "ime-arm-01", "screen_descriptor": screen}

    def box(x, y, w, h):
        return {"x": x, "y": y, "width": w, "height": h}

    def anchor(name, x, y):
        return {"id": name, "asset_id": tile["id"], "search_region": box(max(0, x - .03), max(0, y - .03), .10, .08),
                "reference_rect": box(x, y, .04, .02), "required": True}

    def page(state):
        anchors = [anchor("upper", .03, .04), anchor("lower" if state in states[:2] else "keyboard_boundary", .03, .90 if state in states[:2] else .72)]
        if state == "chat_ime_text":
            anchors.append(anchor("send_button_feature", .85, .63))
        def item(name, rect, field, mode_or_purpose):
            origin = anchors[0]["reference_rect"]
            result = {"id": name, "anchor_id": "upper", "relative_rect": {
                "dx": round(rect["x"] - origin["x"], 6), "dy": round(rect["y"] - origin["y"], 6),
                "width": rect["width"], "height": rect["height"]}}
            result[field] = mode_or_purpose
            return result
        if state == "wechat_list":
            targets = [item("wechat_chats_tab", box(.05, .92, .1, .04), "mode", "tap")]
            regions = [item("unread", box(.1, .89, .05, .03), "purpose", "unread"),
                       item("content", box(.05, .14, .9, .70), "purpose", "content")]
        else:
            targets = [item("back_button", box(.02, .05, .06, .04), "mode", "tap"),
                       item("input_box", box(.12, .62, .65, .06), "mode", "tap")]
            if state == "chat_ime_text":
                targets.append(item("send_button", box(.84, .63, .1, .04), "mode", "tap"))
            regions = [item("title", box(.12, .04, .7, .06), "purpose", "title"),
                       item("input", box(.12, .62, .65, .06), "purpose", "input"),
                       item("content", box(.05, .14, .9, .45), "purpose", "content")]
        return {"state": state, "anchors": anchors, "targets": targets, "regions": regions}

    template = signed({"contract_version": "ui-layout-template.v1", "template_id": "wechat-controlled", "version": 1, "parent_version": None,
                       "compatibility": compatibility, "reference_screen_descriptor": descriptor, "pages": [page(state) for state in states],
                       "assets": [tile], "created_at": time(45), "created_by": principal})
    template_ref, region_ref = v.identity(template, "template"), v.identity(region, "region")
    captures, checks, colors, session_pages = [], [], [], []

    def evidence(capture_id, state, number, seconds):
        evidence_id = f"layout-frame-{number:03d}"
        source = {"device_id": device, "installation_binding": binding, "source_size": region["source_size"],
                  "source_geometry_id": region["source_geometry_id"], "evidence_id": evidence_id,
                  "source_session_id": uid(20), "frame_seq": number,
                  "source_captured_at": time(seconds), "server_received_at": time(seconds + .1)}
        return {"evidence_id": evidence_id, "device_id": device, "principal_id": principal, "capture_id": capture_id,
                "page_state": state, "source_binding": source, "image_sha256": hashlib.sha256(source_image).hexdigest(),
                "timing_proof": {"source_clock_id": "mach-absolute:" + "d" * 64, "clock_registration_id": uid(21),
                                 "source_generation_id": uid(20), "source_frame_seq": number, "source_capture_seq": number + 100,
                                 "source_captured_monotonic_ns": round((1000 + seconds * 1000) * 1000000),
                                 "source_capture_uncertainty_ms": 0, "source_timing_provenance": "camera_pts_host_clock.v1"},
                "media_type": "image/jpeg", "server_instance_id": instance,
                "captured_monotonic_ms": round(1000 + seconds * 1000), "received_monotonic_ms": round(1100 + seconds * 1000),
                "privacy_scope": "controlled_test"}

    def capture(state, index):
        cid, seconds = uid(100 + index), (index + 1) * 10
        return {"capture_id": cid, "device_id": device, "principal_id": principal, "owner_kind": "session",
                "owner_id": session_id, "owner_revision": index + 1, "state": state,
                "evidence": [evidence(cid, state, index * 2 + 10, seconds), evidence(cid, state, index * 2 + 11, seconds + .2)],
                "created_at": time(seconds + .3), "expires_at": time(seconds + 120.3), "consumed_by": None}

    def checked_item(name, kind, rect, evidence_ids, tap):
        safe = {**rect, "x": round(rect["x"] + .002, 12), "y": round(rect["y"] + .001, 12),
                "width": round(rect["width"] - .004, 12), "height": round(rect["height"] - .002, 12)} if tap else None
        return {"id": name, "kind": kind, "status": "passed", "resolved_rect": rect, "safe_rect": safe, "evidence_ids": evidence_ids, "reason_code": None}

    for index, definition in enumerate(template["pages"]):
        state = definition["state"]
        captured = capture(state, index)
        captures.append(captured)
        evidence_ids = [item["evidence_id"] for item in captured["evidence"]]
        anchors = [{"id": item["id"], "status": "passed", "candidate_count": 1, "matched_rect": item["reference_rect"],
                    "best_score": .98, "runner_up_score": None, "localization_error_bound_px": 1, "reason_code": None}
                   for item in definition["anchors"]]
        items = []
        for kind, field in (("target", "targets"), ("region", "regions")):
            for item in definition[field]:
                origin = next(anchor_item["reference_rect"] for anchor_item in definition["anchors"] if anchor_item["id"] == item["anchor_id"])
                items.append(checked_item(item["id"], kind, v.relative_box(origin, item["relative_rect"]), evidence_ids, kind == "target" and item["mode"] == "tap"))
        if index >= 2:
            items.extend([checked_item("ime_candidate_bar", "target", box(.05, .74, .75, .06), evidence_ids, True),
                          checked_item("ime_delete_key", "target", box(.86, .86, .08, .08), evidence_ids, True)])
        check = {"state": state, "status": "passed", "evidence_ids": evidence_ids, "anchor_results": anchors, "item_results": items, "reason_codes": []}
        checks.append(check)
        color = {"state": state, "foreground_rgb": [30, 31, 32], "background_rgb": [240, 240, 240],
                 "empty_input_asset_ref": {"id": empty_asset["id"], "sha256": empty_asset["sha256"]} if index in (1, 2) else None,
                 "evidence_ids": evidence_ids}
        colors.append(color)
        session_pages.append({"state": state, "capture": {
            "accepted_at": time((index + 1) * 10 + .3), "server_instance_id": instance,
            "accepted_monotonic_ms": (index + 1) * 10000 + 1300, "configuration_sha256": "0" * 64,
            "capture_id": captured["capture_id"], "check": check, "local_overrides": [], "color_sample": color}})

    adaptation = signed({"contract_version": "device-layout-adaptation.v1", "device_id": device, "version": 1,
                         "template_ref": template_ref, "ime_device_id": "ime-arm-01", "keyboard_geometry_mode": "live_ime_v1",
                         "installation_region_ref": region_ref, "screen_descriptor": screen, "compatibility_observed": compatibility,
                         "page_checks": checks, "local_overrides": [], "color_samples": colors, "reviewed_by": principal, "reviewed_at": time(50)})
    adaptation_ref = v.identity(adaptation, "adaptation")
    session = {"capture_session_id": session_id, "device_id": device, "principal_id": principal, "revision": 5,
               "template_ref": template_ref, "installation_region_ref": region_ref, "binding_snapshot": snapshot,
               "compatibility_snapshot": compatibility, "keyboard_geometry_mode": "live_ime_v1", "expected_pointer_revision": 0,
               "created_at": time(0), "expires_at": time(1800), "server_instance_id": instance, "created_monotonic_ms": 1000,
               "pages": session_pages, "assets": [asset_record(empty_asset, empty_image)]}
    for entry in session["pages"]:
        entry["capture"]["configuration_sha256"] = v.configuration(session)
    draft = {"draft_id": draft_id, "device_id": device, "principal_id": principal, "revision": 6, "base_template_ref": None,
             "binding_snapshot": snapshot, "compatibility": compatibility, "reference_screen_descriptor": descriptor,
             "pages": [{"state": definition["state"], "definition": definition, "page_check": check,
                        "source_evidence_ids": check["evidence_ids"], "updated_at": time(40), "missing_items": []}
                       for definition, check in zip(template["pages"], checks)],
             "assets": [asset_record(tile, image)], "created_at": time(0), "updated_at": time(40), "published_refs": []}
    current = {"capture_id": uid(200), "device_id": device, "principal_id": principal, "owner_kind": "search_region",
               "owner_id": None, "owner_revision": 0, "state": None, "evidence": [evidence(uid(200), None, 100, 49.8)],
               "created_at": time(49.9), "expires_at": time(169.9), "consumed_by": None}
    accepted = v.passed_keys(checks)
    preview = {"preview_id": preview_id, "device_id": device, "principal_id": principal, "request_hash": "f" * 64,
               "capture_session_id": session_id, "session_revision": 5, "binding_snapshot": snapshot,
               "configuration_sha256": v.configuration(session), "current_evidence_id": current["evidence"][0]["evidence_id"],
               "page_checks": checks, "proposed_overrides": [], "accepted_items": accepted,
               "created_at": time(49.9), "expires_at": time(169.9), "server_instance_id": instance,
               "created_monotonic_ms": 50900, "consumed_by": None}
    calibration = {"schema_version": 3, "geometry_hash": screen["calibration_geometry_hash"], "installation_binding": binding,
                   "source_geometry_id": region["source_geometry_id"], "mapping_error_bound_px": 1}
    add("complete-template", "layout", "template", template)
    add("complete-adaptation", "layout", "adaptation", adaptation)
    add("complete-draft", "layout", "draft", draft)
    add("complete-session", "layout", "session", session)
    add("complete-preview-record", "layout", "preview_record", preview)
    add("complete-capture", "layout", "capture", captures[0])
    add("complete-graph", "checks", "graph", {"template": template, "adaptation": adaptation, "region": region, "assets": [asset_record(empty_asset, empty_image)], "calibration": calibration})
    add("four-pages-one-confirmation", "checks", "session_case", {
        "session": session, "template": template, "region": region, "captures": captures, "current_capture": current,
        "preview": preview, "accepted_items": accepted, "now": time(50), "now_ms": 51000,
        "server_instance_id": instance, "current_binding": snapshot, "current_compatibility": compatibility,
        "current_calibration": calibration, "device_idle": True})
    changed = copy.deepcopy(session)
    changed["revision"] += 1
    add("page-edit-preserves-other-pages", "checks", "page_edit_case", {"before": session, "after": changed, "changed_state": states[0]})
    inherited = copy.deepcopy(session)
    inherited.update({"capture_session_id": uid(401), "revision": 1, "created_at": time(50), "expires_at": time(1850), "created_monotonic_ms": 51000})
    inherited["pages"] = [inherited["pages"][0]] + [{"state": state, "capture": None} for state in states[1:]]
    add("inherit-preserves-accepted-time", "checks", "inherit_case", {"before": session, "after": inherited, "states": [states[0]], "now": time(50), "server_instance_id": instance})
    unknown = copy.deepcopy(compatibility)
    unknown["theme"] = None
    unknown["sources"]["theme"] = None
    add("unknown-compatibility-in-draft", "layout", "compatibility", unknown)
    failed = copy.deepcopy(checks[1])
    failed["status"], failed["reason_codes"] = "failed", ["safe_rect_empty"]
    failed["item_results"][0].update({"status": "failed", "safe_rect": None, "reason_code": "safe_rect_empty"})
    add("one-item-failed-preserves-results", "layout", "page_check", failed)

    resources = [{"kind": "adaptation", "ref": adaptation_ref}, {"kind": "region", "ref": region_ref}, {"kind": "template", "ref": template_ref}]
    entries, payloads = [], {}
    for resource, record in zip(resources, (adaptation, region, template)):
        prefix = {"adaptation": "adaptations", "region": "regions", "template": "templates"}[resource["kind"]]
        base = f'{prefix}/{resource["ref"]["id"]}/{resource["ref"]["version"]}/'
        payload = v.canonical(record)
        entries.append({"path": base + "record.json", "kind": "record", "resource": resource, "owner": None, "asset": None, "byte_size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        payloads[base + "record.json"] = payload
        assets = [(empty_asset, empty_image)] if resource["kind"] == "adaptation" else [(tile, image)] if resource["kind"] == "template" else []
        for asset, data in assets:
            path = base + asset["relative_path"]
            entries.append({"path": path, "kind": "asset", "resource": None, "owner": resource, "asset": asset, "byte_size": len(data), "sha256": asset["sha256"]})
            payloads[path] = data
    manifest = signed({"contract_version": "layout-package-manifest.v1", "package_id": uid(8), "roots": [resources[0]],
                       "entries": sorted(entries, key=lambda item: item["path"]), "file_count": len(entries) + 1,
                       "total_bytes": sum(item["byte_size"] for item in entries), "created_at": time(60), "created_by": principal,
                       "privacy_scope": "sanitized_layout_only"})
    payloads["manifest.json"] = v.canonical(manifest)

    def zip_bytes(files):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in sorted(files.items()):
                info = zipfile.ZipInfo(name, (2026, 10, 3, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100600 << 16)
                archive.writestr(info, payload)
        return output.getvalue()

    archive = zip_bytes(payloads)
    archive_sha = hashlib.sha256(archive).hexdigest()
    imported_items = [{"resource": entry["resource"], "status": "new"} for entry in manifest["entries"] if entry["kind"] == "record"]
    add("closed-package-manifest", "package", "manifest", manifest)
    add("closed-package-upload", "package", "import_preview_request", {"archive_sha256": archive_sha, "archive_base64": base64.b64encode(archive).decode(), "privacy_confirmed": True})
    issue = {"issue_id": uid(9), "device_id": device, "kind": "item", "resource_ref": adaptation_ref, "item": accepted[0],
             "revision": 1, "state": "open", "reason_code": "anchor_missing", "opened_at": time(60), "resolution": None}
    add("complete-issue", "records", "issue", issue)
    request_payloads = {
        "region_preview": v.resolve_fixture(old_examples["preview_request"], old_examples),
        "region_save": v.resolve_fixture(old_examples["save_request"], old_examples),
        "template_publish": {"client_request_id": request_id, "template_id": template["template_id"], "expected_parent_version": None, "draft_id": draft_id, "expected_draft_revision": 6},
        "draft_create": {"client_request_id": request_id, "device_id": device, "base_template_ref": None},
        "draft_metadata": {"client_request_id": request_id, "expected_revision": 6, "compatibility": compatibility},
        "draft_page": {"client_request_id": request_id, "expected_revision": 6, "source_evidence_ids": checks[0]["evidence_ids"], "page_definition": template["pages"][0]},
        "draft_asset_upload": {"client_request_id": request_id, "expected_revision": 6, "asset": tile, "content_base64": base64.b64encode(image).decode(), "source_evidence_ids": [], "privacy_confirmed": True},
        "capture": {"client_request_id": request_id, "owner_kind": "session", "owner_id": session_id, "expected_owner_revision": 1, "page_state": states[0], "controlled_test_confirmed": True},
        "session_create": {"client_request_id": request_id, "template_ref": template_ref, "expected_revision": 0, "keyboard_geometry_mode": "live_ime_v1", "inherit_session_id": None, "inherit_states": []},
        "session_page": {"client_request_id": request_id, "expected_session_revision": 4, "source_evidence_ids": checks[0]["evidence_ids"], "local_overrides": []},
        "adaptation_preview": {"capture_session_id": session_id, "expected_session_revision": 5},
        "adaptation_publish": {"client_request_id": request_id, "expected_revision": 0, "preview_id": preview_id, "accepted_items": accepted},
        "review": {"client_request_id": request_id, "issue_ids": [issue["issue_id"]], "expected_revision": 1, "evidence_ids": checks[0]["evidence_ids"], "reason": "controlled-page-recheck"},
        "package_export": {"client_request_id": request_id, "roots": [resources[0]]},
        "package_import_preview": {"archive_sha256": archive_sha, "archive_base64": base64.b64encode(archive).decode(), "privacy_confirmed": True},
        "package_import": {"client_request_id": request_id, "preview_id": preview_id, "archive_sha256": archive_sha},
        "rollback_preview": {"kind": "phone_search_region", "target_ref": region_ref, "expected_revision": 1, "source_evidence_id": current["evidence"][0]["evidence_id"], "capture_session_id": None, "expected_session_revision": None},
        "rollback": {"client_request_id": request_id, "expected_revision": 1, "preview_id": preview_id},
        "ime_session": old_examples["handshake_request"],
        "ime_report": v.resolve_fixture(old_examples["ime_snapshot"], old_examples),
    }
    region_pointer = copy.deepcopy(v.resolve_fixture(old_examples["active_pointer"], old_examples))
    region_pointer.update({"revision": 2, "operation_id": operation_id, "updated_at": time(70)})
    created_session = copy.deepcopy(session)
    created_session.update({"revision": 1, "pages": [{"state": state, "capture": None} for state in states]})
    updated_draft = copy.deepcopy(draft)
    updated_draft["revision"] += 1
    responses = {
        "region_get": v.resolve_fixture(old_examples["unregistered"], old_examples),
        "region_preview": v.resolve_fixture(old_examples["preview"], old_examples),
        "region_save": v.resolve_fixture(old_examples["save_response"], old_examples),
        "region_version": {"record": region},
        "template_list": {"items": [{"template_ref": template_ref, "parent_version": None, "compatibility": compatibility, "created_at": time(45)}], "limit": 20, "next_cursor": None, "snapshot_at": time(60), "snapshot_expires_at": time(960)},
        "template_version": {"record": template},
        "template_publish": {"operation_id": operation_id, "template_ref": template_ref, "replayed": False},
        "draft_create": {"operation_id": operation_id, "draft_id": draft_id, "revision": 1, "replayed": False},
        "draft_get": {"draft": draft, "blocking_reasons": []},
        "draft_metadata": {"operation_id": operation_id, "draft": updated_draft, "replayed": False},
        "draft_page": {"operation_id": operation_id, "revision": 7, "page": draft["pages"][0], "replayed": False},
        "draft_asset_upload": {"operation_id": operation_id, "revision": 7, "asset": asset_record(tile, image), "replayed": False},
        "capture": {"operation_id": operation_id, "capture": captures[0], "replayed": False},
        "evidence_get": {"evidence": captures[0]["evidence"][0]},
        "session_create": {"operation_id": operation_id, "session": created_session, "required_states": list(states), "replayed": False},
        "session_get": {"session": session, "evaluated_at": time(50), "page_status": [{"state": state, "status": "passed", "expires_at": time((index + 1) * 10 + 900.3), "reason_codes": []} for index, state in enumerate(states)]},
        "session_page": {"operation_id": operation_id, "session_revision": 5, "page": session["pages"][0], "replayed": False},
        "adaptation_preview": {key: preview[key] for key in ("preview_id", "page_checks", "proposed_overrides", "accepted_items", "created_at", "expires_at")},
        "adaptation_publish": {"operation_id": operation_id, "revision": 1, "adaptation_ref": adaptation_ref,
                               "per_item_status": [{"item": key, "status": "passed", "reason_code": None} for key in accepted], "replayed": False},
        "adaptation_get": {"revision": 1, "selected_ref": adaptation_ref, "status": "verified", "page_checks": checks, "blocking_reasons": [], "execution_blocked": True, "execution_blocking_reasons": ["calibration_unavailable"]},
        "adaptation_version": {"record": adaptation},
        "issue_list": {"revision": 1, "items": [issue], "limit": 20, "next_cursor": None, "snapshot_at": time(60), "snapshot_expires_at": time(960)},
        "review": {"operation_id": operation_id, "revision": 2, "results": [{"issue_id": issue["issue_id"], "state": "resolved", "reason_code": None}], "replayed": False},
        "operation_request": {"operation_id": operation_id, "phase": "prepared", "original_response": None, "error": None},
        "operation_get": {"operation_id": operation_id, "phase": "prepared", "original_response": None, "error": None},
        "package_export": {"operation_id": operation_id, "package_id": manifest["package_id"], "manifest": manifest, "archive_sha256": archive_sha, "archive_byte_size": len(archive), "replayed": False},
        "package_import_preview": {"preview_id": preview_id, "manifest": manifest, "archive_sha256": archive_sha, "items": imported_items, "can_import": True, "created_at": time(60), "expires_at": time(180)},
        "package_import": {"operation_id": operation_id, "package_id": manifest["package_id"], "items": imported_items, "pointers_changed": False, "replayed": False},
        "rollback_preview": {"preview_id": preview_id, "kind": "phone_search_region", "target_ref": region_ref, "expected_revision": 1, "status": "verified", "reason_codes": [], "created_at": time(60), "expires_at": time(180)},
        "rollback": {"operation_id": operation_id, "pointer": region_pointer, "review_id": uid(11), "replayed": False},
        "ime_session": old_examples["handshake_response"], "ime_report": old_examples["report_response"], "ime_poll": old_examples["poll_response"],
    }
    responses["package_get"] = responses["package_export"]
    path_values = {"device_id": device, "ime_device_id": "ime-arm-01", "version": 1, "template_id": template["template_id"],
                   "asset_id": tile["id"], "draft_id": draft_id, "state": states[0], "session_id": session_id,
                   "evidence_id": captures[0]["evidence"][0]["evidence_id"], "client_request_id": request_id,
                   "operation_id": operation_id, "package_id": manifest["package_id"]}
    for route in v.ROUTES:
        name, fleet = route["id"], route["protocol"] == "fleet"
        headers = {}
        body = copy.deepcopy(request_payloads.get(name))
        if route["method"] == "GET" and fleet:
            headers = {"X-Correlation-Id": correlation, "X-Layout-Contract-Version": route["contract_version"]}
        elif route["method"] != "GET":
            headers["Content-Type"] = "application/json"
        if body is not None and fleet:
            body.update({"contract_version": route["contract_version"], "correlation_id": correlation})
        query = {"limit": 20, "cursor": None} if route["query"] == "api:pagination" else old_examples["poll_query"] if name == "ime_poll" else {}
        request = {"route_id": name, "method": route["method"], "path": {key: path_values[key] for key in route["path_parameters"]}, "headers": headers, "query": query, "body": body}
        add("http-" + name + "-request", "wire", name + "_request", request)
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        if fleet:
            headers.update({"X-Correlation-Id": correlation, "X-Layout-Contract-Version": route["contract_version"]})
        if route["response"].startswith("binary:"):
            data = archive if route["response"] == "binary:zip" else empty_image if name == "adaptation_asset" else source_image if name == "evidence_image" else image
            if name == "adaptation_asset":
                request["path"]["asset_id"] = empty_asset["id"]
            body = base64.b64encode(data).decode()
            headers.update({"Content-Type": "application/zip" if route["response"] == "binary:zip" else "image/png",
                            "Content-Length": len(data), "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
                            "Content-Disposition": "attachment; filename=layout-package.zip" if route["response"] == "binary:zip" else "inline"})
        else:
            body = copy.deepcopy(responses[name])
            if fleet:
                body.update({"contract_version": route["contract_version"], "correlation_id": correlation})
        add("http-" + name + "-response", "wire", name + "_response", {"route_id": name, "status": 200, "headers": headers, "body": body})
        for code, status in v.ERROR_STATUS.items():
            if code.startswith("layout_") != fleet:
                continue
            error = {"contract_version": "layout-error.v1" if fleet else "ime-geometry.v1",
                     "code": code, "message": "\u8d44\u6599\u6821\u9a8c\u672a\u901a\u8fc7", "motion_started": False, "retryable": False}
            error_headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
            if fleet:
                error.update({"correlation_id": correlation, "operation_id": None})
                error_headers.update({"X-Correlation-Id": correlation, "X-Layout-Contract-Version": "layout-error.v1"})
            fixture_name = "http-" + name + "-error-" + code
            add(fixture_name, "wire", name + "_error", {"route_id": name, "status": status, "headers": error_headers, "body": error})
        bad("reject-" + name + "-unknown-query", "http-" + name + "-request", "/query/extra", 1, "schema:")
        bad("reject-" + name + "-missing-path", "http-" + name + "-request", "/path", None, "schema:", delete=True)
        bad("reject-" + name + "-unknown-response", "http-" + name + "-response", "/unexpected", True, "schema:")
        if body is not None and not route["response"].startswith("binary:"):
            bad("reject-" + name + "-missing-response-version", "http-" + name + "-response", "/body/contract_version", None, "schema:", delete=True)
    add("http-ime_poll-no-request", "wire", "ime_poll_response", {"route_id": "ime_poll", "status": 204, "headers": {"Cache-Control": "no-store"}, "body": None})

    wire_request = examples["http-region_save-request"]
    wire_response = examples["http-region_save-response"]
    saved_op_id = wire_response["body"]["operation_id"]
    operation = {"operation_id": saved_op_id, "resource_kind": "region", "resource_id": device, "device_id": device, "principal_id": principal,
                 "request_id": wire_request["body"]["client_request_id"], "request_hash": v.request_hash(wire_request),
                 "before_revision": 0, "after_revision": 1, "phase": "committed", "version_ref": region_ref,
                 "response": wire_response, "error": None, "created_at": region["created_at"], "updated_at": region["created_at"]}
    before = v.resolve_fixture(old_examples["empty_pointer"], old_examples)
    after = v.resolve_fixture(old_examples["active_pointer"], old_examples)
    file_plan = {"relative_path": "devices/arm-01/phone_search_region/versions/1.json", "sha256": hashlib.sha256(v.canonical(region)).hexdigest(), "byte_size": len(v.canonical(region))}
    intent = {"operation_id": saved_op_id, "request": wire_request, "request_hash": operation["request_hash"], "planned_response": wire_response,
              "files": [file_plan], "pointer_before": before, "pointer_after": after,
              "mutations": [{"table": "catalog", "key": v.state_key("catalog", {"resource": {"kind": "region", "ref": region_ref}}), "expected_revision": 0,
                             "value": {"resource": {"kind": "region", "ref": region_ref}, "operation_id": saved_op_id, "created_at": region["created_at"]}}],
              "created_at": region["created_at"]}
    add("complete-operation", "records", "operation", operation)
    add("complete-intent", "records", "intent", intent)
    add("complete-operation-row", "records", "row", {"record_id": saved_op_id, "device_id": device, "kind": "operation", "revision": 2, "json": operation})
    add("complete-region-draft-record", "records", "region_draft", {"device_id": device, "ref": region_ref, "operation_id": saved_op_id, "created_at": time(1), "principal_id": principal})
    add("complete-review-revision", "records", "review_revision", {"device_id": device, "revision": 0, "updated_at": None, "operation_id": None})
    add("complete-adaptation-asset", "records", "adaptation_asset", {"owner_ref": adaptation_ref, "record": asset_record(empty_asset, empty_image), "operation_id": operation_id})
    add("complete-region-preview-record", "records", "region_preview", {"principal_id": principal, "request_hash": v.request_hash(examples["http-region_preview-request"]),
        "request": examples["http-region_preview-request"], "response": examples["http-region_preview-response"]["body"],
        "server_instance_id": instance, "created_monotonic_ms": 1000, "consumed_by": None})
    add("complete-rollback-preview-record", "records", "rollback_preview", {
        "principal_id": principal, "device_id": device, "request_hash": v.request_hash(examples["http-rollback_preview-request"]),
        "request": examples["http-rollback_preview-request"], "response": examples["http-rollback_preview-response"]["body"],
        "binding_snapshot": snapshot, "evidence_ids": [current["evidence"][0]["evidence_id"]],
        "server_instance_id": instance, "created_monotonic_ms": 61000, "consumed_by": None})
    add("complete-import-preview-record", "package", "preview_record", {
        "preview_id": preview_id, "principal_id": principal, "request_hash": v.request_hash(examples["http-package_import_preview-request"]),
        "archive_sha256": archive_sha, "manifest": manifest, "items": imported_items, "created_at": time(60), "expires_at": time(180),
        "server_instance_id": instance, "created_monotonic_ms": 61000, "consumed_by": None})
    add("complete-export-record", "package", "export_record", {
        "package_id": manifest["package_id"], "principal_id": principal, "operation_id": operation_id, "manifest": manifest,
        "archive_sha256": archive_sha, "archive_byte_size": len(archive), "created_at": time(60)})
    add("complete-review-record", "records", "review", {
        "review_id": uid(11), "device_id": device, "operation_id": operation_id, "principal_id": principal,
        "request_hash": v.request_hash(examples["http-review-request"]), "before_revision": 1, "after_revision": 2,
        "original_issues": [issue], "results": responses["review"]["results"], "evidence_ids": checks[0]["evidence_ids"],
        "reason": "controlled-page-recheck", "created_at": time(70), "original_response": examples["http-review-response"]})
    replay_response = copy.deepcopy(wire_response)
    replay_response["body"]["replayed"] = True
    add("idempotent-replay-original-result", "checks", "replay_case", {
        "operation": operation, "intent": intent, "retry_request": wire_request, "principal_id": principal, "response": replay_response})
    for label, pointer, files, committed, expected in (
        ("before-files", before, [], False, "aborted"), ("files-only", before, [file_plan], False, "aborted"),
        ("pointer-before-store", after, [file_plan], False, "committed"),
        ("store-committed", after, [file_plan], True, "committed"),
        ("pointer-with-missing-file", after, [], False, "unknown"),
    ):
        pending_op = copy.deepcopy(operation)
        pending_op.update({"phase": "prepared", "response": None, "after_revision": None})
        add("crash-" + label, "checks", "crash_case", {"operation": pending_op, "intent": intent, "observed_pointer": pointer,
            "durable_files": files, "store_committed": committed, "result_phase": expected, "result_pointer": pointer})
    cursor = {"cursor_id": uid(12), "principal_id": principal, "route_id": "template_list", "device_id": None,
              "limit": 20, "offset": 0, "snapshot_at": time(60), "expires_at": time(960),
              "template_items": responses["template_list"]["items"], "issue_items": [], "review_revision": 0}
    add("complete-pagination-cursor", "records", "cursor", cursor)
    add("pagination-snapshot-final-page", "checks", "pagination_case", {"cursor": cursor, "principal_id": principal,
        "route_id": "template_list", "device_id": None, "limit": 20, "now": time(65), "response": responses["template_list"]})

    state_payloads = {
        "draft": (draft, device, principal, 6),
        "session": (session, device, principal, 5),
        "capture": (captures[0], device, principal, 1),
        "evidence": (captures[0]["evidence"][0], device, principal, 1),
        "region_draft": (examples["complete-region-draft-record"], device, principal, 1),
        "region_preview": (examples["complete-region-preview-record"], device, principal, 1),
        "adaptation_preview": (preview, device, principal, 1),
        "rollback_preview": (examples["complete-rollback-preview-record"], device, principal, 1),
        "package_import_preview": (examples["complete-import-preview-record"], None, principal, 1),
        "package_export": (examples["complete-export-record"], None, principal, 1),
        "cursor": (cursor, None, principal, 1),
        "review_revision": (examples["complete-review-revision"], device, None, 1),
        "adaptation_asset": (examples["complete-adaptation-asset"], device, None, 1),
        "catalog": (intent["mutations"][0]["value"], device, None, 1),
        "intent": (intent, operation["device_id"], operation["principal_id"], 1),
    }
    for kind, (payload, owner_device, owner_principal, revision) in state_payloads.items():
        row = {"kind": kind, "state_key": v.state_key(kind, payload), "device_id": owner_device,
               "principal_id": owner_principal, "revision": revision, "json": copy.deepcopy(payload)}
        add("state-row-" + kind, "records", "state_row", row)
        bad("state-reject-" + kind + "-wrong-key", "state-row-" + kind, "/state_key", "0" * 64, "state:key")
    for table, kind in v.STATE_MUTATIONS.items():
        row = examples["state-row-" + kind]
        add("state-mutation-" + table, "records", "mutation",
            {"table": table, "key": row["state_key"], "expected_revision": row["revision"] - 1, "value": row["json"]})
    inserted = copy.deepcopy(examples["state-row-draft"])
    inserted["revision"] = inserted["json"]["revision"] = 1
    add("state-cas-insert", "records", "state_cas_case", {"before": None, "after": inserted, "expected_revision": 0})
    for kind in ("draft", "session"):
        before_row = examples["state-row-" + kind]
        after_row = copy.deepcopy(before_row)
        after_row["revision"] += 1
        after_row["json"]["revision"] += 1
        add("state-cas-update-" + kind, "records", "state_cas_case",
            {"before": before_row, "after": after_row, "expected_revision": before_row["revision"]})
    for kind in sorted(v.CONSUMABLE_STATES):
        before_row = examples["state-row-" + kind]
        after_row = copy.deepcopy(before_row)
        after_row["revision"], after_row["json"]["consumed_by"] = 2, request_id
        add("state-cas-consume-" + kind, "records", "state_cas_case",
            {"before": before_row, "after": after_row, "expected_revision": 1})
    before_row = examples["state-row-review_revision"]
    after_row = copy.deepcopy(before_row)
    after_row["revision"] = 2
    after_row["json"].update({"revision": 1, "updated_at": time(70), "operation_id": operation_id})
    add("state-cas-review-revision", "records", "state_cas_case",
        {"before": before_row, "after": after_row, "expected_revision": 1})
    changed_owner = copy.deepcopy(examples["state-cas-update-draft"]["after"])
    changed_owner["principal_id"] = changed_owner["json"]["principal_id"] = "operator-02"
    changed_identity = copy.deepcopy(examples["state-cas-update-draft"]["after"])
    changed_identity["json"]["draft_id"] = uid(901)
    changed_identity["state_key"] = v.state_key("draft", changed_identity["json"])
    doubled = examples["state-cas-consume-adaptation_preview"]["after"]
    for name, base, path, replacement, error in (
        ("state-unknown-kind", "state-row-draft", "/kind", "other", "schema:"),
        ("state-wrong-kind-payload", "state-row-draft", "/kind", "session", "schema:"),
        ("state-extra-column", "state-row-draft", "/extra", True, "schema:"),
        ("state-extra-json-field", "state-row-draft", "/json/extra", True, "schema:"),
        ("state-wrong-device", "state-row-draft", "/device_id", "arm-02", "state:owner"),
        ("state-wrong-principal", "state-row-session", "/principal_id", None, "state:owner"),
        ("state-shared-catalog-owner", "state-row-catalog", "/principal_id", principal, "state:owner"),
        ("state-export-device", "state-row-package_export", "/device_id", device, "state:owner"),
        ("state-region-draft-ref-owner", "state-row-region_draft", "/json/ref/id", "arm-02", "state:owner"),
        ("state-intent-anonymous", "state-row-intent", "/principal_id", None, "state:intent_principal"),
        ("state-revision-zero", "state-row-draft", "/revision", 0, "schema:"),
        ("state-draft-revision-mismatch", "state-row-draft", "/revision", 7, "state:revision"),
        ("state-review-offset-mismatch", "state-row-review_revision", "/revision", 2, "state:revision"),
        ("state-insert-not-zero", "state-cas-insert", "/expected_revision", 1, "state:insert_revision"),
        ("state-stale-cas", "state-cas-update-draft", "/expected_revision", 5, "state:cas_conflict"),
        ("state-cas-owner-changed", "state-cas-update-draft", "/after", changed_owner, "state:identity_changed"),
        ("state-cas-key-changed", "state-cas-update-draft", "/after", changed_identity, "state:identity_changed"),
        ("state-cas-rewrites-clock", "state-cas-consume-adaptation_preview", "/after/json/created_monotonic_ms", 50901, "state:consumption_changed_payload"),
        ("state-mutation-not-derived-key", "complete-intent", "/mutations/0/key", "region-arm-01-1", "state:mutation_key"),
        ("state-mutation-nonzero-insert", "state-mutation-catalog", "/expected_revision", 1, "state:revision"),
        ("state-cannot-enter-review-table", "complete-operation-row", "/kind", "draft", "schema:"),
    ):
        bad(name, base, path, replacement, error)
    for name, value, error in (
        ("state-immutable-update", {"before": examples["state-row-evidence"], "after": examples["state-row-evidence"], "expected_revision": 1}, "state:immutable"),
        ("state-double-consumption", {"before": doubled, "after": doubled, "expected_revision": 2}, "state:already_consumed"),
    ):
        examples[name] = value
        cases.append({"name": name, "base": name, "family": "records", "definition": "state_cas_case", "valid": False, "error": error})

    for name, base, path, replacement, error in (
        ("template-extra-property", "complete-template", "/device_id", device, "schema:"),
        ("template-unknown-compatibility", "complete-template", "/compatibility/theme", None, "layout:"),
        ("template-missing-asset", "complete-template", "/pages/0/anchors/0/asset_id", "missing", "layout:asset_reference"),
        ("template-duplicate-state", "complete-template", "/pages/1/state", states[0], "layout:"),
        ("template-parent-self", "complete-template", "/parent_version", 1, "layout:parent"),
        ("template-digest-corrupt", "complete-template", "/sha256", "0" * 64, "layout:digest_mismatch"),
        ("template-relative-offscreen", "complete-template", "/pages/0/targets/0/relative_rect/dx", -.5, "layout:rect_origin"),
        ("template-id-version-string", "complete-template", "/version", "latest", "schema:"),
        ("template-boolean-version", "complete-template", "/version", True, "schema:"),
        ("template-unknown-source", "unknown-compatibility-in-draft", "/sources/theme", "operator", "layout:unknown_source"),
        ("adaptation-camera-on-other-device", "complete-adaptation", "/installation_region_ref/id", "arm-02", "layout:region_owner"),
        ("adaptation-bogus-geometry-mode", "complete-adaptation", "/keyboard_geometry_mode", "auto", "schema:"),
        ("adaptation-page-hides-failure", "complete-adaptation", "/page_checks/1/status", "failed", "layout:page_reason_missing"),
        ("failed-target-has-safe-rect", "one-item-failed-preserves-results", "/item_results/0/safe_rect", box(.02, .05, .02, .02), "layout:failed_safe_rect"),
        ("anchor-ambiguous-pass", "complete-adaptation", "/page_checks/0/anchor_results/0/best_score", .8, "layout:score_threshold"),
        ("anchor-runner-up-missing", "complete-adaptation", "/page_checks/0/anchor_results/0/candidate_count", 2, "layout:runner_up"),
        ("session-binding-changed", "four-pages-one-confirmation", "/current_binding/installation_binding/camera_binding_id", uid(900), "session:binding_changed"),
        ("session-source-geometry-changed", "four-pages-one-confirmation", "/current_binding/source_geometry_id", "b" * 64, "session:binding_changed"),
        ("session-calibration-v2-rejected", "four-pages-one-confirmation", "/current_calibration/schema_version", 2, "schema:"),
        ("session-expired-page", "four-pages-one-confirmation", "/now", time(1000), "session:page_expired"),
        ("session-restart-no-clock-renewal", "four-pages-one-confirmation", "/server_instance_id", uid(901), "session:server_restarted"),
        ("session-cannot-publish-busy", "four-pages-one-confirmation", "/device_idle", False, "session:device_busy"),
        ("session-other-page-back-button", "four-pages-one-confirmation", "/accepted_items/0/page_state", states[1], "schema:"),
        ("capture-repeated-source-seq", "complete-capture", "/evidence/1/source_binding/frame_seq", 10, "capture:source_sequence"),
        ("capture-clock-forged", "complete-capture", "/evidence/1/captured_monotonic_ms", 11010, "capture:derived_clock"),
        ("capture-interval-too-short", "complete-capture", "/evidence/1/timing_proof/source_capture_uncertainty_ms", 1, "capture:interval"),
        ("capture-stale-frame", "complete-capture", "/evidence/0/received_monotonic_ms", 15000, "capture:stale_source"),
        ("page-edit-erases-other-page", "page-edit-preserves-other-pages", "/after/pages/1/capture", None, "edit:other_page_changed"),
        ("inherit-refreshes-time", "inherit-preserves-accepted-time", "/after/pages/0/capture/accepted_at", time(50), "inherit:refreshed_page"),
        ("package-add-permission", "closed-package-manifest", "/input_enabled", True, "schema:"),
        ("package-file-total-lie", "closed-package-manifest", "/file_count", 2, "package:file_count"),
        ("package-path-traversal", "closed-package-manifest", "/entries/0/path", "../current.json", "schema:"),
        ("operation-committed-no-result", "complete-operation", "/response", None, "operation:committed_fields"),
        ("issue-resolved-no-evidence", "complete-issue", "/state", "resolved", "issue:resolution"),
        ("operation-wrong-replay-body", "idempotent-replay-original-result", "/retry_request/body/publish", False, "replay:request_conflict"),
        ("operation-wrong-principal", "idempotent-replay-original-result", "/principal_id", "operator-02", "replay:identity"),
        ("crash-does-not-republish", "crash-files-only", "/result_phase", "committed", "crash:wrong_recovery_phase"),
        ("pagination-limit-zero", "http-template_list-request", "/query/limit", 0, "schema:"),
        ("pagination-wrong-principal", "pagination-snapshot-final-page", "/principal_id", "operator-02", "pagination:scope"),
        ("pagination-expired", "pagination-snapshot-final-page", "/now", time(960), "pagination:expired"),
        ("rollback-fake-geometry", "http-rollback_preview-request", "/body/capture_session_id", session_id, "rollback:session"),
        ("import-never-switches-pointer", "http-package_import-response", "/body/pointers_changed", True, "schema:"),
        ("http-error-status-mismatch", "http-region_save-error-layout_device_busy", "/status", 422, "http:error_status"),
        ("upload-digest-mismatch", "http-draft_asset_upload-request", "/body/asset/sha256", "0" * 64, "image:digest"),
        ("upload-no-privacy-confirmation", "http-draft_asset_upload-request", "/body/privacy_confirmed", False, "schema:"),
        ("download-bad-length", "http-template_asset-response", "/headers/Content-Length", 1, "http:content_length"),
    ):
        bad(name, base, path, replacement, error)
    # Archive attacks use real ZIP bytes, not claimed metadata-only failures.
    for name, changed_files, error in (
        ("zip-path-traversal", {**payloads, "../escape": b"x"}, "package:unsafe_path"),
        ("zip-unlisted-private-file", {**payloads, "templates/hidden/1/record.json": b"{}"}, "package:unlisted_file"),
        ("zip-corrupt-resource", {**payloads, next(path for path in payloads if path.endswith(".png")): b"corrupt"}, "package:entry_size"),
    ):
        data = zip_bytes(changed_files)
        base_name = "closed-package-upload"
        cases.append({"name": name, "base": base_name, "family": "package", "definition": "import_preview_request", "valid": False, "error": error,
                      "set": [{"path": "/archive_sha256", "value": hashlib.sha256(data).hexdigest()},
                              {"path": "/archive_base64", "value": base64.b64encode(data).decode()}]})
    return examples, cases
