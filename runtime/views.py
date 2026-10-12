"""实时画面查看会话：只读查看能力。

- 查看会话不获取机械臂租约、不改变任务、不暂停任务、不默认录制。
- 默认有效期 60 秒；到期或摄像头不可用显示“画面不可用”。
- 一个设备同时最多 2 个查看会话。
- 状态属于查看会话：有效/即将过期/等待画面恢复/已关闭/已过期/画面不可用。
- R4-12-E（wx-camera-binding.v1）：经可信代理创建的会话可绑定 WX 登录
  会话（用户名/会话摘要/凭据代际）；绑定会话接受 WX 推送撤销与周期性
  授权复核（退出/禁用/重置/自然到期 → 已建流 fail-closed 停止）。
"""
from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path

from runtime.errors import ApiError
from runtime.status import CAMERA_QUALITY, VIEW_SESSION_STATUS, display
from runtime.util import iso, new_id, parse_iso, utc_now


class ViewSessionManager:
    def __init__(self, store, devices, audit, events, config):
        self._store = store
        self._devices = devices
        self._audit = audit
        self._events = events
        self._config = config
        self._sweeper_task: asyncio.Task | None = None
        self._running = False
        # R4-12-E 授权复核状态：sid -> {"ts": monotonic, "fails": n}
        self._wx_recheck_state: dict[str, dict] = {}
        self._wx_token_cache: str | None = None

    # ---------- 创建 ----------
    def create(
        self,
        *,
        device_id: str,
        camera_id: str | None,
        purpose: str,
        requested_by: str,
        correlation_id: str,
        client_request_id: str,
        operator_role: str = "readonly",
        wx_binding: dict | None = None,
    ) -> dict:
        device = self._devices.get(device_id)
        if camera_id and camera_id != device.get("camera_id"):
            raise ApiError("view_session_device_mismatch", f"摄像头 {camera_id} 不属于设备 {device_id}")
        active = self.active_sessions_for_device(device_id)
        limit = int(self._config.get("view_session_max_per_device", 2))
        if len(active) >= limit:
            raise ApiError("view_session_limit", f"设备 {device_id} 已有 {len(active)} 个查看会话（上限 {limit} 个）")

        ttl = int(self._config.get("view_session_ttl_seconds", 60))
        now = utc_now()
        camera = self._devices.camera(device_id)
        # Real CameraHelper runs as a separate process. In that mode the
        # Arm Fleet camera driver itself is intentionally not opened; the
        # external frame provider is the live source of truth.
        external_source = getattr(camera, "_frame_provider", None)
        camera_ok = (
            bool(camera.is_open()) if hasattr(camera, "is_open") else True
        ) or external_source is not None
        # 新鲜黑帧保持只读查看，不能将等待恢复当成采集故障。
        status = "valid"
        try:
            current_quality = camera._quality if hasattr(camera, "_quality") else "clear"
            status = (
                {"clear": "valid", "waiting": "waiting"}.get(current_quality, "unavailable")
                if camera_ok else "unavailable"
            )
        except Exception:
            status = "valid" if camera_ok else "unavailable"

        # 0.0.0.0/:: are bind addresses, not browser destinations.
        listen_host = str(self._config.get("listen_host") or "127.0.0.1")
        advertised_host = "127.0.0.1" if listen_host in {"0.0.0.0", "::"} else listen_host
        session = {
            "view_session_id": new_id("view"),
            "device_id": device_id,
            "camera_id": device.get("camera_id"),
            "purpose": purpose,
            "requested_by": requested_by,
            "correlation_id": correlation_id,
            "client_request_id": client_request_id,
            "stream_protocol": "mjpeg",
            "stream_url": (
                f"http://{advertised_host}:{self._config.get('listen_port')}"
                f"/v1/camera/view-sessions/{new_id('placeholder')}/stream"
            ),
            "started_at": iso(now),
            "expires_at": iso(now + timedelta(seconds=ttl)),
            "width": getattr(camera, "_width", None),
            "height": getattr(camera, "_height", None),
            # The session advertises the high-rate media plane when it is enabled;
            # the API stream still falls back to the existing sampled source if
            # that optional service is unavailable.
            "frame_rate": round(
                float(self._config.get("camera_preview_target_fps", 30))
                if self._config.get("camera_preview_enabled", False)
                else 1.0 / float(self._config.get("camera_frame_interval_seconds", 0.5)),
                2,
            ),
            "status": status,
            "status_display": display(VIEW_SESSION_STATUS, status),
            "last_frame_at": None,
            "closed_at": None,
            "close_reason": None,
            "contract_version": "camera-view.v1",
        }
        if wx_binding:
            # R4-12-E：仅可信代理注入的绑定入库；契约视图（contract_view）
            # 不外泄绑定字段。原始凭据（会话令牌明文）不入库——只存摘要。
            session.update({
                "wx_bound": True,
                "wx_username": str(wx_binding["username"]),
                "wx_session_id": str(wx_binding["session_id"]),
                "wx_credential_revision": int(
                    wx_binding.get("credential_revision") or 0),
            })
        session["stream_url"] = (
            f"http://{advertised_host}:{self._config.get('listen_port')}"
            f"/v1/camera/view-sessions/{session['view_session_id']}/stream"
        )
        self._store.save_view_session(session)
        self._audit.record(
            "view_session_create",
            operator_id=requested_by,
            device_id=device_id,
            reason=f"创建只读实时画面查看会话（{purpose}）",
            correlation_id=correlation_id,
            client_request_id=client_request_id,
            detail={"view_session_id": session["view_session_id"]},
        )
        return session

    # ---------- 查询 / 状态推导 ----------
    def get(self, view_session_id: str, refresh: bool = True) -> dict:
        session = self._store.get_view_session(view_session_id)
        if not session:
            raise ApiError("view_session_not_found", view_session_id)
        if refresh:
            session = self._refresh_status(session)
        return session

    def _refresh_status(self, session: dict) -> dict:
        if session["status"] in ("closed", "expired"):
            return session
        now = utc_now()
        expires = parse_iso(session["expires_at"])
        if expires and expires <= now:
            if session["status"] != "expired":
                session["status"] = "expired"
                session["status_display"] = display(VIEW_SESSION_STATUS, "expired")
                self._store.save_view_session(session)
                self._audit.record(
                    "view_session_expired",
                    device_id=session["device_id"],
                    reason="查看会话到期未续期，已自动过期",
                    detail={"view_session_id": session["view_session_id"]},
                )
            return session
        advice = int(self._config.get("view_session_renew_advice_seconds", 20))
        if session["status"] == "valid" and expires and (expires - now).total_seconds() <= advice:
            session["status"] = "expiring"
            session["status_display"] = display(VIEW_SESSION_STATUS, "expiring")
            self._store.save_view_session(session)
        return session

    def contract_view(self, session: dict) -> dict:
        """camera-view.v1 契约视图（只返回契约字段）。"""
        keys = [
            "view_session_id", "device_id", "camera_id", "purpose", "stream_protocol",
            "stream_url", "started_at", "expires_at", "width", "height", "frame_rate",
            "status", "status_display", "last_frame_at", "closed_at", "close_reason",
            "contract_version",
        ]
        return {key: session.get(key) for key in keys}

    def update_quality(self, view_session_id: str, quality: dict, frame: dict | None = None) -> dict:
        """只更新仍存活的查看会话；采集期间关闭/过期不能被旧快照覆盖。"""
        session = self.get(view_session_id)
        if session["status"] in ("closed", "expired"):
            return session
        key = quality.get("quality")
        if quality.get("frozen") or (frame and frame.get("frozen")):
            key = "frozen"
        status = {"clear": "valid", "waiting": "waiting"}.get(key, "unavailable")
        expires = parse_iso(session["expires_at"])
        advice = int(self._config.get("view_session_renew_advice_seconds", 20))
        if status == "valid" and expires and (expires - utc_now()).total_seconds() <= advice:
            status = "expiring"
        session["status"] = status
        session["status_display"] = display(VIEW_SESSION_STATUS, status)
        session["close_reason"] = (
            f"画面不可用：摄像头画面异常（{quality.get('quality_display') or '画面异常'}）"
            if status == "unavailable" else None
        )
        if frame is not None and status != "unavailable":
            session["last_frame_at"] = frame["captured_at"]
        self._store.save_view_session(session)
        return session

    async def refresh_camera_status(self, view_session_id: str) -> dict:
        """状态轮询复用 broker 质量检查，不打开摄像头或触发任何设备动作。"""
        session = self.get(view_session_id)
        if session["status"] in ("closed", "expired"):
            return session
        try:
            quality = await self._devices.camera_broker(session["device_id"]).check_quality()
        except Exception:
            return self.mark_unavailable(view_session_id, "画面不可用：摄像头采集异常")
        return self.update_quality(view_session_id, quality)

    async def capture_frame(self, view_session_id: str) -> tuple[dict, dict | None]:
        """经共享 broker 采集并判定同一帧，不用旧缓存质量放行新帧。"""
        session = self.get(view_session_id)
        if session["status"] in ("closed", "expired"):
            return session, None
        device_id = session["device_id"]
        broker = self._devices.camera_broker(device_id)
        try:
            frame = await broker.capture()
            camera = self._devices.camera(device_id)
            analyze = getattr(camera, "_analyze", None)
            classify = getattr(camera, "_classify", None)
            if analyze is not None and classify is not None:
                # 与 broker.check_quality 相同的驱动判定，固定本次帧而非可被
                # 其他订阅者替换的 latest；不复制亮度/冻结阈值。
                key = classify(analyze(frame.get("image_bytes", frame.get("image"))),
                               bool(frame.get("frozen")))
                quality = {"quality": key, "quality_display": display(CAMERA_QUALITY, key)}
            else:
                quality = await broker.check_quality()
        except Exception:
            return self.mark_unavailable(view_session_id, "画面不可用：摄像头采集异常"), None
        return self.update_quality(view_session_id, quality, frame), frame

    def active_sessions_for_device(self, device_id: str) -> list[dict]:
        sessions = (
            self._refresh_status(s) for s in self._store.all_view_sessions()
            if s["device_id"] == device_id
        )
        return [s for s in sessions if s["status"] in ("valid", "expiring", "waiting")]

    # ---------- 续期 / 关闭 ----------
    def renew(self, view_session_id: str, requested_by: str, correlation_id: str | None = None) -> dict:
        session = self.get(view_session_id)
        if session["status"] in ("closed", "expired"):
            raise ApiError("view_session_expired", f"查看会话已{session['status_display']}，请重新创建")
        ttl = int(self._config.get("view_session_ttl_seconds", 60))
        session["expires_at"] = iso(utc_now() + timedelta(seconds=ttl))
        if session["status"] == "expiring":
            session["status"] = "valid"
            session["status_display"] = display(VIEW_SESSION_STATUS, "valid")
        self._store.save_view_session(session)
        self._audit.record(
            "view_session_renew",
            operator_id=requested_by,
            device_id=session["device_id"],
            reason="续期实时画面查看会话",
            correlation_id=correlation_id,
            detail={"view_session_id": view_session_id},
        )
        return session

    def close(self, view_session_id: str, requested_by: str, reason: str) -> dict:
        session = self.get(view_session_id, refresh=False)
        self._wx_recheck_state.pop(view_session_id, None)
        if session["status"] == "closed":
            return session
        session["status"] = "closed"
        session["status_display"] = display(VIEW_SESSION_STATUS, "closed")
        session["closed_at"] = iso(utc_now())
        session["close_reason"] = reason
        self._store.save_view_session(session)
        self._audit.record(
            "view_session_close",
            operator_id=requested_by,
            device_id=session["device_id"],
            reason=f"关闭查看会话：{reason}",
            detail={"view_session_id": view_session_id},
        )
        return session

    def mark_unavailable(self, view_session_id: str, reason: str) -> dict:
        session = self.get(view_session_id)
        if session["status"] in ("closed", "expired"):
            return session
        session["status"] = "unavailable"
        session["status_display"] = display(VIEW_SESSION_STATUS, "unavailable")
        session["close_reason"] = reason
        self._store.save_view_session(session)
        return session

    # ---------- R4-12-E：WX 授权失效传播（wx-camera-binding.v1） ----------
    def revoke_wx_sessions(self, session_ids: list[str], username: str,
                           reason: str) -> int:
        """按 WX 推送撤销绑定会话：精确 (username, wx_session_id) 作用域——
        其他用户、同用户新登录代际一律不误伤；幂等（已终态跳过）。"""
        wanted = {str(sid) for sid in session_ids if sid}
        closed = 0
        for session in self._store.all_view_sessions():
            if not session.get("wx_bound"):
                continue
            if session.get("wx_username") != username:
                continue
            if session.get("wx_session_id") not in wanted:
                continue
            if session.get("status") in ("closed", "expired"):
                continue
            self.close(session["view_session_id"],
                       requested_by=f"wx:{username}", reason=reason)
            closed += 1
        return closed

    def _wx_camera_token(self) -> str:
        if self._wx_token_cache is None:
            path = str(self._config.get("wx_camera_token_file", "") or "").strip()
            if not path:
                return ""
            try:
                self._wx_token_cache = Path(path).read_text(
                    encoding="ascii").strip()
            except OSError:
                return ""
        return self._wx_token_cache

    async def recheck_wx_authorization(self, view_session_id: str,
                                       *, force: bool = False) -> dict:
        """WX 绑定会话的授权复核（退出/禁用/重置/自然到期传播的权威通道）。

        - 未绑定/已终态/未配置复核 URL：零动作（撤销推送仍独立生效）。
        - 节流 wx_camera_recheck_seconds（默认 2s）；force 绕过（续期/停帧窗）。
        - introspect 判无效 → 立即 close（流循环下一检查点 ≤1s 停止供给）。
        - WX 不可达：连续失败超过宽限（默认 1 次）→ fail-closed close，
          不允许“复核失联仍无限供流”；单次失败保留宽限防瞬时抖动误杀。
        """
        session = self.get(view_session_id)
        if not session.get("wx_bound") or session["status"] in ("closed", "expired"):
            return session
        url = str(self._config.get("wx_camera_introspect_url", "") or "").strip()
        if not url:
            return session
        now = time.monotonic()
        interval = max(0.5, float(self._config.get("wx_camera_recheck_seconds", 2.0)))
        state = self._wx_recheck_state.setdefault(
            view_session_id, {"ts": 0.0, "fails": 0})
        if not force and now - state["ts"] < interval:
            return session
        state["ts"] = now
        token = self._wx_camera_token()
        headers = {"X-WX-Internal-Token": token} if token else {}
        import httpx

        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                resp = await client.post(
                    url, json={"session_id": session["wx_session_id"]},
                    headers=headers)
            if resp.status_code != 200:
                raise ValueError(f"introspect HTTP {resp.status_code}")
            data = resp.json()
            state["fails"] = 0
            if data.get("valid") is False:
                return self.close(
                    view_session_id,
                    requested_by=f"wx:{session['wx_username']}",
                    reason=f"WX 授权失效：{data.get('reason') or '会话无效'}")
            return session
        except Exception:
            state["fails"] += 1
            grace = int(self._config.get("wx_camera_recheck_grace", 1))
            if state["fails"] > grace:
                return self.close(
                    view_session_id,
                    requested_by="wx-recheck",
                    reason="授权复核失败：WX 认证服务连续不可达（fail-closed）")
            return session

    # ---------- 后台清理 ----------
    async def start_background(self) -> None:
        self._running = True
        self._sweeper_task = asyncio.create_task(self._sweep_loop())

    async def stop_background(self) -> None:
        self._running = False
        if self._sweeper_task:
            self._sweeper_task.cancel()
            try:
                await self._sweeper_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _sweep_loop(self) -> None:
        while self._running:
            for session in self._store.all_view_sessions():
                if session["status"] not in ("closed", "expired"):
                    self._refresh_status(session)
            await asyncio.sleep(1)
