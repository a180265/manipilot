"""设备级目标档案、标定与安全参数的读取层（daemon 驱动桥的唯一事实源）。

目录布局（统一校准实施方案 §10/§11；daemon 文档中的旧文件仅作兼容输入）：

    data/devices/{device_id}/calibration.json   摄像头→机械标定（调试台产出）
    data/devices/{device_id}/targets.json       旧固定目标输入（显式迁移用）
    data/devices/{device_id}/arm_profile.json   悬停/点击默认参数

规则：
- calibration_bundle.json 是自动运行时唯一目标/校准事实源；targets.json 只允许显式迁移读取。
- 目标只存画面归一化坐标（nx, ny）与 Z 参数；机械坐标每次使用按当前标定现算。
- 目标有效 = 标定存在、已拟合、且标定记录的会话锚点 == daemon 当前会话。
- 旧 data/motor_debug_calibration.json、data/screen_targets.json 已废弃，
  本模块不做迁移、不并列读取。
- 本模块只读标定/档案事实；写入只提供"调试台登记目标"这一个入口（含校验），
  业务运行时不得写入。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from drivers.daemon_protocol import (
    DEFAULT_TAP_FEED,
    DEFAULT_TARGET_MODES,
    DEFAULT_XY_FEED,
    TARGET_DISPLAY_NAMES,
    TARGET_MODE_TAP,
    check_feed,
)

DEFAULT_HOVER_Z = 2.0
DEFAULT_TAP_DEPTH = 3.0


def device_dir(device_id: str, data_root: Path | None = None) -> Path:
    root = Path(data_root) if data_root else Path(__file__).resolve().parents[1] / "data"
    return root / "devices" / device_id


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_calibration(device_id: str, data_root: Path | None = None) -> dict | None:
    """读取设备标定；缺失或字段不完整返回 None。"""
    data = _read_json(device_dir(device_id, data_root) / "calibration.json")
    if not data:
        return None
    affine = data.get("affine") or {}
    if not all(k in affine for k in ("a", "b", "c", "d", "e", "f")):
        return None
    if not data.get("session") or not data.get("frameW") or not data.get("frameH"):
        return None
    return data


def load_runtime_calibration(device_id: str, data_root: Path | None = None) -> dict | None:
    """读取业务运行时唯一校准事实源。

    新统一向导产出 ``calibration_bundle.json``；旧 ``calibration.json``
    仅作为已部署设备的兼容回退，避免两套档案同时决定新设备行为。
    """
    from drivers import calibration_bundle

    bundle = calibration_bundle.load_bundle(device_id, data_root)
    if calibration_bundle.bundle_path(device_id, data_root).exists():
        # A present but malformed bundle is a hard failure, not permission to
        # fall back to stale legacy calibration data.
        return bundle
    if bundle is not None:
        return bundle
    return load_calibration(device_id, data_root)


def load_legacy_targets(device_id: str, data_root: Path | None = None) -> dict:
    """Read the old targets.json only for an explicit migration/import action."""
    return _read_json(device_dir(device_id, data_root) / "targets.json") or {}


def load_targets(device_id: str, data_root: Path | None = None) -> dict:
    """Read runtime targets from calibration_bundle.json only.

    A bundle present with an empty target map intentionally returns an empty map;
    silently falling back to targets.json would allow stale coordinates to drive
    business motion after a rebind or recalibration.
    """
    try:
        from drivers import calibration_bundle
        bundle = calibration_bundle.load_bundle(device_id, data_root)
        bundle_present = calibration_bundle.bundle_path(device_id, data_root).exists()
        if bundle is not None:
            return dict(bundle.get("targets") or {})
        if bundle_present:
            return {}
    except Exception:
        # If the bundle path exists but cannot be decoded, do not use stale
        # legacy coordinates for automatic motion.
        try:
            if calibration_bundle.bundle_path(device_id, data_root).exists():
                return {}
        except Exception:
            return {}
    # No bundle exists: retain legacy deployed-device compatibility. This is
    # intentionally not used once a bundle has been created.
    return load_legacy_targets(device_id, data_root)


def load_profile(device_id: str, data_root: Path | None = None) -> dict:
    data = {}
    bundle_present = False
    try:
        from drivers import calibration_bundle
        bundle_present = calibration_bundle.bundle_path(device_id, data_root).exists()
        bundle = calibration_bundle.load_bundle(device_id, data_root)
        if bundle is not None:
            # Once the unified bundle exists, stale arm_profile.json must not
            # override its calibrated Z parameters.
            z = bundle.get("z") or {}
            data = {key: z.get(key) for key in ("hoverZ", "tapDepth", "tapFeed")}
            # Z 经 G92 重锚定后安全悬停可能是负值；promote 时 hoverZ=zSafe，
            # 但 draft 态（自动流程已确认 zSafe、尚未提升）hoverZ 可能为空——
            # 必须回退读 zSafe，绝不静默落到 DEFAULT_HOVER_Z=2.0
            # （2026-09-04 真机"先下压 7mm 再走位"事故的同类成因，审计 R2/R3）。
            if data.get("hoverZ") is None and z.get("zSafe") is not None:
                data["hoverZ"] = z.get("zSafe")
        elif bundle_present:
            return {"hoverZ": DEFAULT_HOVER_Z, "tapDepth": DEFAULT_TAP_DEPTH,
                    "tapFeed": DEFAULT_TAP_FEED}
    except Exception:
        try:
            from drivers import calibration_bundle
            if calibration_bundle.bundle_path(device_id, data_root).exists():
                return {"hoverZ": DEFAULT_HOVER_Z, "tapDepth": DEFAULT_TAP_DEPTH,
                        "tapFeed": DEFAULT_TAP_FEED}
        except Exception:
            return {"hoverZ": DEFAULT_HOVER_Z, "tapDepth": DEFAULT_TAP_DEPTH,
                    "tapFeed": DEFAULT_TAP_FEED}
    if not bundle_present and (not data or not any(value is not None for value in data.values())):
        data = _read_json(device_dir(device_id, data_root) / "arm_profile.json") or {}
    return {
        "hoverZ": float(data.get("hoverZ") if data.get("hoverZ") is not None else DEFAULT_HOVER_Z),
        "tapDepth": float(data.get("tapDepth") if data.get("tapDepth") is not None else DEFAULT_TAP_DEPTH),
        "tapFeed": float(data.get("tapFeed") if data.get("tapFeed") is not None else DEFAULT_TAP_FEED),
    }


def save_profile(device_id: str, profile: dict, data_root: Path | None = None) -> dict:
    merged = {**load_profile(device_id, data_root),
              **{k: float(profile[k]) for k in ("hoverZ", "tapDepth", "tapFeed") if k in profile}}
    from drivers import calibration_bundle
    bundle = calibration_bundle.load_bundle(device_id, data_root)
    bundle_present = calibration_bundle.bundle_path(device_id, data_root).exists()
    if bundle is not None:
        bundle = dict(bundle)
        z = dict(bundle.get("z") or {})
        z.update(merged)
        # Direct parameter edits require the normal calibration/acceptance pass.
        z["validated"] = False
        z["deleteValidated"] = False
        bundle["z"] = z
        bundle["status"] = "draft"
        calibration_bundle.save_bundle(bundle, data_root)
    elif bundle_present:
        raise ValueError("统一校准档案损坏：请先修复或重新生成 calibration_bundle.json")
    else:
        path = device_dir(device_id, data_root) / "arm_profile.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    return merged


def camera_to_robot(affine: dict, camera_x: float, camera_y: float) -> tuple[float, float]:
    """robot = A·camera + b（拟合方向即此方向，直接套用，不做逆变换）。"""
    return (affine["a"] * camera_x + affine["b"] * camera_y + affine["c"],
            affine["d"] * camera_x + affine["e"] * camera_y + affine["f"])


def resolve_target(target: dict, calibration: dict, profile: dict) -> dict:
    """把档案目标解析为机械坐标与 Z 参数（含 profile 兜底与安全预检）。"""
    if "affine" in calibration:
        cx = float(target["nx"]) * float(calibration["frameW"])
        cy = float(target["ny"]) * float(calibration["frameH"])
        rx, ry = camera_to_robot(calibration["affine"], cx, cy)
    else:
        # 新档案将 camera -> screen 与 robot -> screen 分开保存，
        # 业务目标仍以摄像头帧归一化坐标登记，因此运行时先投影到屏幕，
        # 再用 XY 仿射逆变换得到机械坐标。
        from drivers import calibration_bundle

        frame = calibration.get("frame") or {}
        width = float(frame.get("width") or 0)
        height = float(frame.get("height") or 0)
        if width <= 0 or height <= 0:
            raise ValueError("统一校准档案缺少有效摄像头帧尺寸")
        cx = float(target["nx"]) * width
        cy = float(target["ny"]) * height
        camera = calibration.get("camera") or {}
        homography = (camera.get("cameraToScreen") or {}).get("homography")
        robot_to_screen = (calibration.get("xy") or {}).get("robotToScreen")
        if not homography or not robot_to_screen:
            raise ValueError("统一校准档案缺少 camera→screen 或 robot→screen 映射")
        sx, sy = calibration_bundle.apply_homography(homography, cx, cy)
        rx, ry = calibration_bundle.robot_from_screen(robot_to_screen, sx, sy)
    # hoverZ 是设备级统一校准参数。历史目标档案可能带有保存时的旧
    # hoverZ，不能让某个目标把刚完成的 Z 标定覆盖掉；点击/停靠一律
    # 以当前 calibration profile 的悬停高度为准。
    resolved = {
        "robotX": round(rx, 3),
        "robotY": round(ry, 3),
        "hoverZ": float(profile["hoverZ"]),
        "tapDepth": float(target.get("tapDepth") or profile["tapDepth"]),
        "tapFeed": float(target.get("tapFeed") or profile["tapFeed"]),
        "targetMode": target.get("targetMode") or TARGET_MODE_TAP,
    }
    check_feed(resolved["tapFeed"], axis="Z")  # F300 等非法进给在解析期即拒绝
    return resolved


def validate_target_name(name: str) -> None:
    if name not in TARGET_DISPLAY_NAMES:
        known = "、".join(sorted(TARGET_DISPLAY_NAMES))
        raise ValueError(f"未知目标：{name}（已知目标：{known}）")


def save_target(device_id: str, name: str, payload: dict,
                data_root: Path | None = None) -> dict:
    """调试台登记/更新一个固定目标（唯一写入入口，业务运行时只读）。"""
    validate_target_name(name)
    nx = float(payload["nx"]); ny = float(payload["ny"])
    if not 0 <= nx <= 1 or not 0 <= ny <= 1:
        raise ValueError("画面位置必须是 0~1 的归一化坐标")
    targets = load_targets(device_id, data_root)
    entry = {
        "name_display": TARGET_DISPLAY_NAMES[name],
        "targetMode": payload.get("targetMode") or DEFAULT_TARGET_MODES.get(name, TARGET_MODE_TAP),
        "nx": round(nx, 4), "ny": round(ny, 4),
        "hoverZ": float(payload.get("hoverZ") or DEFAULT_HOVER_Z),
        "tapDepth": float(payload.get("tapDepth") or DEFAULT_TAP_DEPTH),
        "tapFeed": float(payload.get("tapFeed") or DEFAULT_TAP_FEED),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    targets[name] = entry
    from drivers import calibration_bundle
    bundle = calibration_bundle.load_bundle(device_id, data_root)
    bundle_present = calibration_bundle.bundle_path(device_id, data_root).exists()
    if bundle is not None:
        # Target edits invalidate target acceptance and the ready gate. Keep the
        # complete calibration record in one file so stale targets.json cannot
        # silently win at runtime.
        bundle = dict(bundle)
        bundle["targets"] = targets
        bundle["status"] = "draft"
        calibration_bundle.save_bundle(bundle, data_root)
    elif bundle_present:
        raise ValueError("统一校准档案损坏：请先修复或重新生成 calibration_bundle.json")
    else:
        path = device_dir(device_id, data_root) / "targets.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(targets, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    return entry


def resolve_dynamic_target_ref(target_ref: dict | None) -> None:
    """动态会话目标（conversation_item）v1 不开放：需要 chat-send.v1 校验链路。"""
    raise ValueError(
        "动态会话目标（conversation_item）需要已校验的 WX SendCommand 与感知坐标，"
        "当前版本未开放；请使用设备级固定目标")
