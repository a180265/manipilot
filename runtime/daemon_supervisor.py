"""每设备 arm_daemon3 进程监督。

监督器只管理 daemon 进程本身：启动监听、观察退出、有限重启和优雅停止。
它不会发送 CONNECT，也不会直接打开串口；串口连接仍由调试台或明确的人工
运维动作触发，以保留 ESP32 打开串口会复位并重新生成会话锚点的安全语义。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable


STATUS_DISPLAY = {
    "disabled": "未启用监督",
    "starting": "启动中",
    "running": "运行中",
    "restarting": "重启中",
    "stopped": "已停止",
    "crashed": "进程异常退出",
    "failed": "重启次数耗尽",
    "config_error": "配置错误",
    "unbound": "串口未绑定（等待人工绑定）",
    "port_conflict": "端口被其他进程占用",
    "external": "已连接独立 daemon（保留现有校准）",
}


def _split_addr(value: str | None) -> tuple[str, int] | None:
    raw = str(value or "").strip()
    host, sep, port = raw.rpartition(":")
    if not sep:
        return None
    try:
        parsed_port = int(port)
    except ValueError:
        return None
    if not host or not (1 <= parsed_port <= 65535):
        return None
    return host, parsed_port


def _port_is_open(host: str, port: int, timeout: float = 0.15) -> bool:
    """仅探测 TCP 监听，不发送 daemon 指令或触发串口动作。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _existing_daemon(host: str, port: int, device_id: str) -> dict | None:
    """只读握手；不得向已有 daemon 发送 CONNECT 或运动指令。"""
    try:
        with socket.create_connection((host, port), timeout=0.5) as sock:
            with sock.makefile("rwb") as stream:
                if stream.readline(1024).split()[:2] != [b"arm-daemon3", b"ready"]:
                    return None
                stream.write(b"STATUS\n")
                stream.flush()
                status = json.loads(stream.readline(16384))
                if isinstance(status, dict) and status.get("deviceId") == device_id:
                    return status
    except (OSError, ValueError, AttributeError):
        pass
    return None


class DaemonSupervisor:
    """按设备维护独立 arm_daemon3 子进程。

    ``process_factory`` 是测试注入点，生产环境默认使用
    :class:`subprocess.Popen`。监督器状态是内存运行态，设备绑定和审计仍由
    ``DeviceManager``/``Store`` 持久化；进程重启不会保留旧 daemon session。
    """

    def __init__(
        self,
        root: Path,
        *,
        hardware_mode: str = "simulation",
        enabled: bool = True,
        python_executable: str | None = None,
        script_path: str | Path | None = None,
        log_dir: str | Path | None = None,
        poll_interval_seconds: float = 1.0,
        restart_delay_seconds: float = 1.0,
        max_restarts: int = 5,
        process_factory: Callable[..., Any] | None = None,
        event_handler: Callable[[dict], Awaitable[None] | None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.hardware_mode = str(hardware_mode or "simulation")
        self.enabled = bool(enabled)
        self.python_executable = str(python_executable or sys.executable)
        self.script_path = Path(script_path) if script_path else self.root / "scripts" / "arm_daemon3.py"
        if not self.script_path.is_absolute():
            self.script_path = self.root / self.script_path
        self.log_dir = Path(log_dir) if log_dir else self.root / "data" / "daemons"
        if not self.log_dir.is_absolute():
            self.log_dir = self.root / self.log_dir
        self.poll_interval_seconds = max(0.1, float(poll_interval_seconds))
        self.restart_delay_seconds = max(0.0, float(restart_delay_seconds))
        self.max_restarts = max(0, int(max_restarts))
        self._process_factory = process_factory or subprocess.Popen
        self._event_handler = event_handler
        self._processes: dict[str, Any] = {}
        self._external_devices: dict[str, tuple[str, int]] = {}
        self._configs: dict[str, dict] = {}
        self._states: dict[str, dict] = {}
        self._stop_requested: set[str] = set()
        self._restart_due: dict[str, float] = {}
        self._monitor_task: asyncio.Task | None = None
        self._running = False

    @property
    def states(self) -> list[dict]:
        return [dict(self._states[key]) for key in sorted(self._states)]

    def state(self, device_id: str) -> dict:
        return dict(self._states.get(device_id) or {
            "device_id": device_id,
            "status": "disabled",
            "status_display": STATUS_DISPLAY["disabled"],
            "pid": None,
            "daemon_addr": None,
            "serial_port": None,
            "restart_count": 0,
            "last_error": None,
            "started_at": None,
            "last_exit_at": None,
        })

    async def _emit(self, event: dict) -> None:
        if self._event_handler is None:
            return
        result = self._event_handler(dict(event))
        if asyncio.iscoroutine(result):
            await result

    def _set_state(self, device_id: str, status: str, **extra: Any) -> dict:
        current = self._states.setdefault(device_id, {
            "device_id": device_id,
            "status": "disabled",
            "status_display": STATUS_DISPLAY["disabled"],
            "pid": None,
            "daemon_addr": None,
            "serial_port": None,
            "restart_count": 0,
            "last_error": None,
            "started_at": None,
            "last_exit_at": None,
        })
        current.update(extra)
        current["status"] = status
        current["status_display"] = STATUS_DISPLAY.get(status, status)
        return dict(current)

    @staticmethod
    def _eligible(config: dict, hardware_mode: str) -> bool:
        # 仅监督 daemon 驱动桥；driver_mode=real 是明确保留的直连/离线
        # 兼容路径，不能被监督器误当成需要启动 arm_daemon3。
        mode = str(config.get("driver_mode") or "")
        if mode != "daemon":
            return False
        # Simulation never starts a real process unless a test/fixture explicitly
        # opts in.  This keeps ten-device tests isolated from the host machine.
        return hardware_mode == "real" or bool(config.get("supervise_in_simulation"))

    def _validate_config(self, config: dict) -> tuple[str, int] | None:
        addr = _split_addr(config.get("daemon_addr"))
        if addr is None:
            self._set_state(
                str(config.get("device_id") or ""), "config_error",
                last_error="daemon_addr 必须是 host:port",
            )
            return None
        host, port = addr
        if host not in {"127.0.0.1", "localhost"}:
            self._set_state(
                str(config.get("device_id") or ""), "config_error",
                last_error="daemon 只允许监听本机地址",
            )
            return None
        if not str(config.get("serial_port") or "").strip():
            # 串口未绑定（含业主在调试台「解除绑定」后的空值）不是配置错误：
            # 这是"等待人工绑定"的正常中间态。以前显示"配置错误"，会让业主以为坏了。
            self._set_state(
                str(config.get("device_id") or ""), "unbound",
                last_error="串口未绑定（在调试台绑定后会自动拉起 daemon）",
            )
            return None
        if not self.script_path.exists():
            self._set_state(
                str(config.get("device_id") or ""), "config_error",
                last_error=f"daemon 启动脚本不存在：{self.script_path}",
            )
            return None
        return host, port

    async def start(self, configs: list[dict]) -> list[dict]:
        """启动所有符合条件的 daemon，并开始监督循环。"""
        self._configs = {
            str(config.get("device_id")): dict(config)
            for config in configs
            if str(config.get("device_id") or "").strip()
        }
        self._running = True
        for config in self._configs.values():
            device_id = str(config["device_id"])
            if not self.enabled or not self._eligible(config, self.hardware_mode):
                self._set_state(device_id, "disabled", daemon_addr=config.get("daemon_addr"),
                                serial_port=config.get("serial_port"))
                continue
            await self.start_device(device_id)
        if self._monitor_task is None or self._monitor_task.done():
            self._monitor_task = asyncio.create_task(self._monitor_loop())
        return self.states

    async def start_device(self, device_id: str) -> dict:
        config = self._configs.get(device_id)
        if config is None:
            return self._set_state(device_id, "config_error", last_error="未找到设备 daemon 配置")
        addr = self._validate_config(config)
        if addr is None:
            return self.state(device_id)
        host, port = addr
        self._stop_requested.discard(device_id)
        self._restart_due.pop(device_id, None)
        process = self._processes.get(device_id)
        if process is not None and process.poll() is None:
            return self.state(device_id)
        if _port_is_open(host, port):
            existing = await asyncio.to_thread(_existing_daemon, host, port, device_id)
            if device_id in self._stop_requested:
                return self.state(device_id)
            if existing:
                self._external_devices[device_id] = (host, port)
                return self._set_state(
                    device_id, "external", daemon_addr=f"{host}:{port}",
                    serial_port=config.get("serial_port"), pid=None,
                    daemon_session=existing.get("session"), last_error=None,
                    daemon_connected=existing.get("connected"),
                    process_owned=False)
            return self._set_state(
                device_id, "port_conflict", daemon_addr=f"{host}:{port}",
                serial_port=config.get("serial_port"),
                last_error=f"{host}:{port} 已被其他进程监听，未强行占用",
            )
        log_path = self.log_dir / f"{device_id}.log"
        env = os.environ.copy()
        env.update({
            "ARM_DAEMON_DEVICE_ID": device_id,
            "ARM_DAEMON_PORT": str(port),
            "ARM_DAEMON_SERIAL": str(config.get("serial_port")),
            "ARM_DAEMON_CONFIG_FILE": str((self.root / "configs" / "devices.json").resolve()),
        })
        if config.get("baud_rate") is not None:
            env["ARM_DAEMON_BAUD"] = str(config["baud_rate"])
        log_handle = None
        command = [self.python_executable, str(self.script_path)]
        # Fleet's service controller stops its entire process group on reload.
        # A daemon owns a serial session and must outlive that group; explicit
        # stop_device/stop_all still terminate the exact retained Popen process.
        process_options = {}
        if os.name == "posix":
            process_options["start_new_session"] = True
        elif os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("a", encoding="utf-8")
            process = self._process_factory(
                command,
                cwd=str(self.root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                **process_options,
            )
        except Exception as exc:
            if log_handle is not None:
                log_handle.close()
            state = self._set_state(device_id, "crashed", daemon_addr=f"{host}:{port}",
                                    serial_port=config.get("serial_port"),
                                    pid=None, daemon_session=None,
                                    last_error=f"启动 daemon 失败：{exc}",
                                    last_exit_at=time.time())
            await self._emit({"kind": "daemon_start_failed", **state})
            await self._schedule_restart(device_id)
            return self.state(device_id)
        self._processes[device_id] = process
        # Keep the handle on the process object for deterministic shutdown;
        # Popen instances allow attributes and test doubles can mirror this.
        try:
            process._arm_fleet_log_handle = log_handle
        except Exception:
            self._log_handles = getattr(self, "_log_handles", {})
            self._log_handles[device_id] = log_handle
        state = self._set_state(
            device_id, "running", daemon_addr=f"{host}:{port}",
            serial_port=config.get("serial_port"), pid=getattr(process, "pid", None),
            started_at=time.time(), last_error=None,
            process_owned=True, daemon_session=None, daemon_connected=False,
        )
        await self._emit({"kind": "daemon_started", **state})
        return state

    async def stop_device(self, device_id: str, reason: str = "管理员停止") -> dict:
        self._stop_requested.add(device_id)
        self._restart_due.pop(device_id, None)
        if device_id in self._external_devices:
            self._external_devices.pop(device_id)
            return self._set_state(
                device_id, "external", process_owned=False,
                last_error="已停止监督；独立 daemon 保持运行，不断开串口或清除校准")
        process = self._processes.pop(device_id, None)
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                await asyncio.to_thread(process.wait, 5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        self._close_log(device_id, process)
        state = self._set_state(device_id, "stopped", pid=None, last_error=reason,
                                last_exit_at=time.time())
        await self._emit({"kind": "daemon_stopped", **state, "reason": reason})
        return state

    async def stop_all(self, reason: str = "Arm Fleet 关闭") -> None:
        self._running = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        for device_id in set(self._processes) | set(self._external_devices) | set(self._configs):
            await self.stop_device(device_id, reason)

    async def detach_all(self) -> None:
        """停止监督但保留独立 daemon 进程和串口会话。

        Arm Fleet 8788 重载时不能顺手终止 8795。下一次 runtime 启动会
        通过只读 STATUS 重新接管现有 daemon；daemon 自己退出则由新监督器
        按正常策略处理。
        """
        self._running = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        self._processes.clear()
        self._external_devices.clear()
        self._restart_due.clear()
        self._stop_requested.clear()

    async def _schedule_restart(self, device_id: str) -> None:
        if device_id in self._stop_requested:
            return
        count = int(self.state(device_id).get("restart_count", 0))
        if count >= self.max_restarts:
            state = self._set_state(device_id, "failed",
                                    last_error="daemon 重启次数已耗尽，请人工检查串口和进程")
            await self._emit({"kind": "daemon_restart_exhausted", **state})
            return
        self._set_state(device_id, "restarting", restart_count=count + 1)
        self._restart_due[device_id] = time.monotonic() + self.restart_delay_seconds

    async def _sync_from_disk(self) -> None:
        """绑定表变了就同步：业主在调试台绑定串口后，daemon 必须被自动拉起。

        2026-10-31 事故：supervisor 只在 start() 读一次绑定表，之后绑定变化
        （调试台绑串口/发现循环自动分配）都不会触发 daemon 启动，于是调试台
        永远等不到 daemon 状态、显示"当前状态暂不可确认"、连接按钮一直是灰的。
        这里每个监控周期按文件 mtime 检查一次，变化时：
          · 新增/改绑的设备 → 更新配置并尝试启动；
          · 变成未绑定（serial_port 空）的设备 → 停掉它的 daemon。
        """
        path = self.root / "configs" / "devices.json"
        try:
            stat = path.stat()
        except OSError:
            return
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == getattr(self, "_configs_stamp", None):
            return
        self._configs_stamp = stamp
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            entries = payload.get("devices")
            if not isinstance(entries, list):
                return
        except (OSError, ValueError):
            return
        fresh = {str(item.get("device_id")): dict(item)
                 for item in entries
                 if isinstance(item, dict) and str(item.get("device_id") or "").strip()}
        for device_id, config in fresh.items():
            previous = self._configs.get(device_id) or {}
            changed = any(str(previous.get(key) or "") != str(config.get(key) or "")
                          for key in ("serial_port", "daemon_addr", "driver_mode"))
            self._configs[device_id] = config
            running = (device_id in self._processes or device_id in self._external_devices)
            if not self.enabled or not self._eligible(config, self.hardware_mode):
                continue
            unbound = not str(config.get("serial_port") or "").strip()
            if unbound and running:
                await self.stop_device(device_id, reason="串口绑定被解除")
                continue
            if not unbound and (changed or not running):
                if changed and running:
                    await self.stop_device(device_id, reason="串口/地址变更，按新配置重建")
                await self.start_device(device_id)

    async def _monitor_once(self) -> None:
        for device_id, (host, port) in list(self._external_devices.items()):
            previous = self.state(device_id)
            current = await asyncio.to_thread(_existing_daemon, host, port, device_id)
            if device_id in self._stop_requested:
                continue
            if current is None:
                listening = await asyncio.to_thread(_port_is_open, host, port)
                if device_id in self._stop_requested:
                    continue
                state = self._set_state(
                    device_id, "port_conflict" if listening else "crashed",
                    daemon_session=None, daemon_connected=False,
                    last_error="独立 daemon 身份/响应不可确认" if listening else "独立 daemon 监听已断开")
                if previous["status"] == "external":
                    await self._emit({"kind": "daemon_exited", **state})
                if listening:
                    continue  # Never replace or stop an unverified external service.
                self._external_devices.pop(device_id, None)
                await self._schedule_restart(device_id)
                continue
            if (previous.get("daemon_session") != current.get("session")
                    or (previous.get("daemon_connected") is True
                        and current.get("connected") is not True)):
                state = self._set_state(device_id, "crashed", daemon_session=None,
                                        last_error="独立 daemon 会话/连接状态变化，旧资格失效")
                # Reuse the runtime's invalidation event before recording the new session.
                await self._emit({"kind": "daemon_exited", **state})
            self._set_state(device_id, "external", daemon_session=current.get("session"),
                            daemon_connected=current.get("connected"), last_error=None)
        for device_id, process in list(self._processes.items()):
            code = process.poll()
            if code is None:
                continue
            self._processes.pop(device_id, None)
            self._close_log(device_id, process)
            state = self._set_state(
                device_id, "crashed", pid=None, daemon_session=None,
                last_exit_at=time.time(),
                last_error=f"daemon 进程退出，返回码 {code}",
            )
            await self._emit({"kind": "daemon_exited", "exit_code": code, **state})
            await self._schedule_restart(device_id)
        for device_id, due in list(self._restart_due.items()):
            if device_id not in self._stop_requested and due <= time.monotonic():
                await self.start_device(device_id)

    async def _monitor_loop(self) -> None:
        while self._running:
            await self._monitor_once()
            await asyncio.sleep(self.poll_interval_seconds)

    def _close_log(self, device_id: str, process: Any | None) -> None:
        handle = getattr(process, "_arm_fleet_log_handle", None) if process is not None else None
        if handle is None:
            handle = getattr(self, "_log_handles", {}).pop(device_id, None)
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
