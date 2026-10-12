"""断线自愈决策链（A1+A3，2026-10-01 拍板）单元测试。

复核分级：前提（档案/几何/串口身份）→ 手机侧（相机四边）→ 臂侧（注入的
锚点验证器，未提供=转人工）→ 全过才转正+证书直恢复+目标重绑。
"""
import sys

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime import recovery


def test_bundle_missing_fails_to_manual():
    result = recovery.attempt_recovery("arm-01", bundle=None, new_session="s2")
    assert result == {"recovered": False, "stage": "bundle_missing",
                      "reason": "无校准档案，必须重新校准"}


def test_serial_identity_change_fails():
    result = recovery.attempt_recovery(
        "arm-01", bundle={"deviceId": "arm-01"}, new_session="s2",
        expected_serial_identity={"vid": "A", "pid": "B", "serial_number": "S1"},
        serial_identity={"vid": "A", "pid": "B", "serial_number": "S2"})
    assert result["stage"] == "serial_identity_changed"


def test_phone_moved_fails_without_motion():
    class FakeQuad:
        def __call__(self, *a, **k):
            class E(Exception):
                pass
            raise E("screen_edge_not_detected")

    result = recovery.attempt_recovery(
        "arm-01", bundle={"deviceId": "arm-01"}, new_session="s2",
        phone_frame_provider=FakeQuad())
    assert result["recovered"] is False and result["stage"] == "phone_moved"


def test_no_anchor_verifier_goes_to_manual():
    """操作员拍板的边界：臂侧锚点未复核（无验证器）= 转人工，不盲目套旧参数。"""
    result = recovery.attempt_recovery(
        "arm-01", bundle={"deviceId": "arm-01"}, new_session="s2")
    assert result["recovered"] is False and result["stage"] == "anchor_unverified"


def test_anchor_mismatch_requires_recalibration():
    result = recovery.attempt_recovery(
        "arm-01", bundle={"deviceId": "arm-01"}, new_session="s2",
        arm_anchor_verifier=lambda device_id: False)
    assert result["stage"] == "anchor_mismatch"
    assert "重新校准" in result["reason"]


def test_full_pass_rebinds_and_restores(tmp_path, monkeypatch):
    """复核全过：bundle 转正 motion_ready+新会话；证书重签（版本+1、
    绑定更新、restored_at 留痕）；ui_targets 重绑被调用。"""
    from drivers import calibration_bundle as cb
    from drivers import gesture_program as gp
    from runtime import automatic_gestures as ag
    from tests.test_automatic_gesture_api import _program_fixture, _run_fixture, \
        _seed_calibration

    data_root = tmp_path / "data"  # 与 _seed_calibration 内部的 data/ 约定一致
    bundle = _seed_calibration(tmp_path, automatic=True)
    # 先取 frame（load 会校验与当前 bundle 会话一致，改会话后再取得 None）
    frame = gp.load_coordinate_frame("arm-01", data_root)
    # 发布哈希=原始坐标桥哈希（_current_binding 单次转换自该帧）。
    # 真实时序（OCR 审查 P1）：证书发布于旧会话（session-01），发布后才拨新会话
    bundle["daemonSession"] = "session-01"
    bundle["status"] = cb.MOTION_READY_STATUS
    cb.save_bundle(bundle, data_root)
    # 发布一份有效证书（在"旧会话"档案上；哈希/绑定=真实档案，恢复比对才一致）
    from drivers import screen_gestures as sg
    run = {"run_id": "run-restore", "state": "passed", "gesture_rules_version":
           "gesture-rules.v1",
           "coordinate_frame_hash": frame["coordinate_frame_hash"],
           "input_binding": sg.binding_snapshot(bundle),
           "action_results": [
               {"gesture_id": "scroll_up", "state": "valid",
                "program": _program_fixture("scroll_up"),
                "validation_status": "three_passes", "trial_ids": ["1", "2", "3"],
                "controller_status": "completed", "contact_status": "continuous",
                "effect_status": "verified", "reason_code": None,
                "evidence_refs": []}]}
    ag.publish_run_results("arm-01", run, data_root=data_root, base_version=0)
    before = ag.load_profile("arm-01", data_root)
    assert before["version"] == 1


    rebinds = []
    result = recovery.attempt_recovery(
        "arm-01", bundle=cb.load_bundle("arm-01", data_root),
        new_session="new-session", data_root=data_root, coordinate_frame=frame,
        arm_anchor_verifier=lambda device_id: True,
        rebind_targets=lambda restored: rebinds.append(restored["daemonSession"]))
    assert result["recovered"] is True and result["stage"] == "done", result
    assert rebinds == ["new-session"]
    restored_bundle = cb.load_bundle("arm-01", data_root)
    assert restored_bundle["status"] == cb.MOTION_READY_STATUS
    assert restored_bundle["daemonSession"] == "new-session"
    profile = ag.load_profile("arm-01", data_root)
    assert profile["version"] == 2
    assert profile["binding"]["daemon_session"] == "new-session"
    assert profile["actions"]["scroll_up"]["restored_at"]
    assert result["certificates"] == 2
