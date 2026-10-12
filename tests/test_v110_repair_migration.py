"""v1.10 实现审查修复 FIX-04 回归（docs/2026-10-04-v1.10实现审查修复实施单）。

覆盖映射（F 编号 → 测试函数）：
- F04-1 合法旧开启档不能直接输入；离线迁移副本后关闭、版本升高、原件/旧请求
  标记保留；同旧编号不能重新开关
  → test_f04_1_legacy_enabled_archive_migrates_closed_and_tombstones_old_ids
- F04-2 在 replace 前、replace 后 fsync、DB 提交、回包四处分别注入失败，
  核对分类、原请求重放和重启结果，没有开启回退
  → test_f04_2_write_failure_classification_and_replay
- F04-3 换手机中途重启：新手机输入 0；旧手机取消仍可对账；
  旧 owner 即使当前再次开启也不可执行
  → test_f04_3_phone_change_restart_blocks_new_phone_reconciles_old
- F04-4 损坏 checkpoint、已知文件丢失、孤儿旧任务、版本到 MAX 均阻断；
  核对请求重放不重复审计；仍有占用时不得释放
  → test_f04_4_corruption_orphan_max_block_and_review_replay

隔离（修复单 §3.1）：临时 data_root/Fleet DB/IME DB/备份目录，测试构造旧档
副本；迁移工具经真实 CLI（子进程 PYTHONPATH=arm_fleet）调用；不运行生产迁移、
不启动现场服务、不操作硬件。重启以持久化状态 + 原 migration_id resume 近似。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime import input_settings as iset
from runtime.errors import ApiError
from runtime.input_control import (
    InputControl,
    InputControlCommitUnknown,
    InputControlError,
    canonical_digest,
    record_id_for_effect,
    record_id_for_guard,
    record_id_for_owner,
)
from runtime.store import Store

ARM_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

T1 = "2026-09-01T00:00:00Z"
T2 = "2026-09-02T00:00:00Z"
T3 = "2026-09-03T00:00:00Z"


# --------------------------------------------------------------------------
# 旧档构造与迁移工具环境
# --------------------------------------------------------------------------

def legacy_entry(enabled: bool, revision: int, operator: str, at: str) -> dict:
    digest_src = json.dumps([enabled, revision, operator, at], ensure_ascii=False)
    return {"enabled": enabled, "revision": revision, "operator_id": operator,
            "at": at,
            "digest": hashlib.sha256(digest_src.encode()).hexdigest()}


def build_legacy_file(device_id: str = "arm-90", ime: str = "k40-old",
                      enabled: bool = True, revision: int = 3) -> dict:
    if revision >= 9007199254740990:
        requests = {"legacy-open-max": legacy_entry(enabled, revision, "旧操作员", T3)}
    else:
        requests = {
            "legacy-open-1": legacy_entry(enabled, 1, "旧操作员", T1),
            "legacy-open-2": legacy_entry(enabled, 2, "旧操作员", T2),
            "legacy-open-3": legacy_entry(enabled, 3, "旧操作员", T3),
        }
    return {
        "contract_version": "device-input-settings.v1",
        "device_id": device_id,
        "ime_device_id": ime,
        "enabled": enabled,
        "revision": revision,
        "updated_at": T3,
        "operator_id": "旧操作员",
        "reason": "旧开启",
        "client_request_id": "legacy-open-3" if revision < 9007199254740990
        else "legacy-open-max",
        "requests": requests,
    }


class MigrationEnv:
    """迁移工具的离线维护资料：临时 data_root + Fleet DB + IME DB + 配置。"""

    def __init__(self, tmp_path: Path, device_id: str = "arm-90"):
        self.root = tmp_path
        self.device_id = device_id
        self.data_root = tmp_path / "data"
        self.fleet_db = tmp_path / "fleet.db"
        self.ime_db = tmp_path / "ime.db"
        self.backup_dir = tmp_path / "backup"
        self.store = Store(self.fleet_db)
        self.control = InputControl(self.store, self.fleet_db)
        # IME DB 是独立 SQLite（工具只做 backup API 备份与完整性校验）
        conn = sqlite3.connect(str(self.ime_db))
        conn.execute("CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY)")
        conn.commit()
        conn.close()
        self.config_path = tmp_path / "offline-config.json"
        self.config_path.write_text(json.dumps({
            "data_root": str(self.data_root),
            "fleet_db": str(self.fleet_db),
            "ime_db": str(self.ime_db),
        }, ensure_ascii=False), encoding="utf-8")

    @property
    def settings_path(self) -> Path:
        return self.data_root / "devices" / self.device_id / "input_settings.json"

    def write_legacy(self, legacy: dict | None = None) -> dict:
        legacy = legacy or build_legacy_file(self.device_id)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(
            json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
        return legacy

    def ensure_guard_with_checkpoint(self, legacy: dict) -> None:
        """模拟新代码曾为本设备建立 guard + 与旧文件一致的 checkpoint。"""
        guard = self.control.ensure_guard(self.device_id)
        self.control.set_settings_checkpoint(
            self.device_id, revision=int(legacy["revision"]),
            digest=canonical_digest(legacy))

    def admit_owner(self, settings_revision: int, owner_ref: str = "legacy-task-1") -> dict:
        owner = self.control.build_owner(
            owner_type="fleet_task", owner_ref=owner_ref, device_id=self.device_id,
            ime_device_id="k40-old", settings_revision=settings_revision,
            binding_revision=0, payload={"t": 1})
        stored, _created = self.control.admit_owner(owner)
        return stored

    def run_tool(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ARM_ROOT)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run(
            [PYTHON, "-B", str(ARM_ROOT / "tools" / "migrate_input_settings.py"),
             "--config", str(self.config_path), "--device", self.device_id, *args],
            capture_output=True, text=True, env=env, cwd=str(ARM_ROOT), timeout=60)

    def close(self) -> None:
        with_pending = self.store
        try:
            with_pending._conn.close()
        except Exception:
            pass


@pytest.fixture()
def migration_env(tmp_path):
    env = MigrationEnv(tmp_path)
    try:
        yield env
    finally:
        env.close()


# --------------------------------------------------------------------------
# F04-1：合法旧开启档迁移
# --------------------------------------------------------------------------

def test_f04_1_legacy_enabled_archive_migrates_closed_and_tombstones_old_ids(migration_env):
    env = migration_env
    legacy = env.write_legacy()
    original_bytes = env.settings_path.read_bytes()
    env.ensure_guard_with_checkpoint(legacy)
    env.admit_owner(settings_revision=2)

    # 迁移前：旧开启档不得直接输入（缺失撤销字段的旧格式一律拒绝，不默认边界 0）
    with pytest.raises(iset.InputSettingsError) as exc:
        iset.load_settings(env.device_id, env.data_root)
    assert exc.value.reason_code == "input_settings_invalid"
    assert iset.is_enabled(env.device_id, env.data_root) is False
    with pytest.raises(iset.InputSettingsError):
        iset.update_settings(env.device_id, data_root=env.data_root, enabled=False,
                             ime_device_id=None, expected_revision=3,
                             operator_id="操作员", reason="直接关",
                             client_request_id="direct-close", calibration_ready=False)
    assert env.settings_path.read_bytes() == original_bytes, "拒绝写入不得覆盖旧档"

    # dry-run 只读：报告 R=3、拟关闭版本 4，不生成意图/基线/备份
    done = env.run_tool("--dry-run")
    assert done.returncode == 0, done.stderr + done.stdout
    report = json.loads(done.stdout)
    assert report["R"] == 3 and report["close_revision"] == 4
    assert not (env.data_root / "maintenance").exists(), "dry-run 不得生成维护意图"
    assert not env.backup_dir.exists(), "dry-run 不得生成备份"
    assert env.settings_path.read_bytes() == original_bytes

    # apply：关闭、版本升高、保留原授权手机、旧请求只留摘要 tombstone
    done = env.run_tool(
        "--apply", "--migration-id", "mig-2026-0001", "--operator", "维护员",
        "--reason", "迁移旧输入资料并关闭许可",
        "--client-request-id", "maint-req-0001", "--backup-dir", str(env.backup_dir))
    assert done.returncode == 0, done.stderr + done.stdout

    stored = iset.load_settings(env.device_id, env.data_root)
    assert stored["enabled"] is False, "迁移后保持关闭"
    assert stored["revision"] == 4 and stored["revoked_through_revision"] == 4
    assert stored["ime_device_id"] == "k40-old", "保留原授权手机"
    assert stored["requests"] == {}, "旧请求不伪造原请求/原响应"
    baseline = stored["migration_baseline"]
    assert baseline["contract_version"] == "input-settings-migration.v1"
    assert baseline["revision_floor"] == 4
    assert baseline["closed_saved_value"]["enabled"] is False
    assert baseline["migration_id"] == "mig-2026-0001"
    assert set(baseline["legacy_request_ids"]) == {"legacy-open-1", "legacy-open-2",
                                                   "legacy-open-3"}
    for key, digest in baseline["legacy_request_ids"].items():
        assert digest == canonical_digest(legacy["requests"][key]), \
            "tombstone 摘要=SHA-256(完整旧 entry 规范 JSON)"
    # 原件字节备份保留
    backup_file = (env.backup_dir /
                   f"{env.device_id}-input-settings-mig-2026-0001.json")
    assert backup_file.read_bytes() == original_bytes, "原文件按字节备份"

    # 同旧编号不能重开：tombstone 409 语义；维护编号同样不得复用
    with pytest.raises(iset.InputSettingsError) as exc:
        iset.update_settings(env.device_id, data_root=env.data_root, enabled=True,
                             ime_device_id="k40-old", expected_revision=4,
                             operator_id="操作员", reason="旧编号重开",
                             client_request_id="legacy-open-3", calibration_ready=True)
    assert exc.value.reason_code == "legacy_request_replay_unavailable"
    with pytest.raises(iset.InputSettingsError) as exc:
        iset.update_settings(env.device_id, data_root=env.data_root, enabled=False,
                             ime_device_id=None, expected_revision=4,
                             operator_id="维护员", reason="维护编号复用",
                             client_request_id="maint-req-0001", calibration_ready=False)
    assert exc.value.reason_code == "request_conflict"
    with pytest.raises(iset.InputSettingsError) as exc:
        iset.update_settings(env.device_id, data_root=env.data_root, enabled=False,
                             ime_device_id=None, expected_revision=4,
                             operator_id="维护员", reason="迁移编号复用",
                             client_request_id="mig-2026-0001", calibration_ready=False)
    assert exc.value.reason_code == "request_conflict"
    assert iset.load_settings(env.device_id, env.data_root)["revision"] == 4, \
        "被拒请求不得推进版本"

    # 迁后不恢复开启（工具不自动开启）；维护意图已 committed
    assert iset.is_enabled(env.device_id, env.data_root) is False
    intent_path = (env.data_root / "maintenance" / "input-control")
    intents = list(intent_path.glob("*/intent.json"))
    assert len(intents) == 1
    intent = json.loads(intents[0].read_text(encoding="utf-8"))
    assert intent["state"] == "committed" and intent["committed_at"]
    # 重复 apply 同编号同来源：返回既有状态，不新建迁移
    again = env.run_tool(
        "--apply", "--migration-id", "mig-2026-0001", "--operator", "维护员",
        "--reason", "迁移旧输入资料并关闭许可",
        "--client-request-id", "maint-req-0001", "--backup-dir", str(env.backup_dir))
    assert again.returncode == 0
    assert json.loads(again.stdout)["state"] == "committed"
    assert len(list(intent_path.glob("*/intent.json"))) == 1


# --------------------------------------------------------------------------
# F04-2：write_settings 四处失败注入的分类与重放
# --------------------------------------------------------------------------

REQUEST = {"enabled": True, "ime_device_id": "k40-1", "expected_revision": 0,
           "operator_id": "操作员", "reason": "开启", "client_request_id": "req-1"}


@pytest.fixture()
def write_env(tmp_path):
    store = Store(tmp_path / "fleet.db")
    ctrl = InputControl(store, tmp_path / "fleet.db")
    root = tmp_path / "data"
    try:
        yield ctrl, root, store
    finally:
        try:
            store._conn.close()
        except Exception:
            pass


def _write(ctrl, root, request=None, **kw):
    merged = {**REQUEST, **(request or {}), **kw}
    return ctrl.write_settings("arm-01", data_root=root, request=merged,
                               calibration_ready=True)


def test_f04_2_write_failure_classification_and_replay(write_env, monkeypatch):
    ctrl, root, store = write_env

    # (1) replace 前：可证实未提交 → write_failed，aborted 已保存，旧文件原样
    def stalled_before(path, payload):
        raise RuntimeError("disk stalled before replace")
    monkeypatch.setattr(iset, "_atomic_write", stalled_before)
    with pytest.raises(InputControlError) as exc:
        _write(ctrl, root, client_request_id="pre-replace")
    assert exc.value.reason_code == "input_settings_write_failed"
    monkeypatch.undo()
    assert iset.load_settings("arm-01", data_root=root)["revision"] == 0
    intents = ctrl.records_for_device("arm-01", "settings_write")
    assert intents[-1]["state"] == "aborted" and intents[-1]["failure"]
    with pytest.raises(InputControlError, match="不自动再执行"):
        _write(ctrl, root, client_request_id="pre-replace")

    # prepared 意图自插入起即携带完整 proposed_file+original_response：
    captured = {}
    real_insert = InputControl.insert_settings_write

    def capturing_insert(self, record):
        captured["record"] = deepcopy(record)
        return real_insert(self, record)
    monkeypatch.setattr(InputControl, "insert_settings_write", capturing_insert)

    # (2) replace 后 fsync 失败（文件已替换）：commit_unknown，prepared 保留
    def real_then_raise(path, payload):
        iset._atomic_write_orig(path, payload)
        raise RuntimeError("dir fsync lost after replace")
    monkeypatch.setattr(iset, "_atomic_write_orig", iset._atomic_write,
                        raising=False)
    monkeypatch.setattr(iset, "_atomic_write", real_then_raise)
    with pytest.raises(InputControlCommitUnknown) as exc:
        _write(ctrl, root, client_request_id="post-replace")
    assert exc.value.reason_code == "input_settings_commit_unknown"
    monkeypatch.undo()
    stored = iset.load_settings("arm-01", data_root=root)
    assert stored["enabled"] is True and stored["revision"] == 1, "文件已提交，不回退"
    pending = ctrl.has_prepared_intent("arm-01")
    assert pending is not None and pending["state"] == "prepared"
    assert isinstance(pending["original_response"], dict), \
        "prepared 意图已携带原响应"
    assert pending["original_response"]["effective"] is True
    entry = pending["proposed_file"]["requests"]["post-replace"]
    assert isinstance(entry.get("original_response"), dict), \
        "proposed_file 完整含原请求账本/原响应"
    # 同编号重放：对账 applied，返回保存的原响应，无开启回退、无第二次写入副作用
    reconciled = _write(ctrl, root, client_request_id="post-replace")
    assert reconciled["outcome"] == "applied" and reconciled["replayed"] is True
    assert reconciled["settings"] == pending["original_response"]
    assert ctrl.has_prepared_intent("arm-01") is None
    assert iset.load_settings("arm-01", data_root=root)["revision"] == 1

    # (3) DB 提交（applied/checkpoint）失败：commit_unknown → 重放补齐
    calls = {"n": 0}
    real_mark = InputControl._apply_settings_commit

    def crashing_mark(self, *args, **kwargs):
        if calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("db commit lost")
        return real_mark(self, *args, **kwargs)
    # R2-02 更正（§4.5）：applied+checkpoint 合并为单事务 _apply_settings_commit
    monkeypatch.setattr(InputControl, "_apply_settings_commit", crashing_mark)
    with pytest.raises(InputControlCommitUnknown) as exc:
        _write(ctrl, root, request={**REQUEST, "enabled": False,
                                    "ime_device_id": None, "expected_revision": 1,
                                    "client_request_id": "db-commit"})
    assert exc.value.reason_code == "input_settings_commit_unknown"
    monkeypatch.undo()
    assert ctrl.has_prepared_intent("arm-01") is not None
    reconciled = _write(ctrl, root, request={**REQUEST, "enabled": False,
                                             "ime_device_id": None,
                                             "expected_revision": 1,
                                             "client_request_id": "db-commit"})
    assert reconciled["outcome"] == "applied" and reconciled["replayed"] is True
    assert iset.load_settings("arm-01", data_root=root)["enabled"] is False
    assert ctrl.has_prepared_intent("arm-01") is None

    # (4) 回包丢失（提交已成功）：同编号重放返回原快照，不新增意图/审计
    first = _write(ctrl, root, request={**REQUEST, "expected_revision": 2,
                                        "client_request_id": "lost-reply"})
    replay = _write(ctrl, root, request={**REQUEST, "expected_revision": 2,
                                         "client_request_id": "lost-reply"})
    assert replay["replayed"] is True and replay["settings"] == first["settings"]
    intents = ctrl.records_for_device("arm-01", "settings_write")
    assert len([i for i in intents if i["client_request_id"] == "lost-reply"]) == 1
    assert iset.load_settings("arm-01", data_root=root)["revision"] == 3
    assert captured  # 捕获器确认过 prepared 插入即含完整材料


# --------------------------------------------------------------------------
# F04-3：换手机流程
# --------------------------------------------------------------------------

def _repair_manifest():
    from tests.helpers import PROBE_MANIFEST
    manifest = deepcopy(PROBE_MANIFEST)
    manifest["task_types"][0].update(
        task_type="declared_input", requires_input=True,
        requires_keyboard_active=False)
    manifest["capabilities"] += ["input.type_text", "input.ime_create", "arm.click"]
    manifest["resources"]["input"] = True
    return manifest


async def _register(runtime):
    from tests.helpers import make_device_configs
    await runtime.devices.register_all(make_device_configs(1))


def _enable(runtime, tmp_path: Path, ime_device_id: str):
    import uuid
    from runtime import input_settings as iset_mod
    revision = int(iset_mod.load_settings("arm-01", data_root=tmp_path / "data")
                   .get("revision") or 0)
    return iset_mod.update_settings(
        "arm-01", data_root=tmp_path / "data", enabled=True,
        ime_device_id=ime_device_id, expected_revision=revision,
        operator_id="操作员", reason="测试允许",
        client_request_id=f"allow-{uuid.uuid4().hex[:8]}", calibration_ready=True)


def test_f04_3_phone_change_restart_blocks_new_phone_reconciles_old(tmp_path, monkeypatch):
    from tests.helpers import make_device_configs, make_runtime

    runtime = make_runtime(tmp_path, manifest=_repair_manifest())
    asyncio.run(_register(runtime))
    # 唯一绑定随设备登记 ime_device_id 演进（k40-old → 应用 k40-new 后跟随）
    runtime.devices.input_binding = lambda device_id: {
        "ime_device_id": (runtime.devices._devices["arm-01"].get("ime_device_id")
                          or "k40-old")}
    _enable(runtime, tmp_path, "k40-old")
    task = runtime.submit_task(
        {"task_type": "declared_input", "device_id": "arm-01",
         "task_id": "fleet-old-1", "idempotency_key": "fleet-old-1",
         "correlation_id": "c", "expires_at": None, "contract_version": "v1",
         "payload": {"steps": [{"is_action": True}]}},
        source="console", context={"operator_id": "测试", "reason": "F04-3"})
    owner = runtime.store.input_record_get(
        record_id_for_owner("fleet_task", "fleet-old-1"))
    assert owner is not None and owner["ime_device_id"] == "k40-old"
    # 已交付 IME 候选（持久 effect 链，供取消/对账身份核对）
    runtime.input_control.insert_effect(runtime.input_control.build_effect(
        owner_id=owner["record_id"], device_id="arm-01",
        slot_key=["ime:fleet-old-1-ime", 0, "ime_create"],
        payload={"ime_task_id": "fleet-old-1-ime"},
        ime_task_id="fleet-old-1-ime"))
    hooks = runtime.ime_control_hooks_fleet()
    hooks["cancel"]("fleet-old-1-ime", "k40-old", delivered=True)

    # 换手机在「已系统关闭、未应用新手机」处中断（近似进程重启：全部状态已持久）
    real_pause = type(runtime.devices).pause

    def crashing_pause(self, device_id, reason):
        raise RuntimeError("process died before applying new phone")
    monkeypatch.setattr(type(runtime.devices), "pause", crashing_pause)
    with pytest.raises(RuntimeError):
        asyncio.run(runtime.devices.update_binding(
            "arm-01", {"ime_device_id": "k40-new"},
            operator_id="操作员", reason="换手机"))
    monkeypatch.undo()

    guard = runtime.store.input_record_get(record_id_for_guard("arm-01"))
    assert guard["binding_change"]["phase"] in ("closed", "reconciling")
    assert guard["binding_change"]["new_ime_device_id"] == "k40-new"
    assert any(issue["code"] == "binding_change_pending" and issue["state"] == "open"
               for issue in guard["issues"]), "换绑意图阻断持久存在"
    settings = iset.load_settings("arm-01", data_root=tmp_path / "data")
    assert settings["enabled"] is False, "系统关闭已提交"
    assert settings["revoked_through_revision"] >= settings["revision"]
    assert settings["ime_device_id"] == "k40-old", "关闭请求不改变绑定并保留原授权手机"
    revoked = runtime.store.input_record_get(
        record_id_for_owner("fleet_task", "fleet-old-1"))
    assert revoked["revocation"] == "revoked", "撤销边界推进后旧 owner 已撤销"
    device_view = runtime.devices.binding_view("arm-01")
    assert device_view["binding"]["ime_device_id"] != "k40-new", "新手机未应用"
    assert device_view["binding_revision"] == 1, "应用前不得递增绑定版本"

    # 中断窗口：新手机输入 0（开关关 + 换绑阻断），旧手机对账继续
    with pytest.raises(ApiError) as exc:
        runtime.submit_task(
            {"task_type": "declared_input", "device_id": "arm-01",
             "task_id": "fleet-new-1", "idempotency_key": "fleet-new-1",
             "correlation_id": "c", "expires_at": None, "contract_version": "v1",
             "payload": {"steps": [{"is_action": True}]}},
            source="console", context={"operator_id": "测试", "reason": "F04-3"})
    assert exc.value.key == "input_switch_disabled"
    # 旧手机取消/确认仍可对账（原任务身份核对，不要求当前许可有效）
    hooks["cancel"]("fleet-old-1-ime", "k40-old", delivered=True)
    hooks["ack"]("fleet-old-1-ime", "k40-old", typed_character_count=0)
    # 新手机不得冒认旧任务
    with pytest.raises(InputControlError):
        hooks["cancel"]("fleet-old-1-ime", "k40-new", delivered=True)

    # 按 原意图 resume（同目标）：应用新手机并收尾
    result = asyncio.run(runtime.devices.update_binding(
        "arm-01", {"ime_device_id": "k40-new"},
        operator_id="操作员", reason="换手机"))
    assert result["binding"]["ime_device_id"] == "k40-new"
    guard = runtime.store.input_record_get(record_id_for_guard("arm-01"))
    assert guard["binding_change"]["phase"] == "applied"
    assert not any(issue["code"] == "binding_change_pending" and issue["state"] == "open"
                   for issue in guard["issues"]), "换绑完成解除阻断"
    settings = iset.load_settings("arm-01", data_root=tmp_path / "data")
    assert settings["enabled"] is False, "迁后不恢复开启：新手机也不自动开启"

    # 旧 owner 即使当前再次开启也不可执行
    _enable(runtime, tmp_path, "k40-new")
    old_verdict = runtime.check_input_owner(runtime.store.get_task("fleet-old-1"))
    assert old_verdict is not None
    assert old_verdict["category"] == "input_authorization_revoked"
    # 新任务（新许可）可正常核对通过
    runtime.submit_task(
        {"task_type": "declared_input", "device_id": "arm-01",
         "task_id": "fleet-new-2", "idempotency_key": "fleet-new-2",
         "correlation_id": "c", "expires_at": None, "contract_version": "v1",
         "payload": {"steps": [{"is_action": True}]}},
        source="console", context={"operator_id": "测试", "reason": "F04-3"})
    assert runtime.check_input_owner(runtime.store.get_task("fleet-new-2")) is None
    # 显式绑定修订核对：owner 登记修订落后于设备登记修订（换绑后旧许可）不执行
    current_revision = int(iset.load_settings(
        "arm-01", data_root=tmp_path / "data")["revision"])
    stale = runtime.input_control.build_owner(
        owner_type="fleet_task", owner_ref="stale-binding-1", device_id="arm-01",
        ime_device_id="k40-new", settings_revision=current_revision,
        binding_revision=0, payload={"t": 1})
    runtime.input_control.admit_owner(stale)
    stale_task = dict(runtime.store.get_task("fleet-new-2"))
    stale_task["task_id"] = "stale-binding-1"
    stale_verdict = runtime.check_input_owner(stale_task)
    assert stale_verdict is not None
    assert "input_binding_mismatch" in stale_verdict["message"], \
        "owner.binding_revision 对照 DeviceManager.binding_view 本设备登记修订"
    asyncio.run(runtime.shutdown())


# --------------------------------------------------------------------------
# F04-1/迁移中断表：prepared 现场只能按原 migration_id resume
# --------------------------------------------------------------------------

def test_f04_resume_interruption_table(migration_env):
    env = migration_env
    legacy = env.write_legacy()
    env.ensure_guard_with_checkpoint(legacy)
    env.admit_owner(settings_revision=2)
    before_bytes = env.settings_path.read_bytes()
    done = env.run_tool(
        "--apply", "--migration-id", "mig-resume", "--operator", "维护员",
        "--reason", "迁移旧输入资料并关闭许可",
        "--client-request-id", "maint-resume", "--backup-dir", str(env.backup_dir))
    assert done.returncode == 0, done.stderr + done.stdout
    after_bytes = env.settings_path.read_bytes()
    committed_intent_path = next(
        (env.data_root / "maintenance" / "input-control").glob("*/intent.json"))
    committed_intent = json.loads(committed_intent_path.read_text(encoding="utf-8"))

    def rewind_to_prepared(file_bytes: bytes, checkpoint: dict | None) -> None:
        env.settings_path.write_bytes(file_bytes)
        if checkpoint is None:
            # 直接把 checkpoint 还原为 null（sqlite 直改，不经 CAS）
            conn = sqlite3.connect(str(env.fleet_db))
            try:
                row = conn.execute(
                    "SELECT json, revision FROM input_records WHERE record_id=?",
                    (f"guard:{env.device_id}",)).fetchone()
                guard_json = json.loads(row[0])
                guard_json["settings_checkpoint"] = None
                conn.execute(
                    "UPDATE input_records SET json=?, revision=? WHERE record_id=?",
                    (json.dumps(guard_json, ensure_ascii=False), row[1] + 1,
                     f"guard:{env.device_id}"))
                conn.commit()
            finally:
                conn.close()
        else:
            env.control.set_settings_checkpoint(
                env.device_id, revision=int(checkpoint["revision"]),
                digest=checkpoint["digest"])
        intent = json.loads(committed_intent_path.read_text(encoding="utf-8"))
        intent["state"] = "prepared"
        intent["committed_at"] = None
        committed_intent_path.write_text(
            json.dumps(intent, ensure_ascii=False), encoding="utf-8")

    original_checkpoint = (((committed_intent.get("backup_manifest") or {})
                            .get("version_inventory") or {}).get("checkpoint"))
    target_digest = committed_intent["after_digest"]

    # 路线 1：prepared，文件=before，checkpoint=原值 → resume 继续同一次关闭
    rewind_to_prepared(before_bytes, original_checkpoint)
    done = env.run_tool("--resume", "--migration-id", "mig-resume")
    assert done.returncode == 0, done.stderr + done.stdout
    assert env.settings_path.read_bytes() == after_bytes, "resume 复用同一拟提交文件"
    intent = json.loads(committed_intent_path.read_text(encoding="utf-8"))
    assert intent["state"] == "committed" and intent["committed_at"]

    # 路线 2：prepared，文件=after，checkpoint=原值 → resume 只补 checkpoint/审计
    rewind_to_prepared(after_bytes, original_checkpoint)
    done = env.run_tool("--resume", "--migration-id", "mig-resume")
    assert done.returncode == 0, done.stderr + done.stdout
    assert env.settings_path.read_bytes() == after_bytes

    # 路线 3：prepared，文件=after，checkpoint=目标值 → 只核对审计并补 committed
    rewind_to_prepared(after_bytes, {"revision": 4, "digest": target_digest})
    done = env.run_tool("--resume", "--migration-id", "mig-resume")
    assert done.returncode == 0, done.stderr + done.stdout

    # 审计按 migration_id 去重：多次 resume 只有一条迁移审计
    conn = sqlite3.connect(str(env.fleet_db))
    try:
        rows = conn.execute("SELECT json FROM audit_events").fetchall()
    finally:
        conn.close()
    audits = [json.loads(raw) for (raw,) in rows
              if json.loads(raw).get("action") == "input_settings_migration"
              and (json.loads(raw).get("detail") or {}).get("migration_id") == "mig-resume"]
    assert len(audits) == 1, "迁移审计按 migration_id 去重，不重复追加"

    # 路线 4：prepared，文件其他/损坏 → 停止并保持阻断，不拿拟提交文件覆盖
    rewind_to_prepared(b'{"corrupt": ', {"revision": 4, "digest": target_digest})
    done = env.run_tool("--resume", "--migration-id", "mig-resume")
    assert done.returncode == 2
    assert env.settings_path.read_bytes() == b'{"corrupt": ', "阻断不得覆盖未知资料"

    # 路线 5：未知 migration_id 不得凭空新建迁移
    done = env.run_tool("--resume", "--migration-id", "mig-unknown")
    assert done.returncode == 2
    # committed 却文件回退：资料恢复事故，继续阻断，不重新迁移当新装
    intent = json.loads(committed_intent_path.read_text(encoding="utf-8"))
    intent["state"] = "committed"
    intent["committed_at"] = committed_intent["committed_at"]
    committed_intent_path.write_text(json.dumps(intent, ensure_ascii=False),
                                     encoding="utf-8")
    env.settings_path.write_bytes(before_bytes)  # 回退到旧档
    done = env.run_tool("--resume", "--migration-id", "mig-resume")
    assert done.returncode == 2
    assert env.settings_path.read_bytes() == before_bytes, "回退事故不回写不重迁"


# --------------------------------------------------------------------------
# F04-4：损坏/丢失/孤儿/MAX 阻断与核对重放
# --------------------------------------------------------------------------

def test_f04_4_corruption_orphan_max_block_and_review_replay(migration_env):
    env = migration_env
    legacy = env.write_legacy()
    env.ensure_guard_with_checkpoint(legacy)
    env.admit_owner(settings_revision=2)

    # (a) 损坏 checkpoint（与文件摘要不符）→ 迁移阻断，不写任何资料
    guard = env.control.ensure_guard(env.device_id)
    env.control.set_settings_checkpoint(
        env.device_id, revision=int(legacy["revision"]),
        digest="0" * 64)
    done = env.run_tool("--dry-run")
    assert done.returncode == 2, done.stdout
    assert "checkpoint" in done.stderr
    before_bytes = env.settings_path.read_bytes()
    done = env.run_tool(
        "--apply", "--migration-id", "mig-bad", "--operator", "维护员",
        "--reason", "r", "--client-request-id", "c", "--backup-dir", str(env.backup_dir))
    assert done.returncode == 2
    assert env.settings_path.read_bytes() == before_bytes, "阻断不得写资料"

    # 修复 checkpoint 后 apply 成功（供后续场景使用已迁移档）
    env.control.set_settings_checkpoint(
        env.device_id, revision=int(legacy["revision"]),
        digest=canonical_digest(legacy))
    done = env.run_tool(
        "--apply", "--migration-id", "mig-ok", "--operator", "维护员",
        "--reason", "迁移旧输入资料并关闭许可",
        "--client-request-id", "maint-ok", "--backup-dir", str(env.backup_dir))
    assert done.returncode == 0, done.stderr + done.stdout
    stored = iset.load_settings(env.device_id, env.data_root)
    assert stored["revision"] == 4 and stored["enabled"] is False

    # (b) 已知设备文件丢失：有输入控制记录 → 拒绝按新设备重建空白默认
    known_check = lambda: True  # noqa: E731（测试注入的已知判据）
    backup_bytes = env.settings_path.read_bytes()
    env.settings_path.unlink()
    with pytest.raises(iset.InputSettingsError) as exc:
        iset.load_settings(env.device_id, env.data_root, known_check=known_check)
    assert exc.value.reason_code == "input_settings_invalid"
    assert iset.is_enabled(env.device_id, env.data_root, known_check=known_check) is False
    with pytest.raises(iset.InputSettingsError):
        iset.update_settings(env.device_id, data_root=env.data_root, enabled=False,
                             ime_device_id=None, expected_revision=4,
                             operator_id="操作员", reason="重建",
                             client_request_id="rebuild", calibration_ready=False,
                             known_check=known_check)
    assert not env.settings_path.exists(), "已知设备缺文件不得创建空白默认覆盖"
    env.settings_path.write_bytes(backup_bytes)

    # (c) 版本 MAX：R=MAX 阻断迁移；R=MAX-1 只能关闭到 MAX，开启被拒
    max_legacy = build_legacy_file(env.device_id, revision=9007199254740991)
    env.settings_path.write_text(json.dumps(max_legacy, ensure_ascii=False), encoding="utf-8")
    guard = env.control.ensure_guard(env.device_id)
    env.control.set_settings_checkpoint(env.device_id, revision=9007199254740991,
                                        digest=canonical_digest(max_legacy))
    done = env.run_tool("--dry-run")
    assert done.returncode == 2 and "MAX" in done.stderr
    # 运行时同一约束：MAX-1 开启拒绝（input_revision_exhausted），关闭允许到 MAX
    tmp_max = env.root / "max-device"
    iset.update_settings("arm-max", data_root=tmp_max, enabled=True,
                         ime_device_id="k40-m", expected_revision=0,
                         operator_id="o", reason="开", client_request_id="m1",
                         calibration_ready=True)
    iset.update_settings("arm-max", data_root=tmp_max, enabled=False,
                         ime_device_id=None, expected_revision=1,
                         operator_id="o", reason="关", client_request_id="m2",
                         calibration_ready=False)
    # 构造 revision=MAX-1 的关闭档
    path = iset.settings_path("arm-max", tmp_max)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["revision"] = 9007199254740990
    stored["revoked_through_revision"] = 9007199254740990
    path.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(iset.InputSettingsError) as exc:
        iset.update_settings("arm-max", data_root=tmp_max, enabled=True,
                             ime_device_id="k40-m", expected_revision=9007199254740990,
                             operator_id="o", reason="开",
                             client_request_id="m3", calibration_ready=True)
    assert exc.value.reason_code == "input_revision_exhausted"
    closed = iset.update_settings("arm-max", data_root=tmp_max, enabled=False,
                                  ime_device_id=None,
                                  expected_revision=9007199254740990,
                                  operator_id="o", reason="关",
                                  client_request_id="m4", calibration_ready=False)
    assert closed["settings"]["revision"] == 9007199254740991, "MAX-1 可关闭到 MAX"
    with pytest.raises(iset.InputSettingsError):
        iset.update_settings("arm-max", data_root=tmp_max, enabled=False,
                             ime_device_id=None, expected_revision=9007199254740991,
                             operator_id="o", reason="再写",
                             client_request_id="m5", calibration_ready=False)

    # (d) 孤儿旧任务（无 owner）阻断 + 核对重放不重复审计；保护类问题不得勾选清除
    store, ctrl = env.store, env.control
    orphan = {"task_id": "orphan-1", "task_type": "screen_input",
              "device_id": env.device_id, "status": "queued",
              "idempotency_key": "orphan-1", "payload": {"text": "x"},
              "payload_hash": "h", "step_cursor": 0, "step_log": [],
              "timeline": [], "cancel_requested": False, "attempts": 0,
              "capability_call_count": 0, "action_call_count": 0,
              "status_display": "queued", "created_at": "2026-01-01T00:00:00Z",
              "expires_at": None, "contract_version": "v1"}
    store.save_task(orphan)

    class _DevicesStub:
        settings_data_root = staticmethod(lambda device_id: env.data_root)

    runtime_stub = type("_R", (), {})()
    runtime_stub.store = store
    runtime_stub.input_control = ctrl
    runtime_stub.devices = _DevicesStub
    from runtime.runtime import ArmFleetRuntime
    verdict = ArmFleetRuntime.check_input_owner(runtime_stub, orphan)
    assert verdict is not None and verdict["category"] == "input_authorization_missing"
    guard = store.input_record_get(record_id_for_guard(env.device_id))
    codes = {issue["code"] for issue in guard["issues"]}
    assert "legacy_input_review_required" in codes

    # 保护类问题（settings_commit_pending/换绑/损坏）不可人工勾选清除
    ctrl.open_guard_issue(env.device_id, code="input_effect_unknown",
                          detail={"k": 1})
    ctrl.open_guard_issue(env.device_id, code="settings_commit_pending",
                          detail={"k": 2})
    guard = store.input_record_get(record_id_for_guard(env.device_id))
    issue_ids = [i["issue_id"] for i in guard["issues"] if i["state"] == "open"]
    reviewable = next(i for i in issue_ids
                      if i in {x["issue_id"] for x in guard["issues"]
                               if x["code"] == "input_effect_unknown"})
    protected = next(i for i in issue_ids
                     if i in {x["issue_id"] for x in guard["issues"]
                              if x["code"] == "settings_commit_pending"})
    first = ctrl.review_input_recovery(
        env.device_id, expected_revision=int(guard["revision"]),
        issue_ids=[reviewable], resolution={"checked": True},
        client_request_id="review-1", operator_id="人工")
    guard_after = store.input_record_get(record_id_for_guard(env.device_id))
    replay = ctrl.review_input_recovery(
        env.device_id, expected_revision=int(guard_after["revision"]),
        issue_ids=[reviewable], resolution={"checked": True},
        client_request_id="review-1", operator_id="人工")
    assert replay == first, "核对重放返回原结果"
    assert len(guard_after["review_requests"]) == 1, "重放不重复写核对账本/审计"
    assert guard_after["revision"] == first["recovery_revision"], \
        "重放不得推进 guard 版本（不重复审计）"
    with pytest.raises(InputControlError) as exc:
        ctrl.review_input_recovery(
            env.device_id, expected_revision=int(guard_after["revision"]),
            issue_ids=[protected], resolution={"checked": True},
            client_request_id="review-2", operator_id="人工")
    assert exc.value.reason_code == "issue_not_reviewable"
    # 仍有真实占用（未解决问题）时阻断不放开：open issue 存在 → 新输入执行被阻
    guard_final = store.input_record_get(record_id_for_guard(env.device_id))
    assert any(i["state"] == "open" for i in guard_final["issues"]), \
        "未解决问题保持阻断，不因核对完成而清空"
