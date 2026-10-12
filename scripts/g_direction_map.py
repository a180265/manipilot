"""方向/比例测量：X/Y 各走 3mm 单程（摄像头逐段验证），测量图像位移向量。

产出（现场工作区映射的基础数据）：
- 机器 X+/X-/Y+/Y- 在图像中的位移方向（dx,dy 像素）与比例（px/mm）；
- 每段帧差验证结果与证据帧（evidence/g_live/direction_map/）。

杆位移测量：对触针杆取两个正交方向的暗像素质心差（3mm 时信号约几十像素，
远高于噪声）。Y+ 与 X- 共用回程（净零位移）。

运行（必须现场确认安全）：
  .venv/bin/python scripts/g_direction_map.py --site-safe --confirm 确认执行
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivers.grbl import PyserialGrblTransport  # noqa: E402
from drivers.real_arm import RealArmDriver  # noqa: E402
from drivers.real_camera import CameraMotionVerifier, HttpFrameSource, RealCameraDriver  # noqa: E402

EVIDENCE_DIR = PROJECT_ROOT / "evidence" / "g_live" / "direction_map"
FRAME_BRIDGE_URL = "http://127.0.0.1:8790/latest.jpg"


def make_camera() -> tuple[RealCameraDriver, str]:
    probe = cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION)
    ok, _frame = probe.read()
    probe.release()
    if ok:
        return RealCameraDriver("cam-01"), "avfoundation（直连）"
    return RealCameraDriver("cam-01", frame_provider=HttpFrameSource(FRAME_BRIDGE_URL)), "取流桥（Chrome 推帧）"


def rod_shift(before_png: bytes, after_png: bytes) -> tuple[float, float]:
    """触针杆位移：全图暗像素（杆）质心差，返回 (dx, dy) 像素。"""
    def centroid(png: bytes) -> tuple[float, float]:
        buffer = np.frombuffer(png, dtype=np.uint8)
        gray = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
        ys, xs = np.where(gray < 90)
        return float(xs.mean()), float(ys.mean())

    x0, y0 = centroid(before_png)
    x1, y1 = centroid(after_png)
    return x1 - x0, y1 - y0


async def main() -> int:
    if "--site-safe" not in sys.argv or "--confirm" not in sys.argv or \
            (sys.argv[sys.argv.index("--confirm") + 1] if "--confirm" in sys.argv else "") != "确认执行":
        print(json.dumps({"结论": "拒绝执行：必须带 --site-safe 和 --confirm 确认执行"}, ensure_ascii=False))
        return 2

    camera, source_note = make_camera()
    verifier = CameraMotionVerifier(camera, diff_threshold=0.4, settle_seconds=0.8, evidence_dir=EVIDENCE_DIR)
    arm = RealArmDriver("arm-01", transport=PyserialGrblTransport())
    arm.motion_verifier = verifier

    summary: dict = {"动作": "X/Y 3mm 单程测量（成对去回，净零位移）", "取流来源": source_note, "测量": {}}
    try:
        await arm.connect()
        await camera.open()
        all_ok = True
        for axis, outward, back in [
            ("X", "x_plus_3mm_measure", "x_minus_3mm_measure"),
            ("Y", "y_plus_3mm_measure", "y_minus_3mm_measure"),
        ]:
            for tag, template in ((f"{axis}+3mm", outward), (f"{axis}-3mm", back)):
                result = await arm.move_preset(template)
                seg = result["verifications"][0] if result["verifications"] else {}
                label = template + ":step0"
                before_png = verifier._pending.get(label)  # after() 已弹出；测量改用已存档证据帧
                before_file = EVIDENCE_DIR / f"{template}_step0-before.png"
                after_file = EVIDENCE_DIR / f"{template}_step0-after.png"
                dx = dy = None
                if before_file.exists() and after_file.exists():
                    dx, dy = rod_shift(before_file.read_bytes(), after_file.read_bytes())
                ok = result["motion_verified"] and result["outcome_hint"] == "success"
                all_ok = all_ok and ok
                summary["测量"][tag] = {
                    "真实移动已证实": result["motion_verified"],
                    "帧差": seg.get("diff_score"),
                    "图像位移px": None if dx is None else {"dx": round(dx, 1), "dy": round(dy, 1)},
                    "MPos": result["position"],
                }
        home = await arm.go_home()
        state = await arm.read_state()
        summary["归位"] = {"ok": home["ok"], "MPos": state["position"]}
        summary["结论"] = (
            "测量完成：每段均被摄像头证实，归位正常" if all_ok and home["ok"] else "存在未证实段，按结果未知处理"
        )
        code = 0 if all_ok and home["ok"] else 1
    finally:
        try:
            await camera.close()
        except Exception:
            pass
        try:
            await arm.disconnect()
        except Exception:
            pass

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
