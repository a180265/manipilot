"""启用前剩余修复 S2：校准定位/动作前门/连续光流接同一本台粗框（§4.S2）。

修前红（本轮亲跑，先于修复）：
- S2-02红：camera.measure_scroll 全图定位（region 未接线=无 region 参数路径）
  ——修前该能力不消费粗框（TGN2fx/ENTRYPOINTS 静态确认 + digest 佐证）；
  修复后正式 Runtime 能力测量受框约束。
- S2-01红/02a/02b/S2-06红：动作前门与向导首次定位的框约束为本次新增能力
  （修前不存在该参数/分支，无法构造同路径红测；以 digest+静态记录佐证，
  修后正例+拒绝例为本轮亲跑绿）。

替身：假 broker 相机（合成帧）、假传输/假 arm；真实 check_live_calibration、
真实 measure_scroll 光流链、真实向导装配、真实粗框裁剪/定位。断言不 stub 门。
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import cv2
import numpy as np
import pytest

from drivers import calibration_bundle as cb
from drivers.screen_edge import ScreenEdgeError
from runtime import calibration_gate
from runtime.calibration_gate import check_live_calibration
from runtime.phone_region_context import RegionContextError
from tests.test_layout_observation import offline_only  # noqa: F401


def _now():
    return datetime.now(timezone.utc).isoformat()


FW, FH = 320, 240
SAVED = [[40, 40], [180, 40], [180, 180], [40, 180]]  # 本台手机（框内）
OFF = [[230, 60], [310, 60], [310, 200], [230, 200]]  # 框外第二台
BOX_LEFT = {"x": 0.0, "y": 0.0, "width": 0.625, "height": 1.0}  # 盖本台，不含框外


_CANVAS = None


def _scroll_canvas():
    global _CANVAS
    if _CANVAS is None:
        _CANVAS = np.full((FH + 400, FW + 200), 30, np.uint8)
        # 类列表行纹理（强角点、可跟踪、确定性平移）
        # 非周期行距（47/35/52...）：平移量不与行距混叠
        y = 10
        for step in (47, 35, 52, 41, 38, 49, 44):
            cv2.rectangle(_CANVAS, (12, y), (FW + 180, y + 16), 180, -1)
            cv2.rectangle(_CANVAS, (16, y + 3), (FW // 2, y + 13), 90, -1)
            y += step
        while y < FH + 380:
            cv2.rectangle(_CANVAS, (12, y), (FW + 180, y + 16), 180, -1)
            y += 43
    return _CANVAS


def _draw_phone(frame, quad, *, lit=True, texture_offset=0):
    pts = np.asarray(quad, dtype=np.int32)
    x0, y0 = max(0, int(pts[:, 0].min())), max(0, int(pts[:, 1].min()))
    x1, y1 = min(frame.shape[1], int(pts[:, 0].max())), \
        min(frame.shape[0], int(pts[:, 1].max()))
    inner = (210, 210, 210) if lit else (8, 8, 8)
    cv2.rectangle(frame, (x0, y0), (x1, y1), inner, -1)
    if lit:
        # 真实列表滚动语义：固定大画布纹理，取窗随 texture_offset 平移
        # （内容整体刚性平移，只有视野边缘进出——LK 友好）。
        h, w = y1 - y0, x1 - x0
        canvas = _scroll_canvas()
        ys = min(60 + texture_offset, canvas.shape[0] - h)
        xs = min(20, canvas.shape[1] - w)
        # 内容留 3px 亮边：手机外轮廓（210 全亮矩形）对暗背景仍可检测，
        # 内部行纹理供 LK 跟踪——不破坏 locate_screen_quad 的轮廓语义。
        m = 3
        if h > 2 * m and w > 2 * m:
            frame[y0 + m:y1 - m, x0 + m:x1 - m] = \
                canvas[ys + m:ys + h - m, xs + m:xs + w - m, None]


def _frame(*, phone_off=0, offbox_shift=0, lit_in=True, lit_off=True):
    frame = np.full((FH, FW, 3), 28, np.uint8)
    quad = [[x, y + phone_off] for x, y in SAVED]
    _draw_phone(frame, quad, lit=lit_in, texture_offset=phone_off)
    off = [[x, y + offbox_shift] for x, y in OFF]
    _draw_phone(frame, off, lit=lit_off)
    ok, png = cv2.imencode(".png", frame)
    assert ok
    return png.tobytes(), frame


def _v3_bundle():
    bundle = cb.new_bundle("arm-01", camera_id="cam-01")
    bundle.update({
        "status": "ready",
        "cameraDeviceUniqueId": "uvc-01",
        "bindingRevision": 7,
        "cameraBindingId": "cam-bind-9",
        "sourceGeometryId": "geo-s2",
        "daemonAddr": "127.0.0.1:8795",
        "daemonSession": "session-01",
        "frame": {"width": FW, "height": FH},
        "screen": {"orientation": "portrait", "screenWidth": FW,
                   "screenHeight": FH,
                   "edgeModel": {"corners": SAVED, "tolerancePx": 8}},
        "z": {"hoverZ": 2, "tapDepth": 3, "tapFeed": 1500, "tapDwellMs": 0,
              "deleteHoldMs": 2200, "deleteDepth": 3, "deleteFeed": 1500,
              "validated": True, "deleteValidated": True},
        "xy": {"robotToScreen": {"a": 1, "b": 0, "c": 0, "d": 0, "e": 1, "f": 0},
               "residualRmsPx": 0, "maxResidualPx": 0, "validated": True},
        "camera": {"cameraToScreen": {"homography": [[1, 0, 0], [0, 1, 0],
                                                     [0, 0, 1]]},
                   "reprojectionRmsPx": 0, "validated": True},
        "acceptance": {"debug": True, "wechat": True, "checks": []},
    })
    bundle["bindingFingerprint"] = cb.binding_fingerprint(
        device_id="arm-01", camera_id="cam-01",
        camera_device_unique_id="uvc-01", daemon_addr="127.0.0.1:8795",
        frame_width=FW, frame_height=FH)
    return bundle


def _region(rect):
    from runtime.layout_matching import _pixel_rect
    left, top, right, bottom = _pixel_rect(rect, FW, FH)
    return {"rect": dict(rect),
            "pixel_rect": {"left": left, "top": top, "right": right,
                           "bottom": bottom, "frameW": FW, "frameH": FH},
            "source_size": {"width": FW, "height": FH},
            "source_geometry_id": "geo-s2",
            "installation_binding": {"camera_id": "cam-01",
                                     "camera_device_unique_id": "uvc-01",
                                     "camera_binding_id": "cam-bind-9"}}


class RegionCamera:
    """假 broker 相机：依次返回合成帧（真实门禁消费它们，不 stub 门）。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.seq = iter(range(100, 10 ** 6))

    async def capture(self):
        jpg = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return {"image_bytes": jpg, "image": jpg, "camera_id": "cam-01",
                "device_id": "arm-01", "device_unique_id": "uvc-01",
                "binding_revision": 7, "width": FW, "height": FH,
                "frozen": False, "source_frame_seq": next(self.seq),
                "source_received_at": _now(), "source_captured_at": _now(),
                "captured_at": _now(), "sha256": f"{next(self.seq):064x}"}


def _gate(frames, region, monkeypatch, *, require_pose=False):
    monkeypatch.setattr(calibration_gate, "EDGE_CONSENSUS_INTERVAL_S", 0)
    return asyncio.run(check_live_calibration(
        device_id="arm-01", camera_id="cam-01", daemon_addr="127.0.0.1:8795",
        daemon_session="session-01", estop_locked=False,
        camera=RegionCamera(frames), bundle=_v3_bundle(), wechat_send=True,
        search_region=region, require_pose=require_pose))


def test_s2_01_gate_inbox_pass_offbox_ignored(env_none=None):
    """S2-01 合法：框内本台 + 框外亮机 → 真实门通过（框外不干扰）。"""
    jpg, _ = _frame()
    monkeypatch = pytest.MonkeyPatch()
    try:
        checks, frame, _ = _gate([jpg], _region(BOX_LEFT), monkeypatch,
                                 require_pose=True)
        blocking = [c for c in checks if c["code"] in (
            "screen_pose_unavailable", "camera_waiting", "camera_unavailable",
            "region_context_invalid", "camera_binding_changed")]
        assert blocking == [], checks
        assert frame.get("_screen_pose") is not None, "框内姿态必须可观测"
    finally:
        monkeypatch.undo()


def test_s2_01_gate_two_in_box_rejected():
    """S2-01 反例：框内两台 → 多候选拒绝（真实门，不 stub）。"""
    frame = np.full((FH, FW, 3), 28, np.uint8)
    # 干净矩形（无噪声纹理）：保证 _screen_candidates 能数出两个候选
    for quad in ([[15, 35], [85, 35], [85, 195], [15, 195]],
                 [[110, 35], [185, 35], [185, 195], [110, 195]]):
        pts = np.asarray(quad, np.int32)
        x0, y0 = int(pts[:, 0].min()), int(pts[:, 1].min())
        x1, y1 = int(pts[:, 0].max()), int(pts[:, 1].max())
        cv2.rectangle(frame, (x0, y0), (x1, y1), (210, 210, 210), -1)
    ok, png = cv2.imencode(".png", frame)
    from runtime.layout_region import _screen_candidates
    assert len(_screen_candidates(frame[:, :200])) == 2
    monkeypatch = pytest.MonkeyPatch()
    try:
        checks, _f, _d = _gate([png.tobytes()], _region(BOX_LEFT),
                               monkeypatch)
        blocked = any(c["code"] in ("screen_pose_unavailable",
                                    "camera_unavailable",
                                    "camera_waiting") for c in checks)
        assert blocked, checks
    finally:
        monkeypatch.undo()


def test_s2_01_gate_tight_box_rejected():
    """S2-01 反例：紧框裁断本台屏幕 → 明确拒绝（不降级、不换内缘）。"""
    tight = {"x": 0.0, "y": 0.0, "width": 0.34, "height": 1.0}  # 右缘 108 穿过屏幕
    jpg, _ = _frame(lit_off=False)
    monkeypatch = pytest.MonkeyPatch()
    try:
        checks, _f, _d = _gate([jpg], _region(tight), monkeypatch)
        assert any(c["code"] in ("screen_pose_unavailable",
                                 "camera_unavailable") for c in checks), checks
    finally:
        monkeypatch.undo()


@pytest.mark.parametrize("content_shift", [-12, 12])
@pytest.mark.parametrize("perspective", [False, True])
def test_s2_02_measure_scroll_real_region_locked(tmp_path, content_shift, perspective):
    """S2-02 红→绿：正式 Runtime 能力 measure_scroll 框内测量。

    已知位移：屏幕内容上下移 12px（合成列表纹理）；框外第二台同帧大幅
    移动不计入。走真实 Runtime 能力分派（plugin_invoke_capability）。
    """
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime
    from tests.helpers import make_device_configs

    async def scenario():
        tmp = tmp_path
        runtime = ArmFleetRuntime(PlatformConfig(
            {"db_path": str(tmp / "f.db"), "evidence_dir": str(tmp / "ev"),
             "plugin_dir": str(tmp / "p"), "hardware_mode": "simulation",
             "daemon_supervisor_enabled": False,
             "hotplug_discovery_enabled": False, "screen_ime_enabled": False},
            root=tmp))
        await runtime.startup(device_configs=make_device_configs(
            1, camera_device_unique_id="uvc-01", binding_revision=7,
            daemon_addr="127.0.0.1:8795", expected_frame_width=FW,
            expected_frame_height=FH))
        runtime.devices._devices["arm-01"]["daemon_session"] = "session-01"
        runtime.devices.driver("arm-01").data_root = tmp / "data"
        from tests.test_calibrated_scroll import SCROLL_PLUGIN_SOURCE as _sps
        runtime.plugins.install(_sps, operator_id="t", reason="S2 测试")
        runtime.plugins.activate("screen-gesture-adapter", operator_id="t",
                                 reason="S2 测试")
        (tmp / "data").mkdir(exist_ok=True)
        bundle = _v3_bundle()
        quad = np.float32([[40, 40], [180, 45], [175, 185], [45, 180]]
                          if perspective else SAVED)
        screen_corners = np.float32([[0, 0], [140, 0], [140, 140], [0, 140]])
        camera_to_screen = cv2.getPerspectiveTransform(quad, screen_corners)
        bundle["screen"].update(screenWidth=140, screenHeight=140,
                                edgeModel={"corners": quad.tolist(), "tolerancePx": 8})
        bundle["camera"]["cameraToScreen"]["homography"] = camera_to_screen.tolist()
        cb.save_bundle(bundle, tmp / "data")
        runtime.store.save_task({"task_id": "t-s2", "device_id": "arm-01",
                                 "task_type": "calibrated_scroll",
                                 "status": "awaiting_device",
                                 "payload": {},
                                 "plugin_id": "screen-gesture-adapter"})
        task = runtime.store.get_task("t-s2")
        _lease = runtime.leases.acquire("arm-01", "t-s2")
        task.update(lease_id=_lease["lease_id"], status="executing")
        runtime.store.save_task(task)
        runtime.validate_plugin_context("screen-gesture-adapter", task)

        # 安装本台粗框（新模式）
        from runtime.layout_region import canonical_digest
        search = _region({"x": 20 / FW, "y": 10 / FH,
                          "width": 180 / FW, "height": 210 / FH})
        record = {"contract_version": "phone-search-region.v1",
                  "device_id": "arm-01", "version": 1,
                  "installation_binding": _region(BOX_LEFT)[
                      "installation_binding"],
                  "source_size": {"width": FW, "height": FH},
                  "source_geometry_id": "geo-s2", "rect": search["rect"],
                  "pixel_rect": search["pixel_rect"],
                  "reference_evidence_id": "e", "created_at": _now(),
                  "created_by": "t"}
        record["sha256"] = canonical_digest(record)
        store = runtime.layout_bundle()["region_store"]
        store.insert_version("arm-01", 1, record)
        pointer = store.cas_pointer("arm-01", 0, 1, record["sha256"], "op-s2")
        pinned = {"revision": pointer["revision"],
                  "selected_version": 1, "selected_sha256": record["sha256"]}
        from runtime.scroll_context import ScrollContext
        observation_context = ScrollContext(runtime, "arm-01").archive_context()

        def grab(label, content_shift, offbox_shift):
            frame = np.full((FH, FW, 3), 28, np.uint8)
            flat = np.full((141, 141, 3), 210, np.uint8)
            texture = np.full((240, 141, 3), 210, np.uint8)
            rng = np.random.default_rng(412)
            for x, y in rng.integers([18, 8], [120, 232], size=(90, 2)):
                cv2.rectangle(texture, (int(x), int(y)), (int(x + 4), int(y + 4)),
                              (65, 65, 65), -1)
            offset = 40 + content_shift
            flat[3:-3, 3:-3] = texture[offset + 3:offset + 138, 3:138]
            screen_to_camera = np.linalg.inv(camera_to_screen)
            projected = cv2.warpPerspective(flat, screen_to_camera, (FW, FH))
            mask = cv2.warpPerspective(np.full((141, 141), 255, np.uint8),
                                       screen_to_camera, (FW, FH))
            frame[mask > 0] = projected[mask > 0]
            off = [[x, y + offbox_shift] for x, y in OFF]
            _draw_phone(frame, off, lit=True)
            ok, png = cv2.imencode(".png", frame)
            rec = runtime.evidence.archive_camera_frame(
                {"image_bytes": png.tobytes(), "width": FW, "height": FH,
                 "device_unique_id": "uvc-01", "binding_revision": 7,
                 "camera_binding_id": "cam-bind-9", "source_geometry_id": "geo-s2",
                 "source_generation_id": "offline-s2", "source_frame_seq": 1 if label == "before" else 2,
                 "camera_id": "cam-01", "frozen": False},
                observation_context, device_id="arm-01", camera_id="cam-01",
                task_id="t-s2", label=label, operator="plugin:test")
            return rec["evidence_id"]

        before = grab("before", 0, 0)
        after = grab("after", content_shift, 60)
        result = await runtime.plugin_invoke_capability(
            "screen-gesture-adapter", task, "camera.measure_scroll", {
                "op": "measure", "evidence_ids": [before, after],
                "roi": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
                "roi_frame": "screen_norm",
                "search_region_pointer": pinned})
        assert result["status"] == "displaced", repr(result)
        assert result["dy_px"] == pytest.approx(-content_shift, abs=3), result
        await runtime.shutdown()
        return runtime

    runtime = asyncio.run(scenario())
    runtime.store._conn.close() if hasattr(runtime.store, "_conn") else None


def test_s2_03_region_change_invalidates_chain(tmp_path):
    """S2-03 红→绿：抓帧后换框（钉定指针≠当前指针）→ unknown，非有效结论。"""
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime
    from tests.helpers import make_device_configs

    async def scenario():
        tmp = tmp_path
        runtime = ArmFleetRuntime(PlatformConfig(
            {"db_path": str(tmp / "f.db"), "evidence_dir": str(tmp / "ev"),
             "plugin_dir": str(tmp / "p"), "hardware_mode": "simulation",
             "daemon_supervisor_enabled": False,
             "hotplug_discovery_enabled": False, "screen_ime_enabled": False},
            root=tmp))
        from tests.test_calibrated_scroll import SCROLL_PLUGIN_SOURCE as _sps
        runtime.plugins.install(_sps, operator_id="t", reason="S2 测试")
        runtime.plugins.activate("screen-gesture-adapter", operator_id="t",
                                 reason="S2 测试")
        await runtime.startup(device_configs=make_device_configs(
            1, camera_device_unique_id="uvc-01", binding_revision=7,
            daemon_addr="127.0.0.1:8795", expected_frame_width=FW,
            expected_frame_height=FH))
        runtime.devices._devices["arm-01"]["daemon_session"] = "session-01"
        runtime.devices.driver("arm-01").data_root = tmp / "data"
        (tmp / "data").mkdir(exist_ok=True)
        cb.save_bundle(_v3_bundle(), tmp / "data")
        runtime.store.save_task({"task_id": "t-s23", "device_id": "arm-01",
                                 "task_type": "calibrated_scroll",
                                 "status": "awaiting_device",
                                 "payload": {},
                                 "plugin_id": "screen-gesture-adapter"})
        task = runtime.store.get_task("t-s23")
        _lease = runtime.leases.acquire("arm-01", "t-s23")
        task.update(lease_id=_lease["lease_id"], status="executing")
        runtime.store.save_task(task)
        runtime.validate_plugin_context("screen-gesture-adapter", task)
        from runtime.layout_region import canonical_digest
        record = {"contract_version": "phone-search-region.v1",
                  "device_id": "arm-01", "version": 1,
                  "installation_binding": _region(BOX_LEFT)[
                      "installation_binding"],
                  "source_size": {"width": FW, "height": FH},
                  "source_geometry_id": "geo-s2", "rect": dict(BOX_LEFT),
                  "pixel_rect": _region(BOX_LEFT)["pixel_rect"],
                  "reference_evidence_id": "e", "created_at": _now(),
                  "created_by": "t"}
        record["sha256"] = canonical_digest(record)
        store = runtime.layout_bundle()["region_store"]
        store.insert_version("arm-01", 1, record)
        pointer = store.cas_pointer("arm-01", 0, 1, record["sha256"], "op-a")
        stale_pinned = {"revision": pointer["revision"], "selected_version": 1,
                        "selected_sha256": record["sha256"]}
        # 抓帧后换框：CAS 到版本 2（新框=右半）
        record2 = dict(record)
        record2["version"] = 2
        record2["rect"] = {"x": 0.375, "y": 0.0, "width": 0.625, "height": 1.0}
        record2["pixel_rect"] = {"left": 120, "top": 0, "right": FW,
                                 "bottom": 240, "frameW": FW, "frameH": FH}
        record2["sha256"] = canonical_digest(
            {key: value for key, value in record2.items() if key != "sha256"})
        store.insert_version("arm-01", 2, record2)
        pointer2 = store.cas_pointer("arm-01", pointer["revision"], 2,
                                     record2["sha256"], "op-b")
        assert pointer2["selected_version"] == 2

        f = np.full((FH, FW, 3), 28, np.uint8)
        _draw_phone(f, SAVED, lit=True)
        ok, png = cv2.imencode(".png", f)
        rec = runtime.evidence.archive_camera_frame(
            {"image_bytes": png.tobytes(), "width": FW, "height": FH,
             "device_unique_id": "uvc-01", "binding_revision": 7,
             "camera_id": "cam-01", "frozen": False},
            {"search_region_pointer": stale_pinned}, device_id="arm-01",
            task_id="t-s23", label="before", operator="plugin:test")
        f2 = np.full((FH, FW, 3), 28, np.uint8)
        _draw_phone(f2, SAVED, lit=True)
        ok, png2 = cv2.imencode(".png", f2)
        rec2 = runtime.evidence.archive_camera_frame(
            {"image_bytes": png2.tobytes(), "width": FW, "height": FH,
             "device_unique_id": "uvc-01", "binding_revision": 7,
             "camera_id": "cam-01", "frozen": False},
            {"search_region_pointer": stale_pinned}, device_id="arm-01",
            task_id="t-s23", label="after", operator="plugin:test")
        result = await runtime.plugin_invoke_capability(
            "screen-gesture-adapter", task, "camera.measure_scroll", {
                "op": "measure",
                "evidence_ids": [rec["evidence_id"], rec2["evidence_id"]],
                "roi": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
                "roi_frame": "screen_norm",
                "search_region_pointer": stale_pinned})
        assert result["status"] == "unknown", result
        assert result["reason_code"] == "region_context_changed", result
        stored = runtime.store.get_task("t-s23")
        assert stored.get("lease_id") == task.get("lease_id"), "租约事实复核"
        await runtime.shutdown()
        return runtime, task, result

    runtime, task, result = asyncio.run(scenario())


def test_s2_06_wizard_first_locate_region_bounded(tmp_path):
    """S2-06 红→绿：无 ready/H/模板，真实向导装配首次定位受粗框约束。"""
    from tests.test_calibration_wizard import Env, CAM_W, CAM_H
    assert CAM_W > FW and CAM_H > FH

    def render(phones, lit_flags):
        frame = np.full((CAM_H, CAM_W, 3), 28, np.uint8)
        for (quad, lit) in zip(phones, lit_flags):
            _draw_phone(frame, quad, lit=lit)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
        return jpg.tobytes()

    seq = iter(range(1, 10 ** 6))
    in_box_big = [[100, 80], [520, 80], [520, 460], [100, 460]]
    off_big = [[700, 80], [930, 80], [930, 460], [700, 460]]
    frames = [render([in_box_big, off_big], [True, True])]
    env = Env(tmp_path, wizard_extra={
        "camera_fetch": lambda: (frames.pop(0) if len(frames) > 1
                                 else frames[0], next(seq)),
        "search_region_provider": lambda: {
            "rect": {"x": 0.0, "y": 0.0, "width": 0.625, "height": 1.0},
            "pixel_rect": {"left": 0, "top": 0,
                           "right": int(0.625 * CAM_W), "bottom": CAM_H,
                           "frameW": CAM_W, "frameH": CAM_H},
            "source_size": {"width": CAM_W, "height": CAM_H}},
    })
    quad = env.wizard._edge_quad()
    assert quad["frameW"] == CAM_W and quad["frameH"] == CAM_H
    corners = np.asarray(quad["corners"], dtype=float)
    expected = np.asarray(in_box_big, dtype=float)
    assert np.max(np.abs(corners - expected)) < 40, (
        corners, expected, "首次定位必须回到原图坐标的本台屏幕")


def test_s2_07_wizard_region_context_failure_is_terminal_not_h01(tmp_path):
    """S2-07：粗框上下文失效 → WizardError 终态（不是 H01 姿态提示继续）。"""
    from tests.test_calibration_wizard import CAM_H, CAM_W, Env
    from drivers.calibration_wizard import WizardError, PoseUnlocatable

    context = {"broken": False}

    def provider():
        if context["broken"]:
            raise RegionContextError("region_context_invalid",
                                     "生效粗框已损坏")
        return {"rect": {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
                "pixel_rect": {"left": 0, "top": 0, "right": CAM_W,
                               "bottom": CAM_H, "frameW": CAM_W,
                               "frameH": CAM_H},
                "source_size": {"width": CAM_W, "height": CAM_H}}

    good_frame = np.full((CAM_H, CAM_W, 3), 28, np.uint8)
    _draw_phone(good_frame, [[100, 80], [520, 80], [520, 460], [100, 460]],
                lit=True)
    good = cv2.imencode(".jpg", good_frame)[1].tobytes()
    frames = [good, good, good]
    env = Env(tmp_path, wizard_extra={
        "camera_fetch": lambda: (frames.pop(0) if len(frames) > 1
                                 else frames[0], next(iter(range(1, 10**6)))),
        "search_region_provider": provider,
        "edge_detector": lambda jpg: {"corners": [[100, 80], [520, 80],
                                                  [520, 460], [100, 460]],
                                      "frameW": CAM_W, "frameH": CAM_H},
    })
    # 第一次：生效粗框正常 → 走框内定位成功
    first = env.wizard._edge_quad()
    assert first["frameW"] == CAM_W
    # 第二次：provider 报粗框上下文失效 → WizardError 终态（非 H01 提示）
    context["broken"] = True
    with pytest.raises(WizardError) as raised:
        env.wizard._edge_quad()
    assert "region_context_invalid" in str(raised.value)
    assert not isinstance(raised.value, PoseUnlocatable), \
        "上下文失效不得降为 H01 姿态提示"
