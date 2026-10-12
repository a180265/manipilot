"""屏幕反馈标定：机械臂点击手机上的标定网页，用页面回传的触摸坐标拟合映射。

前提（人工步骤）：
1. 电脑与手机同一局域网；启动 scripts/calibration_server.py；
2. 手机浏览器打开打印出的标定地址，页面点「就绪」；
3. 人工把机械臂拨到大约手机屏幕中间的上方（悬空）；
4. 运行本脚本：--site-safe --confirm 确认执行

流程：
- 锚定会话原点（人工摆位处，零运动 G92）→ 清空页面触摸记录；
- Z 探针：0.25mm 步进低速下压，页面收到触摸即找到接触深度，随后抬回悬停高度；
- 3×3 网格（±10mm）：逐点 click（摄像头逐段验证 + 页面触摸为点触真值）；
- 最小二乘拟合 page = A·machine + offset，输出比例（px/mm）、旋转、残差、
  屏幕四角反解的机械可点击范围；保存 configs/screen_calibration.json。

约束：标定过程中手机页面不能缩放/旋转/切换；换浏览器或改动缩放后需重标。
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivers.grbl import PyserialGrblTransport  # noqa: E402
from drivers.real_arm import RealArmDriver  # noqa: E402
from drivers.real_camera import CameraMotionVerifier, HttpFrameSource, RealCameraDriver  # noqa: E402
from drivers.screen_calibration import fit_affine, screen_range_machine  # noqa: E402

CALIB_SERVER = "http://127.0.0.1:8791"
EVIDENCE_DIR = PROJECT_ROOT / "evidence" / "g_live" / "calibration"
FRAME_BRIDGE_URL = "http://127.0.0.1:8790/latest.jpg"
OUTPUT = PROJECT_ROOT / "configs" / "screen_calibration.json"
GRID = [(-10, -10), (0, -10), (10, -10), (10, 0), (10, 10), (0, 10), (-10, 10), (-10, 0), (0, 0)]
Z_STEP_MM = 0.25
Z_MAX_STEPS = 24  # 最多向下探 6mm
TAP_DEPTH_MARGIN = 0.3


def lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def http_get_json(url: str, timeout: float = 3.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def http_post(url: str, timeout: float = 3.0) -> None:
    request = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        response.read()


def wait_ready(timeout_s: float = 180.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            status = http_get_json(f"{CALIB_SERVER}/status")
            if status.get("ready"):
                return status
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError("等待页面就绪超时：请确认手机已打开标定地址并点「就绪」")


def new_tap_since(count: int, timeout_s: float = 6.0) -> dict | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            taps = http_get_json(f"{CALIB_SERVER}/taps").get("taps", [])
            if len(taps) > count:
                return taps[-1]
        except Exception:
            pass
        time.sleep(0.2)
    return None


def make_camera() -> tuple[RealCameraDriver, str]:
    import cv2

    probe = cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION)
    ok, _frame = probe.read()
    probe.release()
    if ok:
        return RealCameraDriver("cam-01"), "avfoundation（直连）"
    return RealCameraDriver("cam-01", frame_provider=HttpFrameSource(FRAME_BRIDGE_URL)), "取流桥（Chrome 推帧）"


async def main() -> int:
    # 单实例锁：两个标定实例并发驱动同一机械臂是严重安全事故
    import fcntl

    lock_handle = open("/tmp/arm_fleet_calibrate.lock", "w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"结论": "已有标定脚本在运行，拒绝并发启动"}, ensure_ascii=False))
        return 2

    if "--site-safe" not in sys.argv or "--confirm" not in sys.argv or \
            (sys.argv[sys.argv.index("--confirm") + 1] if "--confirm" in sys.argv else "") != "确认执行":
        print(json.dumps({
            "人工步骤": [
                "1. 启动 scripts/calibration_server.py（仅局域网）",
                f"2. 手机浏览器打开 http://{lan_ip()}:8791/calibrate 并点「就绪」",
                "3. 人工把机械臂拨到大约手机屏幕中间的上方（悬空）",
                "4. 再运行本脚本：--site-safe --confirm 确认执行",
            ]
        }, ensure_ascii=False, indent=2))
        return 2

    camera, source_note = make_camera()
    verifier = CameraMotionVerifier(camera, diff_threshold=0.4, settle_seconds=0.6)
    arm = RealArmDriver("arm-01", transport=PyserialGrblTransport())
    arm.motion_verifier = verifier

    summary: dict = {"取流来源": source_note, "标定服务": CALIB_SERVER}
    try:
        # 若手机已就绪则直接采用；否则等待新的就绪信号
        status = http_get_json(f"{CALIB_SERVER}/status")
        if not status.get("ready"):
            status = await asyncio.to_thread(wait_ready)
        summary["页面就绪"] = status.get("viewport")

        await arm.connect()
        await camera.open()
        # 人工摆位即原点：屏幕中心附近（零运动 G92）
        anchor = await arm.set_session_origin_here(reason="屏幕反馈标定：人工拨到手机中心后锚定")
        summary["锚定"] = anchor["message"]
        http_post(f"{CALIB_SERVER}/reset")

        # ---- Z 探针找接触深度 ----
        tap_count = http_get_json(f"{CALIB_SERVER}/taps").get("taps", []).__len__()
        steps = 0
        while steps < Z_MAX_STEPS:
            await arm.move_preset("z_lower_0p25_conservative")
            steps += 1
            current = http_get_json(f"{CALIB_SERVER}/taps").get("taps", [])
            if len(current) > tap_count:
                break
        if steps >= Z_MAX_STEPS:
            http_post(f"{CALIB_SERVER}/reset")
            stop = await arm.stop()
            summary["结论"] = f"Z 探针 {Z_MAX_STEPS * Z_STEP_MM}mm 内未接触到屏幕，已停止（结果未知，需人工检查摆位）"
            summary["停止"] = stop
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 1
        touch_depth = round(steps * Z_STEP_MM, 2)
        summary["Z接触深度"] = f"{touch_depth}mm（{steps} 步）"
        for _ in range(steps):  # 抬回悬停高度
            await arm.move_preset("z_lift_0p25_conservative")
        http_post(f"{CALIB_SERVER}/reset")

        # ---- 3×3 网格点击 ----
        pairs: list[tuple[tuple[float, float], tuple[float, float]]] = []
        cur_x, cur_y = 0.0, 0.0
        tap_depth = round(touch_depth + TAP_DEPTH_MARGIN, 2)
        summary["网格点击"] = []
        for gx, gy in GRID:
            # 基线必须在点击前记录：本次点击的触摸在 click 返回前就已上报
            before_count = len(http_get_json(f"{CALIB_SERVER}/taps").get("taps", []))
            result = await arm.click({"dx": gx - cur_x, "dy": gy - cur_y, "tap_depth_mm": tap_depth})
            cur_x, cur_y = gx, gy
            move_ok = bool(result["phases"] and result["phases"][0]["verification"]
                           and result["phases"][0]["verification"].get("verified"))
            tap = await asyncio.to_thread(new_tap_since, before_count)
            if not tap:
                summary["网格点击"].append({"网格": [gx, gy], "结果": "未收到页面触摸（点可能落在屏外）", "移动已证实": move_ok})
                continue
            pairs.append(((float(gx), float(gy)), (float(tap["x"]), float(tap["y"]))))
            summary["网格点击"].append({
                "网格": [gx, gy],
                "页面坐标": [tap["x"], tap["y"]],
                "移动已证实": move_ok,
            })
        summary["有效点数"] = len(pairs)
        if len(pairs) < 6:
            summary["结论"] = f"有效标定点 {len(pairs)} 个（<6），不足以可靠拟合；请检查摆位后重跑"
            home = await arm.go_home()
            summary["归位"] = {"ok": home["ok"]}
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 1

        # ---- 拟合与保存 ----
        machine = [p[0] for p in pairs]
        page = [p[1] for p in pairs]
        calibration = fit_affine(machine, page)
        calibration["viewport"] = status.get("viewport") or {}
        calibration["calibrated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        calibration["touch_depth_mm"] = touch_depth
        calibration["tap_depth_mm"] = tap_depth
        calibration["screen_range"] = screen_range_machine(calibration, calibration["viewport"])
        calibration["note"] = "仅对当前手机/摄像头/机械臂相对位置与本次浏览器视口有效；任一变化后必须重标"
        OUTPUT.write_text(json.dumps(calibration, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["拟合"] = {
            "比例": f"{calibration['scale_px_per_mm']} px/mm",
            "旋转": f"{calibration['rotation_deg']}°",
            "最大残差": f"{calibration['residual_max_px']} px",
            "RMS残差": f"{calibration['residual_rms_px']} px",
            "屏幕可点范围(机械坐标mm)": calibration["screen_range"]["safe_min"],
        }
        summary["保存至"] = str(OUTPUT.relative_to(PROJECT_ROOT))

        home = await arm.go_home()
        state = await arm.read_state()
        summary["归位"] = {"ok": home["ok"], "MPos": state["position"]}
        summary["结论"] = "屏幕反馈标定完成：机械臂坐标↔页面坐标映射已建立并保存"
        code = 0
    except Exception as exc:
        try:
            summary["停止"] = await arm.stop()
        except Exception:
            pass
        summary["结论"] = f"标定中断：{exc}（已发送进给保持，需现场确认）"
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
