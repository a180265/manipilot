"""Read-only production gesture eligibility, without claiming a motion lease."""
from __future__ import annotations

from drivers import calibration_bundle as cb, screen_gestures
from drivers.daemon_protocol import DOUBLE_TAP_FEATURE, SWIPE_FEATURE
from runtime.calibration_gate import check_registered_calibration


async def gesture_metadata(runtime, device_id: str) -> dict:
    device = runtime.devices.get(device_id)
    driver = runtime.devices.driver(device_id)
    root = getattr(driver, "data_root", None) or runtime.config.root / "data"
    bundle = cb.load_bundle(device_id, root)
    reasons = []
    binding = None
    features = []
    verifier = (getattr(driver, "gesture_motion_verifier", None)
                or getattr(driver, "motion_verifier", None))
    if getattr(verifier, "measurement_kind", None) == "frame_change":
        reasons.append("当前只能核验画面变化，尚不能可靠区分机械运动与手机动画")
    readiness = getattr(verifier, "readiness", None)
    if callable(readiness):
        reasons.extend(readiness())
    try:
        binding = screen_gestures.binding_snapshot(bundle)
        screen_gestures.resolve_point({"source": "screen_normalized", "x": .5, "y": .5}, bundle)
        screen_gestures.calibrated_profile(bundle)
    except (ValueError, TypeError, KeyError) as exc:
        reasons.append(str(exc))
    try:
        state = await getattr(driver, "read_gesture_state", driver.read_state)()
        features = state.get("motion_features") or []
        if not state.get("connected"):
            reasons.append("机械臂未连接或状态不可确认")
        if state.get("estop_latched") or state.get("daemon_estop_locked") or runtime.estop.active:
            reasons.append("急停锁定中")
        if binding and device.get("binding_mode") != "simulation" \
                and state.get("session") != binding["daemon_session"]:
            reasons.append("当前机械臂会话与校准不一致")
        if state.get("motion_lease") or runtime.leases.get(device_id):
            reasons.append("当前设备有动作占用")
    except Exception as exc:
        reasons.append(f"无法读取机械臂当前状态：{exc}")
    if device.get("status") not in {"ready", "degraded"}:
        reasons.append("当前设备状态不允许新的动作")
    if device.get("binding_mode") != "simulation":
        reasons.extend(check["message"] for check in check_registered_calibration(device, bundle))
    registered = runtime.plugins.task_type_registry().get("screen_gesture")
    if not registered:
        reasons.append("通用屏幕手势插件尚未启用")
    else:
        reasons.extend(runtime.plugin_context_blockers(registered["plugin_id"], device_id))
    reasons = list(dict.fromkeys(reasons))
    actions = {}
    for action, feature, label in (("double_tap", DOUBLE_TAP_FEATURE, "双击"),
                                    ("swipe", SWIPE_FEATURE, "连续滑动")):
        blocking = list(reasons)
        if feature not in features:
            blocking.append(f"当前守护进程不支持{label}，未下发任何动作")
        actions[action] = {"usable": not blocking, "blocking_reasons": blocking}
    return {"contract_version": "screen-gesture.v1", "device_id": device_id,
            "binding": binding, "coordinate_space": "screen_normalized",
            "usable": not reasons, "blocking_reasons": reasons,
            "features": features, "actions": actions,
            "effect_status": "unverified",
            "effect_status_display": "动作能力不代表手机手势效果已经验收"}
