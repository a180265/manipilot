"""Arm Fleet 设备常驻进程入口。

启动：python -m agent  （默认仅监听 127.0.0.1；局域网监听需 ARM_FLEET_ALLOW_LAN=1）
配置：configs/platform.json + configs/devices.json，环境变量可覆盖（见 docs/部署与环境变量.md）。
"""
from __future__ import annotations

import uvicorn

from api.app import create_app
from runtime.config import load_platform_config


def main() -> None:
    config = load_platform_config()
    app = create_app()
    uvicorn.run(
        app,
        host=str(config.get("listen_host")),
        port=int(config.get("listen_port")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
