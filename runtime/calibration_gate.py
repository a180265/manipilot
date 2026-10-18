"""真实业务动作前的统一校准现场门禁。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import time
import cv2
import numpy as np

from drivers import calibration_bundle
from drivers.real_camera import RealCameraDriver, ScreenRoiNotFound
from drivers.screen_edge import (ScreenEdgeError, classify_quad_change,
                                 locate_screen_quad)
from drivers.screen_pose import fit_pose


def check_registered_calibration(device: dict, bundle: dict | None) -> list[dict]:
    """只读核对档案与当前登记/心跳事实；不替代动作前的新帧现场检查。"""
    try:
        return calibration_bundle.validate_calibration_bundle(
            bundle,
            device_id=device.get("device_id"),
            camera_id=device.get("camera_id"),
            camera_device_unique_id=device.get("camera_device_unique_id"),
            binding_revision=device.get("camera_binding_revision")
                if device.get("camera_binding_revision") is not None
                else device.get("binding_revision"),
            daemon_addr=device.get("daemon_addr"),
            frame_width=device.get("expected_frame_width"),
            frame_height=device.get("expected_frame_height"),
            daemon_session=device.get("daemon_session"),
        )
    except (TypeError, ValueError, AttributeError, OverflowError):
        return [{"code": "calibration_bundle_invalid",
                 "message": "统一校准档案字段无效，必须重新校准并验收"}]


EDGE_CONSENSUS_SAMPLES = 4
EDGE_CONSENSUS_INTERVAL_S = 0.35
SOURCE_FRAME_POLL_S = 0.1
SOURCE_FRAME_WAIT_POLLS = 8
CONTACT_REFERENCE_TTL_S = 10.0


@dataclass(frozen=True)
class ContactReference:
    """Internal, action-local witness from a successful strict live check.

    A saved calibration is never a witness. Only the contact guard for that
    same approach may carry this object into a subsequent live check.
    """

    corners: list
    frame: dict
    bundle: dict
    region: dict | None
    daemon_session: str
    observed_at: float


def _contact_reference_corners(reference, frame, bundle, search_region,
                               daemon_session):
    if (not isinstance(reference, ContactReference)
            or not 0 <= time.monotonic() - reference.observed_at <= CONTACT_REFERENCE_TTL_S
            or reference.bundle != bundle or reference.region != search_region
            or reference.daemon_session != daemon_session):
        raise ValueError("接触前清晰画面参照已过期或绑定已变化")
    _fresh_source_seq(frame, reference.frame["source_frame_seq"])
    for key in ("device_id", "camera_id", "device_unique_id", "binding_revision",
                "width", "height", "camera_binding_id", "source_geometry_id"):
        if frame.get(key) != reference.frame.get(key):
            raise ValueError("接触前画面与走位前清晰参照的来源不一致")
    return reference.corners


def _fresh_source_seq(frame: dict, previous_seq: int | None) -> int:
    seq = frame.get("source_frame_seq")
    if (type(seq) is not int or seq < 0 or frame.get("source_legacy_mode")
            or (previous_seq is not None and seq <= previous_seq)):
        raise ValueError("手机定位缺少连续推进的推流帧")
    try:
        captured = datetime.fromisoformat(frame["source_captured_at"].replace("Z", "+00:00"))
        received = datetime.fromisoformat(frame["source_received_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if (captured.tzinfo is None or received.tzinfo is None
                or not -.5 <= (now - captured).total_seconds() <= 2
                or not -.5 <= (now - received).total_seconds() <= 2
                or not -.5 <= (received - captured).total_seconds() <= 2):
            raise ValueError("stale")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("手机定位帧时间不可确认或已过期") from exc
    return seq


async def _capture_advancing_source(camera, previous_frame: dict) -> dict:
    """Wait at most eight short intervals for a latest-frame source to advance.

    Repeated physical frames are never consensus samples. Invalid timestamps,
    sequence rollback and source/session changes fail immediately.
    """
    previous_seq = previous_frame["source_frame_seq"]
    for attempt in range(SOURCE_FRAME_WAIT_POLLS + 1):
        frame = await camera.capture()
        if not isinstance(frame, dict):
            raise ValueError("手机定位帧元数据无效")
        seq = _fresh_source_seq(frame, None)
        for key in ("device_id", "camera_id", "device_unique_id", "binding_revision",
                    "width", "height", "camera_binding_id", "source_geometry_id",
                    "source_generation_id"):
            if frame.get(key) != previous_frame.get(key):
                raise ValueError("手机定位连续画面的来源或推流会话已变化")
        if seq != previous_seq or frame.get("frozen"):
            _fresh_source_seq(frame, previous_seq)
            return frame
        if attempt == SOURCE_FRAME_WAIT_POLLS:
            _fresh_source_seq(frame, previous_seq)
        await asyncio.sleep(SOURCE_FRAME_POLL_S)
    raise AssertionError("unreachable")


def _quad_of(image_bytes, expected_corners=None,
             search_region: dict | None = None,
             contact_reference_corners=None) -> list | None:
    if not image_bytes:
        return None
    decoded = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if decoded is None:
        return None
    if search_region is not None:
        # S2：动作前定位约束在本台生效粗框内（多候选/裁断/缺框拒绝），不回全图。
        from runtime.phone_region_context import (
            RegionContextError, locate_quad_in_region)
        try:
            if contact_reference_corners is not None:
                from runtime.layout_geometry import locate_in_region, LayoutGeometryError
                try:
                    return locate_in_region(
                        decoded, search_region, expected_corners,
                        contact_reference_corners=contact_reference_corners)["corners"]
                except LayoutGeometryError:
                    return None
            return locate_quad_in_region(
                decoded, search_region, expected_corners)["corners"]
        except RegionContextError:
            return None
    try:
        options = {}
        if contact_reference_corners is not None:
            options["contact_reference_corners"] = contact_reference_corners
        return locate_screen_quad(
            decoded, expected_corners, tolerance_px=32,
            require_fittable=expected_corners is not None, **options)["corners"]
    except ScreenEdgeError:
        return None


def _source_binding_checks(frame: dict, bundle: dict | None, *,
                           device_id: str, camera_id: str,
                           require_metadata: bool) -> list[dict]:
    if not isinstance(frame, dict):
        return [{"code": "camera_unavailable", "message": "动作前画面元数据无效"}]
    archive = bundle or {}
    dimensions = archive.get("frame") or {}
    expected = {
        "device_id": device_id, "camera_id": camera_id,
        "device_unique_id": archive.get("cameraDeviceUniqueId"),
        "binding_revision": archive.get("bindingRevision"),
        "width": dimensions.get("width"), "height": dimensions.get("height"),
    }
    for key, value in expected.items():
        if value is None:
            continue
        if not require_metadata and frame.get(key) is None:
            continue
        if type(frame.get(key)) is not type(value) or frame.get(key) != value:
            return [{"code": "camera_binding_changed",
                     "message": f"手机定位帧的 {key} 与当前设备校准绑定不符，禁止沿用该帧"}]
    # FIX-07：v3 安装/来源几何身份——帧桥提供该身份时必须与本臂 v3 档案
    # 一致；帧未携带（旧通道）不在此判，资格由绑定比较与档案门禁承担。
    for frame_key, archive_key in (("camera_binding_id", "cameraBindingId"),
                                   ("source_geometry_id", "sourceGeometryId")):
        saved = archive.get(archive_key)
        carried = frame.get(frame_key)
        if saved is None or carried is None:
            continue
        if str(carried) != str(saved):
            return [{"code": "camera_binding_changed",
                     "message": f"来源帧的 {frame_key} 与当前 v3 校准绑定不符，禁止沿用该帧"}]
    return []


def _frame_checks(frame: dict, *, screen_wake: bool = False,
                  require_pose: bool = False,
                  wake_binding: dict | None = None,
                  screen_corners=None,
                  search_region: dict | None = None) -> tuple[list[dict], str | None]:
    if screen_wake:
        from drivers.wake_screen import wake_frame_quality

        try:
            quality = wake_frame_quality(
                frame, wake_binding, screen_corners=screen_corners,
                require_phone=require_pose)
        except ValueError as exc:
            return [{"code": "camera_unavailable", "message": str(exc)}], None
        return [], quality
    if frame.get("frozen"):
        return [{"code": "camera_unavailable", "message": "画面冻结，禁止执行动作"}], None
    image = frame.get("image_bytes") or frame.get("image")
    if not image:
        return [{"code": "camera_unavailable", "message": "未取得动作前画面，禁止执行动作"}], None
    try:
        metrics = RealCameraDriver._analyze(
            image, expected_corners=screen_corners, tolerance_px=18,
            search_region=search_region)
        quality = RealCameraDriver._classify(metrics, False)
    except ScreenRoiNotFound:
        return ([{"code": "screen_pose_unavailable",
                  "message": "当前画面无法定位校准手机屏幕，禁止触屏；摄像头连接状态未因此改变"}],
                None)
    except (RuntimeError, ValueError, TypeError, cv2.error):
        return [{"code": "camera_unavailable", "message": "动作前画面无法解码，禁止执行动作"}], None
    if quality == "waiting":
        return [{"code": "camera_waiting", "message": "等待画面恢复后再执行动作"}], None
    if quality != "clear":
        return [{"code": "camera_unavailable",
                 "message": "手机屏幕画质异常，禁止执行动作"}], None
    return [], None


async def check_live_calibration(
        *, device_id: str, camera_id: str, daemon_addr: str,
        daemon_session: str | None, estop_locked: bool, camera,
        bundle: dict | None, required_targets=(), wechat_send: bool = False,
        motion_result_unknown: bool = False,
        require_input_acceptance: bool = False,
        screen_edge_advisory: bool = False,
        screen_wake: bool = False,
        require_pose: bool = False,
        search_region: dict | None = None,
        contact_reference: ContactReference | None = None) -> tuple[list[dict], dict, float | None]:
    """采一帧并按 calibration_bundle v2 校验当前现场；空 checks 才可动作。

    屏幕边缘核验用多帧共识（2026-09-23 真机双态抖动定案）：暗色页面上
    检测器会帧间交替锁定「屏幕亮区」与「手机物理边框」（实测 26px↔47px
    双态，后者纯缩放形态会被判刚性位移误拦）。单帧判定无法区分这种
    检测抖动与真实位移；改为连续多帧采样——帧间角点不一致 = 检测不可
    信 → 按既有政策跳过（deviation=None）；多帧一致的刚性位移仍然硬拦。
    screen_edge_advisory=True（仅回停等安全动作）：边缘核验降级为记录，
    不硬拦——停在固定机械坐标的安全位与手机位置无关，被边缘误报搁浅
    在屏幕上方反而是新的现场风险。
    """
    calibrated = (os.environ.get("ARMFLEET_RUNTIME_VISION_MODE", "live") == "calibrated"
                  and not screen_wake)
    wake_binding = None
    if screen_wake:
        from drivers.screen_gestures import binding_snapshot

        try:
            wake_binding = binding_snapshot(bundle)
        except (ValueError, TypeError, KeyError):
            return [{"code": "calibration_bundle_invalid",
                     "message": "人工唤醒缺少完整校准绑定，未执行动作"}], {}, None
    try:
        frame = await camera.capture()
    except Exception:
        return [{"code": "camera_unavailable",
                 "message": "动作前取图失败，禁止执行动作"}], {}, None
    saved_edge = (((bundle or {}).get("screen") or {}).get("edgeModel") or {})
    saved_corners = saved_edge.get("corners")
    frame_checks = _source_binding_checks(
        frame, bundle, device_id=device_id, camera_id=camera_id,
        require_metadata=require_pose or screen_wake or calibrated)
    if not isinstance(frame, dict):
        return frame_checks, {}, None
    if calibrated:
        # Fixed mounts use accepted calibration; this is not a live pose measurement.
        frame = dict(frame)
        frame.pop("_screen_pose", None)
        frame["geometry_source"] = "saved_calibration"
    reference_corners = None
    if contact_reference is not None and not calibrated:
        try:
            if not require_pose or screen_wake or screen_edge_advisory:
                raise ValueError("清晰画面参照仅用于本次走位后的接触前复核")
            reference_corners = _contact_reference_corners(
                contact_reference, frame, bundle, search_region, daemon_session)
        except (KeyError, TypeError, ValueError) as exc:
            return [{"code": "screen_pose_unavailable", "message": str(exc)}], frame, None
    wake_quality: str | None = None
    if not frame_checks and calibrated:
        try:
            _fresh_source_seq(frame, None)
            if frame.get("frozen"):
                raise ValueError("手机画面冻结，等待推流恢复")
            if not (frame.get("image_bytes") or frame.get("image")):
                raise ValueError("未取得动作前画面，禁止执行动作")
        except ValueError as exc:
            frame_checks = [{"code": "camera_unavailable", "message": str(exc)}]
    elif not frame_checks:
        frame_checks, wake_quality = _frame_checks(
            frame, screen_wake=screen_wake, wake_binding=wake_binding,
            require_pose=require_pose, screen_corners=saved_corners,
            search_region=search_region)
    image = frame.get("image_bytes") or frame.get("image")
    deviation = None
    pose_error = None
    # §9.4/W04/W08：唤醒的暗屏阶段不强制四边共识——有效暗屏允许等待不报错；
    # 姿态若可观测仍使用它（下方单帧尽力拟合，供双击前补偿）。亮屏帧仍必须
    # 验证实时姿态；普通动作不因暗屏例外放行，保持原门禁。
    pose_required_now = (not calibrated and require_pose
                         and (not screen_wake or wake_quality == "clear"))
    geometry_required = not calibrated and ((not screen_wake) or pose_required_now)
    if not frame_checks and geometry_required and image and saved_corners:
        tolerance = float(saved_edge.get("tolerancePx")
                          or calibration_bundle.DEFAULT_EDGE_TOLERANCE_PX)
        quads: list[list] = []
        source_seq = None
        if require_pose:
            try:
                source_seq = _fresh_source_seq(frame, None)
            except ValueError as exc:
                pose_error = str(exc)
        quad_options = {"search_region": search_region}
        if reference_corners is not None:
            quad_options["contact_reference_corners"] = reference_corners
        first = _quad_of(image, saved_corners, **quad_options)
        if search_region is not None and first is None and not frame_checks:
            # S2-01：粗框上下文内无法唯一定位（多候选/被框裁断/未检出）——
            # 普通动作前置门直接阻断，不依赖 require_pose（框内两屏/紧框
            # 在无姿态要求的动作上同样拒绝）。
            frame_checks = [{"code": "screen_pose_unavailable",
                             "message": "生效粗框内无法唯一定位手机屏幕"
                                        "（多候选/裁断/未检出），禁止动作"}]
        if first is not None and not pose_error:
            quads.append(first)
            for _ in range(EDGE_CONSENSUS_SAMPLES - 1):
                await asyncio.sleep(EDGE_CONSENSUS_INTERVAL_S)
                try:
                    extra = (await _capture_advancing_source(camera, frame)
                             if require_pose else await camera.capture())
                except ValueError as exc:
                    pose_error = str(exc)
                    break
                except Exception:
                    frame_checks = [{"code": "camera_unavailable",
                                     "message": "动作前复核取图失败，禁止沿用旧帧执行动作"}]
                    break
                frame = extra
                frame_checks = _source_binding_checks(
                    extra, bundle, device_id=device_id, camera_id=camera_id,
                    require_metadata=require_pose or screen_wake)
                if not isinstance(extra, dict):
                    return frame_checks, {}, None
                if not frame_checks:
                    frame_checks, _extra_quality = _frame_checks(
                        extra, screen_wake=screen_wake, wake_binding=wake_binding,
                        require_pose=require_pose, screen_corners=saved_corners,
                        search_region=search_region)
                if frame_checks:
                    break
                if require_pose:
                    try:
                        source_seq = _fresh_source_seq(extra, source_seq)
                    except ValueError as exc:
                        pose_error = str(exc)
                        break
                if contact_reference is not None:
                    try:
                        _contact_reference_corners(
                            contact_reference, extra, bundle, search_region, daemon_session)
                    except (KeyError, TypeError, ValueError) as exc:
                        pose_error = str(exc)
                        break
                quad = _quad_of(
                    extra.get("image_bytes") or extra.get("image"), saved_corners,
                    **quad_options)
                if quad is None:
                    pose_error = "连续画面中手机屏幕定位丢失"
                    break
                quads.append(quad)
        if quads and not frame_checks and not pose_error:
            import math as _math

            spread = max(
                max(_math.dist(a[k], b[k]) for k in range(4))
                for a in quads for b in quads)
            if spread > min(2.0 * tolerance, 4.0):
                # 帧间锁定目标漂移（亮区↔边框双态）：单帧结论不可信，跳过
                deviation = None
                pose_error = "连续画面中的手机边缘不一致"
            else:
                verdict = classify_quad_change(saved_corners, quads[-1], tolerance)
                # 非刚性偏差=检测受环境亮区/画面内容干扰（无法核实→跳过，
                # 与检测失败同语义）；真实刚性位移（平移/旋转）仍硬拦。
                if verdict["status"] != "anomaly":
                    deviation = verdict["deviationPx"]
                    if require_pose and len(quads) == EDGE_CONSENSUS_SAMPLES:
                        try:
                            pose = fit_pose(saved_corners, quads[-1])
                            frame["_screen_pose"] = pose
                            deviation = 0.0
                        except ValueError as exc:
                            pose_error = str(exc)
                else:
                    pose_error = "手机边缘受画面内容或环境干扰，无法确认位置"
        elif not frame_checks and not pose_error:
            pose_error = "当前画面未定位到手机屏幕"
        if screen_edge_advisory and deviation is not None:
            import logging as _gate_log
            _gate_log.warning(
                "屏幕边缘核验（advisory）测得偏差 %spx，已记录不阻断", deviation)
            deviation = None
    if (screen_wake and not pose_required_now and not frame_checks
            and image and saved_corners and "_screen_pose" not in frame):
        # §9.3/W08：暗屏阶段姿态若仍可观测（机身/屏幕四边可定位）则尽力
        # 拟合一次供双击前补偿；拟合失败不报错——仅校准 ROI 可判断时保持
        # 等待语义，不伪造姿态。
        quad = _quad_of(image, saved_corners, search_region=search_region)
        if quad is not None:
            try:
                frame["_screen_pose"] = fit_pose(saved_corners, quad)
            except (ValueError, TypeError):
                pass

    screen = (bundle or {}).get("screen") or {}
    checks = calibration_bundle.validate_calibration_bundle(
        bundle,
        device_id=device_id,
        camera_id=frame.get("camera_id") or camera_id,
        camera_device_unique_id=frame.get("device_unique_id"),
        binding_revision=frame.get("binding_revision"),
        daemon_addr=daemon_addr,
        frame_width=frame.get("width"),
        frame_height=frame.get("height"),
        daemon_session=daemon_session,
        # FIX-07：帧桥携带的安装/来源几何身份作为当前事实进入 v3 门禁。
        camera_binding_id=frame.get("camera_binding_id"),
        source_geometry_id=frame.get("source_geometry_id"),
        estop_locked=estop_locked,
        screen_orientation=screen.get("orientation"),
        screen_width=screen.get("screenWidth"),
        screen_height=screen.get("screenHeight"),
        screen_edge_deviation_px=deviation,
        required_targets=required_targets,
        wechat_send=wechat_send,
        require_input_acceptance=require_input_acceptance,
        motion_result_unknown=motion_result_unknown,
    )
    if pose_required_now and not screen_edge_advisory and not frame_checks:
        if "_screen_pose" not in frame:
            checks.append({"code": "screen_pose_unavailable",
                           "message": pose_error or "手机屏幕位置不可确认，禁止动作"})
        elif frame.get("source_frame_seq") is None or frame.get("source_legacy_mode"):
            checks.append({"code": "screen_pose_unavailable",
                           "message": "手机定位缺少持续推流的新鲜帧证明"})
    all_checks = checks + frame_checks
    if (not all_checks and require_pose and not screen_wake
            and not screen_edge_advisory and contact_reference is None
            and "_screen_pose" in frame):
        # These four strictly located, source-bound fresh frames establish the
        # witness. A contact check never renews it or creates a new baseline.
        frame["_screen_pose"]["_contact_reference"] = ContactReference(
            corners=deepcopy(frame["_screen_pose"]["corners"]),
            frame={key: value for key, value in frame.items()
                   if key not in ("image", "image_bytes", "_screen_pose")},
            bundle=deepcopy(bundle), region=deepcopy(search_region),
            daemon_session=daemon_session, observed_at=time.monotonic())
    return all_checks, frame, deviation
