"""R2-16（FIX-13）：四态适配的服务端真实匹配。

服务端读取真实图像 + 模板定义，逐态、逐锚点运行 matchTemplate 匹配；
accepted/failed/blocking 全部由匹配结果生成，客户端传入的 passed 事实
一律不参与判定。合成图（含可检测手机/页面特征）必须真实跑过本模块才能
得到 passed，不能直接指定结果。
"""
from __future__ import annotations

import math
from decimal import Decimal, localcontext
from pathlib import Path

import cv2
import numpy as np

# 逐项阈值（契约 §6：passed 要求 >=0.90 且分差 >=0.08）
PASS_SCORE = 0.90
PASS_MARGIN = 0.08

# 最低项（契约 §5 / 主文）：按 item_id 检查存在性（anchor/target/region 合计）
# 列表含 wechat_chats_tab、unread/content；聊天含 back_button/input_box、
# title/input/content；键盘展开页有 keyboard_boundary 锚点；有字页另
# send_button 和 send_button_feature。
MINIMUM_ITEMS = {
    "wechat_list": ("wechat_chats_tab", "unread", "content"),
    "chat_ime_hidden": ("back_button", "input_box", "title", "input",
                        "content"),
    "chat_ime_empty": ("back_button", "input_box", "title", "input",
                       "content", "keyboard_boundary"),
    "chat_ime_text": ("back_button", "input_box", "title", "input",
                      "content", "keyboard_boundary", "send_button",
                      "send_button_feature"),
}


class ImageDecodeError(Exception):
    pass


def decode_image(data: bytes) -> np.ndarray:
    """真实解码 PNG/JPEG 原字节（不接受占位/扩展名伪装）。"""
    try:
        array = np.frombuffer(data, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR) if array.size else None
    except (ValueError, TypeError, cv2.error):
        image = None
    if image is None:
        raise ImageDecodeError("图像字节无法解码为 PNG/JPEG")
    return image


def _pixel_rect(rect: dict, width: int, height: int) -> tuple[int, int, int, int]:
    """完整屏幕归一化框 → 左上 floor、右下 ceil；非法框拒绝，不截断。"""
    if not isinstance(rect, dict):
        raise ValueError("区域必须为归一化矩形")
    values = [rect.get(key) for key in ("x", "y", "width", "height")]
    if any(isinstance(value, bool) or not isinstance(value, (float, int))
           or not math.isfinite(value) for value in values):
        raise ValueError("区域坐标必须为有限数值")
    x, y, w, h = values
    if not (0 <= x < 1 and 0 <= y < 1 and w > 0 and h > 0
            and x + w <= 1 + 1e-9 and y + h <= 1 + 1e-9):
        raise ValueError("区域超出完整屏幕")
    # Ignore floating-point representation noise at exact pixel boundaries.
    left, top = math.floor(x * width + 1e-9), math.floor(y * height + 1e-9)
    right = min(width, math.ceil((x + w) * width - 1e-9))
    bottom = min(height, math.ceil((y + h) * height - 1e-9))
    if right - left < 2 or bottom - top < 2:
        raise ValueError("区域过小")
    return left, top, right, bottom


def match_anchor(frame_bgr: np.ndarray, anchor_image_bgr: np.ndarray,
                 search_region: dict) -> dict:
    """在 search_region 内真实模板匹配，返回候选分数列表（降序、去重叠）。"""
    empty = {"scores": [], "matched_rect": None}
    if not frame_bgr.size or not anchor_image_bgr.size:
        return empty
    frame_h, frame_w = frame_bgr.shape[:2]
    try:
        left, top, right, bottom = _pixel_rect(search_region, frame_w, frame_h)
    except ValueError:
        return empty
    region = frame_bgr[top:bottom, left:right]
    anchor_h, anchor_w = anchor_image_bgr.shape[:2]
    if region.shape[0] < anchor_h or region.shape[1] < anchor_w:
        return empty
    gray = cv2.cvtColor(anchor_image_bgr, cv2.COLOR_BGR2GRAY)
    if float(gray.std()) < 8 or float(gray.mean()) < 20:
        return empty
    result = cv2.matchTemplate(region, anchor_image_bgr,
                               cv2.TM_CCOEFF_NORMED)
    scores: list[float] = []
    matched_rect = None
    work = np.where(np.isfinite(result), result, -1.0)
    for _ in range(4):
        _, max_val, _, max_loc = cv2.minMaxLoc(work)
        if max_val <= 0:
            break
        if not scores:
            patch = region[max_loc[1]:max_loc[1] + anchor_h,
                           max_loc[0]:max_loc[0] + anchor_w]
            patch_gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
            if float(patch_gray.std()) < 8 or float(patch_gray.mean()) < 20:
                return empty
        # Thresholds use the unrounded measurement, not its display rounding.
        scores.append(min(float(max_val), 1.0))
        if matched_rect is None and max_val >= PASS_SCORE:
            matched_rect = {
                "x": (left + max_loc[0]) / frame_w,
                "y": (top + max_loc[1]) / frame_h,
                "width": anchor_w / frame_w, "height": anchor_h / frame_h}
        # 抑制已取候选邻域，找次高（runner-up 必须来自分离位置）
        x0 = max(0, max_loc[0] - anchor_w // 2)
        y0 = max(0, max_loc[1] - anchor_h // 2)
        x1 = min(work.shape[1], max_loc[0] + anchor_w // 2 + 1)
        y1 = min(work.shape[0], max_loc[1] + anchor_h // 2 + 1)
        work[y0:y1, x0:x1] = -1.0
    return {"scores": scores, "matched_rect": matched_rect}


def _anchor_result(anchor: dict, frame_bgr: np.ndarray,
                   anchor_bytes: bytes | None) -> dict:
    entry = {"id": anchor["id"], "status": "failed",
             "candidate_count": 0, "matched_rect": None,
             "best_score": None, "runner_up_score": None,
             "localization_error_bound_px": None, "reason_code": None}
    if anchor_bytes is None:
        entry["reason_code"] = "anchor_missing"
        return entry
    try:
        anchor_image = decode_image(anchor_bytes)
    except ImageDecodeError:
        entry["reason_code"] = "anchor_missing"
        return entry
    # 资产是参考屏幕上的局部图，不是当前屏幕上的固定像素块。
    # 仅按已登记 reference_rect 计算一种尺寸，不扩大搜索区/降低阈值找匹配。
    height, width = frame_bgr.shape[:2]
    reference = anchor.get("reference_rect")
    try:
        x0, y0, x1, y1 = _pixel_rect(reference, width, height)
        target_w, target_h = x1 - x0, y1 - y0
    except (ValueError, TypeError, KeyError):
        entry["reason_code"] = "item_outside_screen"
        return entry
    if target_w < 2 or target_h < 2:
        entry["reason_code"] = "anchor_missing"
        return entry
    if (target_w, target_h) != (anchor_image.shape[1], anchor_image.shape[0]):
        interpolation = (cv2.INTER_AREA if target_w < anchor_image.shape[1]
                         and target_h < anchor_image.shape[0] else cv2.INTER_LINEAR)
        anchor_image = cv2.resize(
            anchor_image, (target_w, target_h), interpolation=interpolation)
    outcome = match_anchor(frame_bgr, anchor_image, anchor.get("search_region"))
    scores = outcome["scores"]
    entry["candidate_count"] = len(scores)
    if not scores:
        entry["reason_code"] = "anchor_missing"
        return entry
    entry["best_score"] = scores[0]
    entry["runner_up_score"] = scores[1] if len(scores) > 1 else None
    if scores[0] < PASS_SCORE or outcome["matched_rect"] is None:
        entry["reason_code"] = "score_below_threshold"
        return entry
    if len(scores) > 1 and scores[0] - scores[1] < PASS_MARGIN:
        entry["reason_code"] = "anchor_ambiguous"
        return entry
    entry["status"] = "passed"
    entry["matched_rect"] = outcome["matched_rect"]
    # Keep the full measurement until the safety inset has been rounded up.
    ref = anchor.get("reference_rect") or {}
    matched = outcome["matched_rect"] or {}
    height, width = frame_bgr.shape[:2]
    dx = (matched.get("x", 0.0) - ref.get("x", 0.0)) * width
    dy = (matched.get("y", 0.0) - ref.get("y", 0.0)) * height
    entry["localization_error_bound_px"] = float(math.hypot(dx, dy))
    return entry


def _item_outside_screen(resolved: dict, width: int, height: int) -> bool:
    del width, height  # 归一化域：以 1.0 为完整屏幕
    return (resolved.get("x", 0.0) < -1e-9
            or resolved.get("y", 0.0) < -1e-9
            or resolved.get("x", 0.0) + resolved.get("width", 0.0) > 1.0 + 1e-9
            or resolved.get("y", 0.0) + resolved.get("height", 0.0) > 1.0 + 1e-9)


def _resolve_items(page: dict, anchor_results: dict[str, dict],
                   frame_size: tuple[int, int]) -> list[dict]:
    """targets/regions 相对最终定位锚点解析；锚点失败则该项失败。"""
    results: list[dict] = []
    width, height = frame_size
    for kind, entries in (("target", page.get("targets") or []),
                          ("region", page.get("regions") or [])):
        for item in entries:
            anchor = anchor_results.get(item.get("anchor_id") or "")
            base = anchor.get("matched_rect") if anchor else None
            entry = {"id": item["id"], "kind": kind, "status": "failed",
                     "resolved_rect": None, "safe_rect": None,
                     "evidence_ids": [], "reason_code": None}
            if anchor is None or anchor["status"] != "passed" or not base:
                entry["reason_code"] = "anchor_missing"
                results.append(entry)
                continue
            rel = item.get("relative_rect") or {}
            resolved = {
                "x": round(base["x"] + float(rel.get("dx", 0.0)), 6),
                "y": round(base["y"] + float(rel.get("dy", 0.0)), 6),
                "width": float(rel.get("width", 0.0)),
                "height": float(rel.get("height", 0.0))}
            if resolved["width"] <= 0 or resolved["height"] <= 0 \
                    or _item_outside_screen(resolved, width, height):
                entry["reason_code"] = "item_outside_screen"
                results.append(entry)
                continue
            entry["status"] = "passed"
            entry["resolved_rect"] = resolved
            results.append(entry)
    return results


def _overlaps(a: dict, b: dict) -> bool:
    return not (a["x"] + a["width"] <= b["x"] + 1e-9
                or b["x"] + b["width"] <= a["x"] + 1e-9
                or a["y"] + a["height"] <= b["y"] + 1e-9
                or b["y"] + b["height"] <= a["y"] + 1e-9)


def sum_error_bounds(*values):
    """Sum canonical numeric values without returning a smaller float bound."""
    try:
        if not values or any(type(value) not in (float, int)
                             or not math.isfinite(value) or value < 0 for value in values):
            return None
    except OverflowError:
        return None
    terms = [Decimal(str(value)) for value in values]
    with localcontext() as context:
        context.prec = max(28, max(t.adjusted() for t in terms)
                           - min(t.as_tuple().exponent for t in terms)
                           + len(str(len(terms))) + 2)
        exact = sum(terms, Decimal(0))
    total = float(exact)
    if not math.isfinite(total):
        return None
    if Decimal(str(total)) < exact:
        total = math.nextafter(total, math.inf)
    return total if math.isfinite(total) else None


def apply_target_safety(check: dict, page: dict, frame_size: tuple[int, int],
                        mapping_error_bound_px: float | None) -> dict:
    """Only measured tap targets receive an inset; never copy a saved safe box."""
    width, height = frame_size
    definitions = {item["id"]: item for item in page.get("targets") or []}
    anchors = {}
    for anchor in check["anchor_results"]:
        anchors.setdefault(anchor["id"], []).append(anchor)

    for item in check["item_results"]:
        item["safe_rect"] = None
        definition = definitions.get(item["id"]) if item["kind"] == "target" else None
        if not definition or definition.get("mode") != "tap" or item["status"] != "passed":
            continue
        dependencies = anchors.get(definition.get("anchor_id")) or []
        anchor = dependencies[0] if len(dependencies) == 1 else {}
        total = sum_error_bounds(mapping_error_bound_px, anchor.get("localization_error_bound_px"))
        reason = None
        if len(dependencies) > 1:
            reason = "anchor_ambiguous"
        elif anchor.get("status") != "passed" or anchor.get("matched_rect") is None:
            reason = "anchor_missing"
        elif total is None:
            reason = "mapping_error_unknown"
        else:
            inset = max(2, math.ceil(total))
            box = item["resolved_rect"]
            safe = {"x": box["x"] + inset / width, "y": box["y"] + inset / height,
                    "width": box["width"] - 2 * inset / width,
                    "height": box["height"] - 2 * inset / height}
            if safe["width"] <= 0 or safe["height"] <= 0:
                reason = "safe_rect_empty"
            else:
                item["safe_rect"] = safe
        if reason:
            item.update(status="failed", reason_code=reason)
            check["status"] = "failed"
            if reason not in check["reason_codes"]:
                check["reason_codes"].append(reason)
    return check


def compute_page_check(page: dict, frame_jpeg: bytes,
                       anchor_bytes: dict[str, bytes],
                       evidence_ids: list[str], *,
                       mapping_error_bound_px: float | None = None) -> dict:
    """逐态逐项真实匹配 → 冻结 page_check 形状。

    page: 模板 page（state/anchors/targets/regions）；frame_jpeg: 本页
    服务端受控帧原字节；anchor_bytes: anchor_id → 资产原字节。
    客户端不提供任何判定输入。
    """
    frame = decode_image(frame_jpeg)
    frame_h, frame_w = frame.shape[:2]
    anchor_results = []
    anchor_by_id: dict[str, dict] = {}
    for anchor in page.get("anchors") or []:
        result = _anchor_result(anchor, frame,
                                anchor_bytes.get(anchor["id"]))
        anchor_results.append(result)
        anchor_by_id[anchor["id"]] = result
    item_results = _resolve_items(page, anchor_by_id, (frame_w, frame_h))
    # title/input/content 互不重叠（同 purpose 组内检查 region 项）
    region_items = {r["id"]: r for r in item_results if r["kind"] == "region"}
    for a_name, b_name in (("title", "input"), ("title", "content"),
                           ("input", "content")):
        a, b = region_items.get(a_name), region_items.get(b_name)
        if a and b and a["status"] == "passed" and b["status"] == "passed" \
                and _overlaps(a["resolved_rect"], b["resolved_rect"]):
            a["status"] = "failed"
            a["reason_code"] = "region_overlap"
            a["resolved_rect"] = None
    # 最低项：缺失即失败项（missing_item），不因同名互相覆盖
    present = {r["id"] for r in anchor_results if r["status"] == "passed"}
    present |= {r["id"] for r in item_results if r["status"] == "passed"}
    reason_codes: list[str] = []
    for item_id in MINIMUM_ITEMS.get(page.get("state"), ()):
        if item_id not in present:
            item_results.append({"id": item_id, "kind": "region",
                                 "status": "failed", "resolved_rect": None,
                                 "safe_rect": None, "evidence_ids": [],
                                 "reason_code": "missing_item"})
            reason_codes.append("missing_item")
    for result in anchor_results:
        if result["status"] == "failed" and result["reason_code"]:
            reason_codes.append(result["reason_code"])
    for result in item_results:
        if result["status"] == "failed" and result["reason_code"]:
            reason_codes.append(result["reason_code"])
    reason_codes = list(dict.fromkeys(reason_codes))
    anchors_passed = all(r["status"] == "passed"
                         for r in anchor_results) if anchor_results else False
    items_passed = all(r["status"] in ("passed",)
                       for r in item_results) if item_results else True
    status = "passed" if anchors_passed and items_passed else "failed"
    # item_results 的证据 = 本页受控两帧（冻结 item_result.evidence_ids 恰 2 项）
    for result in item_results:
        result["evidence_ids"] = list(evidence_ids)
    check = {"state": page.get("state"),
            "status": status,
            "evidence_ids": list(evidence_ids),
            "anchor_results": anchor_results,
            "item_results": item_results,
            "reason_codes": reason_codes}
    return apply_target_safety(check, page, (frame_w, frame_h), mapping_error_bound_px)


def collect_anchor_bytes(page: dict, asset_resolver) -> dict[str, bytes]:
    """anchor_id → 资产原字节（经统一资源解析器读取真实模板资产）。"""
    mapping: dict[str, bytes] = {}
    for anchor in page.get("anchors") or []:
        asset_id = anchor.get("asset_id")
        if not asset_id:
            continue
        data = asset_resolver(asset_id)
        if data:
            mapping[anchor["id"]] = data
    return mapping
