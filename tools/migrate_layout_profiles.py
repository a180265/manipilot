#!/usr/bin/env python3
"""§11.14.2 一次性迁移工具：旧 ui_targets/screen_proof → 新布局草稿（离线执行）。

用法（禁止在服务运行时 apply；工具按 armfleet.db-wal 近期写入拒绝）：
  PYTHONPATH=arm_fleet .venv-wx/bin/python arm_fleet/tools/migrate_layout_profiles.py \
      --data-root /path/to/copy --device arm-01 --dry-run
  PYTHONPATH=arm_fleet .venv-wx/bin/python arm_fleet/tools/migrate_layout_profiles.py \
      --data-root /path/to/copy --device arm-01 --apply --report /path/to/report.json

语义：dry-run 零写入；apply 先完整备份原文件；坐标域/校准来源无法证明的条目
归档并输出 pending_review，不推断已通过；中途重跑按原摘要复用同一迁移结果，
同名旧文件已改变则停止不覆盖；只创建 draft 版本，绝不自动启用，
绝不把旧 E 验收记录转换为新输入许可；原文件一律保留。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

TARGET_FILE = "ui_targets.json"
PROOF_FILE = "screen_proof.json"
BUNDLE_FILE = "calibration_bundle.json"
STALE_SERVICE_S = 120.0


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _kv_connect(data_root: Path) -> sqlite3.Connection:
    db = data_root / "layout.db"
    conn = sqlite3.connect(str(db), check_same_thread=False)
    conn.execute("CREATE TABLE IF NOT EXISTS layout_kv "
                 "(k TEXT PRIMARY KEY, json TEXT)")
    conn.commit()
    return conn


def _kv_get(conn, key):
    row = conn.execute("SELECT json FROM layout_kv WHERE k=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _kv_put(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO layout_kv (k, json) VALUES (?, ?)",
                 (key, json.dumps(value, ensure_ascii=False, sort_keys=True)))
    conn.commit()


def _service_recently_active(data_root: Path) -> bool:
    now = time.time()
    for suffix in ("-wal", "-shm", ""):
        db = data_root / f"armfleet.db{suffix}"
        if db.exists() and now - db.stat().st_mtime < STALE_SERVICE_S:
            return True
    return False


def _convert_entry(entry: dict) -> dict | None:
    """可证明为 screen_norm 的条目 → 草稿局部修订；否则 None（待复核）。"""
    nx, ny = entry.get("nx"), entry.get("ny")
    if not isinstance(nx, (int, float)) or not isinstance(ny, (int, float)):
        return None
    if not 0.0 <= nx <= 1.0 or not 0.0 <= ny <= 1.0:
        return None
    if entry.get("schemaVersion") not in (None, 1):
        return None
    return {"item_id": entry.get("targetId"), "kind": "target",
            "screen_norm": {"x": nx, "y": ny},
            "target_mode": entry.get("targetMode"),
            "source_evidence_ids": [entry["testEvidence"]["evidenceId"]] if
            isinstance(entry.get("testEvidence"), dict)
            and entry["testEvidence"].get("evidenceId") else [],
            "converted_from": "ui_targets.v1"}


def migrate(data_root: Path, device_id: str, apply: bool,
            report_path: Path | None) -> int:
    dev_dir = data_root / "devices" / device_id
    sources = {name: dev_dir / name
               for name in (TARGET_FILE, PROOF_FILE, BUNDLE_FILE)}
    digests = {name: _digest(p) for name, p in sources.items()}
    if digests[TARGET_FILE] is None:
        print(f"错误：{dev_dir / TARGET_FILE} 不存在，无可迁移档案", file=sys.stderr)
        return 2

    conn = None
    record_key = f"migration_record:{device_id}"
    if (data_root / "layout.db").exists():
        conn = _kv_connect(data_root)
        previous = _kv_get(conn, record_key)
    elif not apply:
        conn = None
        previous = None  # dry-run 绝不创建 layout.db
    else:
        conn = _kv_connect(data_root)
        previous = None
    if previous is not None:
        if previous["source_digests"] != digests:
            print("错误：原文件摘要与上次迁移不一致（旧文件已改变）；"
                  "按规格停止，不覆盖既有迁移结果。", file=sys.stderr)
            return 3
        print(f"检测到同摘要迁移结果，直接复用（migration_id="
              f"{previous['migration_id']}），不重复创建草稿。")
        report = previous["report"]
    else:
        bundle = None
        if digests[BUNDLE_FILE] is not None:
            try:
                bundle = json.loads(sources[BUNDLE_FILE].read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                bundle = None
        calibration_ok = bool(bundle) and bool(
            bundle.get("schemaVersion")) and bundle.get("status") == "ready"
        targets = json.loads(sources[TARGET_FILE].read_text(encoding="utf-8"))
        convertible, pending = [], []
        for target_id, entry in (targets.get("entries") or {}).items():
            converted = _convert_entry(entry)
            if converted is None or not calibration_ok:
                pending.append({
                    "item_id": target_id,
                    "reason": "坐标域或校准来源无法证明" if converted is None
                    else "校准 bundle 缺失或未达 ready，无法证明登记来源",
                    "original_entry": entry})
            else:
                convertible.append(converted)
        draft = {
            "contract_version": "device-layout-adaptation.v1",
            "device_id": device_id,
            "kind": "migration_draft",
            "state": "draft",
            "source_digests": digests,
            "calibration_proven": calibration_ok,
            "pages": {"legacy_registered": convertible},
            "pending_review": pending,
            "created_at": _now(),
            "note": "迁移只生成草稿：不启用、不切换指针、不转换旧 E 记录为输入许可",
        }
        report = {
            "migration_id": hashlib.sha256(
                json.dumps(digests, sort_keys=True).encode()).hexdigest()[:16],
            "device_id": device_id, "mode": "apply" if apply else "dry-run",
            "source_digests": digests,
            "required_calibration": BUNDLE_FILE,
            "calibration_proven": calibration_ok,
            "converted": convertible, "pending_review": pending,
            "target": "device-layout-adaptation.v1 draft（legacy_registered 页）",
            "created_at": _now(),
        }
        if apply:
            if _service_recently_active(data_root):
                print("错误：armfleet.db 近 2 分钟内有写入，服务疑似在运行；"
                      "apply 须设备离线/服务未运行。", file=sys.stderr)
                return 4
            backup_dir = dev_dir / f"migration_backup-{_now().replace(':', '')}"
            backup_dir.mkdir(parents=True, exist_ok=True)
            for name, path in sources.items():
                if path.exists():
                    shutil.copy2(path, backup_dir / name)
            report["backup_dir"] = str(backup_dir)
            _kv_put(conn, f"migration_draft:{device_id}:{report['migration_id']}",
                    draft)
            _kv_put(conn, record_key,
                    {"migration_id": report["migration_id"],
                     "source_digests": digests, "report": report})
            print(f"已备份原文件至 {backup_dir}；草稿 migration_id="
                  f"{report['migration_id']} 已写入 layout.db（未启用）。")
        else:
            print("dry-run：未写入任何文件。")

    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"报告已写入 {report_path}")
    print(f"汇总：可转换 {len(report['converted'])} 项，待复核 "
          f"{len(report['pending_review'])} 项，校准来源"
          f"{'已证明' if report['calibration_proven'] else '未证明'}。")
    return 0


def _migrate_layout_kv(layout_db: Path, apply: bool, report_path) -> int:
    """R2-19（D 包交付）：旧 layout_kv 自由 KV → typed state 离线迁移入口。

    只读扫描逐记录输出源摘要与键映射；不合格记录原样归档待复核；
    可转换旧定义只生成草稿（不继承执行资格）；不删除/不改写旧 KV；
    同根中断后重跑按 journal 幂等续跑（LayoutStateStore.migrate_legacy_kv）。
    """
    import sys as _sys

    root = Path(__file__).resolve().parents[1]
    if str(root) not in _sys.path:
        _sys.path.insert(0, str(root))
    from runtime.layout_state import LayoutStateStore

    class _KVReader:
        """layout_kv 表只读包装（scan_all 协议）。"""

        def __init__(self, db_path: Path):
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            self._rows = [
                (row["k"], json.loads(row["json"]))
                for row in conn.execute(
                    "SELECT k, json FROM layout_kv ORDER BY k")
            ]
            conn.close()

        def scan_all(self):
            return iter(self._rows)

    if not layout_db.exists():
        print(f"错误：布局库不存在：{layout_db}", file=_sys.stderr)
        return 2
    store = LayoutStateStore(layout_db, data_root=layout_db.parent)
    try:
        report = store.migrate_legacy_kv(_KVReader(layout_db), apply=apply)
    finally:
        store.close()
    mode = "apply" if apply else "dry-run"
    converted = sum(1 for e in report["entries"]
                    if e.get("disposition") == "converted")
    archived = sum(1 for e in report["entries"]
                   if e.get("disposition") == "archived")
    print(f"layout_kv 迁移（{mode}）：处理 {report['processed']} 条，"
          f"可转换 {converted} 条（仅草稿，不继承执行资格），"
          f"归档待复核 {archived} 条；旧 KV 原样保留。")
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        print(f"报告已写入 {report_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="旧输入目标档案一次性迁移")
    parser.add_argument("--data-root", type=Path, default=None,
                        help="旧 ui_targets/screen_proof 档案迁移的数据根")
    parser.add_argument("--device", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--layout-kv-db", type=Path, default=None,
                        help="R2-19：改为执行旧 layout_kv → typed state 迁移"
                             "（指向布局 SQLite 库；--data-root/--device 不适用）")
    args = parser.parse_args()
    if args.layout_kv_db is not None:
        if args.dry_run == args.apply:
            parser.error("必须且只能选择 --dry-run 或 --apply 之一")
        return _migrate_layout_kv(args.layout_kv_db, args.apply, args.report)
    if not args.data_root or not args.device:
        parser.error("档案迁移需要 --data-root 与 --device")
    if args.dry_run == args.apply:
        parser.error("必须且只能选择 --dry-run 或 --apply 之一")
    return migrate(args.data_root, args.device, args.apply, args.report)


if __name__ == "__main__":
    sys.exit(main())
