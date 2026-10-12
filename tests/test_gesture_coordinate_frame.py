"""坐标桥旁档（规范第 4.2/4.3/22.4 节）。

覆盖：
- 22.4 节两个坐标反例：View 本地事件 → display → calibration 的纯函数转换，
  不得把 View 本地坐标当全屏，也不得在像素域再乘 density。
- 旁档存取：缺文件返回 None；plan_program(None) 报 coordinate_frame_missing；
  同 run 绑定校验通过；换 bundle（daemonSession 变化）报 gesture_binding_changed。
- 协调器缓存旧校准页元数据 + /legacy-metadata 查询端点（200/404）。
- 向导完成时尽力写旁档：metadata 存在生成可校验旁档；metadata 缺失无文件
  且不抛；异常只留痕；旁档写入前后 calibration_bundle.json 字节不变。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from adapters.screen_ime.gesture_capture import GestureCaptureCoordinator
from adapters.screen_ime.service import (
    ScreenImeApiError, ScreenImeService, set_default_service,
)
from drivers import calibration_bundle as cb
from drivers import calibration_wizard as cw
from drivers import gesture_program as gp
from drivers.screen_gestures import binding_snapshot
from tests.test_screen_gesture_driver import gesture_bundle

CLIENT_INSTANCE_ID = "0b6a6d48-c7e7-4a3f-9d5e-2f2f2e08c001"
LEGACY_SESSION = "cal-9f3c2a1b-4d5e-4f60-8a12-2f2f2e08c0aa"


def _two_point_program():
    return {
        "program_version": "gesture-program.v1",
        "coordinate_space": "calibration_root_normalized.v1",
        "strokes": [{"points": [{"x": 0.5, "y": 0.85, "t_ms": 0},
                                {"x": 0.5, "y": 0.58, "t_ms": 2000}],
                     "hold_before_ms": 0, "hold_after_ms": 0, "gap_after_ms": 0}],
    }


def _frame_for(bundle: dict, *, root_origin, run_id: str = "run-abc123") -> dict:
    return gp.build_coordinate_frame(
        binding=binding_snapshot(bundle),
        calibration_run_id=run_id,
        root_width_px=bundle["screen"]["screenWidth"],
        root_height_px=bundle["screen"]["screenHeight"],
        display_width_px=1080, display_height_px=2400,
        root_origin_display_px=list(root_origin), density=3.0,
        source_client_instance_id=CLIENT_INSTANCE_ID)


# ---------- 22.4 节坐标反例（纯函数） ----------

class TestDisplayToCalibrationCounterexamples:
    def test_view_local_event_is_not_screen_coordinate(self):
        """反例 1：root 原点 (0,0)、View 本地 (200,100)、View 窗口原点 (0,80)。"""
        frame = _frame_for(gesture_bundle(), root_origin=[0, 0])
        event_x, event_y = 200, 100  # 产生该事件的 View 本地坐标
        view_origin_x, view_origin_y = 0, 80  # 该 View 窗口原点（display 域）
        display_x = view_origin_x + event_x
        display_y = view_origin_y + event_y
        assert (display_x, display_y) == (200, 180)
        calibration = gp.display_to_calibration_px(display_x, display_y, frame)
        assert calibration == (200, 180)
        assert calibration != (200, 100)  # 不能把 View 本地坐标当全屏

    def test_root_origin_subtracted_without_density_scaling(self):
        """反例 2：root 原点 (0,24)：calibration=(200,156)，不得再乘 density。"""
        frame = _frame_for(gesture_bundle(), root_origin=[0, 24])
        assert frame["density"] == 3.0
        event_x, event_y = 200, 100
        view_origin_x, view_origin_y = 0, 80
        display_x = view_origin_x + event_x
        display_y = view_origin_y + event_y
        assert (display_x, display_y) == (200, 180)
        calibration = gp.display_to_calibration_px(display_x, display_y, frame)
        assert calibration == (200, 156)
        assert calibration != (200 * frame["density"], 156 * frame["density"])


# ---------- 旁档存取与绑定 ----------

class TestFramePersistence:
    def test_load_missing_file_returns_none(self, tmp_path: Path):
        assert gp.load_coordinate_frame("arm-01", data_root=tmp_path) is None

    def test_plan_program_without_frame_rejected(self):
        bundle = gesture_bundle()
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.plan_program(_two_point_program(), bundle, None)
        assert exc.value.reason_code == "coordinate_frame_missing"

    def test_save_load_validate_same_run(self, tmp_path: Path):
        bundle = gesture_bundle()
        frame = _frame_for(bundle, root_origin=[0, 80])
        gp.save_coordinate_frame(bundle["deviceId"], frame, data_root=tmp_path)
        loaded = gp.load_coordinate_frame(bundle["deviceId"], data_root=tmp_path)
        assert loaded is not None
        gp.validate_coordinate_frame(loaded, bundle)

    def test_changed_daemon_session_is_binding_change(self, tmp_path: Path):
        bundle = gesture_bundle()
        frame = _frame_for(bundle, root_origin=[0, 80])
        gp.save_coordinate_frame(bundle["deviceId"], frame, data_root=tmp_path)
        loaded = gp.load_coordinate_frame(bundle["deviceId"], data_root=tmp_path)
        changed = json.loads(json.dumps(bundle))
        changed["daemonSession"] = "sess-B"
        with pytest.raises(gp.GestureProgramError) as exc:
            gp.validate_coordinate_frame(loaded, changed)
        assert exc.value.reason_code == "gesture_binding_changed"


# ---------- 协调器旧校准页元数据缓存与查询端点 ----------

def _page(page_id: str = "gesture-capture", legacy=None, **override):
    page = {
        "pageId": page_id, "pageGeneration": 3,
        "resumed": True, "hasFocus": True, "rotation": 0,
        "rootWidthPx": 1080, "rootHeightPx": 2320,
        "displayWidthPx": 1080, "displayHeightPx": 2400,
        "rootOriginDisplayPx": [0, 80], "density": 3.0,
        "systemGestureInsets": {"left": 20, "top": 40, "right": 20, "bottom": 60},
        "systemBarInsets": {"left": 0, "top": 80, "right": 0, "bottom": 60},
        "legacyCalibrationSessionId": legacy, "coordinateSpaceVersion": 2,
    }
    page.update(override)
    return page


def _hello(page):
    return {"contractVersion": "gesture-capture.v1", "deviceId": "ime-01",
            "clientInstanceId": CLIENT_INSTANCE_ID, "heartbeatSeq": 1,
            "apkVersionCode": 7, "capabilities": ["gesture_trace.v1"],
            "page": page}


@pytest.fixture()
def rig(tmp_path: Path):
    service = ScreenImeService(tmp_path / "ime.db", tmp_path / "ime_mappings.json")
    coordinator = GestureCaptureCoordinator(
        data_root=tmp_path, legacy_armed_provider=service.calibration_armed)
    service.gesture_capture = coordinator
    set_default_service(service)
    from adapters.screen_ime.gesture_routes import create_gesture_router
    from adapters.screen_ime.routes import (
        create_screen_ime_router, screen_ime_error_handler,
    )

    app = FastAPI()
    app.include_router(create_screen_ime_router(service))
    app.include_router(create_gesture_router(service))
    app.add_exception_handler(ScreenImeApiError, screen_ime_error_handler)
    with TestClient(app) as client:
        yield client, coordinator, tmp_path
    set_default_service(None)


class TestLegacyMetadataCache:
    def test_register_caches_legacy_page_metadata(self, rig):
        _client, coordinator, _root = rig
        page = _page("legacy-calibration", legacy=LEGACY_SESSION)
        asyncio.run(coordinator.register_client("ime-01", _hello(page)))
        found = coordinator.legacy_metadata("ime-01", LEGACY_SESSION)
        assert found is not None
        assert found["sessionId"] == LEGACY_SESSION
        assert found["page"] == page
        assert found["clientInstanceId"] == CLIENT_INSTANCE_ID

    def test_different_session_id_returns_none(self, rig):
        _client, coordinator, _root = rig
        page = _page("legacy-calibration", legacy=LEGACY_SESSION)
        asyncio.run(coordinator.register_client("ime-01", _hello(page)))
        assert coordinator.legacy_metadata("ime-01", "cal-other-0001") is None
        assert coordinator.legacy_metadata("ime-02", LEGACY_SESSION) is None

    def test_plain_page_not_cached(self, rig):
        _client, coordinator, _root = rig
        asyncio.run(coordinator.register_client(
            "ime-01", _hello(_page(legacy=None))))
        assert coordinator.legacy_metadata("ime-01", LEGACY_SESSION) is None


class TestLegacyMetadataEndpoint:
    def test_lookup_200_then_404(self, rig):
        client, _coordinator, _root = rig
        page = _page("legacy-calibration", legacy=LEGACY_SESSION)
        registered = client.post("/api/v1/devices/ime-01/gesture-client",
                                 json=_hello(page))
        assert registered.status_code == 200
        ok = client.get("/api/v1/gesture-protocol/legacy-metadata",
                        params={"deviceId": "ime-01", "sessionId": LEGACY_SESSION})
        assert ok.status_code == 200
        body = ok.json()
        assert body["deviceId"] == "ime-01"
        assert body["sessionId"] == LEGACY_SESSION
        assert body["clientInstanceId"] == CLIENT_INSTANCE_ID
        assert body["page"] == page
        missing = client.get("/api/v1/gesture-protocol/legacy-metadata",
                             params={"deviceId": "ime-01",
                                     "sessionId": "cal-other-0001"})
        assert missing.status_code == 404
        assert missing.json()["error"] == "legacy_metadata_not_found"


# ---------- 向导完成时写旁档（尽力而为） ----------

class _FakeIme:
    def __init__(self, metadata: dict | None = None, error: Exception | None = None):
        self.metadata = metadata
        self.error = error
        self.calls: list[tuple[str, str]] = []

    def fetch_gesture_frame_metadata(self, ime_device_id: str, session_id: str):
        self.calls.append((ime_device_id, session_id))
        if self.error is not None:
            raise self.error
        return self.metadata


class _FakeDaemon:
    def status(self):
        return {"connected": True, "session": "sess-A", "device_present": True,
                "estop_locked": False, "lease": None, "last_error": None}

    def mpos(self):
        return {"x": 0.0, "y": 0.0, "z": 5.0, "state": "Idle"}


def _legacy_metadata() -> dict:
    return {"deviceId": "ime-01", "sessionId": LEGACY_SESSION,
            "clientInstanceId": CLIENT_INSTANCE_ID,
            "page": _page("legacy-calibration", legacy=LEGACY_SESSION,
                          rootOriginDisplayPx=[0, 24])}


def _make_wizard(tmp_path: Path, ime: _FakeIme) -> cw.CalibrationWizard:
    wizard = cw.CalibrationWizard(
        device="arm-01", daemon=_FakeDaemon(), ime=ime, data_root=tmp_path)
    wizard.state["imeDeviceId"] = "ime-01"
    wizard.state["imeSessionId"] = LEGACY_SESSION
    return wizard


class TestWizardSidecarSave:
    def test_metadata_present_writes_valid_sidecar(self, tmp_path: Path):
        bundle = gesture_bundle()
        cb.save_bundle(bundle, tmp_path)
        bundle_file = cb.bundle_path("arm-01", tmp_path)
        before = bundle_file.read_bytes()
        ime = _FakeIme(metadata=_legacy_metadata())
        wizard = _make_wizard(tmp_path, ime)
        wizard._save_gesture_coordinate_frame(bundle)
        frame = gp.load_coordinate_frame("arm-01", data_root=tmp_path)
        assert frame is not None
        gp.validate_coordinate_frame(frame, cb.load_bundle("arm-01", tmp_path))
        assert frame["calibration_run_id"] == "manual-finish"
        assert frame["source_client_instance_id"] == CLIENT_INSTANCE_ID
        assert frame["root_origin_display_px"] == [0, 24]
        assert ime.calls == [("ime-01", LEGACY_SESSION)]
        assert bundle_file.read_bytes() == before  # 旁档是独立文件

    def test_auto_run_id_used_when_present(self, tmp_path: Path):
        bundle = gesture_bundle()
        bundle["autoCalibration"] = {"runId": "auto-run-9", "state": "PROMOTED"}
        cb.save_bundle(bundle, tmp_path)
        ime = _FakeIme(metadata=_legacy_metadata())
        wizard = _make_wizard(tmp_path, ime)
        wizard._save_gesture_coordinate_frame(bundle)
        frame = gp.load_coordinate_frame("arm-01", data_root=tmp_path)
        assert frame is not None
        assert frame["calibration_run_id"] == "auto-run-9"

    def test_metadata_missing_writes_nothing_and_never_raises(self, tmp_path: Path):
        bundle = gesture_bundle()
        cb.save_bundle(bundle, tmp_path)
        bundle_file = cb.bundle_path("arm-01", tmp_path)
        before = bundle_file.read_bytes()
        wizard = _make_wizard(tmp_path, _FakeIme(metadata=None))
        wizard._save_gesture_coordinate_frame(bundle)  # 不抛
        assert gp.load_coordinate_frame("arm-01", data_root=tmp_path) is None
        assert bundle_file.read_bytes() == before

    def test_failure_logged_to_active_auto_run_events(self, tmp_path: Path):
        bundle = gesture_bundle()
        cb.save_bundle(bundle, tmp_path)
        ime = _FakeIme(error=RuntimeError("boom: legacy metadata fetch failed"))
        wizard = _make_wizard(tmp_path, ime)
        run = {"phase": "PROMOTE", "events": []}
        wizard._auto["run"] = run
        wizard._save_gesture_coordinate_frame(bundle)  # 不抛
        assert gp.load_coordinate_frame("arm-01", data_root=tmp_path) is None
        events = run["events"]
        assert events, "自动 run 必须留痕"
        assert events[-1]["event"] == "gesture_frame_save_failed"
        assert "boom" in events[-1]["detail"]
        assert events[-1]["ts"]

    def test_bundle_bytes_unchanged_across_sidecar_write(self, tmp_path: Path):
        bundle = gesture_bundle()
        cb.save_bundle(bundle, tmp_path)
        bundle_file = cb.bundle_path("arm-01", tmp_path)
        before = bundle_file.read_bytes()
        # 先成功写旁档，再模拟失败重写：bundle 文件字节始终不变
        wizard = _make_wizard(tmp_path, _FakeIme(metadata=_legacy_metadata()))
        wizard._save_gesture_coordinate_frame(bundle)
        failing = _make_wizard(tmp_path, _FakeIme(error=RuntimeError("boom")))
        failing._save_gesture_coordinate_frame(bundle)
        assert bundle_file.read_bytes() == before
