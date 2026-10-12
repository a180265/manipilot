"""ui_targets.json 档案层单元测试（WX 图形固定目标标定与安全点击规格书 v1.0 §3/§5/§8）。

覆盖：登记门禁（bundle 必须 ready）、草稿→测试→确认→启用生命周期、
版本递增与审计、绑定快照失配判"已失效"、解析门禁、以及最关键的回归约束——
任何目标操作都不修改 calibration_bundle.json（字节级校验）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pathlib
_WX_EXAMPLE_ABSENT = not pathlib.Path(__file__).resolve().parents[1].joinpath("examples", "plugins", "wx-wechat-adapter", "manifest.json").exists()
needs_wx_example = pytest.mark.skipif(_WX_EXAMPLE_ABSENT, reason="wx-wechat-adapter 示例不在 SDK 发行中，业务载具用例跳过")


from drivers import calibration_bundle as cb
from drivers import ui_targets
from drivers.ui_targets import UiTargetError


def ready_bundle(device: str = "arm-01", session: str = "sess-A") -> dict:
    """能通过门禁的完整档案（结构与 tests.test_calibration_bundle._ready_bundle 一致）。"""
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
        "acceptance": {"debug": True, "wechat": True, "lastRunAt": "t", "checks": []},
    })
    bundle["bindingFingerprint"] = cb.binding_fingerprint(
        device_id=device, camera_id="cam-01", camera_device_unique_id="uvc-01",
        daemon_addr="127.0.0.1:8795", frame_width=1280, frame_height=720)
    return bundle


@pytest.fixture()
def ready_device(tmp_path: Path) -> Path:
    cb.save_bundle(ready_bundle(), tmp_path)
    return tmp_path


def bundle_bytes(tmp_path: Path) -> bytes:
    return cb.bundle_path("arm-01", tmp_path).read_bytes()


def err_code(excinfo) -> str:
    return excinfo.value.code


# ---------- 登记门禁 ----------

def test_register_requires_ready_bundle(tmp_path: Path):
    with pytest.raises(UiTargetError) as exc:
        ui_targets.register_draft("arm-01", "back_button", nx=0.1, ny=0.1,
                                  data_root=tmp_path)
    assert err_code(exc) == "calibration_not_ready"

    bundle = ready_bundle()
    bundle["status"] = "draft"
    cb.save_bundle(bundle, tmp_path)
    with pytest.raises(UiTargetError) as exc:
        ui_targets.register_draft("arm-01", "back_button", nx=0.1, ny=0.1,
                                  data_root=tmp_path)
    assert err_code(exc) == "calibration_not_ready"


def test_register_draft_snapshots_binding(ready_device: Path):
    bundle = cb.load_bundle("arm-01", ready_device)
    entry = ui_targets.register_draft(
        "arm-01", "back_button", nx=0.062, ny=0.083,
        operator="操作员甲", data_root=ready_device)
    assert entry["state"] == ui_targets.STATE_DRAFT
    assert entry["version"] == 1
    assert entry["targetMode"] == "tap"  # 预置目标模式固定
    assert entry["nameDisplay"] == "返回主界面"
    assert entry["calibrationFingerprint"] == bundle["bindingFingerprint"]
    assert entry["daemonSession"] == bundle["daemonSession"]
    assert entry["frame"] == {"width": 1280, "height": 720}
    assert entry["screenOrientation"] == "portrait"
    assert entry["geometryHash"] == cb.profile_hash(
        bundle, keys=ui_targets.GEOMETRY_HASH_KEYS)


def test_preset_delete_key_forced_hover_only(ready_device: Path):
    entry = ui_targets.register_draft("arm-01", "ime_delete_key", nx=0.2, ny=0.6,
                                      target_mode="tap", data_root=ready_device)
    assert entry["targetMode"] == "hover_only"  # 预置模式不可被请求覆盖


@needs_wx_example
def test_custom_target_validation(ready_device: Path):
    with pytest.raises(UiTargetError) as exc:
        ui_targets.register_draft("arm-01", "Bad-ID", nx=0.1, ny=0.1,
                                  target_mode="tap", name_display="x",
                                  owner_plugin="p", data_root=ready_device)
    assert err_code(exc) == "invalid_target_id"

    with pytest.raises(UiTargetError) as exc:
        ui_targets.register_draft("arm-01", "my_button", nx=0.1, ny=0.1,
                                  name_display="我的按钮", owner_plugin="p",
                                  data_root=ready_device)
    assert err_code(exc) == "invalid_target_mode"  # 自定义必须显式 tap/hover_only

    with pytest.raises(UiTargetError) as exc:
        ui_targets.register_draft("arm-01", "my_button", nx=0.1, ny=0.1,
                                  target_mode="tap", owner_plugin="p",
                                  data_root=ready_device)
    assert err_code(exc) == "invalid_name_display"

    entry = ui_targets.register_draft("arm-01", "my_button", nx=0.1, ny=0.1,
                                      target_mode="tap", name_display="我的按钮",
                                      owner_plugin="wx-wechat-adapter",
                                      required_ui_state="conversation",
                                      risk_level="中", data_root=ready_device)
    assert entry["targetMode"] == "tap" and entry["preset"] is False


def test_position_bounds(ready_device: Path):
    with pytest.raises(UiTargetError) as exc:
        ui_targets.register_draft("arm-01", "back_button", nx=1.5, ny=0.1,
                                  data_root=ready_device)
    assert err_code(exc) == "invalid_position"


# ---------- 生命周期 ----------

def test_lifecycle_draft_tested_enabled_disabled(ready_device: Path):
    ui_targets.register_draft("arm-01", "back_button", nx=0.062, ny=0.083,
                              data_root=ready_device)
    # 未测试不能启用
    with pytest.raises(UiTargetError) as exc:
        ui_targets.set_enabled("arm-01", "back_button", enabled=True,
                               data_root=ready_device)
    assert err_code(exc) == "invalid_state"

    # 测试失败保持草稿并留证据
    entry = ui_targets.mark_test_result("arm-01", "back_button", passed=False,
                                        evidence={"reason": "没点到"},
                                        operator="操作员甲", data_root=ready_device)
    assert entry["state"] == ui_targets.STATE_DRAFT
    assert len(entry["testFailures"]) == 1

    entry = ui_targets.mark_test_result("arm-01", "back_button", passed=True,
                                        evidence={"kind": "tap_test"},
                                        operator="操作员甲", data_root=ready_device)
    assert entry["state"] == ui_targets.STATE_TESTED
    assert entry["validatedAt"] and entry["testEvidence"]

    entry = ui_targets.set_enabled("arm-01", "back_button", enabled=True,
                                   operator="操作员甲", data_root=ready_device)
    assert entry["state"] == ui_targets.STATE_ENABLED

    entry = ui_targets.set_enabled("arm-01", "back_button", enabled=False,
                                   operator="操作员甲", data_root=ready_device)
    assert entry["state"] == ui_targets.STATE_DISABLED

    # 已停用可直接重新启用（测试证据仍在）
    entry = ui_targets.set_enabled("arm-01", "back_button", enabled=True,
                                   data_root=ready_device)
    assert entry["state"] == ui_targets.STATE_ENABLED


def test_redraft_increments_version_and_resets_state(ready_device: Path):
    ui_targets.register_draft("arm-01", "back_button", nx=0.062, ny=0.083,
                              data_root=ready_device)
    ui_targets.mark_test_result("arm-01", "back_button", passed=True,
                                data_root=ready_device)
    ui_targets.set_enabled("arm-01", "back_button", enabled=True,
                           data_root=ready_device)
    entry = ui_targets.register_draft("arm-01", "back_button", nx=0.07, ny=0.09,
                                      operator="操作员乙", data_root=ready_device)
    assert entry["version"] == 2
    assert entry["state"] == ui_targets.STATE_DRAFT  # 修改即回草稿，需重新测试
    assert entry["validatedAt"] is None and entry["enabledAt"] is None
    # 解析层必须拒绝（未启用）
    bundle = cb.load_bundle("arm-01", ready_device)
    with pytest.raises(UiTargetError) as exc:
        ui_targets.resolve_entry(entry, bundle)
    assert err_code(exc) == "target_not_enabled"


def test_delete_keeps_audit(ready_device: Path):
    ui_targets.register_draft("arm-01", "back_button", nx=0.062, ny=0.083,
                              operator="操作员甲", data_root=ready_device)
    ui_targets.delete_target("arm-01", "back_button", operator="操作员甲",
                             data_root=ready_device)
    assert ui_targets.load_entry("arm-01", "back_button", ready_device) is None
    audit = ui_targets.load_archive("arm-01", ready_device)["audit"]
    assert [a["op"] for a in audit] == ["register_draft", "delete"]
    with pytest.raises(UiTargetError) as exc:
        ui_targets.delete_target("arm-01", "back_button", data_root=ready_device)
    assert err_code(exc) == "target_not_found"


# ---------- 解析门禁 ----------

def enabled_entry(tmp_path: Path, target_id: str = "back_button") -> dict:
    ui_targets.register_draft("arm-01", target_id, nx=0.062, ny=0.083,
                              data_root=tmp_path)
    ui_targets.mark_test_result("arm-01", target_id, passed=True,
                                evidence={"kind": "tap_test"}, data_root=tmp_path)
    return ui_targets.set_enabled("arm-01", target_id, enabled=True,
                                  data_root=tmp_path)


def test_resolve_entry_enabled_and_binding_match(ready_device: Path):
    entry = enabled_entry(ready_device)
    bundle = cb.load_bundle("arm-01", ready_device)
    resolved = ui_targets.resolve_entry(entry, bundle)
    assert resolved["target_source"] == "ui_fixed_target"
    assert resolved["targetMode"] == "tap"
    assert resolved["nx"] == 0.062 and resolved["ny"] == 0.083


def test_resolve_entry_stale_on_recalibration(ready_device: Path):
    entry = enabled_entry(ready_device)
    bundle = cb.load_bundle("arm-01", ready_device)
    bundle["xy"]["residualRmsPx"] = 0.9  # 重跑校准 → 几何哈希变化
    with pytest.raises(UiTargetError) as exc:
        ui_targets.resolve_entry(entry, bundle)
    assert err_code(exc) == "target_stale"
    assert "重新标定" in str(exc.value)


def test_resolve_entry_stale_on_session_or_frame_or_orientation(ready_device: Path):
    entry = enabled_entry(ready_device)
    base = cb.load_bundle("arm-01", ready_device)
    for mutate in (
        lambda b: b.update({"daemonSession": "sess-B"}),
        lambda b: b.update({"frame": {"width": 1920, "height": 1080}}),
        lambda b: b.update({"screen": dict(b["screen"], orientation="landscape")}),
    ):
        bundle = json.loads(json.dumps(base))
        mutate(bundle)
        with pytest.raises(UiTargetError) as exc:
            ui_targets.resolve_entry(entry, bundle)
        assert err_code(exc) == "target_stale"


def test_corrupted_archive_rejected_not_silently_empty(tmp_path: Path):
    cb.save_bundle(ready_bundle(), tmp_path)
    path = ui_targets.ui_targets_path("arm-01", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(UiTargetError) as exc:
        ui_targets.load_entry("arm-01", "back_button", tmp_path)
    assert err_code(exc) == "archive_corrupted"


# ---------- 关键回归：目标操作绝不触碰 calibration_bundle.json ----------

def test_target_ops_never_touch_calibration_bundle(ready_device: Path):
    before = bundle_bytes(ready_device)
    enabled_entry(ready_device, "back_button")
    ui_targets.register_draft("arm-01", "ime_delete_key", nx=0.2, ny=0.6,
                              data_root=ready_device)
    ui_targets.set_enabled("arm-01", "back_button", enabled=False,
                           data_root=ready_device)
    ui_targets.delete_target("arm-01", "ime_delete_key", data_root=ready_device)
    assert bundle_bytes(ready_device) == before, "目标登记/测试/启停不得修改基础校准档案"


# ---------- 列表视图 ----------

def test_list_view_states_and_robot_resolution(ready_device: Path):
    ui_targets.register_draft("arm-01", "back_button", nx=0.5, ny=0.5,
                              data_root=ready_device)
    view = ui_targets.list_view("arm-01", ready_device)
    assert view["registrationOpen"] is True
    assert view["targets"]["back_button"]["derivedStatus"] == "草稿"
    assert view["targets"]["back_button"]["businessUsable"] is False
    # H=单位阵, robotToScreen a=10,c=100 / e=8,f=200 → (640,360) → (54.0, 20.0)
    assert view["targets"]["back_button"]["robotX"] == pytest.approx(54.0)
    assert view["targets"]["back_button"]["robotY"] == pytest.approx(20.0)

    # 失效展示：改几何后不重写档案也能在视图上暴露"已失效（待复核）"
    bundle = cb.load_bundle("arm-01", ready_device)
    bundle["xy"]["residualRmsPx"] = 0.9
    cb.save_bundle(bundle, ready_device)
    view = ui_targets.list_view("arm-01", ready_device)
    assert view["targets"]["back_button"]["derivedStatus"] == "已失效（待复核）"


def test_list_view_registration_closed_when_not_ready(tmp_path: Path):
    cb.save_bundle(ready_bundle(), tmp_path)
    ui_targets.register_draft("arm-01", "back_button", nx=0.1, ny=0.1,
                              data_root=tmp_path)
    bundle = cb.load_bundle("arm-01", tmp_path)
    bundle["status"] = "draft"
    cb.save_bundle(bundle, tmp_path)
    view = ui_targets.list_view("arm-01", tmp_path)
    assert view["registrationOpen"] is False
    assert view["targets"]["back_button"]["robotX"] is None


# ---------- 连带审查补充（2026-09-03 第二轮） ----------

def test_dynamic_reserved_id_rejected(ready_device: Path):
    # conversation_item 是动态会话目标（OCR 感知坐标），不允许登记为固定目标
    with pytest.raises(UiTargetError) as exc:
        ui_targets.register_draft("arm-01", "conversation_item", nx=0.5, ny=0.5,
                                  data_root=ready_device)
    assert err_code(exc) == "invalid_target_id"
    assert "动态会话目标" in str(exc.value)


def test_entry_view_stale_when_bundle_missing(ready_device: Path):
    ui_targets.register_draft("arm-01", "back_button", nx=0.062, ny=0.083,
                              data_root=ready_device)
    entry = ui_targets.load_entry("arm-01", "back_button", ready_device)
    # 校准档案缺失（被删/未生成）时必须按"已失效"展示，不能误判绑定一致或崩溃
    view = ui_targets.entry_view(entry, None)
    assert view["derivedStatus"] == "已失效（待复核）"
    assert view["businessUsable"] is False
    # binding_snapshot 对 None 档案不崩溃
    snapshot = ui_targets.binding_snapshot(None)
    assert snapshot["calibrationFingerprint"] is None


def test_resolve_entry_test_mode_allows_draft(ready_device: Path):
    ui_targets.register_draft("arm-01", "back_button", nx=0.5, ny=0.5,
                              data_root=ready_device)
    entry = ui_targets.load_entry("arm-01", "back_button", ready_device)
    bundle = cb.load_bundle("arm-01", ready_device)
    # 业务路径（默认）拒绝未启用
    with pytest.raises(UiTargetError) as exc:
        ui_targets.resolve_entry(entry, bundle)
    assert err_code(exc) == "target_not_enabled"
    # 受控测试路径（require_enabled=False）允许草稿换算，绑定门禁仍强制
    resolved = ui_targets.resolve_entry(entry, bundle, require_enabled=False)
    assert resolved["targetMode"] == "tap"
    bundle["xy"]["residualRmsPx"] = 0.9
    with pytest.raises(UiTargetError) as exc:
        ui_targets.resolve_entry(entry, bundle, require_enabled=False)
    assert err_code(exc) == "target_stale"


# ---------- 验收期登记窗口与提升 rebind（2026-09-04 三步收敛） ----------

def test_candidate_bar_is_preset_tap(ready_device: Path):
    entry = ui_targets.register_draft("arm-01", "ime_candidate_bar", nx=0.5, ny=0.8,
                                      data_root=ready_device)
    assert entry["targetMode"] == "tap"
    assert entry["nameDisplay"] == "输入法候选栏"


def test_registration_window_allows_draft_bundle(tmp_path: Path):
    # E 验收先于 promote：活动档案 draft 时，窗口内放行登记并标 bindPending
    cb.save_bundle(ready_bundle(), tmp_path)
    bundle = cb.load_bundle("arm-01", tmp_path)
    bundle["status"] = "draft"
    cb.save_bundle(bundle, tmp_path)

    with pytest.raises(UiTargetError) as exc:
        ui_targets.register_draft("arm-01", "back_button", nx=0.1, ny=0.1,
                                  data_root=tmp_path)
    assert err_code(exc) == "calibration_not_ready"

    entry = ui_targets.register_draft(
        "arm-01", "back_button", nx=0.1, ny=0.1, data_root=tmp_path,
        registration_window={"source": "auto_run", "runId": "run-x"})
    assert entry["bindPending"] is True and entry["registeredVia"] == "auto_run"
    assert entry["registeredAtRun"] == "run-x"


def test_rebind_pending_restamps_only_flagged(ready_device: Path):
    # 正常登记（ready）一条 + 窗口登记（bindPending）一条
    ui_targets.register_draft("arm-01", "send_button", nx=0.5, ny=0.9,
                              data_root=ready_device)
    window_entry = ui_targets.register_draft(
        "arm-01", "back_button", nx=0.1, ny=0.1, data_root=ready_device,
        registration_window={"source": "manual"})
    bundle = cb.load_bundle("arm-01", ready_device)
    before = bundle_bytes(ready_device)

    # 提升后的最终档案：几何字段被 promote 改写
    final = json.loads(json.dumps(bundle))
    final["status"] = "ready"
    final["xy"] = dict(final["xy"], validated=True, residualRmsPx=0.7)
    result = ui_targets.rebind_pending("arm-01", final, data_root=ready_device)
    assert result["rebound"] == ["back_button"]

    entry = ui_targets.load_entry("arm-01", "back_button", ready_device)
    assert "bindPending" not in entry
    assert entry["geometryHash"] == cb.profile_hash(
        final, keys=ui_targets.GEOMETRY_HASH_KEYS)
    # 未标记条目不受影响；基础校准档案未被触碰
    send_entry = ui_targets.load_entry("arm-01", "send_button", ready_device)
    assert "bindPending" not in send_entry
    assert bundle_bytes(ready_device) == before


def test_bind_pending_display_and_business_reject(ready_device: Path):
    ui_targets.register_draft("arm-01", "back_button", nx=0.1, ny=0.1,
                              data_root=ready_device,
                              registration_window={"source": "auto_run", "runId": "r1"})
    ui_targets.mark_test_result("arm-01", "back_button", passed=True,
                                data_root=ready_device)
    entry = ui_targets.set_enabled("arm-01", "back_button", enabled=True,
                                   data_root=ready_device)
    # 展示：待校准提升后生效（不判已失效）
    view = ui_targets.entry_view(entry, cb.load_bundle("arm-01", ready_device))
    assert view["derivedStatus"] == "草稿（待校准提升后生效）"
    # 业务解析：bindPending 一律拒绝（绑定尚未生效）
    with pytest.raises(UiTargetError) as exc:
        ui_targets.resolve_entry(entry, cb.load_bundle("arm-01", ready_device))
    assert err_code(exc) == "target_not_enabled"
    # rebind 后即可正常解析
    final = cb.load_bundle("arm-01", ready_device)
    ui_targets.rebind_pending("arm-01", final, data_root=ready_device)
    resolved = ui_targets.resolve_entry(ui_targets.load_entry(
        "arm-01", "back_button", ready_device), final)
    assert resolved["target_source"] == "ui_fixed_target"


@needs_wx_example
def test_re_stamp_after_recovery_restamps_only_enabled(tmp_path):
    """F4（第七轮复审）：A1 复核全过后的恢复重签——仅 enabled 目标重签
    绑定快照（新会话/指纹），草稿与未启用条目不动；留审计。"""
    from drivers import calibration_bundle as cb
    from drivers import ui_targets
    from tests.test_live_calibration_gate import ready_bundle

    archive_root = tmp_path / "data"
    bundle = ready_bundle()  # 登记：旧会话快照（session-01）
    cb.save_bundle(bundle, archive_root)  # 登记门禁要求 ready 档案在场
    for target_id, state, mode, name in (
            ("delete_key", "enabled", "tap", "删除键"),
            ("back_button", "draft", "hover_only", "返回键")):
        ui_targets.register_draft("arm-01", target_id, nx=0.5, ny=0.5,
                                  target_mode=mode, name_display=name,
                                  owner_plugin="wx-wechat-adapter",
                                  data_root=archive_root)
    # 恢复后的新档案：会话与指纹都已更新
    bundle["daemonSession"] = "sess-new"
    bundle["bindingFingerprint"] = "fp-new"
    archive = ui_targets.load_archive("arm-01", archive_root)
    for target_id, state in (("delete_key", "enabled"), ("back_button", "draft")):
        entry = archive["entries"][target_id]
        entry["state"] = state
    ui_targets._save_archive("arm-01", archive, archive_root)

    result = ui_targets.re_stamp_after_recovery("arm-01", bundle,
                                                data_root=archive_root)
    assert result["stamped"] == ["delete_key"]
    archive = ui_targets.load_archive("arm-01", archive_root)
    stamped_entry = archive["entries"]["delete_key"]
    # 快照字段平铺在条目上（binding_snapshot 展开），重签=会话/指纹更新到
    # 恢复后的新档案；draft 条目保持登记时的旧快照不动
    assert stamped_entry["daemonSession"] == "sess-new"
    assert stamped_entry["calibrationFingerprint"] == "fp-new"
    assert archive["entries"]["back_button"]["state"] == "draft"
    assert archive["entries"]["back_button"].get("daemonSession") == "session-01", \
        "draft 不得被重签"
