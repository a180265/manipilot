"""全自动闭环屏幕标定（用户只负责：机械臂放到屏幕中间大概位置、Z 拉到最顶）。

流程：
1. Z 探触：从当前高度每次下探 1.5mm + 0.6mm 轻触测试，手机上报触点即接触确认
   （触点同时成为第一组标定样本）；
2. 闭环四点：布防十字靶标 → 摄像头测偏差（十字 vs 探头尖）→ 自适应学习
   相机像素/毫米矩阵 → 修正移动 → 点击收敛 → 记录 (MPos, 触点)；
3. 拟合存档：机械↔屏幕仿射 + 摄像头↔屏幕单应；
4. 验收：第 5 个靶标完全由"摄像头看到 → 换算 → 点击"执行，报告实测端到端偏差。

安全：无 $H、Z+ 向下、单段 Z≤10mm/XY≤30mm 软限位、点击后回悬停、异常自动软停止。
用法：.venv/bin/python scripts/camera_closed_loop_calib.py --session <SID> --device <DID>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.screen_ime.camera_calibration import (  # noqa: E402
    CameraCalibrationStore,
    CameraScreenTransform,
    detect_probe_tip,
    fit_camera_to_screen,
)
from adapters.screen_ime.geometry import (  # noqa: E402
    MappingRegistry,
    ScreenRobotTransform,
    fit_affine,
)
from adapters.screen_ime.service import now_iso  # noqa: E402
from drivers.real_arm import RealArmDriver  # noqa: E402
from drivers.real_camera import HttpFrameSource  # noqa: E402

API = "http://127.0.0.1:8788"
POINTS = [(0.50, 0.50), (0.20, 0.28), (0.80, 0.32), (0.55, 0.78)]
VERIFY_POINT = (0.35, 0.65)
AIM_TOLERANCE_PX = 18.0
Z_STEP_DOWN = 1.5      # Z 探触步进（≤10 软限位）
BUMP_DEPTH = 0.6       # 轻触测试深度
TRAVEL_LIFT = 5.0      # 长途移动前的抬升高度
TOUCH_TIMEOUT_S = 8.0


def _api_sync(path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=8) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def _fetch_session_sync(sid: str) -> dict:
    with urllib.request.urlopen(f"{API}/api/v1/calibrations/{sid}", timeout=8) as response:
        return json.loads(response.read())


async def api(path: str, body: dict | None = None):
    return await asyncio.to_thread(_api_sync, path, body)


async def fetch_session(sid: str) -> dict:
    return await asyncio.to_thread(_fetch_session_sync, sid)


def detect_crosshair(frame) -> tuple[float, float] | None:
    """绿色十字靶标质心（安卓校准页 RGB(0,150,100) → OpenCV H≈55-105）。"""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    green = cv2.inRange(hsv, np.array((55, 40, 60)), np.array((105, 255, 255)))
    cnts, _ = cv2.findContours(green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) < 300:
        return None
    m = cv2.moments(c)
    return (m["m10"] / m["m00"], m["m01"] / m["m00"])


class ClosedLoopCalibrator:
    def __init__(self, sid: str, device: str):
        self.sid = sid
        self.device = device
        self.arm = RealArmDriver(device)
        self.frames = HttpFrameSource()
        self.robot_samples: list[dict] = []
        self.camera_pairs: list[dict] = []
        self.screen_meta: dict = {}
        self.M: np.ndarray | None = None
        self.log: list[str] = []

    def say(self, msg: str) -> None:
        print(msg, flush=True)
        self.log.append(msg)

    # ---- 基础 ----
    def frame(self):
        last = None
        for _ in range(4):
            try:
                return self.frames()
            except Exception as exc:
                last = exc
                time.sleep(0.6)
        raise RuntimeError(f"取流桥连续取帧失败：{last}")

    def fresh_frame(self, timeout: float = 10.0):
        """等待取流桥推来"比上一张新"的帧；Chrome 页面切后台时 seq 不推进，给出明确提示。"""
        deadline = time.time() + timeout
        stale = 0
        while time.time() < deadline:
            f = self.frame()
            if self.frames.stream_alive:
                return f
            stale += 1
            time.sleep(0.4)
        raise RuntimeError(
            "取流桥超过 10 秒没有新帧——Chrome 取流页面可能被切到后台/最小化了，"
            "请把它点回前台后重试（这是 Chrome 对后台标签的节流）")

    def observe(self):
        for _ in range(4):
            frame = self.fresh_frame()
            cross = detect_crosshair(frame)
            tip = detect_probe_tip(frame)
            if cross is not None and tip is not None:
                return cross, tip
            time.sleep(0.5)
        raise RuntimeError("相机检测失败（十字或探头不可见）")

    def observe_tip(self):
        for _ in range(4):
            frame = self.fresh_frame()
            tip = detect_probe_tip(frame)
            if tip is not None:
                return tip
            time.sleep(0.5)
        raise RuntimeError("相机检测失败（探头不可见）")

    async def z_move(self, dz: float, feed: float = 30.0) -> None:
        await self.arm._send_motion_line("G91")
        await self.arm._send_motion_line(f"G1 Z{dz:.3f} F{feed:g}")
        await self.arm._send_motion_line("G90")
        await self.arm._wait_idle()

    async def jog(self, dx: float, dy: float) -> None:
        await self.arm.click({"dx": dx, "dy": dy, "feed_xy": 150})

    async def arm_sample(self, target: tuple[float, float] | None) -> str:
        state = await fetch_session(self.sid)
        seq = len(state.get("samples", [])) + 1
        sample_id = f"s{seq}"
        body: dict = {"sampleId": sample_id, "sequence": seq}
        if target is not None:
            body.update({"targetU": target[0], "targetV": target[1],
                         "targetWidthU": 0.14, "targetHeightV": 0.08})
        await api(f"/api/v1/calibrations/{self.sid}/arm", body)
        await asyncio.sleep(1.2)  # 等页面轮询到布防并画出十字
        return sample_id

    async def wait_touch(self, sample_id: str, timeout: float) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = await fetch_session(self.sid)
            for sample in state.get("samples", []):
                if sample["sampleId"] == sample_id:
                    return sample
            await asyncio.sleep(0.3)
        return None

    def record(self, mpos: dict, touch: dict, tip: tuple[float, float]) -> None:
        self.robot_samples.append({
            "robotX": float(mpos["x"]), "robotY": float(mpos["y"]),
            "screenX": float(touch["screenX"]), "screenY": float(touch["screenY"]),
        })
        self.camera_pairs.append({
            "cameraX": tip[0], "cameraY": tip[1],
            "screenX": float(touch["screenX"]), "screenY": float(touch["screenY"]),
        })
        self.screen_meta = {"screenWidth": int(touch.get("screenWidth", 0)),
                            "screenHeight": int(touch.get("screenHeight", 0)),
                            "orientation": touch.get("orientation", "unknown")}

    # ---- 阶段 1：Z 探触 ----
    async def z_probe(self) -> None:
        self.say("阶段 1：Z 探触（自动寻找屏幕接触点）…")
        sample_id = await self.arm_sample(None)
        mpos = (await self.arm.read_state())["position"]
        for step in range(1, 31):
            await self.arm.click({"dx": 0.0, "dy": 0.0, "tap_depth_mm": 0.6})
            touch = await self.wait_touch(sample_id, timeout=2.0)
            if touch is not None:
                self.say(f"  接触确认（第 {step} 次下探，累计 {step * Z_STEP_DOWN:.1f}mm）")
                self.record(mpos, touch, tip=self.observe_tip())
                return
            await self.z_move(+Z_STEP_DOWN)
            mpos = (await self.arm.read_state())["position"]
            self.say(f"  下探累计 {step * Z_STEP_DOWN:.1f}mm（Z={mpos['z']:.1f}），未接触")
        raise RuntimeError("下探 45mm 仍未接触屏幕，停止（请检查机械臂位置后重试）")

    # ---- 阶段 2：闭环点击 ----
    async def aim_at_crosshair(self) -> tuple[float, float]:
        cross, tip = self.observe()
        err = np.array([cross[0] - tip[0], cross[1] - tip[1]])
        self.say(f"  初始偏差 {err.round(1)} px")
        if self.M is None:
            base = np.array(tip)
            await self.jog(2.0, 0.0)
            tip_x = np.array(self.observe_tip())
            await self.jog(0.0, 2.0)
            tip_y = np.array(self.observe_tip())
            if np.linalg.norm(tip_x - base) < 2 or np.linalg.norm(tip_y - tip_x) < 2:
                raise RuntimeError("试 Jog 后画面位移过小（帧可能未更新或探头未动），中止学习")
            self.M = np.column_stack([(tip_x - base) / 2.0, (tip_y - tip_x) / 2.0])
            det = float(self.M[0, 0] * self.M[1, 1] - self.M[0, 1] * self.M[1, 0])
            if abs(det) < 1e-6:
                raise RuntimeError(f"相机/机械方向矩阵退化（det={det}）")
            self.say(f"  学习完成：相机像素/毫米矩阵 {self.M.round(2).tolist()}")
        for attempt in range(5):
            cross, tip = self.observe()
            err = np.array([cross[0] - tip[0], cross[1] - tip[1]])
            if np.linalg.norm(err) < AIM_TOLERANCE_PX:
                self.say(f"  瞄准收敛：残差 {err.round(1)} px")
                return tip
            step = np.linalg.solve(self.M, err)
            norm = float(np.linalg.norm(step))
            if norm > 28.0:
                step = step / norm * 28.0
            lifted = norm > 6.0
            if lifted:
                await self.z_move(-TRAVEL_LIFT)
            self.say(f"  修正 {attempt + 1}: 偏差 {err.round(1)} px → 移动 {step.round(2)} mm")
            await self.jog(float(step[0]), float(step[1]))
            if lifted:
                await self.z_move(+TRAVEL_LIFT)
        cross, tip = self.observe()
        err = float(np.linalg.norm(np.array(cross) - np.array(tip)))
        if err >= AIM_TOLERANCE_PX * 2:
            raise RuntimeError(f"瞄准未收敛（残差 {err:.0f}px）")
        return tip

    async def click_here(self, sample_id: str) -> dict:
        mpos = (await self.arm.read_state())["position"]
        await self.arm.click({"dx": 0.0, "dy": 0.0, "tap_depth_mm": 1.0})
        touch = await self.wait_touch(sample_id, timeout=TOUCH_TIMEOUT_S)
        if touch is None:
            raise RuntimeError(f"点击后未收到触点回报（{sample_id}）——悬停高度可能偏高")
        return {"mpos": mpos, "touch": touch}

    async def run_point(self, index: int, u: float, v: float) -> None:
        self.say(f"点 {index}（靶标 {u:.2f},{v:.2f}）：")
        sample_id = await self.arm_sample((u, v))
        await self.aim_at_crosshair()
        result = await self.click_here(sample_id)
        self.record(result["mpos"], result["touch"], tip=self.observe_tip())
        touch = result["touch"]
        self.say(f"  命中：触点 ({touch['screenX']:.1f},{touch['screenY']:.1f})，"
                 f"MPos ({result['mpos']['x']:.2f},{result['mpos']['y']:.2f})")

    # ---- 拟合与验收 ----
    def fit_and_save(self) -> tuple[dict, dict]:
        fit = fit_affine(self.robot_samples)
        meta = self.screen_meta
        registry = MappingRegistry(ROOT / "data" / "screen_ime_mappings.json")
        transform = ScreenRobotTransform.from_fit(
            fit, device_id=self.device,
            screen_width=int(meta.get("screenWidth", 0)),
            screen_height=int(meta.get("screenHeight", 0)),
            orientation=meta.get("orientation", "unknown"),
            version=1, created_at=now_iso())
        saved = registry.save(transform)
        self.say(f"机械↔屏幕映射已保存 v{saved['version']}，拟合残差 {fit['residualRms']:.2f}px")

        cam_fit = fit_camera_to_screen(self.camera_pairs)
        cam_store = CameraCalibrationStore(ROOT / "data" / "screen_ime_camera.json")
        cam_saved = cam_store.save(CameraScreenTransform.from_dict(
            {"homography": cam_fit["homography"], "reprojectionRms": cam_fit["reprojectionRms"],
             "deviceId": self.device, "createdAt": now_iso()}))
        self.say(f"摄像头↔屏幕单应已保存 v{cam_saved['version']}，"
                 f"重投影残差 {cam_fit['reprojectionRms']:.2f}px")
        return fit, cam_fit

    async def verify(self) -> None:
        self.say(f"验收：靶标 {VERIFY_POINT} 完全由视觉链路执行…")
        sample_id = await self.arm_sample(VERIFY_POINT)
        cross, _tip = self.observe()
        cam_transform = CameraScreenTransform.from_dict(
            CameraCalibrationStore(ROOT / "data" / "screen_ime_camera.json").get(self.device))
        screen = cam_transform.screen_from_camera(*cross)
        robot_transform = ScreenRobotTransform.from_dict(
            MappingRegistry(ROOT / "data" / "screen_ime_mappings.json").get(self.device))
        rx, ry = robot_transform.robot_from_screen(*screen)
        mpos = (await self.arm.read_state())["position"]
        dx, dy = rx - float(mpos["x"]), ry - float(mpos["y"])
        norm = float(np.hypot(dx, dy))
        if norm > 28.0:
            scale = 28.0 / norm
            dx, dy = dx * scale, dy * scale
        lifted = norm > 6.0
        if lifted:
            await self.z_move(-TRAVEL_LIFT)
        if abs(dx) > 0.05 or abs(dy) > 0.05:
            await self.jog(dx, dy)
        if lifted:
            await self.z_move(+TRAVEL_LIFT)
        await self.arm.click({"dx": 0.0, "dy": 0.0, "tap_depth_mm": 1.0})
        touch = await self.wait_touch(sample_id, timeout=TOUCH_TIMEOUT_S)
        if touch is None:
            self.say("  验收点击未收到触点回报，请人工查看屏幕位置")
            return
        err = float(np.hypot(touch["screenX"] - screen[0], touch["screenY"] - screen[1]))
        self.say(f"  验收结果：视觉预测触点 {tuple(round(v, 1) for v in screen)}，"
                 f"实际触点 ({touch['screenX']:.1f},{touch['screenY']:.1f})，"
                 f"端到端偏差 {err:.1f}px")

    async def run(self) -> None:
        self.say("连接机械臂…")
        try:
            info = await self.arm.connect()
        except Exception as exc:
            if "Hold" in str(exc):
                self.say("  控制器处于进给保持（上次中止的软停止），发送 ~ 恢复后重连")
                if self.arm._transport is None:
                    from drivers.grbl import PyserialGrblTransport

                    self.arm._transport = PyserialGrblTransport()
                    await asyncio.to_thread(self.arm._transport.open)
                await asyncio.to_thread(self.arm._transport.send_line, "~")
                await asyncio.sleep(0.5)
                await asyncio.to_thread(self.arm._transport.close)
                self.arm._transport = None
                info = await self.arm.connect()
            else:
                raise
        self.say(f"  已连接 {info['port']}｜控制器 {info['controller_state']}"
                 f"｜会话原点 {info['session_origin_mpos']}")
        await self.z_probe()
        for index, (u, v) in enumerate(POINTS[1:], start=2):
            await self.run_point(index, u, v)
        fit, cam_fit = self.fit_and_save()
        await self.verify()
        await api(f"/api/v1/calibrations/{self.sid}/complete")
        self.say("✅ 校准完成。此后：摄像头看到任意一点 → 单应换算屏幕像素 → 仿射逆变换机械坐标 → 精确点击。")
        (ROOT / "data" / "screen_calib_report.json").write_text(json.dumps({
            "robot_samples": self.robot_samples, "camera_pairs": self.camera_pairs,
            "screen_meta": self.screen_meta, "log": self.log},
            ensure_ascii=False, indent=2), encoding="utf-8")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session")
    parser.add_argument("--device", required=True)
    parser.add_argument("--serve-api", action="store_true",
                        help="内嵌屏幕输入法 API（主服务停用时由标定脚本临时供数）")
    args = parser.parse_args()

    if args.serve_api:
        # 主平台服务会占用串口做自检；标定期间由本进程内嵌输入法 API，
        # 手机校准页面无感（同一地址/端口），串口归标定脚本独占。
        import uvicorn
        from fastapi import FastAPI

        from adapters.screen_ime.routes import (
            create_screen_ime_router,
            screen_ime_error_handler,
        )
        from adapters.screen_ime.service import (
            ScreenImeApiError,
            get_default_service,
        )

        api_app = FastAPI()
        api_app.include_router(create_screen_ime_router(get_default_service()))
        api_app.add_exception_handler(ScreenImeApiError, screen_ime_error_handler)
        server = uvicorn.Server(uvicorn.Config(api_app, host="0.0.0.0", port=8788,
                                               log_level="warning"))
        asyncio.create_task(server.serve())
        await asyncio.sleep(1.0)

    session_id = args.session
    if not session_id:
        created = await api("/api/v1/calibrations", {"deviceId": args.device})
        session_id = created["sessionId"]
        print(f"已创建校准会话 {session_id}", flush=True)
    calib = ClosedLoopCalibrator(session_id, args.device)
    error = False
    try:
        await calib.run()
    except Exception as exc:
        error = True
        calib.say(f"✘ 中止：{exc}")
        raise
    finally:
        try:
            if error:
                await calib.arm.stop()
            await calib.arm.disconnect()
        except Exception:
            pass
        if args.serve_api:
            print("标定结束，内嵌 API 随进程退出（请重启主服务）。", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
