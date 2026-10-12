"""阶段 G 真机只读检查：串口握手/状态查询 + 摄像头采集取证。

只读、零运动：连接串口读取启动信息、发送 ? 查询控制器状态、
摄像头采集一帧并存档（SHA-256）。不发送任何运动命令。

运行：.venv/bin/python scripts/g_live_readonly.py
输出：中文摘要 JSON + 证据文件路径。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivers.grbl import PyserialGrblTransport, parse_status_report  # noqa: E402
from drivers.real_camera import HttpFrameSource, RealCameraDriver  # noqa: E402
from runtime.util import sha256_bytes  # noqa: E402

EVIDENCE_DIR = PROJECT_ROOT / "evidence" / "g_live"
FRAME_BRIDGE_URL = "http://127.0.0.1:8790/latest.jpg"


def make_camera() -> tuple[RealCameraDriver, str]:
    """优先直接 AVFoundation 采集；无 TCC 权限时回退本机取流桥（Chrome 推帧）。"""
    import cv2

    probe = cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION)
    ok, _frame = probe.read()
    probe.release()
    if ok:
        return RealCameraDriver("cam-01"), "avfoundation（直连）"
    return RealCameraDriver("cam-01", frame_provider=HttpFrameSource(FRAME_BRIDGE_URL)), "取流桥（Chrome 推帧，127.0.0.1:8790）"


async def main() -> int:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict = {"检查时间": __import__("datetime").datetime.now().isoformat(timespec="seconds"), "模式": "只读（零运动）"}

    # ---- 串口检查 ----
    try:
        transport = PyserialGrblTransport()
        banner = await asyncio.to_thread(transport.open)
        summary["串口"] = {
            "端口": transport.port,
            "波特率": transport.baud,
            "启动信息": banner,
            "固件确认": "Grbl_ESP32" in banner,
        }
        responses = await asyncio.to_thread(transport.send_line, "?")
        report = None
        for line in responses:
            report = parse_status_report(line) or report
        summary["串口"]["控制器状态"] = report or {"原始响应": responses}
        await asyncio.to_thread(transport.close)
        serial_ok = bool(report and report.get("state") == "Idle")
        summary["串口"]["结论"] = "通信正常，控制器 Idle" if serial_ok else "通信异常：控制器非 Idle，禁止接入运动"
    except Exception as exc:
        summary["串口"] = {"结论": f"串口检查失败：{exc}"}
        serial_ok = False

    # ---- 摄像头检查 ----
    try:
        camera, source_note = make_camera()
        info = await camera.open()
        frame = camera.capture_sync()
        quality = await camera.check_quality()
        target = EVIDENCE_DIR / f"camera_frame_{frame['frame_ref']}.png"
        target.write_bytes(frame["image"])
        on_disk = sha256_bytes(target.read_bytes())
        assert on_disk == frame["sha256"], "证据落盘哈希不一致"
        summary["摄像头"] = {
            "设备名": camera.camera_name,
            "取流来源": source_note,
            "分辨率": f"{info['width']}x{info['height']}",
            "画面质量": quality,
            "证据文件": str(target.relative_to(PROJECT_ROOT)),
            "SHA256": on_disk,
            "结论": "采集正常，证据已存档",
        }
        await camera.close()
        camera_ok = quality.get("quality") == "clear"
    except Exception as exc:
        summary["摄像头"] = {"结论": f"摄像头检查失败：{exc}"}
        camera_ok = False

    summary["总体结论"] = (
        "只读检查通过：串口 Idle + 摄像头清晰，可进入带观察的保守运动验证"
        if serial_ok and camera_ok
        else "只读检查未完全通过：先解决上述异常项"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if serial_ok and camera_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
