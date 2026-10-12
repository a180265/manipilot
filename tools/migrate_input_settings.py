"""旧输入设置档案一次性迁移工具（规格 §4.9 / 输入附件迁移契约；FIX-04）。

只交付维护窗口内的离线迁移：停写、G01 全资源排他锁、严格旧白名单、
SQLite backup API（含 WAL）、原文件字节备份、维护意图持久；旧请求只留
摘要 tombstone；关闭推进到 R+1；中断按原 migration_id resume；迁后不
恢复开启、不清旧取消/输入未知。

用法（必须显式指定离线维护资料配置，不读现场默认配置）：

    PYTHONPATH=arm_fleet .venv-wx/bin/python arm_fleet/tools/migrate_input_settings.py \
        --config /path/to/offline-config.json --device arm-01 --dry-run

    ... --apply --migration-id "$MIGRATION_ID" --operator "$OPERATOR_ID" \
        --reason "迁移旧输入资料并关闭许可" --client-request-id "$MAINTENANCE_REQUEST_ID" \
        --backup-dir /path/to/controlled-backup

    ... --resume --migration-id "$MIGRATION_ID"

config JSON 字段（全部必填，变量由维护人员显式指定）：
    {"data_root": "/path/to/data", "fleet_db": "/path/to/fleet.db",
     "ime_db": "/path/to/ime.db"}

退出码：0 成功/可迁移；2 资料核验阻断（保持阻断，不写任何资料）；1 参数/IO 错误。
本工具不创建 owner/IME 任务/旧请求响应，不执行任何机械或输入动作。
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

CONTRACT_VERSION = "device-input-settings.v1"
BASELINE_VERSION = "input-settings-migration.v1"
BACKUP_MANIFEST_VERSION = "input-settings-migration-backup.v1"
INTENT_VERSION = "input-settings-migration-intent.v1"
MAX_REVISION = 9007199254740991  # MAX 预留给关闭；R=MAX 无法 R+1 → 阻断

# 旧格式白名单（legacy_file/legacy_entry：字段精确集合，缺失撤销字段）
LEGACY_TOP_KEYS = {"contract_version", "device_id", "ime_device_id", "enabled",
                   "revision", "updated_at", "operator_id", "reason",
                   "client_request_id", "requests"}
LEGACY_ENTRY_KEYS = {"enabled", "revision", "operator_id", "at", "digest"}

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 2


def canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def canonical_digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def raw_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def is_digest(value) -> bool:
    return isinstance(value, str) and len(value) == 64 \
        and all(ch in "0123456789abcdef" for ch in value)


def is_timestamp(value) -> bool:
    if not isinstance(value, str) or "T" not in value:
        return False
    from runtime.util import parse_iso
    try:
        return parse_iso(value) is not None
    except ValueError:
        return False


def is_id(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 256


class Blocked(Exception):
    """资料核验阻断：保持阻断状态，不写任何资料。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# ---------------- 锁（G01 全资源排他锁，保持到退出） ----------------

class ResourceLocks:
    """与 InputControl.acquire_resource_locks 同名锁文件：活动实例必然冲突。"""

    def __init__(self):
        self._handles: list = []

    def acquire(self, fleet_db: Path, ime_db: Path, data_root: Path) -> None:
        targets = sorted({
            fleet_db.resolve().parent / (fleet_db.name + ".input-owner.lock"),
            ime_db.resolve().parent / (ime_db.name + ".input-owner.lock"),
            data_root.resolve() / ".input-owner.lock",
        }, key=str)
        for path in targets:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                self.release()
                raise Blocked(f"资源排他锁被占用（Fleet/IME 服务未停写？）：{path}")
            self._handles.append(handle)

    def release(self) -> None:
        for handle in self._handles:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self._handles = []


@contextlib.contextmanager
def locked(fleet_db: Path, ime_db: Path, data_root: Path):
    locks = ResourceLocks()
    locks.acquire(fleet_db, ime_db, data_root)
    try:
        yield locks
    finally:
        locks.release()


# ---------------- 原子落盘（fsync 文件 + 父目录） ----------------

def atomic_write_bytes(path: Path, payload: bytes, *, suffix: str = ".tmp") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=suffix, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


# ---------------- 严格旧格式白名单 ----------------

def validate_legacy_file(device_id: str, stored) -> dict:
    """旧模块档案白名单：字段精确集合 + 类型/范围/唯一版本/时间不倒退/一致审计。

    revision=0 空白对象不是合法旧写入文件；trim 别名碰撞先核对即拒绝。
    """
    def bad(reason: str):
        raise Blocked(f"旧档案未通过白名单校验：{reason}")

    if not isinstance(stored, dict):
        bad("档案必须是对象")
    keys = set(stored)
    if keys - LEGACY_TOP_KEYS:
        bad(f"旧档案存在白名单之外字段：{sorted(keys - LEGACY_TOP_KEYS)}（含撤销字段的新格式请直接使用，不得二次迁移）")
    if LEGACY_TOP_KEYS - keys:
        bad(f"旧档案缺少字段：{sorted(LEGACY_TOP_KEYS - keys)}")
    if stored["contract_version"] != CONTRACT_VERSION:
        bad("contract_version 不受支持")
    if stored["device_id"] != device_id:
        bad(f"device_id 不匹配（档案={stored['device_id']} 请求={device_id}）")
    if not is_id(stored["ime_device_id"]):
        bad("ime_device_id 必须为非空手机编号（空白对象/未绑定档案不接受）")
    if type(stored["enabled"]) is not bool:
        bad("enabled 必须为布尔值")
    revision = stored["revision"]
    if type(revision) is not int or revision < 1 or revision > MAX_REVISION:
        bad("revision 必须为正整数（revision=0 空白对象不作为合法旧写入文件接受）")
    if not is_timestamp(stored["updated_at"]):
        bad("updated_at 必须为带时区时间")
    for key in ("operator_id", "reason", "client_request_id"):
        value = stored[key]
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            bad(f"{key} 必须为非空且无首尾空白")
    requests = stored["requests"]
    if not isinstance(requests, dict) or not requests:
        bad("requests 必须为非空对象（空账本不接受）")
    seen_revisions: dict[int, str] = {}
    digests_by_key: dict[str, str] = {}
    previous_at = None
    latest = None
    for key, entry in requests.items():
        if not isinstance(key, str) or not key or len(key) > 160:
            bad("requests 键必须为非空且 ≤160 字符")
        if key != key.strip():
            bad(f"旧模块曾保存未 trim 的请求原键：{key!r}；原键与其 trim 别名均作为拒绝标记")
        trimmed = key.strip()
        if trimmed in digests_by_key:
            bad(f"请求原键与其 trim 别名碰撞：{key!r}")
        digests_by_key[trimmed] = key
        if not isinstance(entry, dict):
            bad("request entry 必须为对象")
        if set(entry) != LEGACY_ENTRY_KEYS:
            bad(f"request entry 字段必须恰为 {sorted(LEGACY_ENTRY_KEYS)}：{key!r}")
        if type(entry["enabled"]) is not bool or type(entry["revision"]) is not int \
                or entry["revision"] < 1:
            bad(f"request entry enabled/revision 类型或范围无效：{key!r}")
        if not is_id(entry["operator_id"]) or not is_timestamp(entry["at"]) \
                or not is_digest(entry["digest"]):
            bad(f"request entry operator_id/at/digest 无效：{key!r}")
        if entry["revision"] in seen_revisions:
            bad(f"request revision 重复：{entry['revision']}")
        if entry["revision"] > revision:
            bad(f"request revision 超出当前文件版本：{key!r}")
        seen_revisions[entry["revision"]] = key
        if previous_at is not None and entry["at"] < previous_at:
            bad(f"按 revision 顺序时间倒退：{key!r}")
        previous_at = entry["at"]
        latest = entry if latest is None or entry["revision"] > latest["revision"] else latest
    if latest is None or latest["revision"] != revision:
        bad("最新 entry revision 必须等于当前文件版本")
    if latest["enabled"] != stored["enabled"] or latest["operator_id"] != stored["operator_id"] \
            or latest["at"] != stored["updated_at"]:
        bad("最新 entry 与当前值/审计不一致")
    return {"latest": latest, "revisions": seen_revisions}


# ---------------- Fleet DB 记录读取与可信版本清单 ----------------

def read_fleet_records(fleet_db: Path, device_id: str) -> dict:
    if not fleet_db.exists():
        raise Blocked(f"Fleet DB 不存在：{fleet_db}")
    conn = sqlite3.connect(f"file:{fleet_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT json FROM input_records WHERE device_id=?", (device_id,)).fetchall()
    finally:
        conn.close()
    owners, intents, checkpoint = [], [], None
    for (raw,) in rows:
        try:
            record = json.loads(raw)
        except ValueError:
            raise Blocked(f"Fleet DB 存在无法解析的输入控制记录（先核对修复）")
        kind = record.get("kind")
        if kind == "owner":
            if type(record.get("settings_revision")) is not int:
                raise Blocked(f"owner 记录 settings_revision 无效：{record.get('record_id')}")
            owners.append(record)
        elif kind == "device_guard":
            checkpoint = record.get("settings_checkpoint")
        elif kind == "settings_write":
            intents.append(record)
    return {"owners": owners, "intents": intents, "checkpoint": checkpoint}


def compute_version_inventory(file_stored: dict, fleet_records: dict,
                              blockers: list) -> dict:
    """R 来源（锁定资料内重读）：file/ledger、owner、checkpoint、已提交 intent。"""
    file_revision = int(file_stored["revision"])
    semantic = canonical_digest(file_stored)
    max_owner = 0
    for owner in fleet_records["owners"]:
        rev = int(owner["settings_revision"])
        max_owner = max(max_owner, rev)
        if rev > file_revision:
            blockers.append(
                f"owner {owner.get('record_id')} settings_revision={rev} 超出旧文件版本 "
                f"{file_revision}：先核对，不能仅取 max 掩盖来源矛盾")
    checkpoint = fleet_records["checkpoint"]
    if checkpoint is not None:
        if type(checkpoint.get("revision")) is not int or not is_digest(checkpoint.get("digest")):
            blockers.append(f"checkpoint 结构无效：{checkpoint}")
        else:
            if checkpoint["digest"] != semantic or checkpoint["revision"] != file_revision:
                blockers.append(
                    f"checkpoint 版本/摘要与旧文件不符（checkpoint r{checkpoint['revision']}"
                    f"/{checkpoint['digest'][:12]}… vs 文件 r{file_revision}）：先核对")
    ledger_revision = file_revision
    max_intent = 0
    for intent in fleet_records["intents"]:
        state = intent.get("state")
        if state == "prepared":
            blockers.append(
                f"存在未对账 prepared 设置提交意图 {intent.get('client_request_id')}："
                "先按原请求对账，再迁移")
            continue
        if state != "applied":
            continue
        after_revision = int(intent.get("after_revision") or 0)
        max_intent = max(max_intent, after_revision)
        if after_revision > file_revision:
            blockers.append(
                f"已提交意图 {intent.get('record_id')} 出现未来版本 after_revision="
                f"{after_revision} > 文件 {file_revision}：先核对")
        elif after_revision == file_revision and intent.get("after_digest") != semantic:
            blockers.append(
                f"已提交意图 {intent.get('record_id')} 同版本不同摘要：先核对")
    r = max(file_revision, ledger_revision, max_owner, checkpoint["revision"] if
            isinstance(checkpoint, dict) and type(checkpoint.get("revision")) is int else 0,
            max_intent)
    inventory = {
        "file_revision": file_revision,
        "ledger_revision": ledger_revision,
        "owners": [{"record_id": o["record_id"], "settings_revision": int(o["settings_revision"]),
                    "record_sha256": canonical_digest(o)} for o in fleet_records["owners"]],
        "checkpoint": deepcopy(checkpoint) if isinstance(checkpoint, dict) else None,
        "applied_intents": [{"record_id": i.get("record_id"),
                             "after_revision": int(i.get("after_revision") or 0),
                             "after_digest": i.get("after_digest"),
                             "record_sha256": canonical_digest(i)}
                            for i in fleet_records["intents"] if i.get("state") == "applied"],
    }
    return {"R": r, "inventory": inventory}


# ---------------- 备份 ----------------

def sqlite_backup(src: Path, dst: Path) -> None:
    if dst.exists():
        raise Blocked(f"备份不可覆盖已存在文件：{dst}")
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
    finally:
        source.close()


def verify_sqlite_backup(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise Blocked(f"SQLite 备份完整性校验失败：{path} → {result}")
    finally:
        conn.close()


def fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def build_backups(device_id: str, migration_id: str, backup_dir: Path,
                  settings_path: Path, fleet_db: Path, ime_db: Path,
                  source_semantic: str, inventory: dict) -> tuple[dict, str, Path]:
    backup_dir = backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    names = {
        "settings_file": f"{device_id}-input-settings-{migration_id}.json",
        "fleet_db": f"{device_id}-fleet-{migration_id}.sqlite",
        "ime_db": f"{device_id}-ime-{migration_id}.sqlite",
    }
    artifacts = {}
    # settings 原文件按字节复制
    dst = backup_dir / names["settings_file"]
    if dst.exists():
        if raw_digest(dst) != raw_digest(settings_path):
            raise Blocked(f"备份已存在且内容不同（不可覆盖）：{dst}")
    else:
        atomic_write_bytes(dst, settings_path.read_bytes())
    artifacts["settings_file"] = {"source_ref": str(settings_path.resolve()),
                                  "backup_ref": str(dst),
                                  "sha256": raw_digest(dst),
                                  "byte_length": dst.stat().st_size,
                                  "method": "byte_copy"}
    # Fleet/IME DB 用 SQLite backup API（含 WAL 已提交内容）
    for key, src in (("fleet_db", fleet_db), ("ime_db", ime_db)):
        dst = backup_dir / names[key]
        if not src.exists():
            raise Blocked(f"待备份数据库不存在：{src}")
        if dst.exists():
            pass  # 幂等：同迁移编号重跑只核对
        else:
            sqlite_backup(src, dst)
        verify_sqlite_backup(dst)
        fsync_file(dst)
        artifacts[key] = {"source_ref": str(src.resolve()),
                          "backup_ref": str(dst),
                          "sha256": raw_digest(dst),
                          "byte_length": dst.stat().st_size,
                          "method": "sqlite_backup"}
    manifest = {
        "contract_version": BACKUP_MANIFEST_VERSION,
        "migration_id": migration_id,
        "device_id": device_id,
        "created_at": now_iso(),
        "source_file_semantic_digest": source_semantic,
        "artifacts": artifacts,
        "version_inventory": inventory,
    }
    manifest_bytes = canonical_bytes(manifest)  # 规范 JSON UTF-8，不追加换行
    manifest_path = backup_dir / f"{device_id}-backup-manifest-{migration_id}.json"
    if manifest_path.exists():
        if manifest_path.read_bytes() != manifest_bytes:
            raise Blocked(f"备份清单已存在且内容不同（不可覆盖）：{manifest_path}")
    else:
        atomic_write_bytes(manifest_path, manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    # 备份完整性复核：原字节摘要与可读性
    for key, artifact in artifacts.items():
        path = Path(artifact["backup_ref"])
        if not path.exists() or raw_digest(path) != artifact["sha256"]:
            raise Blocked(f"备份复核失败（摘要不一致）：{artifact['backup_ref']}")
    return manifest, manifest_sha, manifest_path


# ---------------- 维护意图 ----------------

def intent_dir(data_root: Path, device_id: str, migration_id: str) -> Path:
    key = json.dumps([device_id, migration_id], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return data_root.resolve() / "maintenance" / "input-control" / digest


def load_intent(data_root: Path, device_id: str, migration_id: str) -> dict | None:
    path = intent_dir(data_root, device_id, migration_id) / "intent.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        raise Blocked(f"维护意图存在但无法解析（保持阻断，勿删除）：{path}")


def build_intent(device_id: str, migration_id: str, before_stored: dict,
                 proposed_file: dict, manifest: dict, manifest_sha: str,
                 manifest_ref: str) -> dict:
    return {
        "contract_version": INTENT_VERSION,
        "migration_id": migration_id,
        "device_id": device_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "before_digest": canonical_digest(before_stored),
        "after_digest": canonical_digest(proposed_file),
        "backup_manifest_ref": manifest_ref,
        "backup_manifest_sha256": manifest_sha,
        "backup_manifest": manifest,
        "proposed_file": proposed_file,
        "state": "prepared",
        "committed_at": None,
    }


def save_intent(data_root: Path, intent: dict) -> None:
    path = intent_dir(data_root, intent["device_id"], intent["migration_id"]) / "intent.json"
    atomic_write_bytes(path, canonical_bytes(intent))


# ---------------- 拟关闭文件（新格式 + migration_baseline） ----------------

def build_proposed_file(device_id: str, legacy: dict, r: int, manifest: dict,
                        manifest_sha: str, operator_id: str, reason: str,
                        client_request_id: str, migration_id: str) -> dict:
    closed_at = now_iso()
    closed_value = {
        "enabled": False,
        "ime_device_id": legacy["ime_device_id"],  # 保留原授权手机
        "revision": r + 1,
        "revoked_through_revision": r + 1,
        "updated_at": closed_at,
    }
    legacy_ids = {}
    for key, entry in legacy["requests"].items():
        legacy_ids[key] = canonical_digest(entry)  # SHA-256(完整旧 entry 规范 JSON)
    proposed = {
        "contract_version": CONTRACT_VERSION,
        "device_id": device_id,
        "ime_device_id": legacy["ime_device_id"],
        "enabled": False,
        "revision": r + 1,
        "revoked_through_revision": r + 1,
        "updated_at": closed_at,
        "operator_id": operator_id,
        "reason": reason,
        "client_request_id": client_request_id,
        "migration_baseline": {
            "contract_version": BASELINE_VERSION,
            "revision_floor": r + 1,
            "closed_saved_value": closed_value,
            "operator_id": operator_id,
            "reason": reason,
            "client_request_id": client_request_id,
            "backup_manifest_ref": str(manifest["artifacts"]["settings_file"]["backup_ref"]),
            "backup_manifest_sha256": manifest_sha,
            "legacy_request_ids": legacy_ids,
            "migration_id": migration_id,
        },
        "requests": {},
    }
    # 运行时读取层同源校验：结构/不变量不合法绝不允许落盘
    from runtime import input_settings as iset
    try:
        iset._validate_stored(device_id, proposed)
    except ValueError as exc:
        raise Blocked(f"拟关闭文件未通过运行时校验：{exc}")
    return proposed


# ---------------- Fleet DB 同事务：checkpoint + 迁移审计 ----------------

AUDIT_ACTION = "input_settings_migration"


def find_migration_audits(conn: sqlite3.Connection, migration_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT json FROM audit_events WHERE json LIKE ?",
        (f'%{migration_id}%',)).fetchall()
    found = []
    for (raw,) in rows:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if event.get("action") == AUDIT_ACTION and \
                (event.get("detail") or {}).get("migration_id") == migration_id:
            found.append(event)
    return found


def commit_db_updates(fleet_db: Path, device_id: str, proposed: dict,
                      migration_id: str, operator_id: str, reason: str,
                      client_request_id: str, r: int, before_digest: str,
                      manifest_sha: str) -> str:
    """同一事务：更新 guard checkpoint + 按 migration_id 去重的迁移审计。

    返回 "committed"（本调用提交）或 "already"（同编号审计已存在且一致）。
    """
    guard_id = f"guard:{device_id}"
    after_digest = canonical_digest(proposed)
    conn = sqlite3.connect(str(fleet_db), isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = find_migration_audits(conn, migration_id)
            if existing:
                detail = existing[0].get("detail") or {}
                if detail.get("after_digest") != after_digest or \
                        int(detail.get("after_revision") or 0) != r + 1:
                    raise Blocked(
                        f"同 migration_id 已存在不同来源的迁移审计：{migration_id}")
                conn.execute("ROLLBACK")
                return "already"
            row = conn.execute(
                "SELECT json, revision FROM input_records WHERE record_id=?",
                (guard_id,)).fetchone()
            if row is None:
                raise Blocked(f"Fleet DB 缺少 guard 记录（{guard_id}）：先启动核对创建")
            guard = json.loads(row[0])
            guard_revision = int(row[1])
            guard["settings_checkpoint"] = {"revision": r + 1, "digest": after_digest}
            conn.execute(
                "UPDATE input_records SET json=?, revision=? WHERE record_id=?",
                (json.dumps(guard, ensure_ascii=False), guard_revision + 1, guard_id))
            event = {
                "audit_id": None,
                "action": AUDIT_ACTION,
                "action_display": "输入设置迁移",
                "operator_id": operator_id,
                "operator_role": "system",
                "outcome": "success",
                "outcome_display": "成功",
                "device_id": device_id,
                "task_id": None,
                "reason": reason,
                "correlation_id": None,
                "client_request_id": client_request_id,
                "detail": {"migration_id": migration_id,
                           "before_revision": r, "after_revision": r + 1,
                           "before_digest": before_digest,
                           "after_digest": after_digest,
                           "backup_manifest_sha256": manifest_sha,
                           "legacy_request_count": len(
                               proposed["migration_baseline"]["legacy_request_ids"])},
                "occurred_at": now_iso(),
            }
            conn.execute(
                "INSERT INTO audit_events (occurred_at, json) VALUES (?, ?)",
                (event["occurred_at"], json.dumps(event, ensure_ascii=False)))
            conn.execute("COMMIT")
            return "committed"
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def read_checkpoint(fleet_db: Path, device_id: str) -> dict | None:
    conn = sqlite3.connect(f"file:{fleet_db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT json FROM input_records WHERE record_id=?", (f"guard:{device_id}",)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    guard = json.loads(row[0])
    checkpoint = guard.get("settings_checkpoint")
    return checkpoint if isinstance(checkpoint, dict) else None


# ---------------- 主体 ----------------

def load_config(path: str) -> dict:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"无法读取维护配置 {path}：{exc}")
    for key in ("data_root", "fleet_db", "ime_db"):
        value = config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise SystemExit(f"维护配置缺少必填字段 {key}（不使用现场默认配置）")
    return config


def settings_file_for(data_root: Path, device_id: str) -> Path:
    return data_root / "devices" / device_id / "input_settings.json"


def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    data_root = Path(config["data_root"]).resolve()
    fleet_db = Path(config["fleet_db"]).resolve()
    ime_db = Path(config["ime_db"]).resolve()
    device_id = args.device
    if not device_id or device_id != device_id.strip():
        raise SystemExit("--device 必须为非空且无首尾空白")
    report = {"mode": args.mode, "device_id": device_id,
              "migration_id": getattr(args, "migration_id", None)}
    try:
        if args.mode in ("apply", "resume") and not args.migration_id:
            raise SystemExit(f"--{args.mode} 必须提供 --migration-id")
        if args.mode == "apply":
            for field in ("operator", "reason", "client_request_id", "backup_dir"):
                if not getattr(args, field):
                    raise SystemExit(f"--apply 必须提供 --{field.replace('_', '-')}")
            if len(args.migration_id) > 160 or not args.migration_id.strip() \
                    or args.migration_id != args.migration_id.strip():
                raise SystemExit("--migration-id 必须为非空、无首尾空白且 ≤160 字符")

        with locked(fleet_db, ime_db, data_root):
            settings_path = settings_file_for(data_root, device_id)
            if not settings_path.exists():
                raise Blocked(
                    f"设置文件不存在：{settings_path}（只有真正从未建立资料的新设备才缺文件；"
                    "新设备不需要迁移，也不得以空白对象绕过历史检查）")
            try:
                legacy = json.loads(settings_path.read_text(encoding="utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise Blocked(f"设置文件无法解析（保持原文件，不覆盖）：{exc}")
            existing = load_intent(data_root, device_id, args.migration_id) \
                if getattr(args, "migration_id", None) else None
            if existing is not None:
                # 同迁移编号：只核对来源一致性；推进必须走 --resume
                report.update({"state": existing.get("state"),
                               "intent_path": str(intent_dir(data_root, device_id,
                                                             args.migration_id))})
                if existing.get("device_id") != device_id:
                    raise Blocked("同 migration_id 的维护意图属于其他设备：拒绝")
                if args.mode != "resume":
                    disk_semantic = canonical_digest(legacy)
                    baseline = legacy.get("migration_baseline") \
                        if isinstance(legacy, dict) else None
                    same_source = (
                        disk_semantic in (existing.get("after_digest"),
                                          existing.get("backup_manifest", {})
                                          .get("source_file_semantic_digest"))
                        or (existing.get("state") == "committed"
                            and isinstance(baseline, dict)
                            and baseline.get("migration_id") == args.migration_id))
                    if not same_source:
                        raise Blocked("同 migration_id 但来源摘要/设备不同："
                                      "拒绝并保留原意图，不新建同名迁移")
                    report["note"] = ("同迁移编号：返回既有意图状态；"
                                      "未完成的推进必须用 --resume，不回写旧关闭文件")
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                    return EXIT_OK if existing.get("state") == "committed" else EXIT_BLOCKED
            # 新格式档案（含 revoked_through_revision 或 migration_baseline）
            # 不是旧档：拒绝二次迁移。resume 按原意图推进，跳过旧档白名单
            # （文件此时可能已是拟提交的 after 状态）。
            resuming = args.mode == "resume" and existing is not None
            source_semantic = None
            inventory = None
            r = None
            if not resuming:
                if isinstance(legacy, dict) and (
                        "revoked_through_revision" in legacy
                        or "migration_baseline" in legacy):
                    raise Blocked("档案已是新格式（含撤销字段/迁移基线）：无需也不得二次迁移")
                validate_legacy_file(device_id, legacy)
                source_semantic = canonical_digest(legacy)
                fleet_records = read_fleet_records(fleet_db, device_id)
                audit = compute_version_inventory(
                    legacy, fleet_records, report.setdefault("blockers", []))
                r, inventory = audit["R"], audit["inventory"]
                report.update({"format": "legacy-whitelisted", "R": r,
                               "close_revision": r + 1,
                               "backup_scope": [str(settings_file_for(data_root, device_id).resolve()),
                                                str(fleet_db), str(ime_db)]})
                if r >= MAX_REVISION:
                    raise Blocked(f"R={r} 已达 MAX（9007199254740991）：无法 R+1，维护阻断；"
                                  "MAX-1 只能生成关闭的 MAX 基线，不得清零")
                if report["blockers"]:
                    raise Blocked("；".join(report["blockers"]))

                if args.mode == "dry-run":
                    report["eligible"] = True
                    report["note"] = "dry-run 只读：未生成意图/基线/备份"
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                    return EXIT_OK

            if args.mode == "apply":
                backup_dir = Path(args.backup_dir).resolve()
                manifest, manifest_sha, _manifest_path = build_backups(
                    device_id, args.migration_id, backup_dir, settings_path,
                    fleet_db, ime_db, source_semantic, inventory)
                proposed = build_proposed_file(
                    device_id, legacy, r, manifest, manifest_sha, args.operator,
                    args.reason, args.client_request_id, args.migration_id)
                intent = build_intent(device_id, args.migration_id, legacy, proposed,
                                      manifest, manifest_sha,
                                      str(backup_dir / f"{device_id}-backup-manifest-"
                                          f"{args.migration_id}.json"))
                save_intent(data_root, intent)  # 先持久 prepared 意图（fsync）
                report["intent_path"] = str(intent_dir(data_root, device_id,
                                                       args.migration_id))
            else:  # resume
                intent = load_intent(data_root, device_id, args.migration_id)
                if intent is None:
                    raise Blocked(
                        f"未找到迁移意图：migration_id={args.migration_id}（中断只能按原 "
                        "migration_id resume；不存在则不得凭空新建）")
                if intent.get("device_id") != device_id:
                    raise Blocked("维护意图 device_id 与 --device 不一致")
                proposed = intent.get("proposed_file")
                if not isinstance(proposed, dict):
                    raise Blocked("维护意图缺少拟提交文件（保持阻断）")
                if canonical_digest(proposed) != intent.get("after_digest"):
                    raise Blocked("维护意图 proposed_file 与 after_digest 不一致（损坏）：保持阻断")
                if intent.get("state") == "committed":
                    # committed 后核对：文件/检查点回退=资料恢复事故，继续阻断
                    on_disk_now = json.loads(settings_path.read_text(encoding="utf-8"))
                    checkpoint_now = read_checkpoint(fleet_db, device_id)
                    target = {"revision": int(proposed["revision"]),
                              "digest": canonical_digest(proposed)}
                    rolled_back = (canonical_digest(on_disk_now) != intent.get("after_digest")
                                   or checkpoint_now != target)
                    report.update({"state": "committed",
                                   "file_matches_baseline": not rolled_back})
                    if rolled_back:
                        raise Blocked(
                            "迁移已 committed 但文件/检查点回退：资料恢复事故，"
                            "继续阻断，不重新迁移当新装")
                    report["note"] = "已迁移完成：输入保持关闭；旧请求编号只保留 tombstone"
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                    return EXIT_OK

            # —— 中断路线表：按文件/checkpoint 现场决定唯一处理 ——
            on_disk = json.loads(settings_path.read_text(encoding="utf-8"))
            disk_semantic = canonical_digest(on_disk)
            after_digest = intent["after_digest"]
            before_digest = intent.get("before_digest")
            original_checkpoint = (((intent.get("backup_manifest") or {})
                                    .get("version_inventory") or {})
                                   .get("checkpoint"))
            target_checkpoint = {"revision": int(proposed["revision"]),
                                 "digest": canonical_digest(proposed)}
            if disk_semantic == after_digest:
                pass  # 文件=after：不再替换文件（prepared，文件=after 路线）
            elif disk_semantic == before_digest:
                # prepared，文件=before，checkpoint=原值：resume 继续同一次关闭
                # （不改编号/版本/来源）
                atomic_write_bytes(settings_path, canonical_bytes(proposed))
            else:
                raise Blocked(
                    "维护意图与磁盘文件均不匹配（文件其他/丢失/损坏，或 checkpoint "
                    "与各路线均不匹配）：停止并保持阻断，不拿拟提交文件覆盖未知资料")
            # —— 同一事务补 checkpoint + 迁移审计 ——
            checkpoint = read_checkpoint(fleet_db, device_id)
            if checkpoint != target_checkpoint:
                if original_checkpoint is not None and checkpoint != original_checkpoint:
                    raise Blocked(
                        f"checkpoint 与意图记录的原始 checkpoint 均不匹配（{checkpoint}）："
                        "保持阻断，不拿目标值覆盖未知资料")
                if original_checkpoint is None and checkpoint is not None:
                    raise Blocked(
                        f"checkpoint 出现（{checkpoint}）但意图记录的原始 checkpoint 为空："
                        "保持阻断")
                commit_db_updates(fleet_db, device_id, proposed, args.migration_id,
                                  _operator_of(proposed), _reason_of(proposed),
                                  _crid_of(proposed, args.migration_id),
                                  int(proposed["revision"]) - 1, before_digest,
                                  intent.get("backup_manifest_sha256") or "")
            # DB 持久提交后才允许 committed（committed_at 才允许非 null）
            intent["state"] = "committed"
            intent["committed_at"] = now_iso()
            intent["updated_at"] = intent["committed_at"]
            save_intent(data_root, intent)
            report.update({"state": "committed",
                           "closed_revision": int(proposed["revision"]),
                           "note": ("迁移完成：输入保持关闭，不自动恢复开启；"
                                    "旧取消/输入未知与孤儿任务保持原状，"
                                    "旧请求编号只保留 tombstone")})
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return EXIT_OK
    except Blocked as exc:
        report["blocked"] = exc.reason
        print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        return EXIT_BLOCKED


def _operator_of(proposed: dict) -> str:
    return str(proposed.get("operator_id") or "system")


def _reason_of(proposed: dict) -> str:
    return str(proposed.get("reason") or "迁移旧输入资料并关闭许可")


def _crid_of(proposed: dict, migration_id: str) -> str:
    return str(proposed.get("client_request_id") or migration_id)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="旧输入设置档案一次性迁移（停写维护窗口；不自动开启）")
    parser.add_argument("--config", required=True,
                        help="离线维护资料配置 JSON（data_root/fleet_db/ime_db 全显式）")
    parser.add_argument("--device", required=True, help="设备编号")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_const", const="dry-run", dest="mode",
                      help="只读报告：格式、R、拟关闭版本、备份范围与阻断项")
    mode.add_argument("--apply", action="store_const", const="apply", dest="mode",
                      help="执行迁移（需 --migration-id/--operator/--reason/"
                           "--client-request-id/--backup-dir）")
    mode.add_argument("--resume", action="store_const", const="resume", dest="mode",
                      help="按原 migration_id 续办未完成迁移")
    parser.add_argument("--migration-id", help="迁移编号（apply/resume 必填）")
    parser.add_argument("--operator", help="维护操作者（apply 必填，入基线审计）")
    parser.add_argument("--reason", help="维护原因（apply 必填，入基线审计）")
    parser.add_argument("--client-request-id", help="维护请求编号（apply 必填）")
    parser.add_argument("--backup-dir", help="受控备份目录（apply 必填）")
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
