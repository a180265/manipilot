"""阶段 G 六方向实机运动验证：X±/Y±/Z± 逐方向 1mm 低速运动 + 摄像头逐段帧差证实。

顺序与安全：
- 先 Z（向上离开屏幕，再回位），再 X 对，再 Y 对；每对"去+回"结束都在原位；
- 单次行程 1mm；Z 用 F30（保守），X/Y 用 F100（初次验证速度）；
- 每段前后各采一帧（等待取流桥推帧），帧差必须过阈值才算"真实移动"；
- 每段前后帧落盘 evidence/g_live/directions/，供人工核对运动方向；
- 全部结束后归位（回到会话原点）并复查画面与控制器状态。

运行（必须现场确认安全）：
  .venv/bin/python scripts/g_live_directions.py --site-safe --confirm 确认执行
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

EVIDENCE_DIR = PROJECT_ROOT / "evidence" / "g_live" / "directions"
FRAME_BRIDGE_URL = "http://127.0.0.1:8790/latest.jpg"

# (说明, 去程模板, 回程模板)；去程覆盖六个方向
PAIRS = [
    ("Z 向上（离开屏幕）", "z_lift_1mm_conservative", "z_lower_1mm_conservative"),
    ("Z 向下（回到原高度）", None, None),  # 由上一对回程覆盖，占位说明
    ("X 正方向", "x_plus_1mm_conservative", "x_minus_1mm_conservative"),
    ("X 负方向", None, None),
    ("Y 正方向", "y_plus_1mm_conservative", "y_minus_1mm_conservative"),
    ("Y 负方向", None, None),
]


def make_camera() -> tuple[RealCameraDriver, str]:
    import cv2

    probe = cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION)
    ok, _frame = probe.read()
    probe.release()
    if ok:
        return RealCameraDriver("cam-01"), "avfoundation（直连）"
    return RealCameraDriver("cam-01", frame_provider=HttpFrameSource(FRAME_BRIDGE_URL)), "取流桥（Chrome 推帧）"


async def main() -> int:
    if "--site-safe" not in sys.argv or "--confirm" not in sys.argv or \
            (sys.argv[sys.argv.index("--confirm") + 1] if "--confirm" in sys.argv else "") != "确认执行":
        print(json.dumps({"结论": "拒绝执行：必须带 --site-safe（现场已确认安全）和 --confirm 确认执行"}, ensure_ascii=False))
        return 2

    camera, source_note = make_camera()
    verifier = CameraMotionVerifier(camera, diff_threshold=0.4, settle_seconds=0.8, evidence_dir=EVIDENCE_DIR)
    arm = RealArmDriver("arm-01", transport=PyserialGrblTransport())
    arm.motion_verifier = verifier

    summary: dict = {
        "动作": "六方向 1mm 低速验证（Z 先行，X/Y 成对去回）",
        "取流来源": source_note,
        "证据目录": str(EVIDENCE_DIR.relative_to(PROJECT_ROOT)),
        "方向": {},
    }
    try:
        connect = await arm.connect()
        summary["连接"] = {"控制器状态": connect["controller_state"], "会话原点": connect["session_origin_mpos"]}
        await camera.open()

        all_ok = True
        for name, outward, back in PAIRS:
            if outward is None:
                continue  # 方向已由前一对的去/回覆盖
            for template in (outward, back):
                result = await arm.move_preset(template)
                segment_ok = result["motion_verified"] and result["outcome_hint"] == "success"
                all_ok = all_ok and segment_ok
                summary["方向"].setdefault(name, []).append({
                    "模板": template,
                    "控制器确认": result["ack_ok"],
                    "真实移动已证实": result["motion_verified"],
                    "帧差": [v.get("diff_score") for v in result["verifications"]],
                    "证据帧": [v.get("evidence") for v in result["verifications"]],
                    "MPos": result["position"],
                })

        home = await arm.go_home()
        state = await arm.read_state()
        quality = await camera.check_quality()
        summary["归位"] = {"ok": home["ok"], "MPos": state["position"], "控制器状态": state["controller_state"]}
        summary["运动后画面"] = quality
        summary["结论"] = (
            "六方向验证通过：每段运动均被摄像头证实，归位正常"
            if all_ok and home["ok"] else
            "存在未被摄像头证实的运动段：按「结果未知」处理，请查看证据帧人工核对"
        )
        code = 0 if all_ok and home["ok"] else 1
    except Exception as exc:
        try:
            summary["停止"] = await arm.stop()
        except Exception:
            pass
        summary["结论"] = f"执行中断：{exc}（已发送进给保持，需现场确认）"
        code = 1
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
