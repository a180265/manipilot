"""本台生效粗框（phone-search-region.v1）上下文：S1/S2 正式入口共用。

执行单《2026-10-08-启用前剩余修复执行与验收》§3 统一几何规则：

- 模式判别（主规格 §11.10.2；不能用"适配为空"区分）：
  * 设备在 layout_region_versions 无任何版本 → 未安装粗框包的旧设备
    （legacy 只读：调用方保留原全图路径，兼容规则不扩大）；
  * 有任何版本 → 新模式：必须存在生效指针且其指向记录可用；
    缺指针/指针损坏/记录摘要不符/来源绑定与当前校准不一致 → 明确拒绝
    （RegionContextError），不回全图、不回旧档、不自动扩框。
- 定位共用 layout_geometry.locate_in_region 的裁剪语义：先按粗框裁剪 →
  框内定位（多候选拒绝）→ 四角加回原图偏移 → 触及裁剪边拒绝；自由检测
  失败的梯度回退同样只在裁剪图内进行，不越框。
- 每个异步等待后的关键出口由调用方重新 load 并比对 pointer（revision/
  selected_version/selected_sha256）——上下文失效按各自契约拒绝。
"""
from __future__ import annotations

import cv2
import numpy as np

from runtime.layout_region import _screen_candidates, canonical_digest
from runtime.layout_matching import _pixel_rect


class RegionContextError(Exception):
    """本台粗框上下文不可用或已变化（调用方按各自契约拒绝，不回全图）。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def load_region_context(region_store, device_id: str) -> dict | None:
    """返回 None=legacy（未安装粗框包）；region 上下文 dict=新模式生效粗框。

    新模式缺生效指针或资料损坏抛 RegionContextError（terminal，不回退）。
    """
    if region_store is None:
        return None
    try:
        has_versions = region_store.latest_version(device_id)[0] > 0
    except Exception as exc:  # store 故障按不可用拒绝，不当 legacy
        raise RegionContextError("region_context_unavailable",
                                 f"粗框资料读取失败：{exc}") from exc
    if not has_versions:
        return None
    pointer = region_store.get_pointer(device_id) or {}
    version = pointer.get("selected_version")
    if not (type(version) is int and version > 0 and pointer.get("selected_sha256")
            and type(pointer.get("revision")) is int and pointer["revision"] > 0):
        raise RegionContextError(
            "region_context_invalid",
            "本设备已安装粗框包但缺少生效搜索范围，禁止回退全图路径")
    record = region_store.get_version(device_id, version)
    if (not isinstance(record, dict) or record.get("sha256") != pointer.get("selected_sha256")
            or record.get("device_id") != device_id or record.get("version") != version):
        raise RegionContextError(
            "region_context_invalid", "生效粗框记录缺失或摘要不符，请重新核验")
    try:
        digest = canonical_digest({k: v for k, v in record.items() if k != "sha256"})
    except (ValueError, TypeError) as exc:
        raise RegionContextError(
            "region_context_invalid", "生效粗框内容无效，请重新核验") from exc
    if digest != record["sha256"]:
        raise RegionContextError(
            "region_context_invalid", "生效粗框内容与摘要不符，请重新核验")
    rect = record.get("rect")
    if (not isinstance(rect, dict) or not record.get("pixel_rect")
            or not record.get("source_size")):
        raise RegionContextError("region_context_invalid", "生效粗框字段不完整")
    return {"mode": "region", "device_id": device_id,
            "pointer": {key: pointer[key] for key in
                        ("revision", "selected_version", "selected_sha256")},
            "region": record}


def context_pointer(ctx: dict | None) -> dict | None:
    """当前上下文的指针快照（legacy=None）；调用方在异步出口后比对。"""
    return dict(ctx["pointer"]) if ctx and ctx.get("mode") == "region" else None


def require_unchanged(ctx: dict | None, region_store, device_id: str) -> dict:
    """关键出口重载并比对指针；变化即抛 RegionContextError（不静默换框）。"""
    current = load_region_context(region_store, device_id)
    if context_pointer(current) != context_pointer(ctx):
        raise RegionContextError("region_context_changed",
                                 "粗框在操作过程中已变化，原上下文失效")
    if (ctx is None) != (current is None):
        raise RegionContextError("region_context_changed",
                                 "粗框安装状态在操作过程中已变化")
    return current if current is not None else ctx


def context_matches_bundle(ctx: dict, bundle: dict) -> None:
    """粗框来源绑定与当前校准档案一致（尺寸/来源几何/相机安装身份）。"""
    if not ctx or ctx.get("mode") != "region":
        return
    region = ctx["region"]
    frame = bundle.get("frame") or {}
    source_size = region.get("source_size") or {}
    if (frame.get("width"), frame.get("height")) != (
            source_size.get("width"), source_size.get("height")):
        raise RegionContextError(
            "region_context_invalid", "粗框来源尺寸与当前相机分辨率不一致")
    saved_geometry = region.get("source_geometry_id")
    if (not isinstance(saved_geometry, str) or not saved_geometry.strip()
            or saved_geometry != bundle.get("sourceGeometryId")):
        raise RegionContextError(
            "region_context_invalid", "粗框来源几何与当前校准不一致，请重新框选")
    binding = region.get("installation_binding") or {}
    for saved_key, bundle_key in (
            ("camera_id", "cameraId"),
            ("camera_device_unique_id", "cameraDeviceUniqueId"),
            ("camera_binding_id", "cameraBindingId")):
        saved = binding.get(saved_key)
        if (not isinstance(saved, str) or not saved.strip()
                or saved != bundle.get(bundle_key)):
            raise RegionContextError(
                "region_context_invalid",
                f"粗框安装绑定的 {saved_key} 与当前校准不一致，请重新核验")


def locate_quad_in_region(image, region: dict, saved_corners,
                          *, require_unique: bool = True) -> dict:
    """粗框内定位手机屏幕（业主 2026-10-16 定案：框=搜索范围，仅此而已）。

    框内直接做双极性屏幕检测，跨极性同屏去重后取最大面积者；
    无期望角锚定、无梯度回退、无候选数否决。
    屏幕被框裁断时拒绝（框画得太紧的唯一保护）。
    """
    from drivers.screen_edge import ScreenEdgeError, detect_screen_quad

    height, width = image.shape[:2]
    try:
        left, top, right, bottom = _pixel_rect(region["rect"], width, height)
    except (KeyError, TypeError, ValueError) as exc:
        raise RegionContextError("region_context_invalid", "当前生效粗框无效") from exc
    crop = image[top:bottom, left:right]
    if crop.shape[0] < 40 or crop.shape[1] < 40:
        raise RegionContextError("region_context_invalid", "生效粗框过小，无法定位")
    found = []
    for image_p in (crop, cv2.bitwise_not(crop)):
        try:
            detected = detect_screen_quad(image_p)
        except ScreenEdgeError:
            continue
        cand = np.asarray(detected["corners"], dtype=float)
        dup = False
        for other in found:
            center_offset = float(np.hypot(
                cand[:, 0].mean() - other[:, 0].mean(),
                cand[:, 1].mean() - other[:, 1].mean()))
            area_c = float((cand[:, 0].max() - cand[:, 0].min())
                           * (cand[:, 1].max() - cand[:, 1].min()))
            area_o = float((other[:, 0].max() - other[:, 0].min())
                           * (other[:, 1].max() - other[:, 1].min()))
            span = max(float(cand[:, 0].max() - cand[:, 0].min()),
                       float(cand[:, 1].max() - cand[:, 1].min()),
                       float(other[:, 0].max() - other[:, 0].min()),
                       float(other[:, 1].max() - other[:, 1].min()), 1.0)
            if (center_offset <= 0.25 * span
                    and min(area_c, area_o) / max(area_o, area_c, 1.0) >= 0.36):
                dup = True
                break
        if not dup:
            found.append(cand)
    if not found:
        raise RegionContextError(
            "screen_not_located", "粗框内无法定位手机屏幕；请确认手机亮屏停在框内")
    # 框=搜索范围：取框内最大面积者（画框人保证框住的是目标屏）
    quad = max(found, key=lambda q: float(
        (q[:, 0].max() - q[:, 0].min()) * (q[:, 1].max() - q[:, 1].min())))
    corners = quad + [left, top]
    if (np.any(corners <= [left + 1, top + 1])
            or np.any(corners >= [right - 1, bottom - 1])):
        raise RegionContextError(
            "screen_outside_region", "当前屏幕被粗框裁断，请重新核验搜索范围")
    return {"corners": corners.tolist(), "frameW": width, "frameH": height}


def crop_frame_with_corners(image_gray, region: dict, saved_corners):
    """质量前置共用：按粗框裁剪灰度图并把校准预测四角平移进裁剪坐标。

    返回 (crop, shifted_corners, (left, top, right, bottom))。
    """
    from drivers import screen_pose

    height, width = image_gray.shape[:2]
    try:
        left, top, right, bottom = _pixel_rect(region["rect"], width, height)
    except (KeyError, TypeError, ValueError) as exc:
        raise RegionContextError("region_context_invalid", "当前生效粗框无效") from exc
    saved = screen_pose.validate_corners(saved_corners)
    shifted = saved - [left, top]
    return image_gray[top:bottom, left:right], shifted, (left, top, right, bottom)


def quad_in_crop_bounds(corners, crop_shape) -> bool:
    """检测四角未触及裁剪边（粗框必须完整包住屏幕）。"""
    h, w = crop_shape[:2]
    quad = np.asarray(corners, dtype=float)
    return not (np.any(quad <= [1.0, 1.0]) or np.any(quad >= [w - 2.0, h - 2.0]))


def decode_bgr(image_bytes: bytes):
    decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        raise RegionContextError("frame_undecodable", "画面无法解码")
    return decoded
