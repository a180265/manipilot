"""布局模板不可变记录/草稿/资产（2026-10-03 规格 camera-binding §4/layout-reuse L02）。

模板为不可变版本记录（发布后不可改）；草稿为可变工作副本，发布时转为不可
变版本。资产为模板/草稿的二进制文件。CAS 指针控制版本选择。

FIX-14①（2026-10-04 实施单）：已发布模板/资产一律 INSERT + 唯一约束冲突检测，
绝不用 INSERT OR REPLACE 原地覆盖：
- 同 (template_id, version) 同内容摘要 → 复用既有不可变行（幂等）；
- 同 (template_id, version) 异内容摘要 → 409 version_conflict，改资料只能出新版本；
- 资产 sha256 基于原字节（PNG/JPEG bytes），不再对 hex/JSON 求替代摘要；
- 同 (owner, asset_id) 同字节复用，异字节 409 asset_conflict。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from runtime.util import iso, new_id, utc_now


class TemplateError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(f"{code}: {message}")


def canonical(value) -> str:
    import hashlib
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """资产摘要：原字节 SHA-256（FIX-14②：不对 hex/base64/JSON 求替代摘要）。"""
    return hashlib.sha256(data).hexdigest()


def collect_asset_ids(node) -> set:
    """递归收集页面定义中引用的资产 ID（asset_id/asset_ref/empty_input_asset_ref）。"""
    ids: set = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("asset_id", "asset_ref", "empty_input_asset_ref") \
                    and isinstance(value, str) and value:
                ids.add(value)
            elif key == "asset" and isinstance(value, dict) \
                    and isinstance(value.get("id"), str) and value["id"]:
                ids.add(value["id"])
            else:
                ids |= collect_asset_ids(value)
    elif isinstance(node, list):
        for value in node:
            ids |= collect_asset_ids(value)
    return ids


class LayoutTemplateStore:
    def __init__(self, db_path: Path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS layout_templates (
                    template_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    json TEXT NOT NULL,
                    PRIMARY KEY (template_id, version)
                );
                CREATE TABLE IF NOT EXISTS layout_drafts (
                    draft_id TEXT PRIMARY KEY,
                    json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS layout_assets (
                    draft_id TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    data BLOB NOT NULL,
                    PRIMARY KEY (draft_id, asset_id)
                );
            """)
            self._conn.commit()

    def _write(self, sql, params=()):
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _query_one(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        """R2-25：释放本 Store 自己的连接（幂等）。"""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def save_template(self, template_id: str, version: int, record: dict,
                      content_sha: str | None = None) -> str:
        """INSERT + 唯一约束冲突检测。返回 "committed" 或 "reused"。

        同 (template_id, version) 同摘要 → 复用原行（幂等，不覆盖）；
        异摘要 → TemplateError("version_conflict", status=409)。
        """
        sha = content_sha or canonical(record)
        payload = json.dumps(record, ensure_ascii=False)
        with self._lock:
            row = self._query_one(
                "SELECT sha256 FROM layout_templates "
                "WHERE template_id=? AND version=?", (template_id, version))
            if row is not None:
                if row["sha256"] == sha:
                    return "reused"
                raise TemplateError(
                    "version_conflict",
                    f"模板 {template_id} v{version} 已存在且内容不同；"
                    "已发布版本不可覆盖，请发布新版本", status=409)
            try:
                self._conn.execute(
                    "INSERT INTO layout_templates (template_id, version, sha256, json) "
                    "VALUES (?, ?, ?, ?)", (template_id, version, sha, payload))
                self._conn.commit()
            except sqlite3.IntegrityError:
                # 并发竞态：唯一约束兜底，重新按摘要判定
                self._conn.rollback()
                row = self._query_one(
                    "SELECT sha256 FROM layout_templates "
                    "WHERE template_id=? AND version=?", (template_id, version))
                if row is not None and row["sha256"] == sha:
                    return "reused"
                raise TemplateError(
                    "version_conflict",
                    f"模板 {template_id} v{version} 并发写入冲突", status=409)
        return "committed"

    def get_template(self, template_id: str, version: int) -> dict | None:
        row = self._query_one(
            "SELECT json FROM layout_templates WHERE template_id=? AND version=?",
            (template_id, version))
        return json.loads(row["json"]) if row else None

    def get_template_sha(self, template_id: str, version: int) -> str | None:
        row = self._query_one(
            "SELECT sha256 FROM layout_templates WHERE template_id=? AND version=?",
            (template_id, version))
        return row["sha256"] if row else None

    def list_template_versions(self, template_id: str) -> list[int]:
        rows = self._query(
            "SELECT version FROM layout_templates WHERE template_id=? "
            "ORDER BY version", (template_id,))
        return [int(r["version"]) for r in rows]

    def list_templates(self) -> list[dict]:
        rows = self._query(
            "SELECT template_id, version, json FROM layout_templates "
            "ORDER BY template_id, version DESC")
        seen = set()
        result = []
        for row in rows:
            tid = row["template_id"]
            if tid in seen:
                continue
            seen.add(tid)
            result.append(json.loads(row["json"]))
        return result

    def save_draft(self, draft_id: str, record: dict) -> None:
        self._write(
            "INSERT OR REPLACE INTO layout_drafts (draft_id, json) VALUES (?, ?)",
            (draft_id, json.dumps(record, ensure_ascii=False)))

    def get_draft(self, draft_id: str) -> dict | None:
        row = self._query_one(
            "SELECT json FROM layout_drafts WHERE draft_id=?", (draft_id,))
        return json.loads(row["json"]) if row else None

    def save_asset(self, draft_id: str, asset_id: str, data: bytes) -> str:
        """资产 sha256 = 原字节 SHA-256；同 ID 同字节复用，异字节 409 冲突。"""
        sha = sha256_bytes(data)
        with self._lock:
            row = self._query_one(
                "SELECT sha256 FROM layout_assets WHERE draft_id=? AND asset_id=?",
                (draft_id, asset_id))
            if row is not None:
                if row["sha256"] == sha:
                    return sha
                raise TemplateError(
                    "asset_conflict",
                    f"资产 {asset_id} 已存在且字节不同；请改用新 asset ID",
                    status=409)
            try:
                self._conn.execute(
                    "INSERT INTO layout_assets (draft_id, asset_id, sha256, data) "
                    "VALUES (?, ?, ?, ?)", (draft_id, asset_id, sha,
                                            sqlite3.Binary(data)))
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                row = self._query_one(
                    "SELECT sha256 FROM layout_assets WHERE draft_id=? AND asset_id=?",
                    (draft_id, asset_id))
                if row is not None and row["sha256"] == sha:
                    return sha
                raise TemplateError(
                    "asset_conflict",
                    f"资产 {asset_id} 并发写入冲突", status=409)
        return sha

    def get_asset(self, draft_id: str, asset_id: str) -> bytes | None:
        row = self._query_one(
            "SELECT data FROM layout_assets WHERE draft_id=? AND asset_id=?",
            (draft_id, asset_id))
        return bytes(row["data"]) if row else None

    def list_assets(self, draft_id: str) -> list[dict]:
        rows = self._query(
            "SELECT asset_id, sha256, LENGTH(data) AS size "
            "FROM layout_assets WHERE draft_id=? ORDER BY asset_id", (draft_id,))
        return [{"asset_id": r["asset_id"], "sha256": r["sha256"],
                 "size_bytes": int(r["size"])} for r in rows]


class LayoutTemplateService:
    """L02：模板不可变记录/草稿/资产 CAS 管理。"""

    def __init__(self, store: LayoutTemplateStore):
        self._store = store

    def publish(self, request: dict) -> dict:
        template_id = request.get("template_id")
        version = request.get("version")
        pages = request.get("pages")
        if not template_id or not isinstance(version, int) or version < 1 \
                or not isinstance(pages, dict) or not pages:
            raise TemplateError("invalid_request",
                                "template_id/version(正整数)/pages 必填")
        metadata = request.get("metadata") or {}
        # 内容摘要不含发布时间：同 ID+version 同内容幂等复用，异内容明确冲突
        content_sha = canonical({"template_id": template_id,
                                 "version": version, "pages": pages,
                                 "metadata": metadata})
        record = {"template_id": template_id, "version": version,
                  "pages": pages, "metadata": metadata,
                  "published_at": iso(utc_now()),
                  "sha256": content_sha}
        # 先备齐资产再提交模板记录：发布只复制页面实际引用的资产到模板
        # 所有者（草稿资产不随发布导出其余部分），避免出现缺资产的半成品版本
        draft_id = request.get("draft_id")
        if draft_id:
            for asset_id in sorted(collect_asset_ids(pages)):
                data = self._store.get_asset(draft_id, asset_id)
                if data is None:
                    raise TemplateError(
                        "asset_missing",
                        f"页面引用的资产 {asset_id} 在草稿 {draft_id} 中不存在")
                self._store.save_asset(template_id, asset_id, data)
        status = self._store.save_template(template_id, version, record,
                                           content_sha=content_sha)
        return {"template_id": template_id, "version": version,
                "sha256": content_sha, "reused": status == "reused"}

    def create_draft(self, request: dict) -> dict:
        draft_id = new_id("draft")
        record = {"draft_id": draft_id,
                  "template_id": request.get("template_id"),
                  "pages": request.get("pages") or {},
                  "metadata": request.get("metadata") or {},
                  "created_at": iso(utc_now())}
        self._store.save_draft(draft_id, record)
        return record

    def get_draft(self, draft_id: str) -> dict | None:
        return self._store.get_draft(draft_id)

    def update_draft_metadata(self, draft_id: str, metadata: dict) -> dict:
        draft = self._store.get_draft(draft_id)
        if draft is None:
            raise TemplateError("draft_not_found", f"草稿 {draft_id} 不存在",
                                status=404)
        draft["metadata"] = metadata
        self._store.save_draft(draft_id, draft)
        return draft

    def write_draft_page(self, draft_id: str, state: str, page: dict) -> dict:
        draft = self._store.get_draft(draft_id)
        if draft is None:
            raise TemplateError("draft_not_found", f"草稿 {draft_id} 不存在",
                                status=404)
        draft.setdefault("pages", {})[state] = page
        self._store.save_draft(draft_id, draft)
        return draft

    def upload_asset(self, draft_id: str, asset_id: str, data: bytes) -> str:
        return self._store.save_asset(draft_id, asset_id, data)

    def get_asset(self, draft_id: str, asset_id: str) -> bytes | None:
        return self._store.get_asset(draft_id, asset_id)
