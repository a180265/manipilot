from __future__ import annotations

import json

from runtime.config import load_platform_config


def _write_platform(root, **overrides):
    (root / "configs").mkdir(parents=True, exist_ok=True)
    payload = {"listen_host": "127.0.0.1", "listen_port": 8788, **overrides}
    (root / "configs" / "platform.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_non_loopback_listener_requires_explicit_lan_flag(tmp_path, monkeypatch):
    _write_platform(tmp_path, listen_host="0.0.0.0")
    monkeypatch.delenv("ARM_FLEET_HOST", raising=False)
    monkeypatch.delenv("ARM_FLEET_ALLOW_LAN", raising=False)

    config = load_platform_config(tmp_path)

    assert config.get("listen_host") == "127.0.0.1"
    assert "阻止未显式授权" in config.get("listen_host_note")


def test_explicit_lan_flag_preserves_listener_and_exposes_warning(tmp_path, monkeypatch):
    _write_platform(tmp_path, listen_host="0.0.0.0")
    monkeypatch.delenv("ARM_FLEET_HOST", raising=False)
    monkeypatch.setenv("ARM_FLEET_ALLOW_LAN", "1")

    config = load_platform_config(tmp_path)

    assert config.get("listen_host") == "0.0.0.0"
    assert "显式启用局域网监听" in config.get("listen_host_note")

