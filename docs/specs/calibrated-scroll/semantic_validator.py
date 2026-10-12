"""Pure documentation invariants. Does not import Fleet or contact any service."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import unicodedata


BINDING_KEYS = (
    "device_id", "daemon_session", "camera_id", "camera_device_unique_id",
    "camera_binding_id", "source_geometry_id", "calibration_fingerprint",
    "frame_width", "frame_height",
)
METRICS = (
    "dx_px", "dy_px", "horizontal_path_px", "vertical_path_px",
    "reverse_path_px", "valid_points", "inlier_ratio", "mad_dy_px",
)
OWNERS = ("task_id", "action_instance_id", "device_id")
TERMINAL_STATES = {"completed", "review_required", "reviewed", "aborted_before_motion"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def same_owner(left, right):
    for key in OWNERS:
        require(left[key] == right[key], f"Owner mismatch: {key}")


def same_binding(left, right):
    for key in BINDING_KEYS:
        require(left[key] == right[key], f"Binding identity mismatch: {key}")


def close(actual, expected):
    require(actual is not None and math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9),
            f"Derived metric mismatch: {actual} != {expected}")


def check_roi(roi):
    require(roi["x"] + roi["width"] <= 1 and roi["y"] + roi["height"] <= 1,
            "Normalized ROI extends outside the screen")


def check_pair(pair):
    require(pair["pair_index"] == pair["from_sample_index"]
            and pair["to_sample_index"] == pair["from_sample_index"] + 1,
            "Pair is not adjacent")
    corners, points = pair["corners"], pair["valid_points"]
    require(points <= corners, "Valid points exceed corners")
    close(pair["inlier_ratio"], points / corners if corners else 0)
    if points == 0:
        require(all(pair[key] is None for key in
                    ("forward_backward_max_px", "dx_px", "dy_px", "mad_dy_px")),
                "No retained points cannot have measured metrics")


def check_frame(record):
    source, binding = record["source_frame"], record["binding"]
    mapping = {
        "device_id": "device_id", "camera_id": "camera_id",
        "device_unique_id": "camera_device_unique_id",
        "camera_binding_id": "camera_binding_id", "source_geometry_id": "source_geometry_id",
        "width": "frame_width", "height": "frame_height",
    }
    for source_key, binding_key in mapping.items():
        require(source[source_key] == binding[binding_key], f"Source binding mismatch: {source_key}")
    earliest = source["source_captured_monotonic_ns"] - source["source_capture_uncertainty_ms"] * 1_000_000
    require(earliest <= record["accepted_monotonic_ns"], "Accepted before earliest capture")
    age = (instant(record["freshness_checked_at"]) - instant(source["source_captured_at"])).total_seconds()
    require(-0.5 <= age <= 2, "Capture-time freshness check failed")
    received_age = (instant(source["source_received_at"]) - instant(source["source_captured_at"])).total_seconds()
    require(received_age >= -source["source_capture_uncertainty_ms"] / 1000,
            "Bridge received a frame before its capture interval")


def chain_metrics(pairs, height):
    return {
        "dx_px": sum(pair["dx_px"] for pair in pairs),
        "dy_px": sum(pair["dy_px"] for pair in pairs),
        "horizontal_path_px": sum(abs(pair["dx_px"]) for pair in pairs),
        "vertical_path_px": sum(abs(pair["dy_px"]) for pair in pairs),
        "reverse_path_px": min(
            sum(max(pair["dy_px"] - 0.002 * height, 0) for pair in pairs),
            sum(max(-pair["dy_px"] - 0.002 * height, 0) for pair in pairs)),
        "valid_points": min(pair["valid_points"] for pair in pairs),
        "inlier_ratio": min(pair["inlier_ratio"] for pair in pairs),
        "mad_dy_px": max(pair["mad_dy_px"] for pair in pairs),
    }


def stationary_pair(pair, width, height):
    return (pair["quality_ok"] and abs(pair["dx_px"]) <= 0.01 * width
            and abs(pair["dy_px"]) <= 0.01 * height and pair["mad_dy_px"] <= 0.01 * height)


def check_chain(record):
    require(record["binding"]["device_id"] == record["device_id"], "Chain device mismatch")
    ids, pairs = record["frame_evidence_ids"], record["pairs"]
    require(len(ids) == record["frame_count"], "Frame ID coverage mismatch")
    require(record["before_evidence_id"] == ids[0], "Wrong before frame")
    if record["after_evidence_id"] is not None:
        require(record["after_evidence_id"] == ids[-1] and len(ids) > 1, "Wrong after frame")
    require(len(pairs) <= len(ids) - 1, "Too many pairs")
    if record["sampling_status"] == "complete":
        require(len(pairs) == len(ids) - 1, "Incomplete pair coverage")
    else:
        require(record["effect_status"] == "unknown", "Incomplete sampling cannot confirm an effect")
    for index, pair in enumerate(pairs):
        check_pair(pair)
        require(pair["pair_index"] == index, "Non-contiguous pair chain")
    failed = next((i for i, pair in enumerate(pairs) if not pair["quality_ok"]), None)
    require(record["failure_pair_index"] == failed, "Wrong first failed pair")
    if not pairs or len(pairs) != len(ids) - 1 or failed is not None:
        require(record["effect_status"] == "unknown" and record["flow_status"] == "unknown",
                "Failed/missing pairs cannot confirm flow")
        require(all(record[key] is None for key in METRICS), "Partial totals must be null")
        require(record["direction_consistent"] is None, "Partial chain cannot establish direction")
        if failed is not None:
            require(record["reason_code"] == pairs[failed]["reason_code"], "Lost first pair failure")
        return
    width, height = record["roi_px"]["width"], record["roi_px"]["height"]
    metrics = chain_metrics(pairs, height)
    for key, expected in metrics.items():
        close(record[key], expected)
    for pair in pairs:
        require(abs(pair["dx_px"]) <= 0.03 * width and pair["mad_dy_px"] <= 0.02 * height
                and abs(pair["dy_px"]) <= 0.25 * height,
                "quality_ok pair exceeds ROI limits")
    reason = None
    flow = "unknown"
    if abs(metrics["dx_px"]) > 0.03 * width or metrics["horizontal_path_px"] > 0.03 * width:
        reason = "accumulated_horizontal_motion"
    elif metrics["reverse_path_px"] > 0.01 * height:
        reason = "bidirectional_motion"
    elif abs(metrics["dy_px"]) >= 0.04 * height:
        flow = "displaced"
    elif (metrics["horizontal_path_px"] <= 0.01 * width
          and metrics["vertical_path_px"] <= 0.01 * height
          and all(stationary_pair(pair, width, height) for pair in pairs)):
        flow = "stationary"
    else:
        reason = "between_thresholds"
    require(record["flow_status"] == flow, "Incorrect flow classification")
    if reason:
        require(record["effect_status"] == "unknown" and record["reason_code"] == reason
                and record["direction_consistent"] is None, "Wrong chain failure precedence")
        return
    tail = record["settled_tail_pairs"]
    require(tail <= len(pairs), "Impossible settled tail")
    if tail:
        require(all(stationary_pair(pair, width, height) for pair in pairs[-tail:]),
                "Claimed tail is moving")
    ocr = record["ocr_summary"]
    if ocr:
        require(ocr["before_evidence_id"] == ids[0] and ocr["after_evidence_id"] == ids[-1],
                "OCR belongs to different endpoints")
        require(ocr["matched_rows"] + ocr["contradictory_rows"] <= ocr["comparable_unique_rows"],
                "Impossible OCR counts")
        if ocr["status"] == "consistent":
            require(ocr["matched_rows"] >= 2 and ocr["contradictory_rows"] == 0,
                    "Insufficient consistent OCR")
        else:
            expected = {"invalid": "ocr_endpoint_invalid", "insufficient": "ocr_rows_insufficient",
                        "contradictory": "ocr_rows_not_consistent"}[ocr["status"]]
            require(record["effect_status"] == "unknown" and record["reason_code"] == expected,
                    "OCR failure cannot be ignored")
            return
    direction = None if flow == "stationary" else metrics["dy_px"] * (
        -1 if record["expected_direction"] == "swipe_up" else 1) > 0
    require(record["direction_consistent"] == direction, "Incorrect direction classification")
    if direction is False:
        require(record["effect_status"] == "unknown" and record["reason_code"] == "direction_mismatch",
                "Reverse motion reported as success")
    elif record["sampling_status"] == "complete":
        require(tail >= 2 and record["after_evidence_id"] is not None, "No settled completion")
        require(record["effect_status"] == flow and record["reason_code"] is None,
                "Incorrect complete effect classification")


def check_capability(record):
    actions = record["actions"]
    require(record["usable"] == any(item["usable"] for item in actions.values()),
            "Top-level usability is any supported direction")
    reasons = {item["code"] for action in actions.values() for item in action["blocking_reasons"]}
    require([item["code"] for item in record["blocking_reasons"]] == sorted(reasons),
            "Capability reasons must be the sorted de-duplicated action union")
    if record["binding"] is not None:
        require(record["binding"]["device_id"] == record["device_id"], "Capability device mismatch")
        require(record["calibration_schema_version"] == 3, "New binding requires a validated v3 calibration")
    for action in actions.values():
        codes = {item["code"] for item in action["blocking_reasons"]}
        if action["motion_budget"]["status"] != "within_window":
            require("scroll_observation_window_insufficient" in codes, "Unproven/long budget must block")
        if record["calibration_schema_version"] != 3:
            require(not action["usable"] and "calibration_v3_required" in codes, "v2 cannot be usable")
        if not record["sampling_support"]["bounded_capture"]:
            require(not action["usable"] and "capture_not_bounded" in codes, "Unbounded capture must block")
        if not record["sampling_support"]["source_timing_proven"]:
            require(not action["usable"] and "source_timing_unproven" in codes, "Unproven clock must block")


def check_result(record):
    require(record["device_id"] == record["binding"]["device_id"], "Result binding mismatch")
    require(record["action_display"] == {"swipe_up": "上滑", "swipe_down": "下滑"}[record["action"]],
            "Action display mismatch")
    refs = record["frame_chain_evidence"]
    ids = [ref["evidence_id"] for ref in refs]
    require(len(set(ids)) == len(ids), "Duplicate frame references")
    for ref in refs:
        same_owner(record, ref)
    if record["before_evidence"] is not None:
        require(bool(refs) and record["before_evidence"] == refs[0], "Before reference mismatch")
    else:
        require(not refs, "Frame chain without before reference")
    if record["after_evidence"] is not None:
        require(len(refs) > 1 and record["after_evidence"] == refs[-1], "After reference mismatch")
    if record["observation_roi"]:
        check_roi(record["observation_roi"])
    observation = record["observation"]
    if observation is None:
        require(record["effect_status"] in {"not_started", "unknown"}, "No observation cannot confirm effect")
        require(record["flow_status"] == record["effect_status"], "Unobserved flow mismatch")
        require(all(record[key] is None for key in
                    ("dx_px", "dy_px", "valid_points", "inlier_ratio", "direction_consistent")),
                "Missing observation cannot have metrics")
        require(record["after_evidence"] is None, "No verified after without observation")
    else:
        check_chain(observation)
        same_owner(record, observation)
        same_binding(record["binding"], observation["binding"])
        for key in ("planning_version", "observation_version", "config_sha256",
                    "flow_status", "effect_status", "direction_consistent", "dx_px", "dy_px",
                    "valid_points", "inlier_ratio"):
            require(record[key] == observation[key], f"Result/observation mismatch: {key}")
        require(record["action"] == observation["expected_direction"], "Wrong action direction")
        require(ids == observation["frame_evidence_ids"], "Result frame ordering mismatch")
        after_id = record["after_evidence"]["evidence_id"] if record["after_evidence"] else None
        require(after_id == observation["after_evidence_id"], "Result after mismatch")
    if record["status"] == "unknown":
        require(not (record["mechanical_status"] == "confirmed" and record["safe_cleanup_confirmed"]
                     and record["effect_status"] in {"displaced", "stationary"}),
                "Fully confirmed facts have a deterministic non-unknown outcome")
    if record["mechanical_status"] == "unknown":
        require(record["reason_code"] in {"mechanical_result_unknown", "interrupted_after_claim",
                                         "storage_unavailable", "internal_error"},
                "Mechanical uncertainty must not be hidden behind visual failure")
    elif record["mechanical_status"] == "confirmed" and not record["safe_cleanup_confirmed"]:
        require(record["status"] == "unknown" and record["reason_code"] == "safe_cleanup_unconfirmed",
                "Unsafe cleanup must retain mechanical protection")
    elif record["status"] == "unknown" and observation:
        require(record["reason_code"] == observation["reason_code"], "Visual reason mismatch")


def check_timeline(timeline):
    order = ["before_accepted_ns", "dispatch_claimed_ns", "driver_invoked_ns"]
    known = [timeline[key] for key in order if timeline[key] is not None]
    require(known == sorted(known), "Dispatch time order invalid")
    if any(timeline[key] is not None for key in order + [
            "mechanical_completed_ns", "safe_cleanup_confirmed_ns", "sampling_finished_ns"]):
        require(timeline["source_clock_id"] is not None, "Missing comparable clock")
    completed, cleanup = timeline["mechanical_completed_ns"], timeline["safe_cleanup_confirmed_ns"]
    fence = max(completed, cleanup) if completed is not None and cleanup is not None else None
    require(timeline["completion_fence_ns"] == fence, "Completion fence mismatch")
    if timeline["driver_invoked_ns"] is not None:
        for value in (completed, cleanup):
            require(value is None or value >= timeline["driver_invoked_ns"], "Completion predates invocation")


def check_plan(plan):
    require(plan["device_id"] == plan["binding"]["device_id"], "Plan binding mismatch")
    start, end = ({"x": 0.5, "y": 0.75}, {"x": 0.5, "y": 0.3})
    if plan["action"] == "swipe_down":
        start, end = end, start
    require(plan["start"] == start and plan["end"] == end, "Plan changed the default stroke")
    budget = plan["motion_budget"]
    require(budget["total_ms"] == sum(budget[key] for key in (
        "hover_move_ms", "press_ms", "stroke_ms", "lift_ms",
        "driver_verification_ms", "safe_cleanup_ms", "margin_ms")), "Budget total mismatch")
    require(budget["stroke_ms"] == plan["duration_ms"], "Stroke duration mismatch")
    for point in (plan["camera_start"], plan["camera_end"]):
        require(point["frameW"] == plan["binding"]["frame_width"]
                and point["frameH"] == plan["binding"]["frame_height"]
                and point["cameraX"] < point["frameW"] and point["cameraY"] < point["frameH"],
                "Plan camera coordinate mismatch")
    same_owner(plan, plan["calibration_evidence"])
    same_owner(plan, budget["basis_evidence"])


def check_review_receipt(receipt):
    request, response = receipt["request"], receipt["response"]
    require(receipt["request_sha256"] == digest({
        "device_id": receipt["device_id"], "issue_id": receipt["issue_id"], "request": request}),
        "Review digest does not cover path and full request")
    require(receipt["old_revision"] == request["expected_revision"]
            and receipt["new_revision"] == receipt["old_revision"] + 1
            and response["revision"] == receipt["new_revision"], "Review CAS mismatch")
    for key in ("device_id", "issue_id", "request_id"):
        require(response[key] == receipt[key], f"Review response mismatch: {key}")
    for key in ("task_id", "action_instance_id", "request_id"):
        require(response[key] == request[key], f"Review request mismatch: {key}")
    require(receipt["issue_id"] not in response["remaining_visual_issue_ids"],
            "Resolved issue still listed as unresolved")


def check_action_record(record):
    require(record["binding"]["device_id"] == record["device_id"], "Action binding mismatch")
    check_timeline(record["timeline"])
    for key in ("plan_evidence", "config_evidence", "clock_registration_evidence", "ocr_evidence",
                "driver_receipt_evidence"):
        if record[key]:
            same_owner(record, record[key])
    if record["state"] == "aborted_before_motion":
        require(record["claim_id"] is None and record["claimed_at"] is None
                and record["driver_invoked_at"] is None, "Claimed action cannot become an unclaimed abort")
    if record["state"] in {"prepared", "aborted_before_motion"}:
        require(record["timeline"]["dispatch_claimed_ns"] is None
                and record["timeline"]["driver_invoked_ns"] is None, "Unclaimed action has dispatch times")
    else:
        require(record["timeline"]["dispatch_claimed_ns"] is not None, "Missing claim timestamp")
    if record["state"] == "completed":
        require(record["task_outcome"] in {"confirmed", "failed"}
                and record["mechanical_status"] == "confirmed"
                and record["safe_cleanup_confirmed"], "Completed action is unsafe")
    require(instant(record["created_at"]) <= instant(record["updated_at"]), "Record time regressed")
    if record["observation"]:
        check_chain(record["observation"])
        require(record["sample_evidence_ids"] == record["observation"]["frame_evidence_ids"],
                "Persistent frame IDs mismatch")
    result = record["result"]
    if result:
        check_result(result)
        same_owner(record, result)
        require(record["binding"] == result["binding"], "Historical result binding was changed")
        for key in ("action", "planning_version", "observation_version", "config_sha256",
                    "motion_started", "mechanical_status", "safe_cleanup_confirmed",
                    "reason_code", "pause_issue_id", "observation"):
            require(record[key] == result[key], f"Record/result mismatch: {key}")
        require(record["task_outcome"] == result["status"], "Record outcome mismatch")
        require(record["sample_evidence_ids"] == [ref["evidence_id"] for ref in result["frame_chain_evidence"]],
                "Record/result sample mismatch")
    if record["review"]:
        check_review_receipt(record["review"])
        require(record["revision"] >= record["review"]["new_revision"], "Review is newer than action")
        require(record["pause_issue_id"] == record["review"]["issue_id"], "Wrong reviewed issue")
        same_owner(record, record["review"]["response"])
    if record["observation_context"]:
        context = record["observation_context"]
        for roi in ([context["list_roi"]] if context["list_roi"] else []) + context["exclusion_regions"]:
            check_roi(roi)
        if context["ocr_mode"] == "none":
            require(record["ocr_evidence"] is None, "Unrequested OCR was attached")
    ticks = [item["tick"] for item in record["sampling_diagnostics"]]
    require(ticks == sorted(set(ticks)), "Sampling ticks are repeated or unordered")
    for item in record["sampling_diagnostics"]:
        start = record["timeline"]["before_accepted_ns"]
        require(start is not None and item["scheduled_ns"] == start + item["tick"] * 200_000_000,
                "Sampling schedule drifted")
        if item["status"] == "accepted":
            require(item["evidence_id"] in record["sample_evidence_ids"] and item["reason_code"] is None,
                    "Accepted tick does not reference the action chain")
        else:
            require(item["evidence_id"] is None and item["reason_code"] is not None,
                    "Failed/duplicate tick became a frame")
        if item["status"] == "skipped":
            require(item["requested_ns"] is None and item["finished_ns"] is None, "Skipped tick performed a read")
        elif item["requested_ns"] is not None and item["finished_ns"] is not None:
            require(0 <= item["finished_ns"] - item["requested_ns"] <= 400_000_000, "Unbounded physical read")


def check_ocr(endpoint, chain, size):
    if endpoint is None:
        require(chain["ocr_summary"] is None, "OCR summary without OCR evidence")
        return
    require(endpoint["before_evidence_id"] == chain["before_evidence_id"]
            and endpoint["after_evidence_id"] == chain["after_evidence_id"], "OCR endpoints mismatch")
    indexes = []
    for rows in (endpoint["before_rows"], endpoint["after_rows"]):
        index = {}
        for row in rows:
            require(row["center_x"] < size["width"] and row["center_y"] < size["height"],
                    "OCR coordinate outside upright screen")
            text = unicodedata.normalize("NFC", row["text"]).strip()
            if text:
                index.setdefault(text, []).append(row)
        indexes.append(index)
    common = [text for text in indexes[0] if len(indexes[0][text]) == 1
              and len(indexes[1].get(text, [])) == 1]
    width, height = chain["roi_px"]["width"], chain["roi_px"]["height"]
    bad = 0
    for text in common:
        before, after = indexes[0][text][0], indexes[1][text][0]
        dx, dy = after["center_x"] - before["center_x"], after["center_y"] - before["center_y"]
        mismatch = abs(dx - chain["dx_px"]) > 0.03 * width or abs(dy - chain["dy_px"]) > 0.04 * height
        if chain["flow_status"] == "stationary":
            mismatch = mismatch or abs(dx) > 0.01 * width or abs(dy) > 0.01 * height
        elif abs(dy) > 0.01 * height:
            mismatch = mismatch or dy * chain["dy_px"] < 0
        bad += bool(mismatch)
    summary = chain["ocr_summary"]
    require(summary is not None, "Supplied OCR was ignored")
    require(summary["comparable_unique_rows"] == len(common) and summary["contradictory_rows"] == bad
            and summary["matched_rows"] == len(common) - bad, "OCR counts mismatch")
    status = "contradictory" if bad else "insufficient" if len(common) < 2 else "consistent"
    require(summary["status"] == status, "OCR classification mismatch")


def check_bundle(bundle):
    record, plan, config, frames = (bundle[key] for key in ("record", "plan", "config", "frames"))
    check_action_record(record)
    check_plan(plan)
    require(config["config_sha256"] == digest(config["parameters"]), "Configuration digest mismatch")
    for item in (plan, config):
        same_owner(record, item)
        require(item["config_sha256"] == record["config_sha256"], "Mixed configuration snapshots")
    same_binding(record["binding"], plan["binding"])
    same_binding(record["binding"], bundle["current_binding"])
    require(record["plan_evidence"]["sha256"] == digest(plan), "Plan file digest mismatch")
    require(record["config_evidence"]["sha256"] == digest(config), "Config file digest mismatch")
    require(record["clock_registration_evidence"]["evidence_id"] == bundle["clock_registration_id"],
            "Different clock registration proof")
    ids = [frame["evidence_id"] for frame in frames]
    require(ids == record["sample_evidence_ids"], "Evidence bundle is incomplete or reordered")
    timeline = record["timeline"]
    sources = []
    for index, frame in enumerate(frames):
        check_frame(frame)
        require(frame["sample_index"] == index, "Frame indices are not contiguous")
        require(frame["task_id"] == record["task_id"] and frame["action_instance_id"] == record["action_instance_id"],
                "Evidence belongs to another action")
        same_binding(record["binding"], frame["binding"])
        for key in ("planning_version", "observation_version", "config_sha256"):
            require(frame[key] == record[key], f"Frame snapshot mismatch: {key}")
        source = frame["source_frame"]
        require(source["source_clock_id"] == timeline["source_clock_id"], "Frame clock changed")
        if sources:
            previous = sources[-1]
            require(source["source_generation_id"] == previous["source_generation_id"], "Capture generation changed")
            for key in ("source_frame_seq", "source_capture_seq", "source_captured_monotonic_ns"):
                require(source[key] > previous[key], f"Source sequence/time did not advance: {key}")
            gap = (source["source_captured_monotonic_ns"] - previous["source_captured_monotonic_ns"]
                   + (source["source_capture_uncertainty_ms"] + previous["source_capture_uncertainty_ms"]) * 1_000_000)
            require(gap <= 400_000_000, "Capture interval including uncertainty exceeds 400ms")
            require(0 <= frame["accepted_monotonic_ns"] - frames[index - 1]["accepted_monotonic_ns"] <= 400_000_000,
                    "Local receive interval exceeds 400ms")
            require(0 < (instant(source["source_received_at"]) - instant(previous["source_received_at"])).total_seconds()
                    <= 0.4, "Bridge receive interval invalid")
        sources.append(source)
    if frames:
        require(frames[0]["phase"] == "before" and timeline["before_accepted_ns"] == frames[0]["accepted_monotonic_ns"],
                "Missing before phase or wrong t0")
        before = sources[0]
        earliest = before["source_captured_monotonic_ns"] - before["source_capture_uncertainty_ms"] * 1_000_000
        latest = before["source_captured_monotonic_ns"] + before["source_capture_uncertainty_ms"] * 1_000_000
        if timeline["dispatch_claimed_ns"] is not None:
            require(0 <= timeline["dispatch_claimed_ns"] - earliest <= 400_000_000, "Before too old at claim")
        if timeline["driver_invoked_ns"] is not None:
            require(latest <= timeline["driver_invoked_ns"], "Before could have been captured after invocation")
        require(frames[-1]["accepted_monotonic_ns"] - frames[0]["accepted_monotonic_ns"] <= 5_800_000_000,
                "Observation exceeded total window")
    chain = record["observation"]
    if chain:
        require(chain["source_clock_id"] == timeline["source_clock_id"], "Chain clock mismatch")
        roi, size = chain["roi_px"], plan["screen_size"]
        require(roi["x"] + roi["width"] <= size["width"] and roi["y"] + roi["height"] <= size["height"],
                "Pixel ROI exceeds upright screen")
        norm = record["result"]["observation_roi"]
        require(norm is not None, "Measured chain lacks normalized ROI")
        require(roi == {"x": math.floor(norm["x"] * size["width"]),
                        "y": math.floor(norm["y"] * size["height"]),
                        "width": math.ceil((norm["x"] + norm["width"]) * size["width"]) - math.floor(norm["x"] * size["width"]),
                        "height": math.ceil((norm["y"] + norm["height"]) * size["height"]) - math.floor(norm["y"] * size["height"])},
                "ROI pixel conversion mismatch")
        after = chain["after_evidence_id"]
        require([frame["phase"] for frame in frames] ==
                ["before"] + ["during"] * (len(frames) - 1 - int(after is not None))
                + (["after"] if after is not None else []), "Frame phases disagree with after")
        if after:
            fence = timeline["completion_fence_ns"]
            require(fence is not None, "Cannot prove after without completion fence")
            tail = chain["settled_tail_pairs"]
            for source in sources[-(tail + 1):]:
                require(source["source_captured_monotonic_ns"]
                        - source["source_capture_uncertainty_ms"] * 1_000_000 > fence,
                        "Tail frame may predate completion")
            require(frames[-1]["accepted_monotonic_ns"] - timeline["mechanical_completed_ns"] <= 1_000_000_000,
                    "After wait exceeded 1000ms")
        if chain["flow_status"] != "unknown":
            check_ocr(bundle["ocr_endpoint"], chain, plan["screen_size"])
    if record["result"]:
        refs = record["result"]["frame_chain_evidence"]
        for ref, frame in zip(refs, frames):
            require(ref["sha256"] == frame["sha256"]
                    and ref["captured_at"] == frame["source_frame"]["source_captured_at"],
                    "Frame reference digest/capture time mismatch")


def check_transition(before, after, event):
    check_action_record(before)
    check_action_record(after)
    require(after["revision"] == before["revision"] + 1, "CAS revision must advance exactly once")
    for key in ("record_version", *OWNERS, "origin", "step_key", "request_sha256",
                "action", "binding", "planning_version", "observation_version", "config_sha256",
                "plan_evidence", "config_evidence", "clock_registration_evidence", "created_at", "lease_id",
                "observation_context"):
        require(before[key] == after[key], f"Immutable action field changed: {key}")
    allowed = {
        "claim": {("prepared", "claimed")},
        "progress": {("claimed", "claimed")},
        "finalize": {("claimed", "completed"), ("claimed", "review_required")},
        "abort": {("prepared", "aborted_before_motion")},
        "recover": {("prepared", "aborted_before_motion"), ("claimed", "review_required")},
        "review": {("review_required", "reviewed")},
        "resume": {("reviewed", "reviewed")},
    }
    require((before["state"], after["state"]) in allowed.get(event, set()), "Forbidden transition/replay")
    if before["claim_id"] is not None:
        require(after["claim_id"] == before["claim_id"] and after["claimed_at"] == before["claimed_at"],
                "Claim changed")
    if before["state"] in TERMINAL_STATES:
        mutable = {"state", "revision", "updated_at", "review", "manual_resume_required"}
        for key in before.keys() - mutable:
            require(before[key] == after[key], f"Historical terminal field changed: {key}")
    if event == "review":
        require(after["manual_resume_required"] and after["review"]["old_revision"] == before["revision"]
                and after["review"]["new_revision"] == after["revision"], "Review resumed automatically")
    if event == "resume":
        require(before["manual_resume_required"] and not after["manual_resume_required"]
                and before["review"] == after["review"], "Resume changed history or repeated")


def check_semantics(record, kind=None):
    kind = kind or record.get("record_type")
    handlers = {
        "pair": check_pair, "frame": check_frame, "chain": check_chain,
        "capability": check_capability, "result": check_result,
        "plan_snapshot": check_plan, "action_record": check_action_record,
        "review_receipt": check_review_receipt, "evidence_bundle": check_bundle,
    }
    if kind in handlers:
        handlers[kind](record)
    elif kind == "task_request":
        require(record["device_id"] == record["payload"]["binding"]["device_id"], "Task binding mismatch")
    elif kind == "config_snapshot":
        require(record["config_sha256"] == digest(record["parameters"]), "Config digest mismatch")
    elif kind in {"action_row", "review_row"}:
        check_semantics(record["json"], "action_record" if kind == "action_row" else "review_receipt")
        for key in record.keys() - {"json"}:
            require(record[key] == record["json"][key], f"Column/JSON mismatch: {key}")
    elif kind == "task_view":
        result = record["result"]
        if result:
            check_result(result)
            same_owner(record, result)
            require(record["status"] == result["status"], "Task/result status mismatch")
        active = record["status"] in {"queued", "validating", "awaiting_device", "executing"}
        require((record["finished_at"] is None) == active, "Task finish timestamp mismatch")
        if active:
            require(result is None, "Active task has terminal result")
        elif record["status"] in {"confirmed", "failed", "unknown"}:
            require(result is not None, "Terminal task lacks result")
    elif kind == "review_response":
        require(record["issue_id"] not in record["remaining_visual_issue_ids"], "Resolved issue still pending")
    elif kind == "review_list":
        ids = [issue["issue_id"] for issue in record["issues"]]
        require(len(ids) == len(set(ids)), "Duplicate review issue")
        for issue in record["issues"]:
            result = issue["original_result"]
            check_result(result)
            require(result["device_id"] == record["device_id"] and result["pause_issue_id"] == issue["issue_id"],
                    "Review issue belongs to another device/action")
            for key in ("task_id", "action_instance_id", "binding", "frame_chain_evidence"):
                require(issue[key] == result[key], f"Review projection mismatch: {key}")
            require(issue["mechanical_review_required"] ==
                    (result["mechanical_status"] == "unknown" or not result["safe_cleanup_confirmed"]),
                    "Mechanical protection projection mismatch")
            if issue["state"] == "review_required":
                require(issue["review"] is None and issue["manual_resume_required"], "Unreviewed issue was resumed")
            else:
                require(issue["review"] is not None, "Reviewed issue lacks receipt")
                response = issue["review"]
                require(response["device_id"] == record["device_id"] and response["issue_id"] == issue["issue_id"],
                        "Review response owner mismatch")
    elif kind == "observation_context":
        for roi in ([record["list_roi"]] if record["list_roi"] else []) + record["exclusion_regions"]:
            check_roi(roi)
    elif kind == "internal_call" and record["observation_context"]:
        check_semantics(record["observation_context"], "observation_context")
    elif kind == "driver_receipt":
        check_timeline(record["timeline"])
        if record["mechanical_confirmed"]:
            require(record["motion_verified"] is True and record["swipe_executed"] is True
                    and record["stopped"] is False and record["cancelled"] is False,
                    "Controller receipt alone cannot prove mechanical completion")
