#!/usr/bin/env python3
"""R3-17：手机配对身份库离线初始化工具。

用法（部署清单步骤；现场执行需另行授权）：
    python tools/init_ime_pairing.py --data-root /path/to/arm_fleet/data

行为：
- 不存在 <data-root>/ime_pairing.db：创建库并生成持久 server_id（一次性）。
- 已存在：校验 server_id 在场即通过（幂等）；缺失/损坏时报错退出，不静默重建。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime.ime_pairing import ImePairingService, ImePairingError  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化手机配对身份库")
    parser.add_argument("--data-root", required=True,
                        help="arm_fleet data 目录（临时库测试用临时目录）")
    args = parser.parse_args()
    db = Path(args.data_root) / "ime_pairing.db"
    existed = db.exists()
    svc = ImePairingService(db)
    try:
        if existed:
            try:
                server_id = svc.server_id
                print(f"已存在：server_id={server_id}（校验通过，未改动）")
                return 0
            except ImePairingError as exc:
                print(f"错误：身份库存在但 server_id 缺失/损坏（{exc}）；"
                      "按规格不静默重建，请人工核对后处理", file=sys.stderr)
                return 2
        server_id = svc.init_server_id()
        print(f"已初始化：{db}")
        print(f"server_id={server_id}")
        return 0
    finally:
        svc.close()


if __name__ == "__main__":
    raise SystemExit(main())
