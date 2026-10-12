"""OCR 引擎接口测试：Apple Vision 辅助程序解析、失败语义与真机识别（实施文档 5.2 / 5.2.1）。"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import cv2
import numpy as np
import pytest

from adapters.vision.ocr import (
    AppleVisionOcrEngine,
    OcrEngineError,
    OcrResult,
    OcrWord,
    find_text_bbox,
)


def _scripted_runner(stdout: bytes, returncode: int = 0):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0] if args else [], returncode, stdout=stdout, stderr=b"")
    return run


def _stub_engine(tmp_path, stdout: bytes, returncode: int = 0) -> AppleVisionOcrEngine:
    """注入假 runner 并跳过真实编译（只测解析与失败语义）。"""
    engine = AppleVisionOcrEngine(
        binary_path=tmp_path / "vision_ocr",
        runner=_scripted_runner(stdout, returncode),
    )
    (tmp_path / "vision_ocr").write_bytes(b"stub")
    engine._binary_ready = True
    return engine


def test_region_guard(tmp_path):
    engine = _stub_engine(tmp_path, b"{}")
    with pytest.raises(OcrEngineError):
        engine.recognize(b"png", region="home")


def test_parse_words_pixel_passthrough_and_min_confidence(tmp_path):
    """Swift 辅助程序已把 Vision 归一化坐标（左下原点）转换为像素（左上原点），
    Python 侧只做直通、置信度取整体最低分和结构校验。"""
    payload = {
        "ok": True,
        "region": "messages",
        "request_id": "ocr-1",
        "text": "你好",
        "words": [{"text": "你好", "x": 100, "y": 65, "width": 20, "height": 10, "confidence": 0.9}],
        "confidence": 0.96,
        "truncated": False,
        "engine": "Apple Vision",
        "engine_version": "macOS 26.0",
        "elapsed_ms": 120,
        "width": 200,
        "height": 100,
    }
    engine = _stub_engine(tmp_path, json.dumps(payload).encode())
    result = engine.recognize(b"png", region="messages", request_id="ocr-1")
    assert (result.words[0].x, result.words[0].y) == (100.0, 65.0)
    assert (result.words[0].width, result.words[0].height) == (20.0, 10.0)
    # 置信度取整体最低分
    assert result.confidence == 0.9
    assert result.engine == "Apple Vision"
    assert result.region == "messages" and result.request_id == "ocr-1"


def test_find_text_bbox_joins_adjacent_chinese_words():
    result = OcrResult(
        text="统一 校准 测试",
        words=[
            OcrWord("统一", 120, 180, 55, 24, 0.97),
            OcrWord("校准", 180, 180, 55, 24, 0.96),
            OcrWord("测试", 240, 180, 55, 24, 0.95),
        ],
        confidence=0.95, truncated=False, engine="test", engine_version="1",
        width=640, height=480,
    )
    located = find_text_bbox(result, "统一校准测试", min_confidence=0.9)
    assert located is not None
    assert located["bbox"] == [120.0, 180.0, 295.0, 204.0]
    assert located["cameraX"] == 207.5
    assert located["cameraY"] == 192.0
    assert located["confidence"] == 0.95


def test_find_text_bbox_rejects_low_confidence_phrase():
    result = OcrResult(
        text="发 送",
        words=[
            OcrWord("发", 100, 100, 20, 20, 0.96),
            OcrWord("送", 122, 100, 20, 20, 0.60),
        ],
        confidence=0.60, truncated=False, engine="test", engine_version="1",
        width=320, height=240,
    )
    assert find_text_bbox(result, "发送", min_confidence=0.75) is None


def test_invalid_json_is_structured_failure(tmp_path):
    engine = _stub_engine(tmp_path, "这不是 JSON".encode("utf-8"))
    with pytest.raises(OcrEngineError) as excinfo:
        engine.recognize(b"png", region="messages")
    assert excinfo.value.reason == "ocr_engine_error"


def test_nonzero_exit_is_structured_failure(tmp_path):
    engine = _stub_engine(tmp_path, b'{"ok": false, "error": "decode failed"}', returncode=2)
    with pytest.raises(OcrEngineError):
        engine.recognize(b"png", region="account")


def test_timeout_is_structured_failure(tmp_path):
    def slow_runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="vision_ocr", timeout=10)

    engine = AppleVisionOcrEngine(binary_path=tmp_path / "vision_ocr", runner=slow_runner)
    (tmp_path / "vision_ocr").write_bytes(b"stub")
    engine._binary_ready = True
    with pytest.raises(OcrEngineError) as excinfo:
        engine.recognize(b"png", region="conversation")
    assert "超时" in str(excinfo.value)


def test_missing_swiftc_is_structured_failure(tmp_path):
    engine = AppleVisionOcrEngine(binary_path=tmp_path / "vision_ocr")
    engine._binary_ready = False
    original = shutil.which
    shutil.which = lambda name: None if name == "swiftc" else original(name)
    try:
        with pytest.raises(OcrEngineError) as excinfo:
            engine.ensure_binary()
    finally:
        shutil.which = original
    assert "swiftc" in str(excinfo.value)


@pytest.mark.skipif(shutil.which("swiftc") is None, reason="本机缺少 swiftc，无法编译 Apple Vision 辅助程序")
def test_apple_vision_real_recognition(tmp_path_factory):
    """真机 OCR 验证：本机 Vision 框架识别合成文本图。"""
    binary = tmp_path_factory.mktemp("vision_bin") / "vision_ocr"
    engine = AppleVisionOcrEngine(binary_path=binary)

    image = np.full((240, 640, 3), 255, dtype=np.uint8)
    cv2.putText(image, "Hello Price 128", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2, cv2.LINE_AA)
    cv2.putText(image, "TOTAL 4567", (30, 180), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 0), 2, cv2.LINE_AA)
    ok, png = cv2.imencode(".png", image)
    assert ok

    result = engine.recognize(png.tobytes(), region="messages", request_id="ocr-real-1")
    text_upper = result.text.upper()
    assert "PRICE 128" in text_upper
    assert "4567" in result.text
    assert result.words, "必须返回词级坐标"
    for word in result.words:
        assert 0 <= word.x <= 640 and 0 <= word.y <= 240
        assert word.confidence > 0
    assert result.engine == "Apple Vision"
    assert result.confidence > 0
    assert engine._binary_ready  # 二进制已缓存，后续调用不再编译
