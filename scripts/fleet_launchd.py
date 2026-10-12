"""Arm Fleet macOS launchd 管理工具。

该工具只负责生成/检查 LaunchAgent 所需内容，以及以 ``serve`` 子命令
做启动前的单实例检查。它不安装 plist、不调用 launchctl，也不发送
daemon CONNECT；真正的串口连接仍由人工调试操作触发。
"""
from __future__ import annotations

import argparse
import errno
import fcntl
import ipaddress
import json
import os
import plistlib
import socket
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_LABEL = "com.arm-fleet.agent"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _is_loopback(host: str) -> bool:
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_listener(host: str, port: int, *, allow_lan: bool) -> None:
    """校验启动监听边界，不触碰网络或系统服务。"""
    normalized = str(host or "").strip()
    if not normalized:
        raise ValueError("监听地址不能为空")
    try:
        parsed_port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("监听端口必须是数字") from exc
    if not 1 <= parsed_port <= 65535:
        raise ValueError("监听端口必须在 1-65535 范围内")
    if not _is_loopback(normalized) and not allow_lan:
        raise ValueError("局域网监听必须显式传入 --allow-lan")


def check_listener(host: str, port: int, *, timeout: float = 0.2) -> dict[str, Any]:
    """只读探测 TCP 监听状态；不发送 HTTP、daemon 或串口命令。"""
    address = f"{host}:{port}"
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"address": address, "listening": True, "error": None}
    except OSError as exc:
        return {
            "address": address,
            "listening": False,
            "error": errno.errorcode.get(getattr(exc, "errno", None), type(exc).__name__),
        }


def _default_log_path(root: Path) -> Path:
    return root / "data" / "arm-fleet-launchd.log"


def _assert_daemon_retention(root: Path) -> None:
    """拒绝会在 Fleet 退出时主动终止 daemon 的配置。"""
    config_path = root / "configs" / "platform.json"
    if not config_path.exists():
        return
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"无法读取 Fleet 配置，拒绝启动：{config_path}") from exc
    if payload.get("daemon_supervisor_stop_on_shutdown") is True:
        raise RuntimeError(
            "daemon_supervisor_stop_on_shutdown=true 会中断 daemon/校准保留，拒绝常驻启动"
        )


def _validate_callback_url(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    parsed = urlparse(str(value).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("WX 回调地址必须是 http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("WX 回调地址不得包含账号、密码、查询参数或片段")
    return str(value).strip()


def build_launchd_plist(
    *,
    root: Path,
    python_executable: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    allow_lan: bool = False,
    label: str = DEFAULT_LABEL,
    log_path: Path | None = None,
    wx_callback: str | None = None,
) -> dict[str, Any]:
    """构造可重复写出的 LaunchAgent plist 数据。"""
    root = Path(root).resolve()
    python_executable = str(Path(python_executable).expanduser())
    validate_listener(host, port, allow_lan=allow_lan)
    wx_callback = _validate_callback_url(wx_callback)
    if not label or "/" in label:
        raise ValueError("LaunchAgent 标签必须是非空标识，不能包含路径")
    script = root / "scripts" / "fleet_launchd.py"
    log_path = Path(log_path).resolve() if log_path else _default_log_path(root)
    program_arguments = [
        python_executable,
        str(script),
        "serve",
        "--root",
        str(root),
        "--host",
        str(host),
        "--port",
        str(int(port)),
    ]
    if allow_lan:
        program_arguments.append("--allow-lan")
    environment = {
        "ARM_FLEET_HOST": str(host),
        "ARM_FLEET_PORT": str(int(port)),
        "ARM_FLEET_ALLOW_LAN": "1" if allow_lan else "0",
        "ARM_FLEET_DAEMON_SUPERVISOR": "1",
    }
    if wx_callback:
        environment["ARM_FLEET_TASK_CALLBACK"] = wx_callback
        environment["ARM_FLEET_WX_CALLBACK"] = wx_callback  # 旧名兼容：wx 插件直接读此名
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(root),
        "RunAtLoad": True,
        "KeepAlive": True,
        # Fleet 重载只释放监督权；受管 daemon 不随父进程组被杀。
        "AbandonProcessGroup": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
        "EnvironmentVariables": environment,
    }


def plist_bytes(payload: dict[str, Any]) -> bytes:
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def _lock_path(root: Path) -> Path:
    return root / "data" / "launchd" / "arm-fleet.lock"


def _acquire_instance_lock(root: Path):
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise RuntimeError(f"Fleet 启动锁已被占用：{path}") from exc
        raise
    os.set_inheritable(handle.fileno(), True)
    return handle


def _serve(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    validate_listener(args.host, args.port, allow_lan=args.allow_lan)
    _assert_daemon_retention(root)
    try:
        lock_handle = _acquire_instance_lock(root)
    except RuntimeError as exc:
        print(json.dumps({
            "status": "already_starting",
            "status_display": "已有 Fleet 启动实例，未重复启动",
            "detail": str(exc),
        }, ensure_ascii=False))
        return 0
    listener = check_listener(args.host, args.port)
    if listener["listening"]:
        lock_handle.close()
        print(json.dumps({
            "status": "already_running",
            "status_display": "已有服务监听，未重复启动",
            **listener,
        }, ensure_ascii=False))
        return 0

    # launchd 的 WorkingDirectory 只影响 launchd 子进程；这里也显式切换，
    # 使手工重跑和 launchd 启动拥有完全相同的模块/相对路径语义。
    os.chdir(root)
    env = os.environ.copy()
    env.update({
        "ARM_FLEET_HOST": str(args.host),
        "ARM_FLEET_PORT": str(args.port),
        "ARM_FLEET_ALLOW_LAN": "1" if args.allow_lan else "0",
        "ARM_FLEET_DAEMON_SUPERVISOR": "1",
    })
    os.set_inheritable(lock_handle.fileno(), True)
    os.execve(
        args.python_executable,
        [args.python_executable, "-m", "agent"],
        env,
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Arm Fleet launchd 生成与只读检查工具")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, required=True)
    common.add_argument("--host", default=DEFAULT_HOST)
    common.add_argument("--port", type=int, default=DEFAULT_PORT)
    common.add_argument("--allow-lan", action="store_true")
    common.add_argument("--python", dest="python_executable", default=sys.executable)
    common.add_argument(
        "--wx-callback",
        default=(os.environ.get("ARM_FLEET_TASK_CALLBACK")
                 or os.environ.get("ARM_FLEET_WX_CALLBACK")),
        help="覆盖任务回调基址（ARM_FLEET_TASK_CALLBACK，旧名兼容）；默认沿用当前环境值",
    )

    render = sub.add_parser("plist", parents=[common], help="输出或写出 LaunchAgent plist")
    render.add_argument("--label", default=DEFAULT_LABEL)
    render.add_argument("--output", type=Path)

    check = sub.add_parser("check", parents=[common], help="只读检查端口监听")
    check.add_argument("--label", default=DEFAULT_LABEL)

    serve = sub.add_parser("serve", parents=[common], help="单实例启动 Fleet，不安装或重载 launchd")
    serve.set_defaults(command="serve")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "plist":
        payload = build_launchd_plist(
            root=args.root,
            python_executable=args.python_executable,
            host=args.host,
            port=args.port,
            allow_lan=args.allow_lan,
            label=args.label,
            wx_callback=args.wx_callback,
        )
        rendered = plist_bytes(payload)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(rendered)
        else:
            sys.stdout.buffer.write(rendered)
        return 0
    if args.command == "check":
        validate_listener(args.host, args.port, allow_lan=args.allow_lan)
        result = check_listener(args.host, args.port)
        result["label"] = args.label
        result["status_display"] = "已监听" if result["listening"] else "未监听"
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["listening"] else 1
    return _serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
