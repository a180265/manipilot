"""屏幕视觉感知适配（平台中立）：OCR 引擎接口与 Apple Vision 本地引擎。

自 adapters/wechat 抽出（2026-10-12 隔离手术）：平台核心（drivers/runtime/api）
一律从本包取 OCR 能力；adapters/wechat/ocr.py 仅保留兼容 shim，随 WX 适配层发行。
"""
from adapters.vision.ocr import (
    AppleVisionOcrEngine,
    OcrEngine,
    OcrEngineError,
    OcrResult,
    OcrWord,
    find_text_bbox,
    new_request_id,
    normalize_ocr_text,
    ocr_roi_bounds,
)

__all__ = [
    "AppleVisionOcrEngine",
    "OcrEngine",
    "OcrEngineError",
    "OcrResult",
    "OcrWord",
    "find_text_bbox",
    "new_request_id",
    "normalize_ocr_text",
    "ocr_roi_bounds",
]
