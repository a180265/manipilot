"""v1.10 实现审查修复 FIX-02 回归（docs/2026-10-04-v1.10实现审查修复实施单）。

覆盖映射（F 编号 → 测试函数）：
- F02-1 关通知+2s 扫描恰一 cancel；重启仍扫不重复建命令
  → test_f02_1_periodic_scan_issues_exactly_one_cancel_and_survives_restart
- F02-2 正文已交付、displayed 未回报时关闭保持 cancel_requested；迟到 displayed
  不复活；原手机 ack 可、新手机/错 command/冒认 owner 拒绝
  → test_f02_2_delivered_close_keeps_cancel_requested_and_ack_identity
- F02-3 正文响应丢失后 next 不再交付；typing 响应丢失后不能再次开始；
  长轮询关闭后不返回过期正文；挂载缺注入时业务输入 503
  → test_f02_3_body_never_redelivered_and_typing_gate_409
- F02-4 IME 事务原子性（cancel/ack 故障注入）；待取消计数投影一致；
  已认领开始而 ack=0 仍有 editor_review_required；旧任务取消不阻断他设备
  → test_f02_4a_ime_cancel_ack_transactions_are_atomic
  → test_f02_4b_pending_cancellation_projection_matches_records
  → test_f02_4c_orphan_cancel_does_not_block_other_device
- 装配：attach_runtime 注入 + 挂载标记 + 工作器启停（关机序）
  → test_f02_assembly_attaches_service_and_worker_lifecycle
- 工作器永不重叠
  → test_worker_never_overlaps

隔离（修复单 §3.1）：临时 config/data_root/Fleet DB/IME DB、fake 时钟驱动
工作器周期、探针插件清单；不连现场、不启动真实服务、不操作硬件。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from adapters.screen_ime.service import (
    ImeCancellationWorker,
    ImeTaskRepository,
    ScreenImeApiError,
    ScreenImeService,
)
from tests.helpers import (
    PROBE_MANIFEST, make_device_configs, make_runtime,
)


class FakeClock:
    """可手动推进的时钟（工作器周期扫描用 fake 时钟）。"""

    def __init__(self) -> None:
        self._now = 0.0

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _manifest():
    manifest = deepcopy(PROBE_MANIFEST)
    manifest["task_types"][0].update(
        task_type="declared_input", requires_input=True,
        requires_keyboard_active=False)
    manifest["capabilities"] += ["input.type_text", "input.ime_create", "arm.click"]
    manifest["resources"]["input"] = True
    return manifest


def _enable_switch(runtime, tmp_path: Path, device_id: str, ime_device_id: str):
    """按设备写「允许输入与发送」选择（绑定各自手机，不共用默认值）。"""
    import uuid

    from runtime import input_settings as iset

    try:
        current = iset.load_settings(device_id, data_root=tmp_path / "data")
        revision = int(current.get("revision") or 0)
    except iset.InputSettingsError:
        revision = 0
    return iset.update_settings(
        device_id, data_root=tmp_path / "data", enabled=True,
        ime_device_id=ime_device_id, expected_revision=revision,
        operator_id="操作员", reason="测试允许",
        client_request_id=f"allow-{uuid.uuid4().hex[:8]}", calibration_ready=True)


async def _build_runtime(tmp_path: Path, n_devices: int = 2):
    runtime = make_runtime(tmp_path, manifest=_manifest())
    await runtime.devices.register_all(make_device_configs(n_devices))
    _enable_switch(runtime, tmp_path, "arm-01", "k40-test")
    if n_devices > 1:
        _enable_switch(runtime, tmp_path, "arm-02", "k40-2")

    def binding(device_id):
        # k40-test 唯一归属 arm-01；k40-2 归属 arm-02（若在）
        return {"ime_device_id": "k40-test" if device_id == "arm-01"
                else ("k40-2" if device_id == "arm-02" else None)}
    runtime.devices.input_binding = binding
    # 测试更正（§4.5，2026-10-03）：R3-01 起最终认领在设备短锁内核对运行时
    # 资格（eligibility）——夹具须给出健康设备与手机状态（与 G1 夹具同形）。
    runtime.devices._ime_status_provider = lambda: [
        {"deviceId": ime, "online": True, "keyboardActive": True,
         "inputControlCompatible": True}
        for ime in (("k40-test", "k40-2") if n_devices > 1 else ("k40-test",))]
    runtime.devices._devices["arm-01"].update({
        "status": "ready", "calibration_status": "validated"})
    if n_devices > 1:
        runtime.devices._devices["arm-02"].update({
            "status": "ready", "calibration_status": "validated"})
    return runtime


def _runtime_lifecycle(tmp_path: Path, n_devices: int = 2):
    return asyncio.run(_build_runtime(tmp_path, n_devices))


def _submit_input_task(runtime, task_id: str, device_id: str = "arm-01"):
    return runtime.submit_task(
        {"task_type": "declared_input", "device_id": device_id,
         "task_id": task_id, "idempotency_key": task_id,
         "correlation_id": "c", "expires_at": None, "contract_version": "v1",
         "payload": {"steps": [{"is_action": True}]}},
        source="console",
        context={"operator_id": "测试", "reason": "FIX-02"})


def _seed_device(repo: ImeTaskRepository, device_id: str = "k40-test") -> None:
    repo.touch(device_id)
    with repo._thread_lock:
        repo._conn.execute(
            "UPDATE devices SET keyboard_active=1 WHERE device_id=?", (device_id,))
        repo._conn.commit()


def _close_switch(runtime, tmp_path: Path, request_id: str,
                  device_id: str = "arm-01"):
    """A-开关关闭：推进撤销边界（先持久撤销，再异步处理旧任务取消）。"""
    from runtime import input_settings as iset

    stored = iset.load_settings(device_id, data_root=tmp_path / "data")
    return iset.update_settings(
        device_id, data_root=tmp_path / "data", enabled=False, ime_device_id=None,
        expected_revision=int(stored["revision"]), operator_id="测试",
        reason="关闭输入", client_request_id=request_id, calibration_ready=False)


def _cancel_commands(repo: ImeTaskRepository, task_id: str) -> list:
    with repo._thread_lock:
        return repo._conn.execute(
            "SELECT * FROM control_commands WHERE task_id=? AND command='cancel'"
            " ORDER BY id ASC", (task_id,)).fetchall()


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _Env:
    """单事件循环仓储环境（asyncio Condition 不能跨 loop 复用）。"""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.loop = asyncio.new_event_loop()
        self.repos: list[ImeTaskRepository] = []
        self.workers: list[ImeCancellationWorker] = []
        self.services: list[ScreenImeService] = []

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def repo(self, name: str, hooks: dict) -> ImeTaskRepository:
        repo = ImeTaskRepository(self.tmp_path / name, control_hooks=dict(hooks))
        self.repos.append(repo)
        return repo

    def mounted_repo(self, name: str, hooks: dict) -> ImeTaskRepository:
        """走生产挂载路径（set_control_hooks：持久标记 + 工作器）；
        随后停掉自动工作器，由测试显式驱动扫描/fake 时钟。"""
        repo = self.repo(name, hooks)
        repo.set_control_hooks(dict(hooks))
        worker = repo.cancellation_worker
        if worker is not None:
            worker.stop()
            repo._worker = None
        return repo

    def worker(self, repo: ImeTaskRepository, clock: FakeClock,
               interval: float = 2.0) -> ImeCancellationWorker:
        worker = ImeCancellationWorker(repo, interval_seconds=interval,
                                       time_func=clock.now, poll_seconds=0.01)
        self.workers.append(worker)
        return worker

    def close(self) -> None:
        for worker in self.workers:
            worker.stop()
        for service in self.services:
            service.close()
        for repo in self.repos:
            repo.close()
        self.loop.close()


@pytest.fixture()
def env(tmp_path):
    runtime = _runtime_lifecycle(tmp_path)
    e = _Env(tmp_path)
    e.runtime = runtime
    e.hooks = runtime.ime_control_hooks_fleet()
    try:
        yield e
    finally:
        e.close()
        asyncio.run(runtime.shutdown())


# ---------------------------------------------------------------- F02-1
def test_f02_1_periodic_scan_issues_exactly_one_cancel_and_survives_restart(env):
    """关闭后丢失所有内存通知，fake 时钟推进 2 秒扫描恰出一个 cancel；
    重启（同 IME DB 重开仓储）仍扫描且不重复建命令；健康候选不被误扫。"""
    runtime, tmp_path = env.runtime, env.tmp_path
    _submit_input_task(runtime, "fleet-1")
    _submit_input_task(runtime, "fleet-2")
    repo = env.repo("ime-f02-1.db", env.hooks)
    _seed_device(repo)
    clock = FakeClock()
    worker = env.worker(repo, clock)
    worker.start()

    def make(task_id, content="你好"):
        env.run(repo.create({
            "taskId": task_id, "deviceId": "k40-test", "targetUser": "conv",
            "content": content, "typingSpeed": "random", "source": "api"}))

    make("fleet-1-ime")
    time.sleep(0.003)  # 保证 created_at 可排序，交付顺序确定
    make("fleet-2-ime")
    # fleet-1 已交付正文（deliver 认领）；fleet-2 保持未交付 pending
    delivered = env.run(repo.next_task("k40-test", 0))
    assert delivered["taskId"] == "fleet-1-ime"

    # 关闭前：健康 pending/delivered 候选不被周期扫描误伤
    clock.advance(2.0)
    assert _wait_until(lambda: worker.scan_count >= 2
                       and repo.find("fleet-2-ime")["status"] == "pending"
                       and repo.find("fleet-1-ime")["status"] == "pending")

    # 关闭（推进撤销边界）；本环境不接通知——内存通知全部丢失
    _close_switch(runtime, tmp_path, "close-f02-1")
    # fake 时钟推进一个 2 秒扫描周期：只有周期兜底扫描在工作
    clock.advance(2.0)
    assert _wait_until(lambda: repo.find("fleet-1-ime")["status"] == "cancel_requested")
    # 已交付 → cancel_requested + 恰一个 pending cancel 命令
    assert len(_cancel_commands(repo, "fleet-1-ime")) == 1
    # 未交付 pending → 本地取消（原语义），且不产生控制命令
    assert repo.find("fleet-2-ime")["status"] == "cancelled"
    assert _cancel_commands(repo, "fleet-2-ime") == []
    # 再推进一个周期：不重复建命令
    clock.advance(2.0)
    assert _wait_until(lambda: worker.scan_count >= 4)
    assert len(_cancel_commands(repo, "fleet-1-ime")) == 1

    # 重启：同一 IME DB 重开仓储 + 新工作器，仍扫描且不重复建命令
    worker.stop()
    repo.close()
    env.repos.clear()
    repo2 = env.repo("ime-f02-1.db", env.hooks)
    worker2 = env.worker(repo2, clock)
    worker2.start()
    clock.advance(2.0)
    assert _wait_until(lambda: worker2.scan_count >= 1)
    assert repo2.find("fleet-1-ime")["status"] == "cancel_requested"
    assert len(_cancel_commands(repo2, "fleet-1-ime")) == 1, \
        "重启扫描不得重复建取消命令"


# ---------------------------------------------------------------- F02-2
def test_f02_2_delivered_close_keeps_cancel_requested_and_ack_identity(env):
    """正文已交付、displayed 尚未回报时关闭 → 保持 cancel_requested；
    迟到 displayed 只补事实；原手机 ack 可成功，新手机/错 command/冒认拒绝。"""
    from runtime.input_control import record_id_for_owner

    runtime, tmp_path = env.runtime, env.tmp_path
    _submit_input_task(runtime, "fleet-1")
    repo = env.repo("ime-f02-2.db", env.hooks)
    _seed_device(repo)
    env.run(repo.create({
        "taskId": "fleet-1-ime", "deviceId": "k40-test", "targetUser": "conv",
        "content": "你好", "typingSpeed": "random", "source": "api"}))
    delivered = env.run(repo.next_task("k40-test", 0))
    assert delivered["taskId"] == "fleet-1-ime"

    # 关闭（displayed 尚未回报）→ 扫描把已交付 pending 转 cancel_requested
    _close_switch(runtime, tmp_path, "close-f02-2")
    assert repo.sweep_cancellations() >= 1
    task = repo.find("fleet-1-ime")
    assert task["status"] == "cancel_requested", "已交付 pending 不得本地取消"
    assert len(_cancel_commands(repo, "fleet-1-ime")) == 1

    # 迟到 displayed：200 + 真实取消态，不改回 displayed、不复活任务
    late = env.run(repo.update_status("fleet-1-ime", {
        "deviceId": "k40-test", "status": "displayed", "typedCharacterCount": 0}))
    assert late["status"] == "cancel_requested"
    detail = repo.detail("fleet-1-ime")
    assert any(h["status"] == "displayed_observed" for h in detail["history"])

    # 原手机 ack（真实进度 2）：200 cancelled + Fleet cancel_state=confirmed
    cmd = env.run(repo.next_control("fleet-1-ime", "k40-test", 0))
    assert cmd is not None and cmd["command"] == "cancel"
    acked = env.run(repo.acknowledge_control("fleet-1-ime", {
        "deviceId": "k40-test", "commandId": cmd["commandId"],
        "typedCharacterCount": 2}))
    assert acked["status"] == "cancelled"
    assert acked["typedCharacterCount"] == 2, "取消回执保留真实进度，不归零"
    effects = [r for r in runtime.store.input_records_by_device("arm-01", "effect")
               if r.get("ime_task_id") == "fleet-1-ime"
               and r["effect_kind"] == "ime_create"]
    assert effects and effects[0]["cancel_state"] == "confirmed"
    # 计数>0 → editor_review_required（取消确认不证明编辑框为空）
    guard = runtime.input_control.ensure_guard("arm-01")
    assert any(i["code"] == "editor_review_required" and i["state"] == "open"
               for i in guard["issues"])

    # 重复 ack 幂等：补齐一致，不重复推进
    again = env.run(repo.acknowledge_control("fleet-1-ime", {
        "deviceId": "k40-test", "commandId": cmd["commandId"],
        "typedCharacterCount": 2}))
    assert again["status"] == "cancelled"

    # 新手机冒认旧任务（IME 层 deviceId 核对）：409，变更前拒绝
    with pytest.raises(ScreenImeApiError) as caught:
        env.run(repo.acknowledge_control("fleet-1-ime", {
            "deviceId": "k40-other", "commandId": cmd["commandId"],
            "typedCharacterCount": 2}))
    assert caught.value.code == "device_mismatch"

    # 错 command：404
    with pytest.raises(ScreenImeApiError) as caught:
        env.run(repo.acknowledge_control("fleet-1-ime", {
            "deviceId": "k40-test", "commandId": 999999,
            "typedCharacterCount": 2}))
    assert caught.value.code == "command_not_found"

    # Fleet 层身份核对：owner 归属手机与 IME 任务手机不一致（冒认）→ 409
    from runtime.input_control import record_id_for_owner
    owner = runtime.input_control.build_owner(
        owner_type="fleet_task", owner_ref="fleet-x", device_id="arm-01",
        ime_device_id="k40-impersonated", settings_revision=1, binding_revision=0,
        payload={"t": "x"})
    runtime.input_control.admit_owner(owner)
    env.run(repo.create({
        "taskId": "fleet-x-ime", "deviceId": "k40-test", "targetUser": "conv",
        "content": "x", "typingSpeed": "random", "source": "api"}))
    with pytest.raises(ScreenImeApiError) as caught:
        env.run(repo.cancel("fleet-x-ime"))
    assert caught.value.code == "device_mismatch"
    assert repo.find("fleet-x-ime")["status"] == "pending", \
        "身份拒绝时 IME 状态不变"


# ---------------------------------------------------------------- F02-3
async def _report_typing(repo: ImeTaskRepository, task_id: str, device_id="k40-test"):
    return await repo.update_status(task_id, {
        "deviceId": device_id, "status": "typing", "typedCharacterCount": 0})


def test_f02_3_body_never_redelivered_and_typing_gate_409(env):
    """正文响应丢失后 next 不再交付；typing 响应丢失后不能再次开始；
    关闭后 typing 409；长轮询关闭后不返回过期正文；挂载缺注入时业务输入 503。"""
    runtime, tmp_path = env.runtime, env.tmp_path
    _submit_input_task(runtime, "fleet-1")
    _submit_input_task(runtime, "fleet-2")
    # 生产挂载路径：Fleet 挂载标记持久存在 IME DB
    repo = env.mounted_repo("ime-f02-3.db", env.hooks)
    _seed_device(repo)
    env.run(repo.create({
        "taskId": "fleet-1-ime", "deviceId": "k40-test", "targetUser": "conv",
        "content": "你好", "typingSpeed": "random", "source": "api"}))

    # 正文交付一次；响应丢失后重试 next 不再交付（IME DB 交付记录）
    first = env.run(repo.next_task("k40-test", 0))
    assert first["taskId"] == "fleet-1-ime"
    assert env.run(repo.next_task("k40-test", 0)) is None, \
        "同一 taskId 正文至多交付一次（响应丢失不自动重交付）"

    # typing 开始许可：响应丢失重试 → 409 already_claimed，不发第二次
    env.run(repo.update_status("fleet-1-ime", {
        "deviceId": "k40-test", "status": "displayed", "typedCharacterCount": 0}))
    granted = env.run(_report_typing(repo, "fleet-1-ime"))
    assert granted["status"] == "typing"
    with pytest.raises(ScreenImeApiError) as caught:
        env.run(_report_typing(repo, "fleet-1-ime"))
    assert caught.value.status == 409, "typing 拒绝不能回 200+cancelled"
    assert caught.value.code == "input_start_already_claimed"

    # 关闭后 typing：409 input_authorization_revoked（不能返回 200+cancelled）
    _close_switch(runtime, tmp_path, "close-f02-3")
    with pytest.raises(ScreenImeApiError) as caught:
        env.run(_report_typing(repo, "fleet-1-ime"))
    assert caught.value.status == 409
    assert caught.value.code == "input_authorization_revoked"

    # 长轮询关闭后不返回过期正文：已认领交付的门拒绝，保持等待（到截止 204）
    env.run(repo.create({
        "taskId": "fleet-2-ime", "deviceId": "k40-test", "targetUser": "conv",
        "content": "第二段", "typingSpeed": "random", "source": "api"}))
    assert env.run(repo.next_task("k40-test", 0)) is None
    assert repo.find("fleet-2-ime")["status"] == "pending"
    # 扫描按"交付已认领"处理：cancel_requested + cancel 命令（不本地取消）
    repo.sweep_cancellations()
    assert repo.find("fleet-2-ime")["status"] == "cancel_requested"
    assert len(_cancel_commands(repo, "fleet-2-ime")) == 1

    # 重启后未注入 InputControl 的服务拒绝业务输入（挂载标记持久存在）
    repo.close()
    env.repos.clear()
    bare = ImeTaskRepository(tmp_path / "ime-f02-3.db")  # 未注入钩子
    env.repos.append(bare)
    with pytest.raises(ScreenImeApiError) as caught:
        env.run(bare.create({
            "taskId": "t-x", "deviceId": "k40-test", "targetUser": "c",
            "content": "x", "typingSpeed": "random", "source": "api"}))
    assert caught.value.status == 503
    assert caught.value.code == "input_control_not_ready"
    with pytest.raises(ScreenImeApiError) as caught:
        env.run(bare.next_task("k40-test", 0))
    assert caught.value.code == "input_control_not_ready"
    with pytest.raises(ScreenImeApiError) as caught:
        env.run(_report_typing(bare, "fleet-1-ime"))
    assert caught.value.code == "input_control_not_ready"
    # 取消/状态/控制端点照常可用（只拦业务输入）
    detail = bare.detail("fleet-2-ime")
    assert detail["task"]["status"] == "cancel_requested"


# ---------------------------------------------------------------- F02-4
def test_f02_4a_ime_cancel_ack_transactions_are_atomic(env, monkeypatch):
    """IME DB 事务中间故障：命令/任务/历史全提交或全未提交；重启后一致。"""
    runtime, tmp_path = env.runtime, env.tmp_path
    _submit_input_task(runtime, "fleet-1")
    repo = env.repo("ime-f02-4a.db", env.hooks)
    _seed_device(repo)
    env.run(repo.create({
        "taskId": "fleet-1-ime", "deviceId": "k40-test", "targetUser": "conv",
        "content": "你好", "typingSpeed": "random", "source": "api"}))
    env.run(repo.next_task("k40-test", 0))

    class FlakyCommitConn:
        """持久层故障注入代理：仅拦截 commit 边界（外部 IO），放行其余调用。"""

        def __init__(self, conn, state):
            self._conn = conn
            self._state = state

        def commit(self):
            if self._state["armed"]:
                self._state["armed"] = False
                raise sqlite3.OperationalError("injected commit failure")
            return self._conn.commit()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    injected = {"armed": False}
    real_conn = repo._conn

    def arm():
        repo._conn = FlakyCommitConn(real_conn, injected)
        injected["armed"] = True

    def disarm():
        injected["armed"] = False
        repo._conn = real_conn

    # --- cancel 事务中途失败：任务/历史/命令全未提交 ---
    arm()
    with pytest.raises(sqlite3.OperationalError):
        env.run(repo.cancel("fleet-1-ime"))
    disarm()
    task = repo.find("fleet-1-ime")
    assert task["status"] == "pending", "事务回滚：任务状态未变"
    assert _cancel_commands(repo, "fleet-1-ime") == []
    history = [h for h in repo.detail("fleet-1-ime")["history"]
               if h["status"] in ("cancel_requested", "cancelled")]
    assert history == [], "事务回滚：不留半条历史"

    # 重试成功（Fleet 钩子幂等）：全提交
    cancelled = env.run(repo.cancel("fleet-1-ime"))
    assert cancelled["status"] == "cancel_requested"
    assert len(_cancel_commands(repo, "fleet-1-ime")) == 1

    # --- ack 事务中途失败：命令未确认、任务未取消（无永久中间态）---
    arm()
    with pytest.raises(sqlite3.OperationalError):
        env.run(repo.acknowledge_control("fleet-1-ime", {
            "deviceId": "k40-test", "commandId": 1, "typedCharacterCount": 3}))
    disarm()
    with repo._thread_lock:
        cmd = repo._conn.execute(
            "SELECT state FROM control_commands WHERE id=1").fetchone()
    assert cmd["state"] == "pending", "ack 事务回滚：命令不得已确认"
    assert repo.find("fleet-1-ime")["status"] == "cancel_requested"
    acked = env.run(repo.acknowledge_control("fleet-1-ime", {
        "deviceId": "k40-test", "commandId": 1, "typedCharacterCount": 3}))
    assert acked["status"] == "cancelled"
    with repo._thread_lock:
        cmd = repo._conn.execute(
            "SELECT state FROM control_commands WHERE id=1").fetchone()
    assert cmd["state"] == "acknowledged"

    # 重启（同一 DB 重开）：只能看到全提交后的稳定事实
    repo.close()
    env.repos.clear()
    repo2 = env.repo("ime-f02-4a.db", env.hooks)
    assert repo2.find("fleet-1-ime")["status"] == "cancelled"
    assert repo2.pending_cancellation_count() == 0


def test_f02_4b_pending_cancellation_projection_matches_records(env):
    """状态 API 待取消数从实际记录投影；装配 attach + 工作器启停（关机序）。"""
    from api.app import _ime_pending_cancellations

    runtime, tmp_path = env.runtime, env.tmp_path
    _submit_input_task(runtime, "fleet-1")
    _submit_input_task(runtime, "fleet-2")
    service = ScreenImeService(tmp_path / "ime-f02-4b.db")
    env.services.append(service)
    # 正式装配路径：hooks 注入 → attach_runtime 挂到 Runtime → 工作器启动
    service.set_control_hooks(runtime.ime_control_hooks_fleet())
    assert runtime.screen_ime is service
    worker = service.tasks.cancellation_worker
    assert worker is not None and worker.running

    repo = service.tasks
    _seed_device(repo)
    for task_id in ("fleet-1-ime", "fleet-2-ime"):
        env.run(repo.create({
            "taskId": task_id, "deviceId": "k40-test", "targetUser": "conv",
            "content": "你好", "typingSpeed": "random", "source": "api"}))
        env.run(repo.next_task("k40-test", 0))
    _close_switch(runtime, tmp_path, "close-f02-4b")
    repo.sweep_cancellations()
    assert repo.pending_cancellation_count() == 2
    assert _ime_pending_cancellations(runtime) == 2, \
        "状态 API 从挂载仓储实际记录投影待取消数"

    # 确认一个后投影同步下降；ack 计数 0 且开始未认领：无需编辑框核对
    cmd = env.run(repo.next_control("fleet-1-ime", "k40-test", 0))
    env.run(repo.acknowledge_control("fleet-1-ime", {
        "deviceId": "k40-test", "commandId": cmd["commandId"],
        "typedCharacterCount": 0}))
    assert repo.pending_cancellation_count() == 1
    assert _ime_pending_cancellations(runtime) == 1
    guard = runtime.input_control.ensure_guard("arm-01")
    assert not any(i["code"] == "editor_review_required" for i in guard["issues"])

    # 关机序：service.close 停工作器；重复关闭幂等
    service.close()
    assert not worker.running
    assert service.tasks.cancellation_worker is None
    service.close()  # 幂等


def test_f02_4c_orphan_cancel_does_not_block_other_device(env):
    """已认领开始而 ack=0 仍有 editor_review_required；无归属旧任务取消不建立
    阻断、不阻断其他设备；撤销边界只影响原设备。"""
    runtime, tmp_path = env.runtime, env.tmp_path
    _submit_input_task(runtime, "fleet-1")                # arm-01 / k40-test
    _submit_input_task(runtime, "fleet-other", "arm-02")  # arm-02 / k40-2
    repo = env.repo("ime-f02-4c.db", env.hooks)
    _seed_device(repo)
    _seed_device(repo, "k40-2")
    env.run(repo.create({
        "taskId": "fleet-1-ime", "deviceId": "k40-test", "targetUser": "conv",
        "content": "你好", "typingSpeed": "random", "source": "api"}))
    env.run(repo.create({
        "taskId": "fleet-other-ime", "deviceId": "k40-2", "targetUser": "conv",
        "content": "别动我", "typingSpeed": "random", "source": "api"}))

    # arm-01：交付 + typing（开始许可已认领）→ 关闭 → 扫描 → 原手机 ack=0
    env.run(repo.next_task("k40-test", 0))
    env.run(repo.update_status("fleet-1-ime", {
        "deviceId": "k40-test", "status": "displayed", "typedCharacterCount": 0}))
    env.run(repo.update_status("fleet-1-ime", {
        "deviceId": "k40-test", "status": "typing", "typedCharacterCount": 0}))
    _close_switch(runtime, tmp_path, "close-f02-4c")
    repo.sweep_cancellations()
    cmd = env.run(repo.next_control("fleet-1-ime", "k40-test", 0))
    env.run(repo.acknowledge_control("fleet-1-ime", {
        "deviceId": "k40-test", "commandId": cmd["commandId"],
        "typedCharacterCount": 0}))
    # 已认领开始即使 ack 计数为 0 也须核对残留编辑框
    guard1 = runtime.input_control.ensure_guard("arm-01")
    assert any(i["code"] == "editor_review_required" and i["state"] == "open"
               for i in guard1["issues"])

    # arm-02 的候选不受 arm-01 关闭影响（撤销边界按设备）
    assert repo.find("fleet-other-ime")["status"] == "pending"
    repo.sweep_cancellations()
    assert repo.find("fleet-other-ime")["status"] == "pending", \
        "他设备健康候选不因原设备关闭被清扫"
    guard2 = runtime.store.input_records_by_device("arm-02", "device_guard")
    assert not any(issue["state"] == "open"
                   for record in guard2 for issue in record.get("issues", []))

    # 无归属旧任务（owner 缺失）取消：IME 层继续，不建立阻断、不影响他设备
    env.run(repo.create({
        "taskId": "orphan-9-ime", "deviceId": "k40-test", "targetUser": "conv",
        "content": "孤儿", "typingSpeed": "random", "source": "api"}))
    cancelled = env.run(repo.cancel("orphan-9-ime"))
    assert cancelled["status"] == "cancelled"
    guards = runtime.store.input_records_by_device("arm-01", "device_guard") + \
        runtime.store.input_records_by_device("arm-02", "device_guard")
    assert not any(issue["code"] in ("ime_cancel_pending", "input_record_invalid")
                   and issue["state"] == "open"
                   for record in guards for issue in record.get("issues", []))


# ---------------------------------------------------------------- 装配
def test_f02_assembly_attaches_service_and_worker_lifecycle(tmp_path):
    """attach_runtime 注入 + Fleet 挂载标记持久存在 + 工作器随挂载启停。"""
    runtime = _runtime_lifecycle(tmp_path, n_devices=1)
    service = ScreenImeService(tmp_path / "ime-assembly.db")
    try:
        assert service.tasks.cancellation_worker is None
        service.set_control_hooks(runtime.ime_control_hooks_fleet())
        assert runtime.screen_ime is service
        assert service.tasks._get_meta("fleet_mounted") == "1"
        worker = service.tasks.cancellation_worker
        assert worker is not None and worker.running
        # 关闭写入通知唤醒（通知只是加速；周期兜底不依赖通知）
        runtime.input_control._notify_cancellation_worker()
    finally:
        service.set_control_hooks({})
        assert service.tasks._get_meta("fleet_mounted") == "0"
        assert service.tasks.cancellation_worker is None
        service.close()
        asyncio.run(runtime.shutdown())


# ---------------------------------------------------------------- 工具
def test_worker_never_overlaps(tmp_path):
    """单工作线程串行扫描：并发触发与周期扫描互不重叠。"""
    repo = ImeTaskRepository(tmp_path / "ime-overlap.db")
    active = {"n": 0, "max": 0, "scans": 0}
    lock = threading.Lock()
    original = repo.sweep_cancellations

    def slow_sweep():
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            active["scans"] += 1
        time.sleep(0.03)
        try:
            return original()
        finally:
            with lock:
                active["n"] -= 1

    repo.sweep_cancellations = slow_sweep  # type: ignore[method-assign]
    clock = FakeClock()
    worker = ImeCancellationWorker(repo, interval_seconds=0.02,
                                   time_func=clock.now, poll_seconds=0.005)
    try:
        worker.start()
        deadline = time.monotonic() + 0.8
        while time.monotonic() < deadline:
            worker.notify()  # 并发触发提前唤醒
            time.sleep(0.01)
        worker.stop()
        with lock:
            assert active["scans"] >= 3, "未发生多轮扫描，无法验证互斥"
            assert active["max"] == 1, f"扫描出现重叠：max={active['max']}"
    finally:
        repo.close()
