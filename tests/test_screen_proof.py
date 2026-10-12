import asyncio
from datetime import datetime, timezone

import cv2
import numpy as np
import pytest
from pathlib import Path

from drivers import calibration_bundle as cb
from drivers import screen_proof
from drivers.real_camera import RealCameraDriver
from adapters.vision.ocr import OcrEngineError, OcrResult, OcrWord
from tests.test_ui_targets import ready_bundle


def png(image):
    return cv2.imencode(".png", image)[1].tobytes()


def scene():
    image = np.full((720, 1280, 3), 20, np.uint8)
    image[25:695, 80:1201] = 240
    cv2.putText(image, "CHAT", (110, 90), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    return image


def proof_bundle():
    bundle = ready_bundle()
    bundle["screen"]["edgeModel"]["corners"] = [
        [80, 25], [1200, 25], [1200, 694], [80, 694]]
    return bundle


def source_stamp():
    return datetime.now(timezone.utc).isoformat()


class PhoneSource:
    last_frame_seq = 0
    stream_alive = True
    last_camera_id = "cam-01"
    last_device_id = "arm-01"
    last_device_unique_id = "uvc-01"
    last_binding_revision = 7

    def __init__(self, image):
        self.image = image

    def __call__(self):
        self.last_frame_seq += 1
        self.last_received_at = source_stamp()
        self.last_captured_at = self.last_received_at
        return self.image


def box(x1, y1, x2, y2):
    return dict(x1=x1, y1=y1, x2=x2, y2=y2, frameW=1280, frameH=720)


def test_registered_page_survives_driver_restart_and_rejects_changed_binding(tmp_path):
    cb.save_bundle(proof_bundle(), tmp_path)
    image = scene()
    screen_proof.save_reference(
        "arm-01", state="conversation", label="会话界面",
        region=box(100, 50, 220, 110), image=png(image),
        operator="tester", data_root=tmp_path)
    first = RealCameraDriver("cam-01", frame_provider=PhoneSource(image),
                             screen_device_id="arm-01", data_root=tmp_path)
    second = RealCameraDriver("cam-01", frame_provider=PhoneSource(image),
                              screen_device_id="arm-01", data_root=tmp_path)
    for camera in (first, second):
        result = asyncio.run(camera.identify_ui_state())
        assert result["ui_state"] == "conversation"
        assert result["profile_revision"] == 1
    changed = proof_bundle()
    changed["daemonSession"] = "new-session"
    cb.save_bundle(changed, tmp_path)
    assert asyncio.run(second.identify_ui_state())["ui_state"] == "unknown"


def test_identify_waits_on_dark_phone_not_bright_surroundings(tmp_path):
    bundle = proof_bundle()
    bundle["screen"]["edgeModel"]["corners"] = [
        [100, 100], [499, 100], [499, 399], [100, 399]]
    cb.save_bundle(bundle, tmp_path)
    image = np.full((720, 1280, 3), 120, np.uint8)
    image[100:400, 100:500] = 0
    image[100:600, 550:1200] = 230
    camera = RealCameraDriver(
        "cam-01", frame_provider=PhoneSource(image),
        screen_device_id="arm-01", data_root=tmp_path)
    result = asyncio.run(camera.identify_ui_state())
    assert result["ui_state"] == "unknown"
    assert result["note"] == "等待画面恢复，暂不识别页面"


def test_identify_does_not_use_unregistered_whole_frame_reference(tmp_path):
    cb.save_bundle(proof_bundle(), tmp_path)
    image = scene()
    camera = RealCameraDriver(
        "cam-01", frame_provider=PhoneSource(image),
        screen_device_id="arm-01", data_root=tmp_path)
    camera.register_reference("conversation", png(image))
    result = asyncio.run(camera.identify_ui_state())
    assert result["ui_state"] == "unknown"
    assert result["note"] == "尚未登记手机屏幕页面档案"


def test_locate_text_requires_ready_screen_calibration(tmp_path):
    bundle = proof_bundle()
    bundle["status"] = "draft"
    cb.save_bundle(bundle, tmp_path)
    camera = RealCameraDriver(
        "cam-01", frame_provider=PhoneSource(scene()),
        screen_device_id="arm-01", data_root=tmp_path)
    with pytest.raises(OcrEngineError, match="校准尚未就绪"):
        asyncio.run(camera.locate_text("金子", exact=True))


def test_blank_reference_and_other_device_are_not_accepted(tmp_path):
    cb.save_bundle(proof_bundle(), tmp_path)
    with pytest.raises(ValueError, match="特征"):
        screen_proof.save_reference(
            "arm-01", state="conversation", label="会话界面",
            region=box(100, 200, 220, 300), image=png(scene()),
            operator="tester", data_root=tmp_path)
    with pytest.raises(ValueError, match="校准"):
        screen_proof.save_reference(
            "arm-02", state="conversation", label="会话界面",
            region=box(100, 50, 220, 110), image=png(scene()),
            operator="tester", data_root=tmp_path)


def regions():
    return {"title": box(100, 50, 220, 110),
            "input": box(100, 600, 700, 650),
            "content": box(100, 150, 1100, 560),
            "foreground_sample": box(1020, 160, 1040, 180),
            "background_sample": box(110, 160, 130, 180)}


def registered_scene(tmp_path):
    cb.save_bundle(proof_bundle(), tmp_path)
    image = scene()
    image[155:190, 1010:1060] = (80, 210, 100)
    screen_proof.save_reference(
        "arm-01", state="conversation", label="会话界面", region=regions()["title"],
        image=png(image), operator="tester", data_root=tmp_path)
    screen_proof.save_regions("arm-01", regions=regions(), image=png(image),
                              operator="tester", data_root=tmp_path)
    return image


def test_title_ocr_receives_only_registered_title_crop(tmp_path):
    image = registered_scene(tmp_path)

    class Engine:
        def recognize(self, image, *, region):
            frame = screen_proof.decode(image)
            assert frame.shape[:2] == (60, 120)
            return OcrResult(text="金子", words=[OcrWord("金子", 10, 10, 40, 25, .99)],
                             confidence=.99, width=120, height=60,
                             truncated=False, engine="test", engine_version="1")

    camera = RealCameraDriver("cam-01", frame_provider=PhoneSource(image),
                              screen_device_id="arm-01", data_root=tmp_path,
                              ocr_engine=Engine())
    result = asyncio.run(camera.identify_account_and_conversation())
    assert result["conversation_clue"] == "金子"
    assert result["confidence"] == .99
    assert result["ui_state"] == "conversation"


def test_regions_cannot_overlap_input_with_message_evidence(tmp_path):
    image = registered_scene(tmp_path)
    with pytest.raises(ValueError, match="重叠"):
        screen_proof.save_regions("arm-01", regions={**regions(), "content": regions()["input"]},
                                  image=png(image), operator="tester", data_root=tmp_path)


def test_observation_separates_colored_content_from_input_and_checks_status_margin(tmp_path):
    image = registered_scene(tmp_path)
    image[300:365, 650:950] = (80, 210, 100)
    cv2.putText(image, "hello", (675, 343), cv2.FONT_HERSHEY_SIMPLEX, .7, (0, 0, 0), 2)

    class Engine:
        def recognize(self, image, *, region):
            frame = screen_proof.decode(image)
            text = "金子" if region == "conversation" else ("hello" if frame.shape[0] == 65 else "")
            return OcrResult(text=text, words=[], confidence=.99,
                             truncated=False, engine="test", engine_version="1",
                             width=frame.shape[1], height=frame.shape[0])

    camera = RealCameraDriver("cam-01", frame_provider=PhoneSource(image), ocr_engine=Engine(),
                              screen_device_id="arm-01", data_root=tmp_path)
    result = asyncio.run(camera.observe_regions())
    assert result["title"]["text"] == "金子"
    assert result["input_empty"] is True
    outgoing = next(row for row in result["components"] if row["text"] == "hello")
    assert outgoing["left_margin_clear"] is True
    cv2.circle(image, (630, 330), 6, (0, 0, 230), 2)
    result = asyncio.run(camera.observe_regions())
    outgoing = next(row for row in result["components"] if row["text"] == "hello")
    assert outgoing["left_margin_clear"] is False


def test_observation_exposes_full_frame_ocr_for_wx_navigation(tmp_path):
    image = registered_scene(tmp_path)

    class Engine:
        def recognize(self, image, *, region):
            frame = screen_proof.decode(image)
            if region == "conversation":
                return OcrResult(text="金子", words=[],
                                 confidence=.99, truncated=False,
                                 engine="test", engine_version="1",
                                 width=frame.shape[1], height=frame.shape[0])
            if region == "messages" and frame.shape[:2] == (720, 1280):
                words = [
                    OcrWord("微信", 570, 40, 80, 32, .98),
                    OcrWord("微信", 90, 660, 72, 28, .97),
                    OcrWord("通讯录", 360, 660, 96, 28, .97),
                    OcrWord("发现", 700, 660, 64, 28, .97),
                    OcrWord("我", 1120, 660, 32, 28, .97),
                ]
                return OcrResult(text="\n".join(word.text for word in words),
                                 words=words, confidence=.97,
                                 truncated=False, engine="test",
                                 engine_version="1", width=1280, height=720)
            return OcrResult(text="", words=[], confidence=.99,
                             truncated=False, engine="test",
                             engine_version="1",
                             width=frame.shape[1], height=frame.shape[0])

    camera = RealCameraDriver("cam-01", frame_provider=PhoneSource(image),
                              screen_device_id="arm-01", data_root=tmp_path,
                              ocr_engine=Engine())
    result = asyncio.run(camera.observe_regions())
    assert (result["frameW"], result["frameH"]) == (1280, 720)
    assert [box["text"] for box in result["ocr_boxes"]] == [
        "微信", "微信", "通讯录", "发现", "我"
    ]
    assert all(set(("text", "x", "y", "w", "h", "confidence")) <= set(box)
               for box in result["ocr_boxes"])


def test_navigation_ocr_ignores_outside_and_crossing_phone_edge(tmp_path):
    image = registered_scene(tmp_path)

    class Engine:
        def recognize(self, patch, *, region):
            frame = screen_proof.decode(patch)
            words = []
            if region == "messages" and frame.shape[:2] == (720, 1280):
                words = [
                    OcrWord("屏外标题", 5, 5, 65, 25, .99),
                    OcrWord("跨边界", 75, 40, 30, 20, .99),
                    OcrWord("微信", 570, 40, 80, 32, .98),
                ]
            return OcrResult(text="\n".join(word.text for word in words),
                             words=words, confidence=.98, truncated=False,
                             engine="test", engine_version="1",
                             width=frame.shape[1], height=frame.shape[0])

    camera = RealCameraDriver("cam-01", frame_provider=PhoneSource(image),
                              screen_device_id="arm-01", data_root=tmp_path,
                              ocr_engine=Engine())
    result = asyncio.run(camera.observe_regions())
    assert [word["text"] for word in result["ocr_boxes"]] == ["微信"]


def test_shifted_phone_observation_uses_screen_not_surroundings(tmp_path):
    image = registered_scene(tmp_path)
    image[300:365, 650:950] = (80, 210, 100)
    shifted = cv2.warpAffine(
        image, np.float32([[1, 0, 7], [0, 1, 5]]), (1280, 720),
        borderValue=(20, 20, 20))
    cv2.putText(shifted, "OUTSIDE", (100, 710),
                cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1)

    class Engine:
        def recognize(self, image, *, region):
            frame = screen_proof.decode(image)
            if frame.shape[:2] == (720, 1280):
                assert np.all(frame[710, 100] == 127)
                assert np.mean(frame[65:85, 100:200]) < 240
            text = "金子" if region == "conversation" else (
                "hello" if frame.shape[0] == 65 else "")
            return OcrResult(text=text, words=[], confidence=.99,
                             truncated=False, engine="test", engine_version="1",
                             width=frame.shape[1], height=frame.shape[0])

    camera = RealCameraDriver("cam-01", frame_provider=PhoneSource(shifted),
                              screen_device_id="arm-01", data_root=tmp_path,
                              ocr_engine=Engine())
    result = asyncio.run(camera.observe_regions())
    assert result["ui"]["ui_state"] == "conversation"
    assert result["input_empty"] is True
    assert any(item["text"] == "hello" for item in result["components"])


@pytest.mark.parametrize("shift", [20, -25])
def test_observation_rejects_phone_outside_compensation(tmp_path, shift):
    image = registered_scene(tmp_path)
    shifted = cv2.warpAffine(
        image, np.float32([[1, 0, shift], [0, 1, 0]]), (1280, 720),
        borderValue=(20, 20, 20))
    camera = RealCameraDriver("cam-01", frame_provider=PhoneSource(shifted),
                              screen_device_id="arm-01", data_root=tmp_path)
    # 位移 20/25px 超 MAX_COMPENSATION_PX=16 → 必须拒绝。姿态门已并入
    # 四边形定位（require_fittable），观察边界抛"未定位到手机屏幕"
    # （fit_pose 的"无法可靠补偿"不再外泄到该路径）。
    with pytest.raises(ValueError, match="当前画面未定位到手机屏幕"):
        asyncio.run(camera.observe_regions())


def test_locate_text_returns_calibrated_coordinates_after_phone_shift(tmp_path):
    cb.save_bundle(proof_bundle(), tmp_path)
    image = cv2.warpAffine(
        scene(), np.float32([[1, 0, 7], [0, 1, 5]]), (1280, 720),
        borderValue=(20, 20, 20))

    class Engine:
        def recognize(self, image, *, region):
            frame = screen_proof.decode(image)
            assert np.all(frame[710, 100] == 127)
            return OcrResult(text="金子", words=[OcrWord("金子", 100, 100, 40, 25, .99)],
                             confidence=.99, width=1280, height=720,
                             truncated=False, engine="test", engine_version="1")

    camera = RealCameraDriver("cam-01", frame_provider=PhoneSource(image),
                              screen_device_id="arm-01", data_root=tmp_path,
                              ocr_engine=Engine())
    located = asyncio.run(camera.locate_text("金子", exact=True))
    assert located["found"] is True
    assert located["coordinateVersion"] == "calibrated_camera.v1"


def test_locate_text_ignores_words_outside_phone_without_roi(tmp_path):
    cb.save_bundle(proof_bundle(), tmp_path)

    class Engine:
        def recognize(self, image, *, region):
            return OcrResult(
                text="外部\n内部",
                words=[OcrWord("外部", 8, 20, 35, 20, .99),
                       OcrWord("内部", 100, 100, 40, 20, .99)],
                confidence=.99, width=1280, height=720,
                truncated=False, engine="test", engine_version="1")

    camera = RealCameraDriver(
        "cam-01", frame_provider=PhoneSource(scene()),
        screen_device_id="arm-01", data_root=tmp_path, ocr_engine=Engine())
    assert not asyncio.run(camera.locate_text("外部", exact=True))["found"]
    assert asyncio.run(camera.locate_text("内部", exact=True))["found"]


def test_phone_proof_rejects_stale_stream_frame(tmp_path):
    image = registered_scene(tmp_path)

    class StaleSource(PhoneSource):
        def __call__(self):
            self.last_frame_seq += 1
            return self.image

    source = StaleSource(image)
    source.last_received_at = "2020-01-01T00:00:00+00:00"
    camera = RealCameraDriver("cam-01", frame_provider=source,
                              screen_device_id="arm-01", data_root=tmp_path)
    with pytest.raises(ValueError, match="已过期"):
        asyncio.run(camera.observe_regions())


@pytest.mark.parametrize("change", [
    {"last_camera_id": "cam-02"}, {"last_device_id": "arm-02"},
    {"last_device_unique_id": "other-camera"}, {"last_binding_revision": 8},
])
def test_visual_proof_rejects_mismatching_frame_source(tmp_path, change):
    image = registered_scene(tmp_path)

    source = PhoneSource(image)
    for key, value in change.items():
        setattr(source, key, value)
    camera = RealCameraDriver("cam-01", frame_provider=source, screen_device_id="arm-01",
                              data_root=tmp_path)
    with pytest.raises(ValueError, match="来源"):
        asyncio.run(camera.observe_regions())


@pytest.mark.parametrize("name", ["1.jpg", "2.jpg", "3.jpg"])
def test_real_recorded_screenshots_title_is_jinzi_not_status_clock(tmp_path, name):
    path = Path(__file__).resolve().parents[2] / name
    binary = Path(__file__).resolve().parents[1] / "data/bin/vision_ocr"
    if not path.exists() or not binary.exists():
        pytest.skip("本机录制截图或本地 OCR 二进制未提供")
    image = cv2.imread(str(path))
    bundle = proof_bundle()
    bundle["frame"] = {"width": image.shape[1], "height": image.shape[0]}
    cb.save_bundle(bundle, tmp_path)
    region = dict(x1=480, y1=140, x2=610, y2=218, frameW=1080, frameH=2444)
    screen_proof.save_reference("arm-01", state="conversation", label="会话界面",
                                region=region, image=png(image),
                                operator="isolated-fixture", data_root=tmp_path)
    registration = cv2.imread(str(path.parent / "1.jpg"))
    def rectangle(x1, y1, x2, y2):
        return dict(x1=x1, y1=y1, x2=x2, y2=y2, frameW=1080, frameH=2444)
    screen_proof.save_regions(
        "arm-01", regions={"title": region, "input": rectangle(150, 2240, 650, 2280),
                          "content": rectangle(100, 240, 1000, 2180),
                          "foreground_sample": rectangle(300, 260, 320, 275),
                          "background_sample": rectangle(40, 500, 60, 520)},
        image=png(registration), operator="isolated-fixture", data_root=tmp_path)
    camera = RealCameraDriver("cam-01", screen_device_id="arm-01", data_root=tmp_path,
                              frame_provider=lambda: image)
    result = asyncio.run(camera.identify_account_and_conversation())
    assert result["conversation_clue"] is None
    assert result["ui_state"] == "unknown"
