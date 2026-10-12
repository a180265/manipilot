"""阶段 G 真机保守运动验证：Z 轴 1mm 低速往复 + 摄像头逐段帧差确认。

严格遵循现场文档：
- 模板 z_first_direction_verify = G91 / G1 Z-1 F30（向上，离开屏幕）/
  G1 Z1 F30（回到原位）/ G90 —— 首次低速方向确认动作；
- 逐行发送、逐条确认 ok；MPos/ok 不作为真实移动证明；
- 每个运动段前后各采一帧，帧差验证真实移动；未证实一律按"结果未知"处理；
- 运动结束后归位（回到会话原点）并再次检查画面质量。

运行（必须现场确认安全后执行）：
  .venv/bin/python scripts/g_live_motion.py --site-safe --confirm 确认执行
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

EVIDENCE_DIR = PROJECT_ROOT / "evidence" / "g_live"
FRAME_BRIDGE_URL = "http://127.0.0.1:8790/latest.jpg"


def make_camera() -> tuple[RealCameraDriver, str]:
    import cv2

    probe = cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION)
    ok, _frame = probe.read()
    probe.release()
    if ok:
        return RealCameraDriver("cam-01"), "avfoundation（直连）"
    return RealCameraDriver("cam-01", frame_provider=HttpFrameSource(FRAME_BRIDGE_URL)), "取流桥（Chrome 推帧）"


async def main() -> int:
    if "--site-safe" not in sys.argv or "--confirm" not in sys.argv:
        print(json.dumps({"结论": "拒绝执行：必须带 --site-safe（现场已观察确认安全）和 --confirm 确认执行"}, ensure_ascii=False))
        return 2
    token = sys.argv[sys.argv.index("--confirm") + 1] if "--confirm" in sys.argv else ""
    if token != "确认执行":
        print(json.dumps({"结论": "拒绝执行：--confirm 必须为「确认执行」"}, ensure_ascii=False))
        return 2

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    camera, source_note = make_camera()
    verifier = CameraMotionVerifier(camera, diff_threshold=0.4)
    arm = RealArmDriver("arm-01", transport=PyserialGrblTransport())
    arm.motion_verifier = verifier

    summary: dict = {"动作": "z_first_direction_verify（Z 轴 1mm 低速往复，先上后下）", "取流来源": source_note}
    try:
        connect = await arm.connect()
        summary["连接"] = {
            "端口": connect["port"],
            "控制器状态": connect["controller_state"],
            "会话原点": connect["session_origin_mpos"],
            "说明": "未收到启动横幅属正常（控制器已上电常驻），以 ? 状态为准",
        }
        await camera.open()

        state_before = await arm.read_state()
        summary["运动前"] = {"MPos": state_before["position"], "状态": state_before["controller_state"]}

        result = await arm.move_preset("z_first_direction_verify")
        summary["运动结果"] = {
            "控制器确认": result["ack_ok"],
            "最终MPos": result["position"],
            "逐段验证": result["verifications"],
            "真实移动已证实": result["motion_verified"],
            "结果提示": result["outcome_hint"],
            "消息": result["message"],
        }

        home = await arm.go_home()
        summary["归位"] = {
            "ok": home["ok"],
            "最终MPos": home["position"],
            "真实归位已证实": home.get("motion_verified"),
            "消息": home["message"],
        }

        quality = await camera.check_quality()
        summary["运动后画面"] = quality
        state_after = await arm.read_state()
        summary["运动后"] = {"MPos": state_after["position"], "状态": state_after["controller_state"]}

        verified = result["motion_verified"]
        summary["结论"] = (
            "保守运动验证通过：控制器确认 + 摄像头逐段证实真实移动，归位正常"
            if verified and home["ok"]
            else "运动未被摄像头证实：按「结果未知」处理，需现场人工确认（不得当作成功）"
        )
        code = 0 if verified and home["ok"] else 1
    except Exception as exc:
        # 任何异常：先软停止，再上报（不猜结果）
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
