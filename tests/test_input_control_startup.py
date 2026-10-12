"""P07 验收（2026-10-03 规格 §4.8.1/§4.8.6-4）：启动锁序与输入状态对账。

- 启动取得 DB+数据根 fcntl 排他锁，第二实例启动失败（I48 前置）；
- 启动对账：prepared 设置意图按文件摘要落定（applied/aborted）；
- 撤销边界扫描：active owner ≤ 边界在启动时被撤销（I38/I41 链）；
- Fleet 挂载仓储注入 ime_control_hooks（撤销后 typing 409，I48 语义）；
- 对账窗口内输入暂不可用（startup_reconciling）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime import input_settings as iset
from runtime.input_control import (
    InputControl, InputControlError, record_id_for_owner,
)
from runtime.store import Store
from tests.helpers import make_device_configs, make_runtime


def test_i48_second_instance_fails_on_same_resources(tmp_path):
    """资源排他锁：同 DB+数据根的第二 runtime startup 必须失败。"""
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime

    def build(tag):
        config = PlatformConfig(
            {"db_path": str(tmp_path / f"fleet-{tag}.db"),
             "evidence_dir": str(tmp_path / f"ev-{tag}"),
             "plugin_dir": str(tmp_path / f"pl-{tag}"),
             "hardware_mode": "simulation"},
            root=tmp_path)
        return ArmFleetRuntime(config)

    first = build("a")
    asyncio.run(first.startup(device_configs=make_device_configs(1)))
    try:
        second = build("b")
        with pytest.raises(InputControlError, match="资源锁被占用"):
            asyncio.run(second.startup(device_configs=make_device_configs(1)))
    finally:
        asyncio.run(first.shutdown())


def test_startup_reconciles_prepared_intent_and_revokes_stale_owners(tmp_path):
    """崩溃窗口对账 + 撤销边界扫描在 startup 内完成（§4.8.6-4）。"""
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime

    # 预置：直接写进 runtime 将使用的 fleet2.db（启动扫描才能扫到）
    store = Store(tmp_path / "fleet2.db")
    control = InputControl(store, tmp_path / "fleet2.db")
    from drivers.fake_arm import FakeArmDriver
    FakeArmDriver  # noqa
    iset.update_settings("arm-01", data_root=tmp_path / "data", enabled=True,
                         ime_device_id="k40-1", expected_revision=0,
                         operator_id="a", reason="开",
                         client_request_id="seed", calibration_ready=True)
    owner = control.build_owner(
        owner_type="fleet_task", owner_ref="legacy-1", device_id="arm-01",
        ime_device_id="k40-1", settings_revision=1, binding_revision=0,
        payload={"t": 1})
    control.admit_owner(owner)
    # 崩溃窗口：prepared 意图已插入、文件未落盘（保持 before=开启 r1）
    request = {"enabled": False, "ime_device_id": None, "expected_revision": 1,
               "operator_id": "a", "reason": "关",
               "client_request_id": "close-crash"}
    prepared = iset.prepare_update(
        "arm-01", data_root=tmp_path / "data", enabled=False,
        ime_device_id=None, expected_revision=1, operator_id="a", reason="关",
        client_request_id="close-crash", calibration_ready=False)
    entry = prepared["stored"]["requests"]["close-crash"]
    intent = control.build_settings_write(
        device_id="arm-01", client_request_id="close-crash",
        call_context={"origin": "human_put", "operator_id": "a", "role": "operator",
                      "operation_id": "input_settings_update"},
        normalized_request=request,
        before=entry["before"], after=entry["after"],
        proposed_file=prepared["stored"],
        before_file_digest=prepared["before_file_digest"])
    control.insert_settings_write(intent)
    stored = iset.load_settings("arm-01", data_root=tmp_path / "data")
    assert stored["enabled"] is True, "崩溃窗口：文件必须仍在 before 状态"

    # startup：对账该意图（保持 applied）+ 撤销边界扫描
    config = PlatformConfig(
        {"db_path": str(tmp_path / "fleet2.db"),
         "evidence_dir": str(tmp_path / "ev"),
         "plugin_dir": str(tmp_path / "pl"),
         "hardware_mode": "simulation"},
        root=tmp_path)
    runtime = ArmFleetRuntime(config)
    asyncio.run(runtime.startup(device_configs=make_device_configs(1)))
    try:
        # startup 对账：before+关闭 → 完成这次已授权的关闭（r2，边界=2）
        stored = iset.load_settings("arm-01", data_root=tmp_path / "data")
        assert stored["enabled"] is False and stored["revoked_through_revision"] == 2
        assert runtime.input_startup_reconciling is False
        # 撤销边界扫描把 owner r1 撤销（revoked_at_revision=2）
        runtime.input_control.ensure_guard("arm-01")
        owners = [r for r in store.input_records_by_device("arm-01", "owner")]
        revoked = [r for r in owners if r["revocation"] == "revoked"]
        assert revoked, "启动扫描必须撤销边界之下的旧 owner"
        assert revoked[0]["revoked_at_revision"] == 2
    finally:
        asyncio.run(runtime.shutdown())


def test_startup_reconciling_blocks_input_switch(tmp_path, monkeypatch):
    """对账窗口内 input_switch_allowed=False（输入暂不可用）。"""
    runtime = make_runtime(tmp_path)
    # 不走 startup：手工模拟对账窗口
    runtime.input_startup_reconciling = True
    try:
        assert runtime.input_switch_allowed("arm-01") is False
        runtime.input_startup_reconciling = False
        assert runtime.input_switch_allowed("arm-01") is False  # 无开关文件仍关
    finally:
        asyncio.run(runtime.shutdown())


def test_fleet_mounted_service_receives_hooks(tmp_path):
    """startup 装配后 Fleet 挂载仓储拿到控制钩子：撤销手机 typing 409。"""
    from adapters.screen_ime.service import (
        ImeTaskRepository, ScreenImeApiError, ScreenImeService,
    )
    from copy import deepcopy
    from tests.helpers import PROBE_MANIFEST
    manifest = deepcopy(PROBE_MANIFEST)
    manifest["task_types"][0].update(
        task_type="declared_input", requires_input=True,
        requires_keyboard_active=False)
    manifest["capabilities"] += ["input.type_text", "input.ime_create", "arm.click"]
    manifest["resources"]["input"] = True
    runtime = make_runtime(tmp_path, manifest=manifest)
    asyncio.run(runtime.startup(device_configs=make_device_configs(1)))
    from tests.helpers import enable_input_switch
    enable_input_switch(runtime, tmp_path)
    # 测试更正（§4.5，2026-10-03）：R3-01 最终认领核验运行时资格——夹具补
    # 健康手机状态与已校准在线设备（与 G1 夹具同形）。
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": "k40-test", "online": True, "keyboardActive": True,
         "inputControlCompatible": True}]
    runtime.devices._devices["arm-01"].update({
        "status": "ready", "calibration_status": "validated"})
    try:
        runtime.submit_task(
            {"task_type": "declared_input", "device_id": "arm-01",
             "task_id": "fleet-9", "idempotency_key": "fleet-9",
             "correlation_id": "c", "expires_at": None, "contract_version": "v1",
             "payload": {"steps": [{"is_action": True}]}},
            source="console", context={"operator_id": "测试", "reason": "P07"})
        service = ScreenImeService(tmp_path / "ime.db",
                                   control_hooks=runtime.ime_control_hooks_fleet())
        # 使手机 k40-test 反查唯一设备 arm-01（declared_input runtime 的绑定）
        def binding(device_id):
            return {"ime_device_id": "k40-test"}
        runtime.devices.input_binding = binding
        repo = service.tasks
        repo.touch("k40-test")
        with repo._thread_lock:
            repo._conn.execute(
                "UPDATE devices SET keyboard_active=1 WHERE device_id='k40-test'")
            repo._conn.commit()
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(repo.create({
                "taskId": "fleet-9-ime", "deviceId": "k40-test",
                "targetUser": "conv", "content": "hi",
                "typingSpeed": "random", "source": "api"}))
            granted = loop.run_until_complete(repo.update_status("fleet-9-ime", {
                "deviceId": "k40-test", "status": "displayed",
                "typedCharacterCount": 0}))
            assert granted["status"] == "displayed"
            granted = loop.run_until_complete(repo.update_status("fleet-9-ime", {
                "deviceId": "k40-test", "status": "typing",
                "typedCharacterCount": 0}))
            assert granted["status"] == "typing"
            # 关闭开关（推进撤销边界）→ 手机再报 typing：409 revoked
            iset.update_settings("arm-01", data_root=tmp_path / "data",
                                 enabled=False, ime_device_id=None,
                                 expected_revision=1, operator_id="a", reason="关",
                                 client_request_id="close-1", calibration_ready=False)
            with pytest.raises(ScreenImeApiError) as caught:
                loop.run_until_complete(repo.update_status("fleet-9-ime", {
                    "deviceId": "k40-test", "status": "typing",
                    "typedCharacterCount": 0}))
            assert caught.value.code == "input_authorization_revoked"
        finally:
            loop.close()
    finally:
        asyncio.run(runtime.shutdown())
