"""Arm Fleet launchd 管理工具：只验证生成和安全门禁，不安装或启动服务。"""
from __future__ import annotations

import plistlib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.fleet_launchd import (
    DEFAULT_LABEL,
    _assert_daemon_retention,
    _serve,
    build_launchd_plist,
    check_listener,
    main,
    validate_listener,
)

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fleet_launchd.py"


def test_generated_lan_command_runs_its_declared_root_from_another_directory(tmp_path):
    root = tmp_path / "isolated fleet"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(SCRIPT, root / "scripts" / "fleet_launchd.py")
    (root / "agent.py").write_text(
        "import json, os\n"
        "print(json.dumps({'cwd': os.getcwd(), 'host': os.environ['ARM_FLEET_HOST'], "
        "'lan': os.environ['ARM_FLEET_ALLOW_LAN'], "
        "'callback': os.environ['ARM_FLEET_WX_CALLBACK']}))\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    generated = subprocess.run(
        [
            sys.executable, str(SCRIPT), "plist", "--root", str(root),
            "--python", sys.executable, "--host", "0.0.0.0", "--allow-lan",
            "--port", str(port), "--wx-callback", "http://127.0.0.1:8787",
        ],
        cwd=elsewhere, capture_output=True, check=True, timeout=5,
    )
    payload = plistlib.loads(generated.stdout)
    launched = subprocess.run(
        payload["ProgramArguments"],
        cwd=elsewhere,
        env={**os.environ, **payload["EnvironmentVariables"]},
        capture_output=True, text=True, timeout=5,
    )

    assert launched.returncode == 0, launched.stderr
    assert json.loads(launched.stdout) == {
        "cwd": str(root), "host": "0.0.0.0", "lan": "1",
        "callback": "http://127.0.0.1:8787",
    }


def test_plist_defaults_to_loopback_and_preserves_daemon_lifecycle(tmp_path: Path):
    payload = build_launchd_plist(
        root=tmp_path,
        python_executable="/tmp/fleet-python",
        host="127.0.0.1",
        port=8788,
        allow_lan=False,
    )

    assert payload["Label"] == DEFAULT_LABEL
    assert payload["WorkingDirectory"] == str(tmp_path)
    assert payload["ProgramArguments"] == [
        "/tmp/fleet-python",
        str(tmp_path / "scripts" / "fleet_launchd.py"),
        "serve",
        "--root",
        str(tmp_path),
        "--host",
        "127.0.0.1",
        "--port",
        "8788",
    ]
    env = payload["EnvironmentVariables"]
    assert env["ARM_FLEET_HOST"] == "127.0.0.1"
    assert env["ARM_FLEET_ALLOW_LAN"] == "0"
    assert env["ARM_FLEET_PORT"] == "8788"
    assert env["ARM_FLEET_DAEMON_SUPERVISOR"] == "1"
    assert "ARM_FLEET_DAEMON_STOP_ON_SHUTDOWN" not in env
    assert payload["AbandonProcessGroup"] is True
    assert "CONNECT" not in plistlib.dumps(payload).decode()
    assert payload["StandardOutPath"] == str(tmp_path / "data" / "arm-fleet-launchd.log")


def test_plist_allows_lan_only_with_explicit_flag(tmp_path: Path):
    with pytest.raises(ValueError, match="allow-lan"):
        build_launchd_plist(
            root=tmp_path,
            python_executable="/tmp/fleet-python",
            host="0.0.0.0",
            port=8788,
            allow_lan=False,
        )

    payload = build_launchd_plist(
        root=tmp_path,
        python_executable="/tmp/fleet-python",
        host="0.0.0.0",
        port=8788,
        allow_lan=True,
    )
    assert "--allow-lan" in payload["ProgramArguments"]
    assert payload["EnvironmentVariables"]["ARM_FLEET_HOST"] == "0.0.0.0"
    assert payload["EnvironmentVariables"]["ARM_FLEET_ALLOW_LAN"] == "1"


def test_plist_preserves_callback_environment_without_putting_it_in_arguments(tmp_path: Path):
    payload = build_launchd_plist(
        root=tmp_path,
        python_executable="/tmp/fleet-python",
        wx_callback="http://127.0.0.1:8787",
    )

    assert payload["EnvironmentVariables"]["ARM_FLEET_WX_CALLBACK"] == "http://127.0.0.1:8787"
    assert "127.0.0.1:8787" not in payload["ProgramArguments"]
    with pytest.raises(ValueError, match="不得包含"):
        build_launchd_plist(
            root=tmp_path,
            python_executable="/tmp/fleet-python",
            wx_callback="http://user:secret@example.test/callback?token=redacted",
        )


def test_plist_cli_uses_existing_callback_environment(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv("ARM_FLEET_WX_CALLBACK", "http://127.0.0.1:8787")

    assert main([
        "plist", "--root", str(tmp_path), "--python", "/tmp/fleet-python",
    ]) == 0
    payload = plistlib.loads(capsys.readouterr().out.encode())
    assert payload["EnvironmentVariables"]["ARM_FLEET_WX_CALLBACK"] == "http://127.0.0.1:8787"


def test_daemon_retention_guard_rejects_shutdown_stop_config(tmp_path: Path):
    config = tmp_path / "configs"
    config.mkdir()
    (config / "platform.json").write_text(
        '{"daemon_supervisor_stop_on_shutdown": true}', encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="daemon_supervisor_stop_on_shutdown"):
        _assert_daemon_retention(tmp_path)


def test_validate_listener_rejects_invalid_port_and_host():
    with pytest.raises(ValueError, match="端口"):
        validate_listener("127.0.0.1", 0, allow_lan=False)
    with pytest.raises(ValueError, match="监听地址"):
        validate_listener("", 8788, allow_lan=False)
    with pytest.raises(ValueError, match="allow-lan"):
        validate_listener("192.168.1.20", 8788, allow_lan=False)


def test_check_listener_is_read_only_and_reports_free_or_occupied(tmp_path: Path):
    host = "127.0.0.1"
    with socket.socket() as listener:
        listener.bind((host, 0))
        listener.listen()
        port = listener.getsockname()[1]
        occupied = check_listener(host, port)
        assert occupied["listening"] is True
        assert occupied["address"] == f"{host}:{port}"

    free = check_listener(host, port)
    assert free["listening"] is False
    assert free["address"] == f"{host}:{port}"


def test_serve_is_idempotent_when_another_fleet_instance_holds_start_lock(
    tmp_path: Path, monkeypatch, capsys
):
    def occupied_lock(_root):
        raise RuntimeError("Fleet 启动锁已被占用")

    monkeypatch.setattr("scripts.fleet_launchd._acquire_instance_lock", occupied_lock)
    args = type(
        "Args",
        (),
        {
            "root": tmp_path,
            "host": "127.0.0.1",
            "port": 8788,
            "allow_lan": False,
            "python_executable": "/tmp/fleet-python",
        },
    )()

    assert _serve(args) == 0
    assert "已有 Fleet 启动实例" in capsys.readouterr().out


def test_serve_does_not_exec_when_8788_is_already_listening(
    tmp_path: Path, monkeypatch, capsys
):
    lock = type("Lock", (), {"close": lambda self: None})()
    monkeypatch.setattr("scripts.fleet_launchd._acquire_instance_lock", lambda _root: lock)
    monkeypatch.setattr(
        "scripts.fleet_launchd.check_listener",
        lambda *_args, **_kwargs: {
            "address": "127.0.0.1:8788",
            "listening": True,
            "error": None,
        },
    )
    monkeypatch.setattr(
        "scripts.fleet_launchd.os",
        type(
            "OS",
            (),
            {
                "environ": {},
                "set_inheritable": staticmethod(lambda *_args: None),
                "execve": staticmethod(
                    lambda *_args: (_ for _ in ()).throw(
                        AssertionError("occupied 8788 must not exec Fleet")
                    )
                ),
            },
        ),
    )
    args = type(
        "Args",
        (),
        {
            "root": tmp_path,
            "host": "127.0.0.1",
            "port": 8788,
            "allow_lan": False,
            "python_executable": "/tmp/fleet-python",
        },
    )()

    assert _serve(args) == 0
    assert "已有服务监听" in capsys.readouterr().out
