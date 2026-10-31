"""Arm Fleet -> WX 人工接管桥。

Arm Fleet 只负责把操作者明确确认的接管请求转发给 WX；会话接管事实
仍由 WX 事件库保存。未配置回调、HTTP 超时或服务端 5xx 时，不能把
摄像头查看会话误报成已经接管。
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

from runtime.config import task_callback_base_url

from runtime.errors import ApiError
from runtime.util import iso


def _now_iso() -> str:
    return iso(datetime.now(timezone.utc))


class TakeoverBridge:
    def __init__(self, config, audit):
        self._config = config
        self._audit = audit
        self.client: httpx.AsyncClient | None = None

    def _base_url(self) -> str:
        return task_callback_base_url(self._config)

    def _path(self, conversation_id: str) -> str:
        template = str(self._config.get(
            "wx_callback_takeover_path",
            "/internal/conversations/{conversation_id}/takeover",
        ))
        return template.format(conversation_id=conversation_id)

    async def request(self, *, device_id: str, conversation_id: str,
                      customer_id: str, operator_id: str, reason: str,
                      correlation_id: str, client_request_id: str,
                      context: dict[str, Any] | None = None) -> dict[str, Any]:
        base = self._base_url().strip()
        if not base:
            self._audit.record(
                "human_takeover_request", operator_id=operator_id,
                device_id=device_id, outcome="failed", reason="未配置 WX 接管回调地址",
                correlation_id=correlation_id, client_request_id=client_request_id,
                detail={"conversation_id": conversation_id},
            )
            raise ApiError("wx_callback_not_configured")

        payload: dict[str, Any] = {
            "conversation_id": conversation_id,
            "customer_id": customer_id,
            "operator": operator_id,
            "reason": reason,
            "observed_at": _now_iso(),
            "correlation_id": correlation_id,
            "client_request_id": client_request_id,
            "context": {
                "device_id": device_id,
                **(context or {}),
            },
        }
        url = base.rstrip("/") + self._path(conversation_id)
        client = self.client or httpx.AsyncClient(timeout=5, trust_env=False)
        try:
            response = await client.post(url, json=payload)
            try:
                body = response.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            if response.status_code >= 500:
                self._audit.record(
                    "human_takeover_request", operator_id=operator_id,
                    device_id=device_id, outcome="failed",
                    reason=f"WX 接管回调服务异常：HTTP {response.status_code}",
                    correlation_id=correlation_id, client_request_id=client_request_id,
                    detail={"conversation_id": conversation_id, "http_status": response.status_code},
                )
                raise ApiError("takeover_unknown", f"WX 接管回调 HTTP {response.status_code}")
            if response.status_code >= 400:
                detail = str(
                    body.get("error_display") or body.get("message")
                    or body.get("说明") or f"HTTP {response.status_code}"
                )
                self._audit.record(
                    "human_takeover_request", operator_id=operator_id,
                    device_id=device_id, outcome="rejected",
                    reason=f"WX 拒绝接管：{detail}",
                    correlation_id=correlation_id, client_request_id=client_request_id,
                    detail={"conversation_id": conversation_id, "http_status": response.status_code},
                )
                raise ApiError("takeover_rejected", detail)

            # 业务 may return HTTP 200 while explicitly declining to apply the
            # takeover (for example, a stale conversation state). Do not
            # convert that application-level rejection into a success.
            if body.get("accepted") is False:
                detail = str(
                    body.get("error_display") or body.get("message")
                    or body.get("说明") or "WX 未接受人工接管请求"
                )
                self._audit.record(
                    "human_takeover_request", operator_id=operator_id,
                    device_id=device_id, outcome="rejected",
                    reason=f"WX 拒绝接管：{detail}",
                    correlation_id=correlation_id, client_request_id=client_request_id,
                    detail={"conversation_id": conversation_id, "http_status": response.status_code},
                )
                raise ApiError("takeover_rejected", detail)

            self._audit.record(
                "human_takeover_request", operator_id=operator_id,
                device_id=device_id, outcome="success", reason="WX 已接收人工接管请求",
                correlation_id=correlation_id, client_request_id=client_request_id,
                detail={"conversation_id": conversation_id, "wx_response": body},
            )
            return {
                "accepted": True,
                "status": "accepted",
                "status_display": "WX 已接管",
                "device_id": device_id,
                "conversation_id": conversation_id,
                "customer_id": customer_id,
                "correlation_id": correlation_id,
                "wx_response": body,
            }
        except ApiError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            self._audit.record(
                "human_takeover_request", operator_id=operator_id,
                device_id=device_id, outcome="failed", reason=f"WX 接管回调异常：{exc}",
                correlation_id=correlation_id, client_request_id=client_request_id,
                detail={"conversation_id": conversation_id},
            )
            raise ApiError("takeover_unknown", str(exc)) from exc
        finally:
            if self.client is None:
                await client.aclose()
