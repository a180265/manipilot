"""daemon 会话重置后的自动恢复决策链（A1+A3，2026-10-01 拍板）。

复位（ESP32 重启）会重置机械坐标系并使校准资格、滑动证书、ui_targets
三件套失效。本模块实现分级复核：

1. 前提：档案存在、几何哈希未变、串口身份（vid/pid/serial）未变；
2. 手机侧：live 帧四边与档案 edgeModel 比对（手机没动）；
3. 臂侧：anchor_verifier 注入验证器（已校准锚点走位+相机核对）——
   无验证器一律转人工（"机械臂移动过"的场景无法从 MPOS 区分，
   盲目套旧参数会运动超限，见 2026-10-01 与操作员的问答）。

全过 → bundle 转正（motion_ready，绑新会话）+ 证书直接恢复（程序不变、
重签新绑定）+ ui_targets rebind；任一步不过 → 返回失败阶段，转人工。
"""
from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, Callable

logger = logging.getLogger(__name__)




def _fail(stage: str, reason: str, **extra) -> dict:
    return {"recovered": False, "stage": stage, "reason": reason, **extra}


def serial_identity_unchanged(expected: dict | None, current: dict | None) -> bool:
    """vid/pid/serial 三元组跨端口稳定（换口自愈的前提，不含 path）。"""
    keys = ("vid", "pid", "serial_number")
    return all((expected or {}).get(k) and (expected or {}).get(k) == (current or {}).get(k)
               for k in keys)


def locate_recovery_screen(frame, corners: list) -> dict:
    """Use measured boundaries in either lighting, within the recovery search bound."""
    from drivers.screen_edge import (ScreenEdgeError, locate_screen_quad,
                                     locate_quad_by_gradient_lines, quad_deviation_px)
    from drivers.screen_pose import fit_pose

    try:
        return locate_screen_quad(frame, corners, tolerance_px=32,
                                  require_fittable=True)
    except ScreenEdgeError:
        # Match the camera quality check's existing bright-desk/dim-display
        # fallback. Recovery keeps its own stricter 32px search bound, including
        # after gradient iteration, and still requires a near-rigid observed fit.
        located = locate_quad_by_gradient_lines(frame, corners, tolerance_px=32)
        if quad_deviation_px(corners, located["corners"]) > 32:
            raise ScreenEdgeError("恢复复核的屏幕四角超出32px定位范围")
        fit_pose(corners, located["corners"], max_movement=32)
        return located


def recovery_anchor(bundle: dict) -> tuple[list[float], list[float]]:
    """Select a measured robot/touch pair; automatic validations omit robot XY."""
    import math

    def pair(value):
        return (isinstance(value, (list, tuple)) and len(value) == 2
                and all(type(v) in (int, float) and math.isfinite(v) for v in value))

    xy = bundle.get("xy") or {}
    for points, key in ((xy.get("validation") or [], "targetScreen"),
                        (xy.get("samples") or [], "actualTouch")):
        candidates = [(p["effectiveRobotXY"], p[key]) for p in points
                      if isinstance(p, dict) and pair(p.get("effectiveRobotXY"))
                      and pair(p.get(key))]
        if candidates:
            screen = bundle.get("screen") or {}
            center = [float(screen.get("screenWidth") or 0) / 2,
                      float(screen.get("screenHeight") or 0) / 2]
            robot, touch = min(candidates, key=lambda p: math.dist(p[1], center))
            return [float(v) for v in robot], [float(v) for v in touch]
    raise RuntimeError("档案缺少已实测的机械坐标与屏幕触点配对，无法臂侧核对")


def observe_recovery_probe(before, after, corners) -> tuple[float, float]:
    """Observe a unique moved round probe, independently of its expected location.

    The unmarked metal head has a dark central bore. Screen content and supports
    must not stand in for it: require a strong circular boundary inside the
    observed phone, the bore, and a changed disk between the two hover frames.
    """
    import cv2
    import numpy as np

    if before is None or after is None or before.shape != after.shape:
        raise RuntimeError("探头复核帧缺失或尺寸不一致")
    gray = cv2.cvtColor(after, cv2.COLOR_BGR2GRAY)
    size = min(gray.shape)
    circles = cv2.HoughCircles(
        cv2.GaussianBlur(gray, (5, 5), 1), cv2.HOUGH_GRADIENT,
        1, size * .07, param1=100, param2=45,
        minRadius=max(8, round(size * .025)), maxRadius=round(size * .07))
    contour = np.asarray(corners, np.float32)
    changed = np.max(cv2.absdiff(before, after), axis=2) > 28
    yy, xx = np.ogrid[:gray.shape[0], :gray.shape[1]]
    candidates = []
    for x, y, radius in ([] if circles is None else circles[0]):
        x, y, radius = float(x), float(y), float(radius)
        if cv2.pointPolygonTest(contour, (x, y), True) < radius:
            continue
        distance = (xx - x) ** 2 + (yy - y) ** 2
        disk = distance < (radius * .95) ** 2
        bore = distance < (radius * .13) ** 2
        ring = (distance > (radius * .3) ** 2) & (distance < (radius * .75) ** 2)
        if (float(np.mean(changed[disk])) >= .65
                and float(np.mean(gray[ring])) - float(np.mean(gray[bore])) >= 25):
            candidates.append((x, y))
    if len(candidates) != 1:
        raise RuntimeError("移动探头无法唯一识别，拒绝恢复旧坐标")
    return candidates[0]


def verify_phone_unchanged(bundle: dict, frame_provider: Callable[[], Any]) -> dict:
    """手机侧复核：live 帧四边与档案 edgeModel 比对（不移动机械臂）。"""
    import cv2
    import numpy as np

    corners = ((bundle.get("screen") or {}).get("edgeModel") or {}).get("corners")
    if not corners:
        return {"ok": False, "reason": "档案缺少屏幕四边基准，无法核对手机位置"}
    try:
        frame = frame_provider()
    except Exception as exc:
        return {"ok": False, "reason": f"取帧失败：{exc}"}
    decoded = frame.get("image") if isinstance(frame, dict) else frame
    if decoded is None:
        return {"ok": False, "reason": "相机帧为空"}
    if isinstance(decoded, (bytes, bytearray)):
        decoded = cv2.imdecode(np.frombuffer(decoded, np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        return {"ok": False, "reason": "相机帧解码失败"}
    try:
        located = locate_recovery_screen(decoded, corners)
    except Exception as exc:
        return {"ok": False, "reason": f"手机四边无法定位（可能已移动）：{exc}"}
    deviation = float(np.max(np.linalg.norm(
        np.asarray(located["corners"], dtype=float)
        - np.asarray(corners, dtype=float), axis=1)))
    return {"ok": True, "deviationPx": round(deviation, 1)}


def attempt_recovery(device_id: str, *, bundle: dict | None, new_session: str,
                     data_root=None, coordinate_frame: dict | None = None,
                     serial_identity: dict | None = None,
                     expected_serial_identity: dict | None = None,
                     phone_frame_provider: Callable[[], Any] | None = None,
                     arm_anchor_verifier: Callable[[str], bool] | None = None,
                     rebind_targets: Callable[[dict], None] | None = None) -> dict:
    """分级复核并自动恢复；返回 {"recovered": bool, "stage", "reason", ...}。

    arm_anchor_verifier(device_id) -> bool：已校准锚点走位+相机核对的注入
    验证器；未提供 = 无法自动判断臂侧位置（转人工，操作员拍板的边界）。
    """
    from drivers import calibration_bundle as cb
    from runtime import automatic_gestures as ag

    if not isinstance(bundle, dict) or not bundle.get("deviceId"):
        return _fail("bundle_missing", "无校准档案，必须重新校准")
    # 几何哈希守卫：与恢复前记录比对；无记录（首次恢复）不阻断，
    # 由证书恢复阶段的指纹比对兜底。
    recorded = bundle.get("recoveryGeometryHash")
    if recorded:
        # 与恢复时写入的键集一致（剔除会话/状态派生字段，见写入点）。
        from drivers.calibration_wizard import AUTO_PROMOTE_GUARD_KEYS
        guard_keys = tuple(k for k in AUTO_PROMOTE_GUARD_KEYS
                           if k not in ("daemonSession", "status"))
        if cb.profile_hash(bundle, keys=guard_keys) != recorded:
            return _fail("geometry_changed", "几何哈希已变化")
    if expected_serial_identity is not None and not serial_identity_unchanged(
            expected_serial_identity, serial_identity):
        return _fail("serial_identity_changed", "串口身份变化（非同适配器重枚举）")
    if phone_frame_provider is not None:
        phone = verify_phone_unchanged(bundle, phone_frame_provider)
        if not phone.get("ok"):
            return _fail("phone_moved", phone.get("reason", "手机位置无法确认"),
                         detail=phone)
    if arm_anchor_verifier is None:
        return _fail("anchor_unverified",
                     "臂侧锚点未复核：无法区分复位发生在停车位还是移动后位置，"
                     "转人工（把臂移回停车位并重新归零，或重新校准）")
    if not arm_anchor_verifier(device_id):
        return _fail("anchor_mismatch",
                     "锚点核对未通过，无法确认旧坐标系有效，请检查探头识别与机械位置，必要时重新校准")
    # ---- 全部通过：转正 + 证书直恢复 + 目标重绑 ----
    restored = deepcopy(bundle)
    restored["status"] = cb.MOTION_READY_STATUS
    restored["daemonSession"] = new_session
    park = restored.get("safePark") or {}
    if park.get("confirmed") and park.get("daemonSession") == bundle.get("daemonSession"):
        park["daemonSession"] = new_session
    # F2（第七轮复审）：写入恢复时几何哈希，激活下次 attempt_recovery 的
    # geometry_changed 守卫（此前无人写入=守卫死代码）。键集剔除会话/
    # 状态派生字段——daemonSession/status 本身就是被恢复的对象。
    from drivers.calibration_wizard import AUTO_PROMOTE_GUARD_KEYS
    guard_keys = tuple(k for k in AUTO_PROMOTE_GUARD_KEYS
                       if k not in ("daemonSession", "status"))
    restored["recoveryGeometryHash"] = cb.profile_hash(restored, keys=guard_keys)
    if serial_identity:
        # 下次 attempt 的期望身份来源（同适配器换口不变，跨适配器变化=拒绝）
        restored["recoverySerialIdentity"] = deepcopy(serial_identity)
    cb.save_bundle(restored, data_root)
    # The Android origin is measured during calibration, not during reconnect.
    # Rebuild its derived binding from that same run's recorded metadata so
    # input clicks do not retain the previous daemon session after recovery.
    metadata = (restored.get("screen") or {}).get("displayCoordinateMetadata")
    if metadata is not None:
        try:
            from drivers.calibration_wizard import build_gesture_coordinate_frame
            from drivers import gesture_program as gp
            phone_session = ((restored.get("autoCalibration") or {}).get("phoneSessionId")
                             or restored.get("imeSessionId"))
            rebuilt = build_gesture_coordinate_frame(
                restored, metadata, ime_device_id=restored.get("imeDeviceId"),
                session_id=phone_session)
            gp.save_coordinate_frame(device_id, rebuilt, data_root)
        except Exception as exc:
            # Missing/mismatched evidence keeps the original frame rejected;
            # it never licenses guessing a display origin or weakening checks.
            logger.warning("设备 %s 恢复后输入法坐标桥未更新：%s", device_id, exc)
    # OCR 审查 P2：向导/ui_targets 清偿不依赖证书成功——certificates_failed
    # 早退也必须已执行，否则 _sync_bundle 会把旧会话写回档案冲掉恢复成果。
    if rebind_targets is not None:
        try:
            rebind_targets(restored)
        except Exception as exc:
            logger.warning("设备 %s ui_targets 重绑失败：%s", device_id, exc)
    cert = None
    try:
        cert = ag.restore_certificates(device_id, restored, data_root=data_root,
                                       coordinate_frame=coordinate_frame)
    except Exception as exc:
        logger.warning("设备 %s 证书恢复失败（资格已转正）：%s", device_id, exc)
        return {"recovered": True, "stage": "certificates_failed",
                "reason": f"资格已转正但证书恢复失败，需手动重发自检：{exc}",
                "certificates": None}
    return {"recovered": True, "stage": "done",
            "certificates": cert.get("version") if isinstance(cert, dict) else None}

