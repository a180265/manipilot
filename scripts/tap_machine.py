"""机器坐标点触助手：相对当前会话原点移动 (dx,dy) 并点击（或仅移动）。

用法：
  .venv/bin/python scripts/tap_machine.py --dx 7.7 --dy -15.0            # 移动+点击
  .venv/bin/python scripts/tap_machine.py --dx 3 --dy 0 --move-only     # 仅移动（不触屏）
  可选 --depth 3.8（触屏深度 mm，默认取 configs/screen_calibration.json 的 tap_depth_mm）

移动与触屏均逐段摄像头验证；未证实一律报"未知"，不重试。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivers.grbl import PyserialGrblTransport  # noqa: E402
from drivers.real_arm import RealArmDriver  # noqa: E402
from drivers.real_camera import CameraMotionVerifier, HttpFrameSource, RealCameraDriver  # noqa: E402

FRAME_BRIDGE_URL = "http://127.0.0.1:8790/latest.jpg"


def make_camera() -> RealCameraDriver:
    import cv2

    probe = cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION)
    ok, _frame = probe.read()
    probe.release()
    if ok:
        return RealCameraDriver("cam-01")
    return RealCameraDriver("cam-01", frame_provider=HttpFrameSource(FRAME_BRIDGE_URL))


async def main() -> int:
    args = sys.argv[1:]
    cal_path = PROJECT_ROOT / "configs" / "screen_calibration.json"
    cal = json.load(open(cal_path)) if cal_path.exists() else {}
    origin = cal.get("session_origin_mpos") or {"x": 0.0, "y": 0.0, "z": 0.0}

    # --px/--py：按页面坐标点击（需已完成屏幕标定）；否则按相对位移 --dx/--dy
    if "--px" in args and "--py" in args:
        from drivers.screen_calibration import page_to_machine
        px, py = float(args[args.index("--px") + 1]), float(args[args.index("--py") + 1])
        depth = float(args[args.index("--depth") + 1]) if "--depth" in args else float(cal.get("tap_depth_mm", 3.8))
        move_only = False
        page_target = (px, py)
    else:
        dx = float(args[args.index("--dx") + 1]) if "--dx" in args else 0.0
        dy = float(args[args.index("--dy") + 1]) if "--dy" in args else 0.0
        move_only = "--move-only" in args
        depth = float(args[args.index("--depth") + 1]) if "--depth" in args else float(cal.get("tap_depth_mm", 3.8))
        page_target = None

    camera = make_camera()
    verifier = CameraMotionVerifier(camera, diff_threshold=0.4, settle_seconds=0.6)
    arm = RealArmDriver("arm-01", transport=PyserialGrblTransport())
    arm.motion_verifier = verifier

    summary: dict = {"触屏": "否（仅移动）" if move_only else f"{depth}mm"}
    try:
        await arm.connect()
        await camera.open()
        raw = (await arm.read_state())["position"]
        cur = {k: raw[k] - origin[k] for k in ("x", "y", "z")}
        if page_target is not None:
            target = page_to_machine(cal, page_target)
            dx, dy = target[0] - cur["x"], target[1] - cur["y"]
            summary["页面目标"] = [px, py]
            summary["目标机器坐标"] = [round(target[0], 1), round(target[1], 1)]
        summary["相对移动"] = [round(dx, 2), round(dy, 2)]
        if move_only:
            result = await arm.click({"dx": dx, "dy": dy, "feed_xy": 5000})
        else:
            result = await arm.click({"dx": dx, "dy": dy, "tap_depth_mm": depth, "feed_xy": 5000})
        move_ok = bool(result["phases"][0]["verification"]["verified"])
        summary["结果"] = {
            "移动段已证实": move_ok,
            "触屏段已证实": (bool(result["phases"][-1]["verification"]["verified"])
                          if not move_only and result["phases"][-1].get("verification") else None),
            "outcome_hint": result["outcome_hint"],
            "MPos": result["position"],
        }
        if result["outcome_hint"] == "unknown":
            summary["警告"] = "摄像头未证实移动，结果未知：页面若未响应属预期，请人工核查"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        try:
            summary["停止"] = await arm.stop()
        except Exception:
            pass
        summary["结论"] = f"中断：{exc}"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1
    finally:
        try:
            await camera.close()
        except Exception:
            pass
        try:
            await arm.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
