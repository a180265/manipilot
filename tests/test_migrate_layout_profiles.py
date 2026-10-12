"""§11.14.2 迁移工具验收：dry-run 零写入 / apply 备份+草稿 / 重跑复用 / 变更即停。"""
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "migrate_layout_profiles.py"


@pytest.fixture
def data_root(tmp_path):
    root = tmp_path / "data"
    dev = root / "devices" / "arm-01"
    dev.mkdir(parents=True)
    ui = {"schemaVersion": 1, "deviceId": "arm-01",
          "updatedAt": "2026-09-28T00:33:55",
          "entries": {
              "ime_delete_key": {"targetId": "ime_delete_key",
                                 "nx": 0.1878, "ny": 0.745,
                                 "targetMode": "hover_only",
                                 "schemaVersion": 1,
                                 "testEvidence": {"evidenceId": "ev-1"}},
              "bad_coord": {"targetId": "bad_coord", "nx": 5.0, "ny": 0.1,
                            "testEvidence": {"evidenceId": "ev-2"}}}}
    (dev / "ui_targets.json").write_text(
        json.dumps(ui, ensure_ascii=False), encoding="utf-8")
    bundle = {"schemaVersion": 3, "status": "ready", "fingerprint": "fp-1"}
    (dev / "calibration_bundle.json").write_text(
        json.dumps(bundle), encoding="utf-8")
    # 旧 armfleet.db 设为陈旧，避免"服务疑似在运行"拒绝
    db = root / "armfleet.db"
    db.write_bytes(b"")
    import os
    stale = time.time() - 3600
    os.utime(db, (stale, stale))
    return root


def run(root, *args):
    return subprocess.run(
        [sys.executable, str(TOOL), "--data-root", str(root), *args],
        capture_output=True, text=True)


def test_dry_run_reports_without_writing(data_root):
    before = sorted(p.name for p in data_root.rglob("*"))
    result = run(data_root, "--device", "arm-01", "--dry-run")
    assert result.returncode == 0, result.stderr
    after = sorted(p.name for p in data_root.rglob("*"))
    assert before == after  # 零写入
    assert "可转换 1 项" in result.stdout
    assert "待复核 1 项" in result.stdout


def test_apply_backs_up_creates_draft_and_preserves_originals(data_root, tmp_path):
    original = (data_root / "devices/arm-01/ui_targets.json").read_bytes()
    report = tmp_path / "report.json"
    result = run(data_root, "--device", "arm-01", "--apply",
                 "--report", str(report))
    assert result.returncode == 0, result.stderr
    # 原文件保留（字节不变）
    assert (data_root / "devices/arm-01/ui_targets.json").read_bytes() == original
    # 备份目录存在
    backups = list((data_root / "devices/arm-01").glob("migration_backup-*"))
    assert len(backups) == 1 and (backups[0] / "ui_targets.json").exists()
    # 草稿写入 layout.db，且只读草稿态
    import sqlite3
    conn = sqlite3.connect(str(data_root / "layout.db"))
    rows = conn.execute("SELECT k, json FROM layout_kv").fetchall()
    drafts = [json.loads(r[1]) for r in rows if r[0].startswith("migration_draft:")]
    assert len(drafts) == 1
    draft = drafts[0]
    assert draft["state"] == "draft"
    assert draft["pages"]["legacy_registered"][0]["item_id"] == "ime_delete_key"
    assert draft["pending_review"][0]["item_id"] == "bad_coord"
    # 报告文件
    assert json.loads(report.read_text(encoding="utf-8"))["converted"]


def test_rerun_same_digest_reuses_and_changed_source_stops(data_root):
    first = run(data_root, "--device", "arm-01", "--apply")
    assert first.returncode == 0, first.stderr
    again = run(data_root, "--device", "arm-01", "--apply")
    assert again.returncode == 0
    assert "直接复用" in again.stdout
    import sqlite3
    conn = sqlite3.connect(str(data_root / "layout.db"))
    drafts = conn.execute("SELECT COUNT(*) FROM layout_kv WHERE k LIKE "
                          "'migration_draft:%'").fetchone()[0]
    assert drafts == 1  # 不重复创建
    # 原文件改变 → 停止不覆盖
    ui = json.loads((data_root / "devices/arm-01/ui_targets.json").read_text())
    ui["entries"]["ime_delete_key"]["nx"] = 0.5
    (data_root / "devices/arm-01/ui_targets.json").write_text(json.dumps(ui))
    third = run(data_root, "--device", "arm-01", "--apply")
    assert third.returncode == 3
    assert "已改变" in third.stderr


def test_apply_refuses_when_calibration_unprovable(tmp_path):
    root = tmp_path / "data"
    dev = root / "devices" / "arm-01"
    dev.mkdir(parents=True)
    (dev / "ui_targets.json").write_text(json.dumps(
        {"schemaVersion": 1, "entries": {
            "k": {"targetId": "k", "nx": 0.5, "ny": 0.5}}}))
    (root / "armfleet.db").write_bytes(b"")
    import os
    stale = time.time() - 3600
    os.utime(root / "armfleet.db", (stale, stale))
    result = run(root, "--device", "arm-01", "--apply")
    assert result.returncode == 0, result.stderr
    assert "待复核 1 项" in result.stdout and "未证明" in result.stdout
    import sqlite3
    conn = sqlite3.connect(str(root / "layout.db"))
    draft = json.loads(conn.execute(
        "SELECT json FROM layout_kv WHERE k LIKE 'migration_draft:%'"
    ).fetchone()[0])
    assert draft["calibration_proven"] is False
    assert draft["pending_review"][0]["item_id"] == "k"  # 不推断已通过
