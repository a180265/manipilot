"""统一校准档案单元测试：读写、兼容导入、门禁逐项检查（实施方案 §10/§11/§16）。"""
from __future__ import annotations

import json

import pytest

from drivers import calibration_bundle as cb


def _ready_bundle(device="arm-01", session="sess-A") -> dict:
    """构造一份能通过全部门禁的档案。"""
    bundle = cb.new_bundle(device)
    bundle.update({
        "status": "ready",
        "cameraId": "cam-01",
        "cameraDeviceUniqueId": "uvc-01",
        "bindingRevision": 7,
        "daemonAddr": "127.0.0.1:8795",
        "daemonSession": session,
        "frame": {"width": 1280, "height": 720},
        "screen": {"orientation": "portrait", "screenWidth": 1080, "screenHeight": 2400,
                   "edgeModel": {"corners": [[0, 0], [800, 0], [800, 600], [0, 600]],
                                 "tolerancePx": 8, "safeInsetPx": 24}},
        "z": {"hoverZ": 2.0, "tapDepth": 3.0, "tapFeed": 1500.0, "tapDwellMs": 0,
              "deleteHoldMs": 2200, "deleteDepth": 3.0, "deleteFeed": 1500.0,
              "validated": True, "deleteValidated": True},
        "xy": {"robotToScreen": {"a": 10.0, "b": 0.0, "c": 100.0,
                                 "d": 0.0, "e": 8.0, "f": 200.0},
               "residualRmsPx": 0.5, "maxResidualPx": 1.0,
               "samples": [], "validation": [], "validated": True},
        "camera": {"cameraToScreen": {"homography": [1, 0, 0, 0, 1, 0, 0, 0, 1]},
                   "reprojectionRmsPx": 0.2, "validated": True},
        "targets": {name: {"validated": True} for name in
                    ("ime_candidate_bar", "ime_delete_key", "send_button", "back_button")},
        "acceptance": {"debug": True, "wechat": True, "lastRunAt": "t", "checks": []},
    })
    bundle["bindingFingerprint"] = cb.binding_fingerprint(
        device_id=device, camera_id="cam-01", camera_device_unique_id="uvc-01",
        daemon_addr="127.0.0.1:8795", frame_width=1280, frame_height=720)
    return bundle


def _gate(bundle, **kwargs):
    facts = dict(device_id="arm-01", camera_id="cam-01",
                 camera_device_unique_id="uvc-01", binding_revision=7,
                 daemon_addr="127.0.0.1:8795", frame_width=1280, frame_height=720,
                 daemon_session="sess-A", estop_locked=False,
                 screen_orientation="portrait", screen_width=1080, screen_height=2400,
                 screen_edge_deviation_px=2.0, required_targets=(), wechat_send=False)
    facts.update(kwargs)
    return cb.validate_calibration_bundle(bundle, **facts)


def _codes(checks):
    return [c["code"] for c in checks]


# ---------- 读写 ----------

def test_bundle_save_load_roundtrip(tmp_path):
    bundle = cb.new_bundle("arm-01")
    saved = cb.save_bundle(bundle, tmp_path)
    assert saved["status"] == "draft"
    loaded = cb.load_bundle("arm-01", tmp_path)
    assert loaded["deviceId"] == "arm-01"
    assert loaded["schemaVersion"] == cb.BUNDLE_SCHEMA_VERSION
    assert cb.bundle_path("arm-01", tmp_path).name == "calibration_bundle.json"


def test_bundle_load_missing_returns_none(tmp_path):
    assert cb.load_bundle("arm-99", tmp_path) is None


def test_bundle_save_is_atomic_json(tmp_path):
    cb.save_bundle(_ready_bundle(), tmp_path)
    raw = cb.bundle_path("arm-01", tmp_path).read_text(encoding="utf-8")
    assert json.loads(raw)["status"] == "ready"  # 落盘内容始终是完整 JSON


def test_schema_v1_migrates_to_v2_draft(tmp_path):
    path = cb.bundle_path("arm-01", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schemaVersion": 1, "deviceId": "arm-01", "status": "ready",
        "cameraId": "cam-01",
    }), encoding="utf-8")
    migrated = cb.load_bundle("arm-01", tmp_path)
    assert migrated["schemaVersion"] == 2
    assert migrated["status"] == "draft"
    assert migrated["migration"]["requiresRevalidation"] is True


def test_schema_v1_migrates_legacy_camera_alias_to_canonical_id(tmp_path):
    path = cb.bundle_path("arm-01", tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schemaVersion": 1, "deviceId": "arm-01", "cameraId": "camera-01",
    }), encoding="utf-8")
    migrated = cb.load_bundle("arm-01", tmp_path)
    assert migrated["cameraId"] == "cam-01"


# ---------- 兼容导入（§10 迁移策略） ----------

def test_import_legacy_targets_and_references(tmp_path):
    # 旧 targets.json / arm_profile.json / screen_ime_mappings.json / calibration.json
    device_dir = cb.device_dir("arm-01", tmp_path)
    device_dir.mkdir(parents=True)
    (device_dir / "targets.json").write_text(json.dumps({
        "ime_delete_key": {"nx": 0.2, "ny": 0.8, "targetMode": "hover_only",
                           "hoverZ": 2.0, "tapDepth": 3.0, "tapFeed": 1500.0},
    }), encoding="utf-8")
    (device_dir / "calibration.json").write_text(json.dumps({
        "session": "old-session", "affine": {"a": 1, "b": 0, "c": 0, "d": 0, "e": 1, "f": 0},
    }), encoding="utf-8")
    (tmp_path / "screen_ime_mappings.json").write_text(json.dumps({
        "arm-01": {"a": 10.0, "b": 0, "c": 100, "d": 0, "e": 8, "f": 200},
    }), encoding="utf-8")

    bundle = cb.import_legacy("arm-01", tmp_path)
    # 目标照搬但不带验收状态
    assert bundle["targets"]["ime_delete_key"]["validated"] is False
    assert bundle["targets"]["ime_delete_key"]["nx"] == 0.2
    # 旧映射只作参考，不出现在正式位
    assert bundle["xy"]["robotToScreen"] is None
    assert bundle["legacy"]["robotToScreenReference"]["a"] == 10.0
    assert bundle["legacy"]["cameraToRobotReference"]["session"] == "old-session"
    # 导入后的档案不可能 ready
    assert _codes(_gate(bundle, daemon_session="old-session"))


# ---------- 数学辅助 ----------

def test_affine_inverse_roundtrip():
    m = {"a": 10.0, "b": 0.5, "c": 100.0, "d": -0.2, "e": 8.0, "f": 200.0}
    sx, sy = cb.screen_from_robot(m, 12.0, 34.0)
    rx, ry = cb.robot_from_screen(m, sx, sy)
    assert (rx, ry) == pytest.approx((12.0, 34.0), abs=1e-9)


def test_affine_singular_not_invertible():
    assert cb.affine_invertible({"a": 1, "b": 2, "c": 0, "d": 2, "e": 4, "f": 0}) is False
    assert cb.affine_invertible({"a": 1, "b": 0, "c": 0, "d": 0, "e": 1, "f": 0}) is True
    with pytest.raises(ValueError):
        cb.robot_from_screen({"a": 1, "b": 2, "c": 0, "d": 2, "e": 4, "f": 0}, 1, 1)


# ---------- 门禁逐项（§11） ----------

def test_gate_passes_for_ready_bundle():
    assert _gate(_ready_bundle()) == []


def test_gate_missing_bundle():
    codes = _codes(_gate(None))
    assert codes == ["calibration_bundle_missing"]


def test_gate_bundle_not_ready():
    bundle = _ready_bundle()
    bundle["status"] = "draft"
    assert "bundle_not_ready" in _codes(_gate(bundle))


def test_gate_daemon_session_changed():
    codes = _codes(_gate(_ready_bundle(), daemon_session="sess-B"))
    assert codes == ["daemon_session_changed"]


def test_gate_daemon_session_unbound():
    bundle = _ready_bundle()
    bundle["daemonSession"] = None
    assert "daemon_session_unbound" in _codes(_gate(bundle))


def test_gate_rejects_when_current_daemon_session_cannot_be_confirmed():
    codes = _codes(_gate(_ready_bundle(), daemon_session=None))
    assert "daemon_session_unavailable" in codes


@pytest.mark.parametrize(("fact", "code"), [
    ("camera_device_unique_id", "camera_unique_id_unavailable"),
    ("binding_revision", "camera_binding_revision_unavailable"),
])
def test_gate_rejects_when_current_camera_identity_facts_are_unavailable(fact, code):
    assert code in _codes(_gate(_ready_bundle(), **{fact: None}))


def test_runtime_targets_prefer_bundle_over_stale_legacy_file(tmp_path):
    from drivers import target_archive

    device = target_archive.device_dir("arm-01", tmp_path)
    device.mkdir(parents=True)
    (device / "targets.json").write_text(json.dumps({
        "ime_delete_key": {"nx": 0.9, "ny": 0.9, "targetMode": "hover_only"},
    }), encoding="utf-8")
    bundle = _ready_bundle()
    bundle["targets"] = {
        "ime_delete_key": {"nx": 0.1, "ny": 0.2,
                            "targetMode": "hover_only", "validated": True},
    }
    cb.save_bundle(bundle, tmp_path)

    targets = target_archive.load_targets("arm-01", tmp_path)
    assert targets["ime_delete_key"]["nx"] == 0.1


def test_target_edit_updates_bundle_and_invalidates_ready_status(tmp_path):
    from drivers import target_archive

    bundle = _ready_bundle()
    cb.save_bundle(bundle, tmp_path)
    entry = target_archive.save_target("arm-01", "ime_delete_key", {
        "nx": 0.3, "ny": 0.4, "targetMode": "hover_only",
    }, tmp_path)

    saved = cb.load_bundle("arm-01", tmp_path)
    assert entry["nx"] == 0.3
    assert saved["targets"]["ime_delete_key"]["nx"] == 0.3
    assert saved["status"] == "draft"


def test_target_edit_does_not_overwrite_calibrated_profile(tmp_path):
    from drivers import target_archive

    bundle = _ready_bundle()
    bundle["z"].update({"hoverZ": 4.2, "tapDepth": 3.4, "tapFeed": 1500})
    cb.save_bundle(bundle, tmp_path)

    target_archive.save_target("arm-01", "ime_delete_key", {
        "nx": 0.3, "ny": 0.4, "hoverZ": 2.0,
        "tapDepth": 3.0, "tapFeed": 1500,
    }, tmp_path)

    assert target_archive.load_profile("arm-01", tmp_path)["hoverZ"] == 4.2


def test_runtime_profile_prefers_bundle_over_stale_legacy_file(tmp_path):
    from drivers import target_archive

    device = target_archive.device_dir("arm-01", tmp_path)
    device.mkdir(parents=True)
    (device / "arm_profile.json").write_text(json.dumps({
        "hoverZ": 99, "tapDepth": 99, "tapFeed": 99,
    }), encoding="utf-8")
    bundle = _ready_bundle()
    bundle["z"].update({"hoverZ": 2.5, "tapDepth": 3.5, "tapFeed": 1600})
    cb.save_bundle(bundle, tmp_path)

    profile = target_archive.load_profile("arm-01", tmp_path)
    assert profile == {"hoverZ": 2.5, "tapDepth": 3.5, "tapFeed": 1600.0}


def test_target_resolution_uses_calibrated_hover_not_stale_target_override():
    from drivers import target_archive

    resolved = target_archive.resolve_target(
        {"nx": 0.5, "ny": 0.5, "hoverZ": 99.0,
         "tapDepth": 3.0, "tapFeed": 1500.0},
        {"frameW": 100, "frameH": 100,
         "affine": {"a": 0.1, "b": 0, "c": 0,
                    "d": 0, "e": 0.1, "f": 0}},
        {"hoverZ": 2.5, "tapDepth": 3.0, "tapFeed": 1500.0},
    )
    assert resolved["hoverZ"] == 2.5


def test_profile_edit_updates_bundle_and_requires_revalidation(tmp_path):
    from drivers import target_archive

    cb.save_bundle(_ready_bundle(), tmp_path)
    target_archive.save_profile("arm-01", {"hoverZ": 2.8}, tmp_path)
    saved = cb.load_bundle("arm-01", tmp_path)
    assert saved["z"]["hoverZ"] == 2.8
    assert saved["z"]["validated"] is False
    assert saved["z"]["deleteValidated"] is False
    assert saved["status"] == "draft"


def test_malformed_bundle_cannot_fall_back_to_legacy_runtime_files(tmp_path):
    from drivers import target_archive

    device = target_archive.device_dir("arm-01", tmp_path)
    device.mkdir(parents=True)
    (device / "targets.json").write_text(json.dumps({
        "ime_delete_key": {"nx": 0.8, "ny": 0.8, "targetMode": "hover_only"},
    }), encoding="utf-8")
    (device / "arm_profile.json").write_text(json.dumps({
        "hoverZ": 9, "tapDepth": 9, "tapFeed": 900,
    }), encoding="utf-8")
    cb.bundle_path("arm-01", tmp_path).write_text("{broken", encoding="utf-8")

    assert target_archive.load_targets("arm-01", tmp_path) == {}
    assert target_archive.load_profile("arm-01", tmp_path) == {
        "hoverZ": target_archive.DEFAULT_HOVER_Z,
        "tapDepth": target_archive.DEFAULT_TAP_DEPTH,
        "tapFeed": target_archive.DEFAULT_TAP_FEED,
    }
    assert target_archive.load_runtime_calibration("arm-01", tmp_path) is None
    with pytest.raises(ValueError, match="统一校准档案损坏"):
        target_archive.save_target("arm-01", "ime_delete_key",
                                   {"nx": 0.1, "ny": 0.1}, tmp_path)


def test_draft_bundle_with_unset_z_does_not_use_legacy_profile(tmp_path):
    from drivers import target_archive

    device = target_archive.device_dir("arm-01", tmp_path)
    device.mkdir(parents=True)
    (device / "arm_profile.json").write_text(json.dumps({"hoverZ": 99}), encoding="utf-8")
    cb.save_bundle(cb.new_bundle("arm-01"), tmp_path)
    assert target_archive.load_profile("arm-01", tmp_path) == {
        "hoverZ": target_archive.DEFAULT_HOVER_Z,
        "tapDepth": target_archive.DEFAULT_TAP_DEPTH,
        "tapFeed": target_archive.DEFAULT_TAP_FEED,
    }


@pytest.mark.parametrize(("fact", "value", "code"), [
    ("device_id", "arm-02", "device_binding_changed"),
    ("camera_id", "cam-02", "camera_binding_changed"),
    ("camera_device_unique_id", "uvc-02", "camera_unique_id_changed"),
    ("binding_revision", 8, "camera_binding_revision_changed"),
    ("daemon_addr", "127.0.0.1:8796", "daemon_address_changed"),
    ("frame_width", 1920, "camera_resolution_changed"),
])
def test_gate_rejects_binding_changes(fact, value, code):
    assert code in _codes(_gate(_ready_bundle(), **{fact: value}))


def test_gate_rejects_tampered_binding_fingerprint():
    bundle = _ready_bundle()
    bundle["bindingFingerprint"] = "0" * 64
    codes = _codes(_gate(bundle))
    assert "calibration_binding_corrupt" in codes
    assert "binding_fingerprint_changed" in codes


def test_gate_screen_orientation_and_size_changed():
    assert "screen_orientation_changed" in _codes(
        _gate(_ready_bundle(), screen_orientation="landscape"))
    assert "screen_size_changed" in _codes(_gate(_ready_bundle(), screen_width=1170))


def test_gate_screen_edge_unverifiable_and_changed():
    """2026-09-04 语义：无法核验（None=检测失败/场景不适配）不再阻断业务；
    有效检测下的真实偏差超容差仍硬拦。"""
    bundle = _ready_bundle()
    # 无法核验：不产生任何边缘类失败
    assert not [c for c in _codes(_gate(bundle, screen_edge_deviation_px=None))
                if c.startswith("screen_edge")]
    codes = _codes(_gate(bundle, screen_edge_deviation_px=9.0))
    assert "screen_edge_changed" in codes  # 容差 8px
    assert "screen_edge_changed" not in _codes(_gate(bundle, screen_edge_deviation_px=8.0))


def test_gate_z_parameters_missing_and_not_validated():
    bundle = _ready_bundle()
    bundle["z"]["hoverZ"] = None
    assert "z_parameters_not_validated" in _codes(_gate(bundle))
    bundle = _ready_bundle()
    bundle["z"]["validated"] = False
    assert "z_parameters_not_validated" in _codes(_gate(bundle))


def test_gate_z_parameters_out_of_range_forbidden_feed_and_hold():
    bundle = _ready_bundle()
    bundle["z"]["tapFeed"] = 10.0  # 低于 F30 下限（F300 已于 2026-09-18 解禁）
    assert "z_parameters_out_of_range" in _codes(_gate(bundle))
    bundle = _ready_bundle()
    bundle["z"]["deleteHoldMs"] = 50  # 低于可信长按范围
    assert "z_parameters_out_of_range" in _codes(_gate(bundle))


def test_gate_xy_mapping_missing_invalid_residual():
    bundle = _ready_bundle()
    bundle["xy"]["robotToScreen"] = None
    assert "xy_mapping_missing" in _codes(_gate(bundle))

    bundle = _ready_bundle()
    bundle["xy"]["robotToScreen"] = {"a": 1, "b": 2, "c": 0, "d": 2, "e": 4, "f": 0}
    assert "xy_mapping_invalid" in _codes(_gate(bundle))

    bundle = _ready_bundle()
    bundle["xy"]["residualRmsPx"] = 4.0
    assert "xy_residual_exceeded" in _codes(_gate(bundle))
    bundle = _ready_bundle()
    bundle["xy"]["maxResidualPx"] = 7.0
    assert "xy_residual_exceeded" in _codes(_gate(bundle))

    bundle = _ready_bundle()
    bundle["xy"]["validated"] = False
    assert "xy_mapping_not_validated" in _codes(_gate(bundle))


def test_gate_camera_mapping_missing_and_residual():
    bundle = _ready_bundle()
    bundle["camera"]["cameraToScreen"] = None
    assert "camera_mapping_missing" in _codes(_gate(bundle))
    bundle = _ready_bundle()
    bundle["camera"]["reprojectionRmsPx"] = 5.0
    assert "camera_residual_exceeded" in _codes(_gate(bundle))


def test_gate_target_not_validated():
    bundle = _ready_bundle()
    bundle["targets"]["send_button"]["validated"] = False
    codes = _codes(_gate(bundle, required_targets=("send_button",)))
    assert codes == ["target_not_validated"]
    codes = _codes(_gate(bundle, required_targets=("unknown_target",)))
    assert codes == ["target_not_validated"]


def test_gate_does_not_require_wechat_acceptance_and_estop():
    bundle = _ready_bundle()
    bundle["acceptance"]["wechat"] = False
    # 业务测试会话不是校准前置；发送仍由每次任务的真实发送证据确认。
    assert _gate(bundle, wechat_send=True) == []
    # 急停锁定
    codes = _codes(_gate(_ready_bundle(), estop_locked=True))
    assert codes == ["motion_blocked"]


def test_gate_collects_multiple_failures():
    bundle = _ready_bundle()
    bundle["status"] = "draft"
    bundle["z"]["validated"] = False
    codes = _codes(_gate(bundle, daemon_session="other"))
    assert set(codes) == {"bundle_not_ready", "daemon_session_changed",
                          "z_parameters_not_validated"}


# ---------- v1.1 自动校准：可选字段 / candidate 存储 / ready 门禁扩展 ----------

def test_new_bundle_has_v11_optional_defaults(tmp_path):
    """safePark 默认全 null（禁止示例值）、modelLocks UNINITIALIZED、autoCalibration IDLE。"""
    bundle = cb.new_bundle("arm-01")
    assert bundle["safePark"] == {"parkX": None, "parkY": None, "parkZ": None,
                                  "confirmed": False, "source": None, "confirmedAt": None}
    assert bundle["modelLocks"]["cameraToScreen"] == {"state": "UNINITIALIZED", "version": 0}
    assert bundle["modelLocks"]["armAffine"] == {"state": "UNINITIALIZED", "version": 0}
    assert bundle["autoCalibration"]["state"] == "IDLE"
    assert bundle["validation"] == {"points": [], "passed": False}
    assert bundle["z"]["zSafe"] is None
    assert "cameraToArmCandidate" not in json.dumps(bundle)  # §21.2 明确禁止


def test_candidate_save_load_list_roundtrip(tmp_path):
    candidate = {"runId": "run-abc123", "deviceId": "arm-01", "state": "RUNNING",
                 "createdAt": "2026-09-03T10:00:00"}
    cb.save_candidate(candidate, tmp_path)
    loaded = cb.load_candidate("arm-01", "run-abc123", tmp_path)
    assert loaded["runId"] == "run-abc123" and loaded["state"] == "RUNNING"
    assert cb.load_candidate("arm-01", "run-missing", tmp_path) is None
    items = cb.list_candidates("arm-01", tmp_path)
    assert [i["runId"] for i in items] == ["run-abc123"]
    with pytest.raises(ValueError):
        cb.save_candidate({"runId": "", "deviceId": "arm-01"}, tmp_path)


def test_profile_hash_ignores_updated_at_only():
    bundle = _ready_bundle()
    first = cb.profile_hash(bundle)
    bundle["updatedAt"] = "2099-01-01T00:00:00"
    assert cb.profile_hash(bundle) == first
    bundle["xy"]["residualRmsPx"] = 2.0
    assert cb.profile_hash(bundle) != first


def test_gate_requires_locked_models_when_auto_trace_present():
    """带自动校准痕迹（lock 状态非 UNINITIALIZED）的 ready 档案：锁必须 LOCKED。"""
    bundle = _ready_bundle()
    bundle["modelLocks"]["cameraToScreen"]["state"] = "LOCKED"
    bundle["modelLocks"]["armAffine"]["state"] = "CANDIDATE"
    codes = _codes(_gate(bundle))
    assert "model_not_locked" in codes

    bundle["modelLocks"]["armAffine"]["state"] = "LOCKED"
    assert _gate(bundle) == []  # 双锁定即通过

    bundle["modelLocks"]["armAffine"]["state"] = "INVALID"
    assert "model_not_locked" in _codes(_gate(bundle))


def test_gate_requires_independent_validation_passed_when_points_recorded():
    bundle = _ready_bundle()
    bundle["modelLocks"] = {
        "cameraToScreen": {"state": "LOCKED", "version": 1},
        "armAffine": {"state": "LOCKED", "version": 1}}
    bundle["validation"] = {"points": [{"targetId": "validation-1", "touchTargetErrorPx": 4.0}],
                            "passed": False}
    codes = _codes(_gate(bundle))
    assert "independent_validation_failed" in codes
    bundle["validation"]["passed"] = True
    assert _gate(bundle) == []


def test_gate_manual_bundle_without_locks_unchanged():
    """手动流程档案（无 lock 痕迹）不触发新增检查（回归保护）。"""
    bundle = _ready_bundle()
    bundle.pop("modelLocks", None)
    assert _gate(bundle) == []


def test_load_profile_prefers_zsafe_when_hoverZ_missing(tmp_path):
    """Z 基准统一（审计 R2/R3）：draft 态 bundle 已确认 zSafe（G92 锚定后可能为
    负值）但 hoverZ 未写时，load_profile 必须回退读 zSafe，绝不静默 2.0。"""
    from drivers import target_archive

    bundle = _ready_bundle()
    bundle["status"] = "draft"
    bundle["z"] = {**bundle["z"], "hoverZ": None, "zSafe": -5.0}
    cb.save_bundle(bundle, tmp_path)
    profile = target_archive.load_profile("arm-01", tmp_path)
    assert profile["hoverZ"] == pytest.approx(-5.0)

    # ready 档案 hoverZ 优先（promote 语义 hoverZ=zSafe）
    bundle2 = _ready_bundle()
    bundle2["z"] = {**bundle2["z"], "hoverZ": 2.5, "zSafe": -5.0}
    cb.save_bundle(bundle2, tmp_path)
    assert target_archive.load_profile("arm-01", tmp_path)["hoverZ"] == pytest.approx(2.5)


def test_quadratic_model_roundtrip_and_affine_compat():
    """方案 A（2026-09-04）：robotToScreen 携带二次键时前向精确、Newton 反解还原；
    旧档案只有仿射 6 键时行为完全不变。"""
    quad = {"a": 10.0, "b": 0.5, "c": 100.0, "d": 0.3, "e": 8.0, "f": 50.0,
            "qx2": 0.02, "qxy": -0.01, "qy2": 0.005,
            "px2": -0.004, "pxy": 0.008, "py2": 0.015}
    worst = 0.0
    for rx in range(0, 61, 5):
        for ry in range(0, 41, 4):
            sx, sy = cb.screen_from_robot(quad, rx, ry)
            bx, by = cb.robot_from_screen(quad, sx, sy)
            worst = max(worst, abs(bx - rx), abs(by - ry))
    assert worst < 1e-6
    # 屏幕工作区角落（大坐标）同样收敛
    sx, sy = cb.screen_from_robot(quad, 120.0, 80.0)
    assert cb.robot_from_screen(quad, sx, sy) == pytest.approx((120.0, 80.0), abs=1e-6)

    affine_only = {"a": 10.0, "b": 0.0, "c": 0.0, "d": 0.0, "e": 10.0, "f": 0.0}
    assert cb.screen_from_robot(affine_only, 32.0, 24.0) == (320.0, 240.0)
    assert cb.robot_from_screen(affine_only, 320.0, 240.0) == (32.0, 24.0)


def test_p08_conditional_save_rejects_concurrent_modification(tmp_path):
    """P08：以读取时字节摘要为条件的原子写入——磁盘被改后拒绝覆盖。"""
    bundle = cb.new_bundle("arm-01")
    bundle["z"]["hoverZ"] = 2.0
    cb.save_bundle(bundle, tmp_path)
    digest = cb.bundle_file_digest("arm-01", tmp_path)
    assert digest is not None

    # 摘要匹配 → 正常写入。
    bundle["z"]["hoverZ"] = 3.0
    cb.save_bundle(bundle, tmp_path, expected_digest=digest)
    assert cb.load_bundle("arm-01", tmp_path)["z"]["hoverZ"] == 3.0

    # 磁盘被其他写入改动 → 旧摘要拒绝覆盖，内容保持他方写入。
    other = cb.new_bundle("arm-01")
    other["z"]["hoverZ"] = 9.0
    cb.save_bundle(other, tmp_path)
    with pytest.raises(cb.BundleConcurrentWrite, match="并发"):
        cb.save_bundle(bundle, tmp_path, expected_digest=digest)
    assert cb.load_bundle("arm-01", tmp_path)["z"]["hoverZ"] == 9.0


def test_p08_require_absent_only_allows_first_creator(tmp_path):
    bundle = cb.new_bundle("arm-01")
    cb.save_bundle(bundle, tmp_path, require_absent=True)
    with pytest.raises(cb.BundleConcurrentWrite, match="并发创建"):
        cb.save_bundle(bundle, tmp_path, require_absent=True)
    assert cb.bundle_file_digest("arm-01", tmp_path) is not None
