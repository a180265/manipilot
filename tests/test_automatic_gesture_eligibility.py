"""自动导航与单动作资格共用实时校验，所有档案限定在临时目录。"""
from copy import deepcopy
import json
from pathlib import Path

import jsonschema
import pytest

from drivers import gesture_program as gp
from drivers import screen_gestures as sg
from runtime import automatic_gestures as ag
from runtime.errors import ApiError
from tests.test_automatic_gesture_api import (
    _program_fixture, _run_fixture, _seed_calibration, _template_fixture,
)


@pytest.fixture
def eligible(tmp_path):
    bundle = _seed_calibration(tmp_path)
    root = tmp_path / "data"
    frame = gp.load_coordinate_frame("arm-01", root)
    run = _run_fixture(bundle, root)
    ag.publish_run_results("arm-01", run, data_root=root)
    return bundle, frame, root, run


def test_navigation_reuses_live_action_eligibility_after_frame_change(eligible):
    bundle, frame, root, _ = eligible
    changed = deepcopy(frame)
    changed["density"] += 0.1
    changed["coordinate_frame_hash"] = gp.coordinate_frame_hash(changed)
    gesture = ag.gestures_view("arm-01", bundle=bundle, frame=changed,
                               data_root=root)["actions"]["scroll_up"]
    navigation = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                                    bundle=bundle, frame=changed, data_root=root)
    assert gesture["usable"] is False
    action = navigation["actions"]["search_next"]
    assert action["usable"] is False
    assert gesture["blocking_reasons"]
    assert all(reason in action["blocking_reasons"]
               for reason in gesture["blocking_reasons"])


def _edit_profile(root, edit):
    path = ag.profile_path("arm-01", root)
    profile = json.loads(path.read_text())
    edit(profile)
    path.write_text(json.dumps(profile), encoding="utf-8")


@pytest.mark.parametrize("field", sg.BINDING_KEYS)
def test_each_certificate_binding_field_must_match_current_snapshot(eligible, field):
    bundle, frame, root, _ = eligible
    _edit_profile(root, lambda p: p["actions"]["scroll_up"]["binding"].update(
        {field: "stale-value"}))
    action = ag.gestures_view("arm-01", bundle=bundle, frame=frame,
                              data_root=root)["actions"]["scroll_up"]
    assert action["usable"] is False
    assert any(r["code"] == "gesture_binding_changed"
               for r in action["blocking_reasons"])


@pytest.mark.parametrize("patch,reason", [
    ({"binding": None}, "gesture_binding_changed"),
    ({"recipe_version": "old-rules"}, "gesture_rules_changed"),
    ({"program": None}, "gesture_program_missing"),
    ({"program": {}}, "invalid_program"),
    ({"validation_status": "not_tested"}, "gesture_not_validated"),
    ({"state": "unverified"}, "gesture_not_validated"),
    ({"state": "invalid"}, "gesture_not_validated"),
    ({"state": "unsupported"}, "gesture_not_validated"),
    ({"version": 7}, "gesture_dependency_changed"),
])
def test_incomplete_or_stale_certificate_is_not_eligible(eligible, patch, reason):
    bundle, frame, root, _ = eligible
    _edit_profile(root, lambda p: p["actions"]["scroll_up"].update(patch))
    gesture = ag.gestures_view("arm-01", bundle=bundle, frame=frame,
                               data_root=root)["actions"]["scroll_up"]
    navigation = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                                    bundle=bundle, frame=frame, data_root=root)
    assert gesture["usable"] is False
    assert reason in {r["code"] for r in gesture["blocking_reasons"]}
    assert navigation["actions"]["search_next"]["usable"] is False
    if patch.get("validation_status") == "not_tested":
        assert gesture["validation_status"] == "not_tested"
        assert not gesture["contact_validated"] and not gesture["effect_validated"]


def test_program_changes_invalidate_published_certificate(eligible):
    bundle, frame, root, _ = eligible
    _edit_profile(root, lambda p: p["actions"]["scroll_up"]["program"][
        "strokes"][0].update(hold_after_ms=100))
    gesture = ag.gestures_view("arm-01", bundle=bundle, frame=frame,
                               data_root=root)["actions"]["scroll_up"]
    assert not gesture["usable"]
    assert "gesture_program_changed" in {
        r["code"] for r in gesture["blocking_reasons"]}


def test_current_rules_change_invalidates_certificates(eligible):
    bundle, frame, root, _ = eligible
    path = ag.settings_path("arm-01", root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"gesture_rules_version": "gesture-rules.v2"}),
                    encoding="utf-8")
    navigation = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                                    bundle=bundle, frame=frame, data_root=root)
    action = navigation["actions"]["search_next"]
    assert action["usable"] is False
    assert "gesture_rules_changed" in {
        r["code"] for r in action["blocking_reasons"]}


def test_missing_action_has_explicit_certificate_reason(eligible):
    bundle, frame, root, _ = eligible
    navigation = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                                    bundle=bundle, frame=frame, data_root=root)
    action = navigation["actions"]["back_to_list"]
    assert action["usable"] is False
    assert any("证书" in r["message"] for r in action["blocking_reasons"])


def test_republication_refreshes_navigation_version_and_dependencies(eligible):
    bundle, frame, root, run = eligible
    next_run = deepcopy(run)
    next_run["action_results"][0]["gesture_id"] = "scroll_down"
    points = next_run["action_results"][0]["program"]["strokes"][0]["points"]
    points[0]["y"], points[1]["y"] = 0.6, 0.8
    published = ag.publish_run_results("arm-01", next_run, data_root=root,
                                       base_version=1)
    nav = published["navigation_profiles"][ag.NAVIGATION_PROFILE_ID]
    assert nav["version"] == 2
    assert nav["directions"] == {"scroll_up": 1, "scroll_down": 2}
    assert nav["dependency_versions"] == {
        "scroll_up": 1, "scroll_down": 2, "system_back_left": None,
        "system_back_right": None, "system_home": None}
    assert published["actions"]["scroll_up"]["version"] == 1
    view = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                              bundle=bundle, frame=frame, data_root=root)
    assert view["version"] == 2
    assert view["actions"]["search_next"]["usable"] is True
    assert view["actions"]["search_previous"]["usable"] is True


@pytest.mark.parametrize("field", ["directions", "dependency_versions"])
def test_navigation_rejects_stale_action_dependency(eligible, field):
    bundle, frame, root, _ = eligible
    _edit_profile(root, lambda p: p["navigation_profiles"][
        ag.NAVIGATION_PROFILE_ID].update({field: {"scroll_up": 42}}))
    gestures = ag.gestures_view("arm-01", bundle=bundle, frame=frame, data_root=root)
    assert gestures["actions"]["scroll_up"]["usable"] is True
    navigation = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                                    bundle=bundle, frame=frame, data_root=root)
    action = navigation["actions"]["search_next"]
    assert action["usable"] is False
    assert "navigation_dependency_changed" in {
        r["code"] for r in action["blocking_reasons"]}


def _request(eligible):
    bundle, frame, root, _ = eligible
    view = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                              bundle=bundle, frame=frame, data_root=root)
    return ({k: view[k] for k in ("profile_id", "version", "contract_version")},
            view["binding"])


def _resolve(eligible, action="search_next", **overrides):
    bundle, frame, root, _ = eligible
    ref, binding = _request(eligible)
    return ag.resolve_navigation_action(
        "arm-01", overrides.get("profile_ref", ref),
        overrides.get("binding", binding), action,
        bundle=bundle, frame=frame, data_root=root, evidence_root=root.parent / "evidence")


def test_resolver_returns_detached_program_and_view_without_writes(eligible):
    bundle, frame, root, run = eligible
    before = {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in root.rglob("*") if p.is_file()}
    resolved = _resolve(eligible)
    assert set(resolved) == {"gesture_id", "program", "binding", "view"}
    assert resolved["gesture_id"] == "scroll_up"
    assert resolved["program"] == run["action_results"][0]["program"]
    assert resolved["view"] == ag.navigation_view(
        "arm-01", ag.NAVIGATION_PROFILE_ID, bundle=bundle, frame=frame, data_root=root)
    assert resolved["binding"] == resolved["view"]["binding"]
    resolved["program"]["strokes"][0]["points"][0]["x"] = 0.2
    resolved["binding"]["device_id"] = "arm-02"
    again = _resolve(eligible)
    assert again["program"] == run["action_results"][0]["program"]
    assert again["binding"]["device_id"] == "arm-01"
    assert before == {p.relative_to(root): (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("reference", [None, {},
    {"profile_id": "absent", "version": 1, "contract_version": ag.CONTRACT_VERSION},
    {"profile_id": ag.NAVIGATION_PROFILE_ID, "version": 2,
     "contract_version": ag.CONTRACT_VERSION},
    {"profile_id": ag.NAVIGATION_PROFILE_ID, "version": True,
     "contract_version": ag.CONTRACT_VERSION},
    {"profile_id": ag.NAVIGATION_PROFILE_ID, "version": 1,
     "contract_version": "gesture-profile.v1"},
    {"profile_id": ag.NAVIGATION_PROFILE_ID, "version": 1,
     "contract_version": ag.CONTRACT_VERSION, "extra": "not allowed"},
])
def test_resolver_rejects_invalid_navigation_reference(eligible, reference):
    with pytest.raises(ApiError) as caught:
        _resolve(eligible, profile_ref=reference)
    assert caught.value.key == "device_not_eligible"
    assert "档案" in caught.value.detail


@pytest.mark.parametrize("field", (*sg.BINDING_KEYS, "coordinate_frame_hash",
                                   "gesture_profile_version", "recipe_version"))
@pytest.mark.parametrize("mode", ["missing", "changed"])
def test_resolver_requires_every_current_binding_field(eligible, field, mode):
    _, binding = _request(eligible)
    if mode == "missing":
        del binding[field]
    else:
        binding[field] = "stale-value"
    with pytest.raises(ApiError) as caught:
        _resolve(eligible, binding=binding)
    assert caught.value.key == "device_not_eligible"
    assert "绑定" in caught.value.detail


def test_resolver_rejects_new_program_with_old_reference(eligible):
    _, _, root, run = eligible
    ref, binding = _request(eligible)
    updated = deepcopy(run)
    updated["action_results"][0]["program"]["strokes"][0]["hold_after_ms"] = 100
    ag.publish_run_results("arm-01", updated, data_root=root, base_version=1)
    with pytest.raises(ApiError) as caught:
        _resolve(eligible, profile_ref=ref, binding=binding)
    assert caught.value.key == "device_not_eligible"
    assert "版本" in caught.value.detail
    assert _resolve(eligible)["program"]["strokes"][0]["hold_after_ms"] == 100


def test_resolver_propagates_missing_certificate_reason(eligible):
    with pytest.raises(ApiError) as caught:
        _resolve(eligible, "back_to_list")
    assert caught.value.key == "device_not_eligible"
    assert "证书" in caught.value.detail


@pytest.fixture
def all_eligible(eligible):
    bundle, frame, root, original = eligible
    run = deepcopy(original)
    results = []
    for gesture_id in ("scroll_up", "scroll_down", "system_back_left",
                       "system_back_right", "system_home"):
        result = deepcopy(run["action_results"][0])
        result.update(gesture_id=gesture_id, program=_program_fixture(gesture_id))
        results.append(result)
    run["action_results"] = results
    ag.publish_run_results("arm-01", run, data_root=root, base_version=1)
    template_dir = root / "devices" / "arm-01" / "navigation_templates"
    template_dir.mkdir()
    for kind in ("top", "launcher"):
        (template_dir / f"{kind}.json").write_text(json.dumps(
            _template_fixture(run["input_binding"], kind, evidence_root=root.parent / "evidence")),
            encoding="utf-8")
    return bundle, frame, root, run


@pytest.mark.parametrize("action,gesture_id", [
    ("search_next", "scroll_up"), ("search_previous", "scroll_down"),
    ("return_top", "scroll_down"), ("back_to_list", "system_back_left"),
    ("home", "system_home"),
])
def test_resolver_selects_exact_validated_program(all_eligible, action, gesture_id):
    resolved = _resolve(all_eligible, action)
    assert resolved["gesture_id"] == gesture_id
    assert resolved["program"] == _program_fixture(gesture_id)
    assert resolved["view"]["actions"][action]["usable"] is True
    assert resolved["view"]["actions"][action]["blocking_reasons"] == []


def test_resolver_selects_usable_right_back_when_left_certificate_is_invalid(all_eligible):
    _, _, root, _ = all_eligible
    _edit_profile(root, lambda p: p["actions"]["system_back_left"].update(state="invalid"))
    resolved = _resolve(all_eligible, "back_to_list")
    assert resolved["gesture_id"] == "system_back_right"
    assert resolved["program"] == _program_fixture("system_back_right")
    assert resolved["view"]["actions"]["back_to_list"]["blocking_reasons"] == []


@pytest.mark.parametrize("kind,action", [("top", "return_top"), ("launcher", "home")])
@pytest.mark.parametrize("fault", ["missing", "binding", "stale", "device", "unconfirmed",
                                    "contract", "anchors", "malformed"])
def test_templates_must_be_current_and_registered(all_eligible, kind, action, fault):
    bundle, frame, root, _ = all_eligible
    path = root / "devices" / "arm-01" / "navigation_templates" / f"{kind}.json"
    template = json.loads(path.read_text())
    if fault == "missing":
        path.unlink()
    elif fault == "malformed":
        path.write_text("[]", encoding="utf-8")
    else:
        if fault == "binding":
            template["binding"] = None
        elif fault == "stale":
            template["binding"]["daemon_session"] = "previous-session"
        elif fault == "device":
            template["device_id"] = "arm-02"
        elif fault == "unconfirmed":
            template["confirmed_at"] = None
        elif fault == "contract":
            template["contract_version"] = "navigation-template.v0"
        elif fault == "anchors":
            template["anchor_evidence"] = []
        path.write_text(json.dumps(template), encoding="utf-8")
    navigation = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                                    bundle=bundle, frame=frame, data_root=root,
                                    evidence_root=root.parent / "evidence")
    blocked = navigation["actions"][action]
    assert blocked["usable"] is False
    assert any("模板" in r["message"] for r in blocked["blocking_reasons"])
    if kind == "top":
        assert blocked["top_proof_available"] is False
    assert navigation["actions"]["search_previous"]["usable"] is True
    other = "home" if kind == "top" else "return_top"
    assert navigation["actions"][other]["usable"] is True
    with pytest.raises(ApiError) as caught:
        _resolve(all_eligible, action)
    assert caught.value.key == "device_not_eligible"
    assert "模板" in caught.value.detail


@pytest.mark.parametrize("source", ["validated_program", "plan"])
def test_publisher_accepts_existing_program_snapshots_without_recreating_them(eligible, source):
    _, _, root, run = eligible
    compatible = deepcopy(run)
    result = compatible["action_results"][0]
    program = result.pop("program")
    result[source] = (program if source == "validated_program" else {
        "program": program, "program_hash": gp.program_hash(program)})
    ag.publish_run_results("arm-01", compatible, data_root=root, base_version=1)
    assert _resolve(eligible)["program"] == program
    result["validation_status"] = "not_tested"
    ag.publish_run_results("arm-01", compatible, data_root=root, base_version=2)
    with pytest.raises(ApiError) as caught:
        _resolve(eligible)
    assert caught.value.key == "device_not_eligible"
    assert "验收" in caught.value.detail


@pytest.mark.parametrize("field", ["version", "actions", "navigation_profiles", "published"])
def test_resolver_reports_corrupt_profile_as_ineligible(eligible, field):
    bundle, frame, root, _ = eligible
    ref, binding = _request(eligible)
    _edit_profile(root, lambda p: p.update({field: "not-an-object-or-version"}))
    with pytest.raises(ApiError) as caught:
        ag.resolve_navigation_action("arm-01", ref, binding, "search_next",
                                     bundle=bundle, frame=frame, data_root=root)
    assert caught.value.key == "device_not_eligible"
    assert caught.value.detail


@pytest.mark.parametrize("field", ["dependency_versions", "binding", "program"])
def test_resolver_reports_corrupt_certificate_without_crashing(eligible, field):
    bundle, frame, root, _ = eligible
    ref, binding = _request(eligible)
    _edit_profile(root, lambda p: p["actions"]["scroll_up"].update({field: "corrupt"}))
    with pytest.raises(ApiError) as caught:
        ag.resolve_navigation_action("arm-01", ref, binding, "search_next",
                                     bundle=bundle, frame=frame, data_root=root)
    assert caught.value.key == "device_not_eligible"
    assert caught.value.detail


def test_container_binding_change_is_not_silently_ignored(eligible):
    _, _, root, _ = eligible
    _edit_profile(root, lambda p: p["binding"].update(camera_id="another-camera"))
    with pytest.raises(ApiError) as caught:
        _resolve(eligible)
    assert caught.value.key == "device_not_eligible"
    assert "绑定" in caught.value.detail


def test_resolver_rechecks_settings_write_is_rejected(eligible):
    """开关已删除（2026-09-30 拍板）：写开关一律 invalid_request。"""
    _, _, root, _ = eligible
    with pytest.raises(ag.AutomaticGestureError) as caught:
        ag.save_settings("arm-01", {"automatic_gestures_enabled": True}, root)
    assert caught.value.reason_code == "invalid_request"


def test_queries_on_missing_device_do_not_create_data(tmp_path):
    root = tmp_path / "absent"
    assert ag.navigation_view("arm-02", ag.NAVIGATION_PROFILE_ID,
                              bundle=None, frame=None, data_root=root) is None
    assert ag.gestures_view("arm-02", bundle=None, frame=None,
                            data_root=root)["binding"] is None
    with pytest.raises(ApiError) as caught:
        ag.resolve_navigation_action("arm-02", {
            "profile_id": ag.NAVIGATION_PROFILE_ID, "version": 1,
            "contract_version": ag.CONTRACT_VERSION}, {}, "home",
            bundle=None, frame=None, data_root=root)
    assert caught.value.key == "device_not_eligible"
    assert not root.exists()


@pytest.mark.parametrize("response_type", ["gesturesResponse", "navigationResponse"])
def test_live_views_match_the_published_contract(all_eligible, response_type):
    bundle, frame, root, _ = all_eligible
    path = Path(__file__).resolve().parents[1] / "api" / "contracts" / "automatic-gesture-profile.v1.schema.json"
    schema = json.loads(path.read_text())
    if response_type == "gesturesResponse":
        view = ag.gestures_view("arm-01", bundle=bundle, frame=frame, data_root=root)
    else:
        view = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                                  bundle=bundle, frame=frame, data_root=root)
    jsonschema.Draft202012Validator({**schema, "$ref": f"#/$defs/{response_type}"}).validate(view)


@pytest.mark.parametrize("fault", ["missing_bundle", "unaccepted", "missing_frame", "bad_frame"])
def test_resolver_rejects_missing_or_invalid_calibration(eligible, fault):
    bundle, frame, root, _ = eligible
    ref, binding = _request(eligible)
    bundle, frame = deepcopy(bundle), deepcopy(frame)
    if fault == "missing_bundle":
        bundle = None
    elif fault == "unaccepted":
        bundle["status"] = "fitted"
    elif fault == "missing_frame":
        frame = None
    else:
        frame["coordinate_frame_hash"] = "f" * 64
    with pytest.raises(ApiError) as caught:
        ag.resolve_navigation_action("arm-01", ref, binding, "search_next",
                                     bundle=bundle, frame=frame, data_root=root)
    assert caught.value.key == "device_not_eligible"
    assert any(word in caught.value.detail for word in ("校准", "坐标桥", "完整绑定"))


def test_missing_legacy_frame_never_rebinds_old_certificate(eligible):
    bundle, _, root, _ = eligible
    view = ag.gestures_view("arm-01", bundle=bundle, frame=None, data_root=root)
    derived = gp.build_calibration_coordinate_frame(bundle)
    assert view["binding"]["coordinate_frame_hash"] == derived["coordinate_frame_hash"]
    assert not view["actions"]["scroll_up"]["usable"]
    assert not view["actions"]["system_home"]["usable"]
    assert gp.load_coordinate_frame("arm-01", root) is not None


def test_stale_legacy_binding_allows_new_internal_frame_without_edge_metadata(eligible):
    bundle, frame, root, _ = eligible
    changed = deepcopy(bundle)
    changed["daemonSession"] = "new-session"
    derived = gp.frame_for_internal_gesture(changed, frame)
    assert gp.is_internal_coordinate_frame(derived)
    assert "display_width_px" not in derived
    view = ag.gestures_view("arm-01", bundle=changed, frame=frame, data_root=root)
    assert view["binding"]["coordinate_frame_hash"] == derived["coordinate_frame_hash"]
    assert not view["actions"]["scroll_up"]["usable"]
    assert "navigation_mode_unsupported" in {
        item["code"] for item in view["actions"]["system_home"]["blocking_reasons"]}


@pytest.mark.parametrize("patch", [
    {"profile_id": "wrong"}, {"contract_version": "old-contract"}, {"version": 99},
])
def test_invalid_stored_navigation_metadata_is_rejected(eligible, patch):
    _, _, root, _ = eligible
    _edit_profile(root, lambda p: p["navigation_profiles"][ag.NAVIGATION_PROFILE_ID].update(patch))
    with pytest.raises(ApiError) as caught:
        _resolve(eligible)
    assert caught.value.key == "device_not_eligible"
    assert "档案" in caught.value.detail


def test_request_binding_does_not_coerce_bool_to_version(eligible):
    _, binding = _request(eligible)
    binding["gesture_profile_version"] = True
    with pytest.raises(ApiError) as caught:
        _resolve(eligible, binding=binding)
    assert caught.value.key == "device_not_eligible"


def test_republication_does_not_revalidate_other_actions_on_new_calibration(eligible):
    bundle, frame, root, run = eligible
    changed = deepcopy(frame)
    changed["density"] += 0.1
    changed["coordinate_frame_hash"] = gp.coordinate_frame_hash(changed)
    next_run = deepcopy(run)
    next_run["coordinate_frame_hash"] = changed["coordinate_frame_hash"]
    next_run["action_results"][0].update(
        gesture_id="scroll_down", program=_program_fixture("scroll_down"))
    published = ag.publish_run_results("arm-01", next_run, data_root=root, base_version=1)
    assert published["navigation_profiles"][ag.NAVIGATION_PROFILE_ID]["binding"][
        "coordinate_frame_hash"] == changed["coordinate_frame_hash"]
    view = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                              bundle=bundle, frame=changed, data_root=root)
    assert view["version"] == 2
    assert view["actions"]["search_next"]["usable"] is False
    assert view["actions"]["search_previous"]["usable"] is True


def test_not_tested_certificate_does_not_acquire_validation_timestamp(eligible):
    _, _, root, run = eligible
    untested = deepcopy(run)
    untested["action_results"][0]["validation_status"] = "not_tested"
    published = ag.publish_run_results("arm-01", untested, data_root=root, base_version=1)
    entry = published["actions"]["scroll_up"]
    assert entry["validation_status"] == "not_tested"
    assert entry["validated_at"] is None


def test_x04_only_the_certified_direction_is_resolvable(eligible):
    """X04：两个方向只有一个已验收（夹具只验收 scroll_up）→ 仅放行已验收
    方向，另一方向明确阻断且不给程序。"""
    bundle, frame, root, _ = eligible
    ref, binding = _request(eligible)
    ag.resolve_navigation_action("arm-01", ref, binding, "search_next",
                                 bundle=bundle, frame=frame, data_root=root)
    with pytest.raises(ApiError) as caught:
        ag.resolve_navigation_action("arm-01", ref, binding, "search_previous",
                                     bundle=bundle, frame=frame, data_root=root)
    assert caught.value.key == "device_not_eligible"
    assert caught.value.detail
    navigation = ag.navigation_view("arm-01", ag.NAVIGATION_PROFILE_ID,
                                    bundle=bundle, frame=frame, data_root=root)
    assert navigation["actions"]["search_next"]["usable"] is True
    assert navigation["actions"]["search_previous"]["usable"] is False
