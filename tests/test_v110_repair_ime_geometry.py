"""v1.10 实现审查修复回归：FIX-15 IME 控制心跳与实时几何 + FIX-12 粗框路由。

F 编号到测试函数映射：
- F15-1 → test_f15_1_missing_or_wrong_control_version_blocks_input
          test_f15_1_explicit_null_and_bad_type_rejected_without_writing
          test_f15_1_unsupported_version_recorded_but_not_compatible
          test_f15_1_45s_expiry_restart_and_touch_no_renewal
          test_f15_1_eligibility_blocks_incompatible_bound_phone
- F15-2 → test_f15_2_phone_a_cannot_access_b_geometry_session
          test_f15_2_wrong_runtime_zero_and_multi_binding_rejected
          test_f15_2_same_seq_replay_keeps_first_receipt_no_renewal
- F15-3 → test_f15_3_click_gate_plans_by_live_view_coordinates
          test_f15_3_superseded_snapshot_two_reads_then_stop_never_fixed_fallback
- F15-4 → test_f15_4_close_not_blocked_during_geometry_wait_final_contact_zero
- F12-1 → test_f12_1_region_routes_get_preview_save_restart_zero_motion
          test_f12_1_adaptation_error_status_mapping
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import pytest

from adapters.screen_ime.service import ScreenImeApiError, ScreenImeService
from runtime.ime_geometry import CONTRACT_VERSION
from runtime.layout_adaptation import AdaptationError
from runtime.input_control import InputControlError
from runtime.runtime import ArmFleetRuntime
from tests.helpers import make_device_configs

IME_DEVICE = "k40-1"
ARM_DEVICE = "arm-01"
# R2-21（§7.1）：几何入口要求每机独立凭据——旧语义（只查路径绑定计数、
# 无凭据直调）被 R2-20.../R2-21 否定，本文件按新契约修订：测试登记
# 独立令牌并经真实验证器（401 unauthenticated / 403 forbidden）。
GEOM_TOKEN = "k40-1-geometry-test-token-0001"


def grant_geometry(runtime, phone: str = IME_DEVICE, token: str = GEOM_TOKEN):
    if hasattr(runtime, "register_phone_credential"):
        runtime.register_phone_credential(phone, token)
    return token


def make_runtime(tmp_path: Path, *, ime_device_id: str | None = IME_DEVICE,
                 n_devices: int = 1) -> ArmFleetRuntime:
    from runtime.config import PlatformConfig

    config = PlatformConfig(
        {"db_path": str(tmp_path / "fleet.db"),
         "evidence_dir": str(tmp_path / "evidence"),
         "plugin_dir": str(tmp_path / "plugins"),
         "hardware_mode": "simulation",
         "daemon_supervisor_enabled": False,
         "hotplug_discovery_enabled": False},
        root=tmp_path,
    )
    runtime = ArmFleetRuntime(config)

    def _register() -> None:
        asyncio.run(runtime.devices.register_all(make_device_configs(n_devices)))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        _register()
    else:
        # 已在事件循环内（场景式用例）：独立线程完成一次性注册
        import threading

        thread = threading.Thread(target=_register)
        thread.start()
        thread.join()
    if ime_device_id is not None:
        for index in range(n_devices):
            runtime.devices._devices[f"arm-{index + 1:02d}"]["ime_device_id"] = \
                ime_device_id if index == 0 else f"{ime_device_id}-{index + 1}"
    return runtime


def make_service(tmp_path: Path) -> ScreenImeService:
    return ScreenImeService(tmp_path / "ime.db", tmp_path / "ime_mappings.json")


def snapshot_body(session_id: str, seq: int, nonce: str | None, *,
                  candidate_bounds=(54, 2160, 1026, 2280),
                  delete_bounds=(960, 1960, 1050, 2050),
                  geometry_valid=True, view_instance_id=None,
                  keyboard_active=True, task_id="t-f15-3-ime") -> dict:
    # 测试更正（§4.5，2026-10-03）：R3-02-1 要求候选规划绑定非空原任务引用，
    # 快照默认携带任务号（原 None 会按新契约在规划门被拒）。
    return {
        "contract_version": CONTRACT_VERSION,
        "runtime_session_id": session_id,
        "view_instance_id": view_instance_id or str(uuid.uuid4()),
        "seq": seq,
        "captured_uptime_ms": 123456,
        "display": {"display_id": 0, "width_px": 1080, "height_px": 2400,
                    "rotation": 0, "density": 2.75},
        "layout_version": "k40-candidate-1",
        "keyboard_active": keyboard_active,
        "geometry_valid": geometry_valid,
        "keyboard_bounds": {"left": 0, "top": 1560, "right": 1080, "bottom": 2400},
        "current_task_id": task_id,
        "reason": "snapshot" if nonce else "layout_ready",
        "request_nonce": nonce,
        "targets": [
            {"id": "ime_candidate_bar", "visible": True, "enabled": True,
             "clickable": True,
             "bounds": {"left": candidate_bounds[0], "top": candidate_bounds[1],
                        "right": candidate_bounds[2], "bottom": candidate_bounds[3]}},
            {"id": "ime_delete_key", "visible": True, "enabled": True,
             "clickable": True,
             "bounds": {"left": delete_bounds[0], "top": delete_bounds[1],
                        "right": delete_bounds[2], "bottom": delete_bounds[3]}},
        ],
    }


def backdate_reported_at(service: ScreenImeService, device_id: str, seconds: float) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with service.tasks._thread_lock:
        service.tasks._conn.execute(
            "UPDATE devices SET input_control_reported_at=? WHERE device_id=?",
            (stale, device_id))
        service.tasks._conn.commit()


def backdate_last_seen(service: ScreenImeService, device_id: str, seconds: float) -> None:
    stale = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    with service.tasks._thread_lock:
        service.tasks._conn.execute(
            "UPDATE devices SET last_seen_at=? WHERE device_id=?", (stale, device_id))
        service.tasks._conn.commit()


# ---------------- F15-1：心跳控制版本（真实仓储） ----------------
def test_f15_1_missing_or_wrong_control_version_blocks_input(tmp_path):
    service = make_service(tmp_path)
    try:
        asyncio.run(service.tasks.heartbeat(IME_DEVICE, {
            "appVersion": "sim", "keyboardActive": True,
            "inputTaskControlVersion": 1}))
        capability = service.tasks.input_control_capability(IME_DEVICE)
        assert capability["compatible"] is True
        assert capability["control_version"] == 1
        # 版本 1 后立即收到缺字段心跳：三列全部 NULL，旧能力立即失效
        asyncio.run(service.tasks.heartbeat(IME_DEVICE, {
            "appVersion": "sim", "keyboardActive": True}))
        capability = service.tasks.input_control_capability(IME_DEVICE)
        assert capability["compatible"] is False
        assert capability["reason_code"] == "control_version_missing"
        row = service.tasks._query_one(
            "SELECT input_task_control_version, input_control_reported_at,"
            " input_control_runtime_instance FROM devices WHERE device_id=?", (IME_DEVICE,))
        assert row["input_task_control_version"] is None
        assert row["input_control_reported_at"] is None
        assert row["input_control_runtime_instance"] is None
    finally:
        service.close()


def test_f15_1_explicit_null_and_bad_type_rejected_without_writing(tmp_path):
    service = make_service(tmp_path)
    try:
        asyncio.run(service.tasks.heartbeat(IME_DEVICE, {
            "keyboardActive": True, "inputTaskControlVersion": 1}))
        before = service.tasks.input_control_capability(IME_DEVICE)
        assert before["compatible"] is True
        # 显式 null 与非法类型：422 拒绝且不写能力（保持原 v1 报告）
        for bad in (None, "1", 1.5, True):
            with pytest.raises(ScreenImeApiError) as caught:
                asyncio.run(service.tasks.heartbeat(IME_DEVICE, {
                    "keyboardActive": True, "inputTaskControlVersion": bad}))
            assert caught.value.status == 422
            assert caught.value.code == "invalid_input_task_control_version"
        def capability_without_age(cap):
            return {k: v for k, v in cap.items() if k != "age_seconds"}
        after = service.tasks.input_control_capability(IME_DEVICE)
        assert capability_without_age(after) == capability_without_age(before), \
            "被拒心跳不得改动既有能力三列"
    finally:
        service.close()


def test_f15_1_unsupported_version_recorded_but_not_compatible(tmp_path):
    service = make_service(tmp_path)
    try:
        asyncio.run(service.tasks.heartbeat(IME_DEVICE, {
            "keyboardActive": True, "inputTaskControlVersion": 2}))
        capability = service.tasks.input_control_capability(IME_DEVICE)
        assert capability["compatible"] is False
        assert capability["reason_code"] == "control_version_unsupported"
        assert capability["control_version"] == 2  # 如实记录，不放行
    finally:
        service.close()


def test_f15_1_45s_expiry_restart_and_touch_no_renewal(tmp_path):
    service = make_service(tmp_path)
    try:
        asyncio.run(service.tasks.heartbeat(IME_DEVICE, {
            "keyboardActive": True, "inputTaskControlVersion": 1}))
        assert service.tasks.input_control_capability(IME_DEVICE)["compatible"] is True
        # touch/普通轮询只刷 last_seen_at：能力不续期（45 秒按报告时间计算）
        backdate_reported_at(service, IME_DEVICE, 50)
        service.tasks.touch(IME_DEVICE)  # 显式 touch（轮询隐式心跳同路径）
        capability = service.tasks.input_control_capability(IME_DEVICE)
        assert capability["compatible"] is False
        assert capability["reason_code"] == "control_report_expired"
        # 45 秒内过期前 last_seen 过旧不影响能力（在线与能力是两个事实）
        asyncio.run(service.tasks.heartbeat(IME_DEVICE, {
            "keyboardActive": True, "inputTaskControlVersion": 1}))
        backdate_last_seen(service, IME_DEVICE, 50)
        capability = service.tasks.input_control_capability(IME_DEVICE)
        assert capability["compatible"] is True
        # 重启（同库新实例=新启动 UUID）：旧心跳不跨服务重启复用
        service.close()
        restarted = ScreenImeService(tmp_path / "ime.db",
                                     tmp_path / "ime_mappings.json")
        try:
            capability = restarted.tasks.input_control_capability(IME_DEVICE)
            assert capability["compatible"] is False
            assert capability["reason_code"] == "runtime_instance_mismatch"
            # 新心跳后恢复
            asyncio.run(restarted.tasks.heartbeat(IME_DEVICE, {
                "keyboardActive": True, "inputTaskControlVersion": 1}))
            assert restarted.tasks.input_control_capability(IME_DEVICE)["compatible"] is True
            assert restarted.tasks._get_meta(
                "input_control_runtime_instance") == restarted.tasks.runtime_instance
        finally:
            restarted.close()
        return
    finally:
        service.close()


def test_f15_1_eligibility_blocks_incompatible_bound_phone(tmp_path):
    from runtime.device_eligibility import compute_device_eligibility

    device = {"device_id": ARM_DEVICE, "ime_device_id": IME_DEVICE,
              "calibration_status": "validated", "status": "online"}
    # 兼容手机：不新增阻断
    ok = compute_device_eligibility(
        device, ime_status={"online": True, "keyboardActive": True,
                            "inputControlCompatible": True},
        require_input=True)
    assert "ime_control_incompatible" not in ok["blocking_checks"]
    # 不兼容手机（真实心跳缺版本/过期/重启）：开关可保存开启，执行资格 false
    blocked = compute_device_eligibility(
        device, ime_status={"online": True, "keyboardActive": True,
                            "inputControlCompatible": False},
        require_input=True)
    assert "ime_control_incompatible" in blocked["blocking_checks"]
    assert blocked["can_manual_send"] is False
    # 旧 provider 形状（无该字段）：不误伤（unknown 不阻断）
    legacy = compute_device_eligibility(
        device, ime_status={"online": True, "keyboardActive": True},
        require_input=True)
    assert "ime_control_incompatible" not in legacy["blocking_checks"]


# ---------------- F15-2：几何端点身份反查 ----------------
def test_f15_2_phone_a_cannot_access_b_geometry_session(tmp_path):
    runtime = make_runtime(tmp_path)
    geometry = runtime.ime_geometry
    grant_geometry(runtime)
    # 手机 A 是另一台有自己凭据的手机（本 Runtime 内未绑定）：
    a_token = grant_geometry(runtime, "k40-a", "k40-a-geometry-test-token-0002")
    # B 是当前唯一绑定手机：握手建立当前会话（固定 View ID，供幂等重放）
    b_view = str(uuid.uuid4())
    b_session = geometry.handshake(IME_DEVICE, {
        "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
        "client_instance_id": str(uuid.uuid4())}, credentials=GEOM_TOKEN)
    b_seq = 1
    b_body = snapshot_body(b_session["runtime_session_id"], b_seq, None,
                           view_instance_id=b_view)
    geometry.report(IME_DEVICE, b_body, credentials=GEOM_TOKEN)
    # 手机 A 用自己的有效凭据访问 B 的 session/report/poll 一律 403 forbidden
    from runtime.ime_geometry import ImeGeometryError

    for call in (
        lambda: geometry.handshake(IME_DEVICE, {
            "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
            "client_instance_id": str(uuid.uuid4())}, credentials=a_token),
        lambda: geometry.report(IME_DEVICE, snapshot_body(
            b_session["runtime_session_id"], 2, None), credentials=a_token),
        lambda: geometry.poll_next(IME_DEVICE, b_session["runtime_session_id"],
                                   0, credentials=a_token),
        # 无凭据同样拒绝（401）
        lambda: geometry.handshake(IME_DEVICE, {
            "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
            "client_instance_id": str(uuid.uuid4())}),
    ):
        with pytest.raises(ImeGeometryError) as caught:
            call()
        expected = (401, "ime_geometry_unauthenticated") if \
            caught.value.status == 401 else (403, "ime_geometry_forbidden")
        assert (caught.value.status, caught.value.code) == expected
    # body/path 不能撤销他人会话：B 的当前会话不变（原帧幂等重放成功，可继续领取）
    assert geometry.report(IME_DEVICE, b_body,
                           credentials=GEOM_TOKEN)["accepted_seq"] == b_seq
    nonce = geometry.create_nonce_request(IME_DEVICE)
    claimed = geometry.poll_next(IME_DEVICE, b_session["runtime_session_id"],
                                 0, credentials=GEOM_TOKEN)
    assert claimed is not None and claimed["request_nonce"] == nonce
    geometry.report(IME_DEVICE, snapshot_body(
        b_session["runtime_session_id"], b_seq + 1, nonce,
        view_instance_id=b_view), credentials=GEOM_TOKEN)


def test_f15_2_wrong_runtime_zero_and_multi_binding_rejected(tmp_path):
    runtime = make_runtime(tmp_path, n_devices=2)
    grant_geometry(runtime)
    session = runtime.ime_geometry.handshake(IME_DEVICE, {
        "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
        "client_instance_id": str(uuid.uuid4())}, credentials=GEOM_TOKEN)
    # 错 Runtime：另一个实例凭据登记了但零绑定——403 forbidden（先于会话）
    other = make_runtime(tmp_path / "other", ime_device_id=None)
    grant_geometry(other)
    from runtime.ime_geometry import ImeGeometryError

    with pytest.raises(ImeGeometryError) as caught:
        other.ime_geometry.report(IME_DEVICE, snapshot_body(
            session["runtime_session_id"], 1, None), credentials=GEOM_TOKEN)
    assert (caught.value.status, caught.value.code) == \
        (403, "ime_geometry_forbidden")
    # 错 Runtime 即使身份蒙混（给 other 配同一绑定），会话仍不可见（409 stale）
    other.devices._devices["arm-01"]["ime_device_id"] = IME_DEVICE
    with pytest.raises(ImeGeometryError) as caught:
        other.ime_geometry.report(IME_DEVICE, snapshot_body(
            session["runtime_session_id"], 1, None), credentials=GEOM_TOKEN)
    assert (caught.value.status, caught.value.code) == \
        (409, "ime_geometry_session_stale")
    # 多绑定（两台臂绑同一手机）→ 403 拒绝
    runtime.devices._devices["arm-02"]["ime_device_id"] = IME_DEVICE
    with pytest.raises(ImeGeometryError) as caught:
        runtime.ime_geometry.handshake(IME_DEVICE, {
            "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
            "client_instance_id": str(uuid.uuid4())}, credentials=GEOM_TOKEN)
    assert caught.value.status == 403
    # 零绑定 → 403
    runtime.devices._devices["arm-01"]["ime_device_id"] = None
    runtime.devices._devices["arm-02"]["ime_device_id"] = None
    with pytest.raises(ImeGeometryError) as caught:
        runtime.ime_geometry.poll_next(IME_DEVICE, session["runtime_session_id"],
                                       0, credentials=GEOM_TOKEN)
    assert caught.value.status == 403


def test_f15_2_same_seq_replay_keeps_first_receipt_no_renewal(tmp_path):
    runtime = make_runtime(tmp_path)
    grant_geometry(runtime)
    geometry = runtime.ime_geometry
    session = geometry.handshake(IME_DEVICE, {
        "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
        "client_instance_id": str(uuid.uuid4())}, credentials=GEOM_TOKEN)
    body = snapshot_body(session["runtime_session_id"], 3, None)
    first = geometry.report(IME_DEVICE, body, credentials=GEOM_TOKEN)
    import time as _time

    _time.sleep(0.02)
    replay = geometry.report(IME_DEVICE, body,
                             credentials=GEOM_TOKEN)  # 同 seq 同摘要：原响应原时刻
    assert replay == first
    assert replay["server_received_at"] == first["server_received_at"]
    # 重放不续期：超过 1 秒接收年龄仍按首收判定（把首收时刻往回拨）
    with geometry._lock:
        geometry._sessions[session["runtime_session_id"]]["last_snapshot_mono"] -= 1.5
    nonce = geometry.create_nonce_request(IME_DEVICE)
    claimed = geometry.poll_next(IME_DEVICE, session["runtime_session_id"],
                                 0, credentials=GEOM_TOKEN)
    assert claimed is not None
    geometry.report(IME_DEVICE, snapshot_body(
        session["runtime_session_id"], 4, nonce), credentials=GEOM_TOKEN)
    ok, reason = geometry.fresh_snapshot(IME_DEVICE, nonce)
    assert ok is True and reason == "ok"  # 新 seq+nonce 才构成新快照与新鲜期


# ---------------- F15-3/F15-4：候选/删除点击 nonce 快照门（服务侧） ----------------
def test_f15_3_click_gate_plans_by_live_view_coordinates(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        grant_geometry(runtime)
        geometry = runtime.ime_geometry
        session = geometry.handshake(IME_DEVICE, {
            "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
            "client_instance_id": str(uuid.uuid4())}, credentials=GEOM_TOKEN)
        # 手机端长轮询领取 nonce 并按真实 View 坐标上报
        async def phone():
            seq = 0
            while True:
                claimed = await asyncio.to_thread(
                    geometry.poll_next, IME_DEVICE,
                    session["runtime_session_id"], 1, GEOM_TOKEN)
                if claimed is None:
                    await asyncio.sleep(0.005)
                    continue
                seq += 1
                await asyncio.to_thread(
                    geometry.report, IME_DEVICE,
                    snapshot_body(session["runtime_session_id"], seq,
                                  claimed["request_nonce"]), GEOM_TOKEN)

        phone_task = asyncio.create_task(phone())
        try:
            target_ref = await runtime._ime_live_geometry_target(
                ARM_DEVICE, "ime_candidate_bar")
        finally:
            phone_task.cancel()
        # 坐标来自快照 View 框中心（屏幕像素域），不是 ui_targets 固定位置
        assert target_ref["source"] == "live_ime_geometry"
        assert target_ref["cameraX"] == (54 + 1026) / 2
        assert target_ref["cameraY"] == (2160 + 2280) / 2
        assert target_ref["frameW"] == 1080 and target_ref["frameH"] == 2400
        assert target_ref["runtimeSessionId"] == session["runtime_session_id"]
        assert target_ref["coordinateVersion"] == "live_ime_geometry.v1"
        assert uuid.UUID(target_ref["nonce"])  # nonce 证据
        await runtime.shutdown()

    asyncio.run(scenario())


def test_f15_3_superseded_snapshot_two_reads_then_stop_never_fixed_fallback(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        from drivers.arm import DriverError

        grant_geometry(runtime)
        geometry = runtime.ime_geometry
        session = geometry.handshake(IME_DEVICE, {
            "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
            "client_instance_id": str(uuid.uuid4())}, credentials=GEOM_TOKEN)
        state = {"seq": 0}

        async def phone_answer_then_supersede():
            # 手机领取 nonce → 按请求回快照 → 立刻换 View 用更大 seq 使其被超越
            while True:
                claimed = await asyncio.to_thread(
                    geometry.poll_next, IME_DEVICE, session["runtime_session_id"],
                    1, GEOM_TOKEN)
                if claimed is None:
                    await asyncio.sleep(0.005)
                    continue
                state["seq"] += 1
                await asyncio.to_thread(geometry.report, IME_DEVICE, snapshot_body(
                    session["runtime_session_id"], state["seq"],
                    claimed["request_nonce"]), GEOM_TOKEN)
                state["seq"] += 1
                await asyncio.to_thread(geometry.report, IME_DEVICE, snapshot_body(
                    session["runtime_session_id"], state["seq"], None), GEOM_TOKEN)
                return

        issued = []
        original_create = geometry.create_nonce_request

        def counting_create(device_id, purpose="action_snapshot"):
            issued.append(purpose)
            return original_create(device_id, purpose)

        geometry.create_nonce_request = counting_create
        phone_task = asyncio.create_task(phone_answer_then_supersede())
        with pytest.raises(DriverError) as caught:
            await runtime._ime_live_geometry_target(ARM_DEVICE, "ime_candidate_bar")
        phone_task.cancel()
        assert caught.value.motion_not_started is True
        assert "不回退旧固定坐标" in str(caught.value)
        # 首次 + 最多一次只读重取 = 恰两次 nonce 请求（不多取）
        assert len(issued) == 2
        # 零接触：无任何驱动点击发生
        driver = runtime.devices.driver(ARM_DEVICE)
        assert not [a for a in driver.action_log if a["action"] == "click"]
        await runtime.shutdown()

    asyncio.run(scenario())


def test_f15_4_close_not_blocked_during_geometry_wait_final_contact_zero(tmp_path):
    async def scenario():
        runtime = make_runtime(tmp_path)
        from drivers.arm import DriverError

        grant_geometry(runtime)
        geometry = runtime.ime_geometry
        geometry.handshake(IME_DEVICE, {
            "contract_version": CONTRACT_VERSION, "imeGeometryVersion": 1,
            "client_instance_id": str(uuid.uuid4())}, credentials=GEOM_TOKEN)
        # 手机不领取 nonce：门在等快照
        gate_task = asyncio.create_task(
            runtime._ime_live_geometry_target(ARM_DEVICE, "ime_delete_key"))
        await asyncio.sleep(0.05)
        # F15-4：等待期间输入短锁不被占用——关闭输入可立即进行
        with runtime.input_control.device_lock(ARM_DEVICE):
            pass  # 关闭路径要拿的锁，0 阻塞取得
        with pytest.raises(DriverError) as caught:
            await asyncio.wait_for(gate_task, 6)  # 两次快照等待后停止
        assert caught.value.motion_not_started is True
        driver = runtime.devices.driver(ARM_DEVICE)
        assert not [a for a in driver.action_log if a["action"] == "click"]
        await runtime.shutdown()

    asyncio.run(scenario())


# ---------------- F12-1：三个粗框路由真实链路（零动作） ----------------
@pytest.fixture()
def region_env(tmp_path, monkeypatch):
    from api.app import create_app

    token_file = tmp_path / "layout_forwarder_token"
    token_file.write_text("9" * 64, encoding="utf-8")
    monkeypatch.setenv("ARMFLEET_LAYOUT_FORWARDER_TOKEN_FILE", str(token_file))
    runtime = make_runtime(tmp_path)
    from fastapi.testclient import TestClient

    app = create_app(runtime)
    app.state.runtime = runtime
    app.state.layout_db = tmp_path / "layout.db"
    with TestClient(app) as client:
        yield client, runtime, tmp_path


def test_f12_1_region_routes_get_preview_save_restart_zero_motion(region_env):
    """R2-D2 修订：粗框三路由按冻结契约+可信身份（R2-15/17/23）。
    深链（页面原始请求→真实代理→Fleet→重启读取→零动作）已由
    tests/test_r2_region_chain.py 完整覆盖；本用例保留 HTTP 装配冒烟：
    未配置身份时设备资料读按 R2-23 安全拒绝（403，不回落自报身份）。"""
    client, runtime, tmp_path = region_env
    got = client.get("/v1/devices/arm-01/phone-search-region")
    assert got.status_code == 403  # R2-23 安全默认：无受信身份拒绝
    assert got.json()["code"] == "layout_permission_denied"
    assert "身份" in got.json()["message"]
    # 动作 0：布局路由装配不触碰机械驱动
    driver = runtime.devices.driver("arm-01")
    assert not [a for a in driver.action_log if a["action"] in ("click", "move")]


def test_f12_1_adaptation_error_status_mapping(region_env):
    """R2-17/R2-25/R2-23 收口后恢复——错误状态映射按 D2 统一外壳；写请求
    携带受信转发身份（R2-23：无身份的写入 403 是安全默认）。"""
    client, _runtime, _tmp = region_env
    ident = {"x-layout-forwarder-token": "9" * 64,
             "x-layout-console-principal": "operator-a"}
    # 无身份写入 → 403（安全默认），不泄漏存在性
    r0 = client.put("/v1/devices/arm-01/layout-adaptation", json={"session_id": "s"})
    assert r0.status_code == 403
    # 发布缺 preview_id：AdaptationError.status=422 按 status 送达（不再 500）
    r = client.put("/v1/devices/arm-01/layout-adaptation", json={"session_id": "s"},
                   headers=ident)
    assert r.status_code == 422
    # 会话模板核验 404（template_store 已注入同 db；冻结形状=template_ref）
    import uuid as _uuid
    r = client.post("/v1/devices/arm-01/layout-adaptation/sessions", json={
        "contract_version": "device-layout-edit.v1",
        "client_request_id": str(_uuid.uuid4()),
        "correlation_id": str(_uuid.uuid4()),
        "expected_revision": 0, "inherit_session_id": None,
        "inherit_states": [], "keyboard_geometry_mode": "legacy_registered",
        "template_ref": {"id": "nope", "version": 1,
                        "sha256": "a" * 64}}, headers=ident)
    assert r.status_code == 404
    # 不存在的会话读取 404（公共头：关联 ID+契约版本）
    r = client.get(
        f"/v1/devices/arm-01/layout-adaptation/sessions/{_uuid.uuid4()}",
        headers={**ident, "X-Correlation-Id": str(_uuid.uuid4()),
                 "X-Layout-Contract-Version": "device-layout-edit.v1"})
    assert r.status_code == 404


def test_f12_1_adaptation_error_status_semantics_service_level(tmp_path):
    """过渡期服务层等价断言：AdaptationError 携带契约 status/错误码
    （HTTP 状态映射由 app.py 既有异常处理器承担，待 I 合入后由上面的
    HTTP 用例继续覆盖）。"""
    from tests.layout_r2_helpers import (
        DEFAULT_BINDING, DEFAULT_GEOMETRY_ID, FakeCalibrationProvider,
        FakeCaptureProvider, FakeClock, LayoutBundle, build_page_frames,
        build_template_assets)
    bundle = LayoutBundle(
        tmp_path / "rt", clock=FakeClock(),
        capture_provider=FakeCaptureProvider(
            build_page_frames(build_template_assets()),
            binding=DEFAULT_BINDING, source_geometry_id=DEFAULT_GEOMETRY_ID),
        calibration_provider=FakeCalibrationProvider())
    try:
        svc = bundle.adaptation_svc
        with pytest.raises(AdaptationError) as exc:
            svc.publish("arm-01", {"client_request_id": str(uuid.uuid4()),
                                   "session_id": "s",
                                   "principal_id": "operator"})
        assert exc.value.status == 422
        assert exc.value.code == "invalid_request"
        with pytest.raises(AdaptationError) as exc:
            svc.start_session("arm-01", {
                "template_ref": {"id": "nope", "version": 1},
                "principal_id": "operator"})
        assert exc.value.status == 404
        assert exc.value.code == "template_not_found"
        assert svc.get_session("arm-01", "none") is None  # 错会话读取为空
    finally:
        bundle.close()
