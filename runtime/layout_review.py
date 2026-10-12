"""L06/§11.11.5：当前资料问题（layout-review.v1，R2-17 冻结形状）。

问题记录 = records:issue（issue_id UUID/device_id/kind/resource_ref/item/
revision/state/reason_code/opened_at/resolution），落在既有
layout_review_records（kind 仅 operation/issue/review）。

- `review_revision` 是本设备资料问题集合 revision（typed kind=review_revision，
  J.revision 从 0 起、SQL=J+1；每次实际问题集合变化同事务 +1）。
- POST review：按冻结 review_request 闭集校验；验证 issue 全部属于本设备、
  仍 open；新证据必须来自同设备/当前绑定；仅解决有证据的项，未解决保留
  resolution=null、原 reason_code 不覆写；无一可解决 → 409 items_unverified。
- 同事务更新 issues、集合 revision、逐 issue revision，并保存原 issues/版本/
  请求 hash/身份/证据/原响应到不可变 review 记录；CAS 冲突不做部分提交。
- 同 client_request_id 重放返回原响应（幂等台账）。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from runtime.layout_state import (
    LayoutStateError,
    LayoutStateStore,
    content_sha,
    state_key,
)
from runtime.util import iso, utc_now

ISSUE_KINDS = ("search_region", "adaptation", "item")


class ReviewError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status = status
        self.code = code
        self.message = message


def _now(clock) -> datetime:
    value = (clock or utc_now)()
    if isinstance(value, datetime) and value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


class LayoutReviewService:
    """资料问题集合与人工核对（零机械动作）。"""

    def __init__(self, store, state: LayoutStateStore | None = None,
                 clock=None):
        self._store = store
        self._state = state
        self._clock = clock

    # ---------------- 问题集合 revision（集合 CAS） ----------------
    def _revision_record(self, device_id: str) -> dict:
        if self._state is None:
            return {"revision": 0}
        row = self._state.state_get(
            "review_revision", state_key("review_revision", [device_id]))
        if row is None:
            return {"revision": 0}
        return row

    def collection_revision(self, device_id: str) -> int:
        return int(self._revision_record(device_id).get("revision") or 0)

    def _bump_revision(self, device_id: str, principal: str) -> int:
        """集合 revision +1（J.revision 从 0；SQL=J+1 的 CAS）。"""
        row = self._revision_record(device_id)
        current_j = int(row.get("revision") or 0)
        record = {k: v for k, v in row.items() if k != "revision"}
        record["device_id"] = device_id
        record["revision"] = current_j + 1
        record["updated_at"] = iso(_now(self._clock))
        record["updated_by"] = principal
        if self._state is None:
            return current_j + 1
        key = state_key("review_revision", [device_id])
        existing = self._state.state_row("review_revision", key)
        if existing is None:
            # 首次使用：先建零值行（J.revision=0，SQL revision=1）
            self._state.state_insert(
                "review_revision", key, device_id, None,
                {"device_id": device_id, "revision": 0,
                 "updated_at": iso(_now(self._clock)),
                 "updated_by": principal})
        # SQL revision 恒为 J.revision+1：CAS 期望值=当前 J+1
        self._state.state_update("review_revision", key, device_id, None,
                                 current_j + 1, record)
        return current_j + 1

    # ---------------- 问题开立/列举 ----------------
    def open_issue(self, device_id: str, kind: str, resource_ref: dict | None,
                   item: dict | None, revision: int | None,
                   reason_code: str) -> dict:
        if kind not in ISSUE_KINDS:
            raise ReviewError(422, "layout_request_invalid",
                              f"kind 必须属于 {ISSUE_KINDS}")
        issue_id = str(uuid.uuid4())
        seq = len(self.list_issues(device_id)) + 1
        record = {"issue_id": issue_id, "device_id": device_id, "kind": kind,
                  "resource_ref": resource_ref, "item": item,
                  "revision": revision if revision is not None else seq,
                  "state": "open",
                  "reason_code": reason_code,
                  "opened_at": iso(_now(self._clock)), "resolution": None}
        if self._state is not None:
            self._state.issue_save(record, insert=True)
        self._store.save(f"review_issue:{device_id}:{issue_id}", record)
        return record

    def _load_issue(self, device_id: str, issue_id: str) -> dict | None:
        if self._state is not None:
            row = self._state.issue_get(issue_id)
            if row is not None:
                return row if row.get("device_id") == device_id else None
        return self._store.load(f"review_issue:{device_id}:{issue_id}")

    def _save_issue(self, device_id: str, record: dict) -> None:
        if self._state is not None:
            self._state.issue_save(record, insert=False)
        self._store.save(f"review_issue:{device_id}:{record['issue_id']}",
                         record)

    def list_issues(self, device_id: str, state: str | None = None) -> list[dict]:
        issues: dict[str, dict] = {}
        if self._state is not None:
            for row in self._state.issue_scan(device_id):
                issues[row["issue_id"]] = row
        for key, record in (self._store.scan_prefix(
                f"review_issue:{device_id}:")
                if hasattr(self._store, "scan_prefix") else []):
            issues.setdefault(record.get("issue_id") or key, record)
        items = [record for record in issues.values()
                 if state is None or record.get("state") == state]
        items.sort(key=lambda r: (r.get("opened_at") or "", r.get("issue_id")))
        return items

    # ---------------- 人工核对（冻结 review_request） ----------------
    def resolve(self, device_id: str, request: dict, principal: str,
                *, evidence_check=None) -> dict:
        """request 为冻结 review_request 闭集（route 层已做 schema 校验）。

        返回冻结 review_response 的业务字段
        {results: [{issue_id, state, reason_code}], revision}。
        """
        client_request_id = request["client_request_id"]
        issue_ids = list(request["issue_ids"])
        expected_revision = int(request["expected_revision"])
        evidence_ids = list(request["evidence_ids"])
        reason = str(request["reason"])
        if not evidence_ids or not issue_ids:
            # route 层 schema 已拒；服务层直调同样按契约拒绝
            raise ReviewError(422, "layout_request_invalid",
                              "review 必须携带 issue_ids 与 evidence_ids 闭集")
        replay = self._replay(client_request_id, device_id, request)
        if replay is not None:
            return replay
        if self._state is not None and self._state.blocked_reasons:
            raise ReviewError(503, "layout_archive_invalid",
                              "存储完整性未恢复：拒绝新写")
        with (self._state.transaction() if self._state is not None
              else _nullcontext()):
            current_revision = self.collection_revision(device_id)
            if current_revision != expected_revision:
                raise ReviewError(
                    409, "layout_revision_conflict",
                    f"资料问题集合 revision 冲突（当前 {current_revision}，"
                    f"期望 {expected_revision}）")
            # 新证据必须来自同设备（证据行/归档证据）
            evidenced = []
            for evidence_id in evidence_ids:
                row = None
                if self._state is not None:
                    for candidate in self._state.state_scan(
                            "evidence", device_id):
                        if candidate["value"].get("evidence_id") == evidence_id:
                            row = candidate["value"]
                            break
                if row is None:
                    record = self._store.load(f"review_issue:{device_id}:{evidence_id}")
                    row = record
                archived = None
                getter = evidence_check
                if callable(getter):
                    archived = getter(evidence_id)
                if row is None and (archived is None
                                    or (archived or {}).get("device_id")
                                    != device_id):
                    raise ReviewError(
                        403, "layout_permission_denied",
                        f"证据 {evidence_id} 不属于本设备：不能跨设备核对")
                evidenced.append(evidence_id)
            results = []
            resolved_any = False
            touched = []
            for issue_id in issue_ids:
                record = self._load_issue(device_id, str(issue_id))
                if record is None:
                    results.append({"issue_id": str(issue_id),
                                    "state": "open",
                                    "reason_code": "issue_not_found"})
                    continue
                if record.get("state") == "resolved":
                    results.append({"issue_id": record["issue_id"],
                                    "state": "open",
                                    "reason_code": record.get("reason_code")})
                    continue
                record["state"] = "resolved"
                record["revision"] = int(record.get("revision") or 1) + 1
                record["resolution"] = {
                    "operation_id": None,
                    "reviewed_by": principal,
                    "reviewed_at": iso(_now(self._clock)),
                    "evidence_ids": evidenced,
                    "reason": reason}
                results.append({"issue_id": record["issue_id"],
                                "state": "resolved",
                                "reason_code": None})
                touched.append(record)
            if not touched:
                raise ReviewError(
                    409, "layout_items_unverified",
                    "没有一项可由提交证据解除：问题不存在或已是 resolved")
            for record in touched:
                # 仅写 typed store（layout_review_records，同连接同事务）；
                # legacy KV 镜像在事务外补写（同库双连接同线程不自锁）
                if self._state is not None:
                    self._state.issue_save(record, insert=False)
            new_revision = self._bump_revision(device_id, principal)
            response = {"results": results, "revision": new_revision}
            if self._state is not None:
                # 台账原响应 = 完整 wire:write_response 形状（含 operation_id/
                # contract_version/correlation_id/replayed）
                operation = self._state.operation_prepare(
                    principal_id=principal, device_id=device_id,
                    resource_kind="review", resource_id=device_id,
                    client_request_id=client_request_id,
                    request={k: v for k, v in request.items()
                             if k not in ("correlation_id",)},
                    planned_response=response, files=[],
                    pointer_before=None, pointer_after=None, mutations=[],
                    version_ref=None)
                response = {**response,
                            "operation_id": operation["operation_id"],
                            "contract_version": "layout-review.v1",
                            "correlation_id": request.get("correlation_id")
                            or response.get("correlation_id")
                            or str(__import__("uuid").uuid4()),
                            "replayed": False}
                self._state.operation_commit(
                    operation["operation_id"], response=response)
            _touched = list(touched)
            _device = device_id
        # 事务外：legacy KV 镜像（只读对账兼容；不参与契约语义）
        for record in _touched:
            self._store.save(f"review_issue:{_device}:{record['issue_id']}",
                             record)
        return {**response, "replayed": False}

    def _replay(self, client_request_id: str, device_id: str,
                request: dict) -> dict | None:
        if self._state is None or not client_request_id:
            return None
        operation = self._state.operation_get_by_request(client_request_id)
        if operation is None:
            return None
        if operation.get("device_id") != device_id:
            raise ReviewError(403, "layout_permission_denied",
                              "client_request_id 已被其他设备使用")
        response = operation.get("response")
        if response is None:
            raise ReviewError(409, "layout_request_conflict",
                              "同编号操作未完成提交；请按 operation 查询对账")
        return {**response, "replayed": True}


class _nullcontext:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
