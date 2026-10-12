"""屏幕 OCR 引擎接口与 Apple Vision 本地引擎（原 adapters/wechat/ocr.py，2026-10-12 隔离手术抽出）。

- OCR 必须是可替换接口，具体引擎不得写进任务插件；
- Mac mini 默认 Apple Vision `VNRecognizeTextRequest` 本地中文 OCR；
- Apple Vision 进程异常、超时或输出不是合法 JSON 时抛 OcrEngineError，
  绝不把空文本当作"没有新消息"。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

# 实施文档 5.2.1 登记的区域
# ``input`` is used by calibration acceptance to locate the text the IME just
# rendered.  It intentionally uses the same full-frame Vision pipeline; the
# caller supplies the semantic target string and converts the returned bbox.
OCR_REGIONS = ("account", "conversation", "messages", "input")


class OcrEngineError(Exception):
    """结构化 OCR 失败（引擎异常/超时/输出非法），reason 固定为 ocr_engine_error。"""

    def __init__(self, message: str, *, reason: str = "ocr_engine_error"):
        super().__init__(message)
        self.reason = reason


@dataclass
class OcrWord:
    text: str
    x: float
    y: float
    width: float
    height: float
    confidence: float


@dataclass
class OcrResult:
    text: str
    words: list[OcrWord]
    confidence: float
    truncated: bool
    engine: str
    engine_version: str
    region: str = ""
    request_id: str = ""
    elapsed_ms: int = 0
    width: int = 0
    height: int = 0

    def to_dict(self) -> dict:
        return {
            "region": self.region,
            "text": self.text,
            "words": [
                {
                    "text": w.text, "x": round(w.x, 2), "y": round(w.y, 2),
                    "width": round(w.width, 2), "height": round(w.height, 2),
                    "confidence": round(w.confidence, 4),
                }
                for w in self.words
            ],
            "confidence": round(self.confidence, 4),
            "truncated": self.truncated,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "request_id": self.request_id,
            "elapsed_ms": self.elapsed_ms,
            "width": self.width,
            "height": self.height,
        }


def normalize_ocr_text(value: str) -> str:
    """用于目标匹配的轻量规范化：忽略空白与大小写，不改业务正文。"""
    import re
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def ocr_roi_bounds(roi: dict, *, frame_width: int, frame_height: int) -> tuple[float, ...]:
    """ROI 必须属于当前全帧坐标系，不缩放或猜测过期区域。"""
    try:
        values = [roi[key] for key in ("x1", "y1", "x2", "y2", "frameW", "frameH")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(value) for value in values):
            raise ValueError()
        x1, y1, x2, y2, width, height = values
        if (width != frame_width or height != frame_height
                or not 0 <= x1 < x2 <= width or not 0 <= y1 < y2 <= height):
            raise ValueError()
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise OcrEngineError("OCR 区域无效或与当前画面尺寸不一致",
                             reason="ocr_roi_invalid") from exc
    return float(x1), float(y1), float(x2), float(y2)


def find_text_bbox(result: "OcrResult", expected_text: str, *,
                   min_confidence: float = 0.75, exact: bool = False) -> dict | None:
    """从 Vision 词级结果中定位目标文字，兼容中文短语被拆成多个词。

    返回目标词组的联合框及最低词置信度；找不到或任一词低于阈值时返回 None。
    坐标保持 OCR 引擎的全帧像素坐标（左上原点）。
    """
    wanted = normalize_ocr_text(expected_text)
    if not wanted:
        return None
    words = [word for word in result.words
             if float(word.confidence) >= float(min_confidence)
             and normalize_ocr_text(word.text)]
    if not words:
        return None

    candidates: list[list[OcrWord]] = []
    for word in words:
        normalized = normalize_ocr_text(word.text)
        if wanted == normalized or (not exact and wanted in normalized):
            candidates.append([word])

    # Vision 可能把中文短语拆成多个同一行词，按横向相邻词滑窗拼接。
    rows: list[list[OcrWord]] = []
    for word in sorted(words, key=lambda item: (item.y + item.height / 2, item.x)):
        cy = word.y + word.height / 2
        row = next((item for item in rows
                    if abs(cy - (item[0].y + item[0].height / 2))
                    <= max(word.height, item[0].height) * 0.8), None)
        if row is None:
            rows.append([word])
        else:
            row.append(word)
    for row in rows:
        row.sort(key=lambda item: item.x)
        for start in range(len(row)):
            joined = ""
            for end in range(start, len(row)):
                current = row[end]
                if end > start:
                    previous = row[end - 1]
                    gap = float(current.x) - float(previous.x + previous.width)
                    max_gap = max(float(previous.height), float(current.height)) * 2.0
                    if gap > max_gap:
                        break
                joined += normalize_ocr_text(current.text)
                if wanted == joined or (not exact and wanted in joined):
                    candidates.append(row[start:end + 1])
                    break
                if len(joined) >= len(wanted):
                    break

    if not candidates:
        return None
    if exact:
        locations = {
            (min(item.x for item in group), min(item.y for item in group),
             max(item.x + item.width for item in group),
             max(item.y + item.height for item in group))
            for group in candidates
        }
        if len(locations) != 1:
            raise OcrEngineError("目标文字对应多个位置，禁止猜测点击目标",
                                 reason="ocr_target_ambiguous")
    chosen = max(candidates, key=lambda group: min(float(item.confidence) for item in group))
    x1 = min(float(item.x) for item in chosen)
    y1 = min(float(item.y) for item in chosen)
    x2 = max(float(item.x + item.width) for item in chosen)
    y2 = max(float(item.y + item.height) for item in chosen)
    return {
        "text": expected_text,
        "cameraX": (x1 + x2) / 2,
        "cameraY": (y1 + y2) / 2,
        "bbox": [x1, y1, x2, y2],
        "confidence": min(float(item.confidence) for item in chosen),
    }


class OcrEngine(Protocol):
    def recognize(self, image_bytes: bytes, *, region: str, request_id: str = "") -> OcrResult: ...


def new_request_id(prefix: str = "ocr") -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class AppleVisionOcrEngine:
    """通过 vision_ocr.swift 编译产物调用 Apple Vision（macOS 本地执行）。

    首次调用时用 swiftc 把 vision_ocr.swift 编译为常驻二进制（按源码哈希缓存），
    之后每次识别只起一次子进程（实测 <0.3s），不重复编译。
    """

    ENGINE_NAME = "Apple Vision"

    def __init__(
        self,
        *,
        swift_source: Path | None = None,
        binary_path: Path | None = None,
        timeout_seconds: float = 10.0,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ):
        package_dir = Path(__file__).resolve().parent
        self.swift_source = swift_source or package_dir / "vision_ocr.swift"
        data_dir = package_dir.parent.parent / "data" / "bin"
        env_bin = os.environ.get("ARM_FLEET_VISION_OCR_BIN")
        self.binary_path = Path(env_bin) if env_bin else (binary_path or data_dir / "vision_ocr")
        self.timeout_seconds = timeout_seconds
        self._runner = runner or subprocess.run
        self._binary_ready = False

    # ---------- 编译 ----------
    def _source_hash(self) -> str:
        return hashlib.sha256(self.swift_source.read_bytes()).hexdigest()[:16]

    def ensure_binary(self) -> Path:
        """按源码哈希缓存编译产物；swiftc 缺失或编译失败都是结构化错误。"""
        if self._binary_ready and self.binary_path.exists():
            return self.binary_path
        compiler = shutil.which("swiftc")
        if compiler is None:
            raise OcrEngineError("本机缺少 swiftc，无法编译 Apple Vision OCR 辅助程序")
        stamp = self.binary_path.with_suffix(self.binary_path.suffix + f".{self._source_hash()}")
        if stamp.exists() and self.binary_path.exists():
            self._binary_ready = True
            return self.binary_path
        self.binary_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_target = self.binary_path.with_suffix(".building")
        result = self._runner(
            [compiler, "-O", str(self.swift_source), "-o", str(tmp_target)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            raise OcrEngineError(f"Apple Vision OCR 辅助程序编译失败：{result.stderr[-400:]}")
        tmp_target.replace(self.binary_path)
        stamp.write_text(self._source_hash())
        self._binary_ready = True
        return self.binary_path

    # ---------- 识别 ----------
    def recognize(self, image_bytes: bytes, *, region: str, request_id: str = "") -> OcrResult:
        if region not in OCR_REGIONS:
            raise OcrEngineError(f"未登记的 OCR 区域：{region}")
        binary = self.ensure_binary()
        request_id = request_id or new_request_id()
        try:
            result = self._runner(
                [str(binary), region, request_id],
                input=image_bytes, capture_output=True, timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrEngineError(f"Apple Vision OCR 超时（>{self.timeout_seconds:.0f}s，region={region}）") from exc
        except OSError as exc:
            raise OcrEngineError(f"Apple Vision OCR 进程无法启动：{exc}") from exc
        stdout = (result.stdout or b"").decode("utf-8", errors="replace").strip()
        if result.returncode != 0:
            raise OcrEngineError(f"Apple Vision OCR 进程失败（exit={result.returncode}）：{stdout[-300:]}")
        return self._parse(stdout, region=region, request_id=request_id)

    def _parse(self, stdout: str, *, region: str, request_id: str) -> OcrResult:
        try:
            payload = json.loads(stdout.splitlines()[-1])
        except (json.JSONDecodeError, IndexError) as exc:
            raise OcrEngineError(f"Apple Vision OCR 输出不是合法 JSON：{stdout[-200:]}") from exc
        if not payload.get("ok"):
            raise OcrEngineError(f"Apple Vision OCR 失败：{payload.get('error', '未知错误')}")
        words = [
            OcrWord(
                text=str(item.get("text", "")),
                x=float(item.get("x", 0.0)),
                y=float(item.get("y", 0.0)),
                width=float(item.get("width", 0.0)),
                height=float(item.get("height", 0.0)),
                confidence=float(item.get("confidence", 0.0)),
            )
            for item in payload.get("words", [])
        ]
        confidence = float(payload.get("confidence", 0.0))
        if words:
            confidence = min(confidence, min(w.confidence for w in words))
        return OcrResult(
            text=str(payload.get("text", "")),
            words=words,
            confidence=confidence,
            truncated=bool(payload.get("truncated", False)),
            engine=str(payload.get("engine", self.ENGINE_NAME)),
            engine_version=str(payload.get("engine_version", "")),
            region=str(payload.get("region", region)),
            request_id=str(payload.get("request_id", request_id)),
            elapsed_ms=int(payload.get("elapsed_ms", 0)),
            width=int(payload.get("width", 0) or 0),
            height=int(payload.get("height", 0) or 0),
        )
