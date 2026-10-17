"""统一设备校准档案（calibration_bundle.json）：读写、兼容导入与运行前门禁。

对应《统一校准与输入法点击验收实施方案》§10/§11：
- 每台设备一份 data/devices/{device}/calibration_bundle.json，是自动任务
  运行前门禁的唯一事实源；旧 calibration.json / screen_ime_mappings.json /
  targets.json 只作兼容输入，不再单独作为门禁依据。
- 写入采用临时文件替换，防止中断产生半份 JSON。
- validate_calibration_bundle() 是纯函数：传入档案与当前现场事实，返回失败
  列表（code + 中文说明）；空列表 = 允许执行自动任务。任何失败统一表现为
  calibration_invalid，具体原因见各检查项 code。
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import tempfile
import time
from decimal import Decimal, localcontext
from pathlib import Path

from drivers.daemon_protocol import check_feed
from drivers.target_archive import device_dir

# FIX-07（2026-10-04 实施单）：v2 是档案基础版本（new_bundle 缺省产出，旧任务
# /迁移继续使用）；schemaVersion=3 只能由校准保存/提升链路在可信绑定提供
# 安装 UUID（cameraBindingId）与来源几何（sourceGeometryId）时产出——
# 缺身份的档案不得自称 v3，也不得补字段升级续用。
BUNDLE_SCHEMA_VERSION = 2
BUNDLE_SCHEMA_VERSION_V3 = 3
V2_SCHEMA_VERSION = BUNDLE_SCHEMA_VERSION
BUNDLE_FILENAME = "calibration_bundle.json"
CANDIDATES_DIRNAME = "calibration_candidates"
MOTION_READY_STATUS = "motion_ready"
# Auto_CS#20 纯观测通道：相机-only 校准的晋升态（屏幕几何+摄像头单应即可，
# 无机械臂动作阶段、不绑定串口会话）。只解除 screen-observation 的会话绑定；
# 运行前运动门禁（validate_calibration_bundle）对该状态一律判 bundle_not_ready
# ——纯观测档案永远不能驱动动作。
OBSERVATION_READY_STATUS = "observation_ready"

# 模型锁状态（v1.1 自动校准 §22；手动流程不写这些字段，门禁跳过）
MODEL_LOCK_UNINITIALIZED = "UNINITIALIZED"
MODEL_LOCK_CANDIDATE = "CANDIDATE"
MODEL_LOCK_VALIDATED = "VALIDATED"
MODEL_LOCK_LOCKED = "LOCKED"
MODEL_LOCK_INVALID = "INVALID"

# 阈值（实施方案 §7.3 / §8.4）
XY_RESIDUAL_RMS_MAX_PX = 3.0
XY_RESIDUAL_MAX_PX = 6.0
CAMERA_REPROJECTION_MAX_PX = 3.0
DEFAULT_SAFE_INSET_PX = 24.0
DEFAULT_EDGE_TOLERANCE_PX = 8.0

# Z 参数（实施方案 §6.4/§6.5）
DEFAULT_TAP_DEPTH = 3.0
DEFAULT_TAP_FEED = 1500.0
DEFAULT_TAP_DWELL_MS = 0
DEFAULT_DELETE_HOLD_MS = 2000
DELETE_HOLD_MIN_MS = 800
DELETE_HOLD_MAX_MS = 5000

Z_PARAM_KEYS = ("hoverZ", "tapDepth", "tapFeed", "tapDwellMs",
                "deleteHoldMs", "deleteDepth", "deleteFeed")


def mapping_error_bound(bundle: dict, *, allow_legacy_defaults: bool = False) -> float | None:
    """Conservative measured screen-pixel envelope, including independent taps.

    Qualification remains the caller's responsibility. Old physical-edge callers
    may retain their missing-field defaults and 1px arm floor; malformed present
    measurements never fall back. Independent camera-to-touch gaps describe arm
    error, so camera reprojection error is added before comparing with nominal
    target error. Training residuals and independent summaries cannot shrink the
    envelope recorded by individual holdout points.
    """
    def finite(value):
        if type(value) not in (int, float) or value < 0 or not math.isfinite(value):
            raise ValueError("校准误差必须为有限非负数")
        return value

    def add(a, b):
        terms = [Decimal(str(a)), Decimal(str(b))]
        with localcontext() as context:
            context.prec = max(28, max(t.adjusted() for t in terms)
                               - min(t.as_tuple().exponent for t in terms) + 3)
            exact = sum(terms, Decimal(0))
        value = float(exact)
        if Decimal(str(value)) < exact:
            value = math.nextafter(value, math.inf)
        return finite(value)

    try:
        xy = bundle.get("xy") or {}
        camera = bundle.get("camera") or {}
        model = camera.get("cameraToScreen") or {}
        arm_error = finite(xy.get("maxResidualPx", XY_RESIDUAL_MAX_PX
                                 if allow_legacy_defaults else None))
        camera_error = finite(model.get("maxErrorPx", CAMERA_REPROJECTION_MAX_PX
                                       if allow_legacy_defaults else None))
        if allow_legacy_defaults:
            arm_error = max(1.0, arm_error)
        nominal_errors = []
        arm_errors = [arm_error]
        for points in ((bundle.get("validation") or {}).get("points", []),
                       xy.get("validation", [])):
            if not isinstance(points, list):
                raise ValueError("独立验证点记录无效")
            for point in points:
                if not isinstance(point, dict):
                    raise ValueError("独立验证点记录无效")
                for key, values in (("touchTargetErrorPx", nominal_errors),
                                    ("cameraTouchGapPx", arm_errors)):
                    if key in point:
                        if point.get("passed") is False:
                            raise ValueError("独立验证点未通过")
                        values.append(finite(point[key]))
        summary = camera.get("visualVerify") or {}
        for key, values in (("contactVsNominalMaxPx", nominal_errors),
                            ("contactVsCameraMaxPx", arm_errors)):
            if key in summary:
                if summary.get("passed") is False:
                    raise ValueError("独立验证汇总未通过")
                values.append(finite(summary[key]))
        return max([add(max(arm_errors), camera_error), *nominal_errors])
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def default_data_root() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def bundle_path(device_id: str, data_root: Path | None = None) -> Path:
    return device_dir(device_id, data_root or default_data_root()) / BUNDLE_FILENAME


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _default_camera_id(device_id: str) -> str:
    suffix = str(device_id).removeprefix("arm-")
    return f"cam-{suffix}" if suffix.isdigit() else "cam-01"


def _canonical_camera_id(raw: object, device_id: str) -> str:
    """Normalize the pre-refactor camera-N alias to the frozen cam-N form."""
    value = str(raw or "").strip().lower()
    if value.startswith("camera-") and value[7:].isdigit():
        return f"cam-{value[7:]}"
    return value or _default_camera_id(device_id)


def binding_fingerprint(*, device_id: str, camera_id: str,
                        camera_device_unique_id: str, daemon_addr: str,
                        frame_width: int, frame_height: int) -> str:
    """计算冻结规范定义的设备/摄像头/daemon/分辨率绑定指纹。"""
    parts = (
        str(device_id).strip(),
        str(camera_id).strip(),
        str(camera_device_unique_id).strip(),
        str(daemon_addr).strip(),
        f"{int(frame_width)}x{int(frame_height)}",
    )
    if any(not part for part in parts):
        raise ValueError("绑定指纹字段不能为空")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _migrate_v1(data: dict) -> dict:
    """v1 只迁移内容，不继承 ready；首次保存后成为正式 v2 draft。

    v1 迁移目标固定为 v2：v3 身份（安装 UUID/来源几何）只能由真实自动
    校准生产链补齐，迁移不得伪造。
    """
    migrated = json.loads(json.dumps(data, ensure_ascii=False))
    migrated["schemaVersion"] = V2_SCHEMA_VERSION
    migrated["status"] = "draft"
    device_id = str(migrated.get("deviceId") or "arm-01")
    migrated["cameraId"] = _canonical_camera_id(migrated.get("cameraId"), device_id)
    migrated.setdefault("cameraDeviceUniqueId", None)
    migrated.setdefault("bindingRevision", None)
    migrated.setdefault("bindingFingerprint", None)
    migrated.setdefault("daemonAddr", None)
    migrated.setdefault("frame", {"width": None, "height": None})
    migrated.setdefault("migration", {})["fromSchemaVersion"] = 1
    migrated["migration"]["requiresRevalidation"] = True
    return migrated


def new_bundle(device_id: str, *, ime_device_id: str = "k40-01",
               camera_id: str | None = None) -> dict:
    """按实施方案 §10 的结构生成空档案（status=draft，未通过验收前不能 ready）。

    v2 基础档案预置 cameraBindingId/sourceGeometryId 身份位（None）——
    schemaVersion=3 只能由保存/提升链路在可信绑定写入两身份后声称
    （FIX-07）；身份不从请求任意填值。
    v1.1 自动校准新增可选字段（§21.2）：safePark / modelLocks / autoCalibration /
    validation / z.zSafe。默认全部为空/UNINITIALIZED——现场坐标必须为 null，
    真实值只能由 operator_confirmed 路径写入（§17.1）。
    """
    return {
        "schemaVersion": BUNDLE_SCHEMA_VERSION,
        "deviceId": device_id,
        "imeDeviceId": ime_device_id,
        "cameraId": camera_id or _default_camera_id(device_id),
        "cameraDeviceUniqueId": None,
        "cameraBindingId": None,
        "sourceGeometryId": None,
        "bindingRevision": None,
        "bindingFingerprint": None,
        "daemonAddr": None,
        "daemonSession": None,
        "status": "draft",
        "frame": {"width": None, "height": None},
        "screen": {
            "orientation": None,
            "screenWidth": None,
            "screenHeight": None,
            "edgeModel": None,
        },
        "safePark": {
            "parkX": None,
            "parkY": None,
            "parkZ": None,
            "confirmed": False,
            "source": None,
            "confirmedAt": None,
        },
        "z": {
            "hoverZ": None,
            "zSafe": None,
            "tapDepth": None,
            "tapFeed": None,
            "tapDwellMs": None,
            "deleteHoldMs": None,
            "deleteDepth": None,
            "deleteFeed": None,
            "validated": False,
        },
        "xy": {
            "robotToScreen": None,
            "residualRmsPx": None,
            "maxResidualPx": None,
            "samples": [],
            "validation": [],
            "validated": False,
        },
        "camera": {
            "cameraToScreen": None,
            "reprojectionRmsPx": None,
            "validated": False,
        },
        "modelLocks": {
            "cameraToScreen": {"state": MODEL_LOCK_UNINITIALIZED, "version": 0},
            "armAffine": {"state": MODEL_LOCK_UNINITIALIZED, "version": 0},
        },
        "autoCalibration": {
            "runId": None,
            "state": "IDLE",
            "phase": None,
            "iteration": 0,
            "modelVersion": 0,
            "lastFailure": None,
        },
        "validation": {"points": [], "passed": False},
        "targets": {},
        "acceptance": {"debug": False, "wechat": False,
                       "lastRunAt": None, "checks": []},
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    }


def load_bundle(device_id: str, data_root: Path | None = None) -> dict | None:
    path = bundle_path(device_id, data_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("schemaVersion") or 0) == 1:
        return _migrate_v1(data)
    return data


class BundleConcurrentWrite(RuntimeError):
    """条件写入（CAS）失败：磁盘档案已不是调用方读取时的那份。"""


def bundle_file_digest(device_id: str, data_root: Path | None = None) -> str | None:
    """当前档案文件的原始字节 SHA-256（不存在返回 None）。

    提升等「读-改-写」路径用它作为并发写入条件：写入前磁盘摘要必须仍
    等于读取时的摘要，否则拒绝覆盖（P08，规范 §5.2.5）。
    """
    path = bundle_path(device_id, data_root)
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save_bundle(bundle: dict, data_root: Path | None = None, *,
                expected_digest: str | None = None,
                require_absent: bool = False) -> dict:
    """原子写入（tmp + replace），并刷新 updatedAt。

    expected_digest：条件写入——磁盘当前字节摘要不等于该值即抛
    BundleConcurrentWrite，不覆盖。require_absent=True 时要求文件原本
    不存在（摘要 None），用于「只允许首个创建者」的场景。
    """
    if int(bundle.get("schemaVersion") or 0) == 1:
        bundle = _migrate_v1(bundle)
    else:
        bundle = dict(bundle)
        version = int(bundle.get("schemaVersion") or 0)
        if version not in (V2_SCHEMA_VERSION, BUNDLE_SCHEMA_VERSION_V3):
            # FIX-07：保存不降级也不悄悄升版——v2 档案保持 v2（旧任务/迁移），
            # v3 档案保持 v3（新链路资格）；未知版本按基础版本落盘。
            bundle["schemaVersion"] = BUNDLE_SCHEMA_VERSION
    bundle["updatedAt"] = now_iso()
    path = bundle_path(str(bundle.get("deviceId") or "arm-01"), data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if expected_digest is not None or require_absent:
        current = bundle_file_digest(str(bundle.get("deviceId") or "arm-01"), data_root)
        if require_absent:
            if current is not None:
                raise BundleConcurrentWrite(
                    "档案已存在，拒绝按「首个创建」条件覆盖（并发创建冲突）")
        elif current != expected_digest:
            raise BundleConcurrentWrite(
                "档案在读取后被其他写入修改（并发提升/写入冲突），本次拒绝覆盖")
    payload = json.dumps(bundle, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return bundle


# ---------- v1.1 自动校准 candidate 存储（§21.4：不得写回活动档案） ----------

def candidates_dir(device_id: str, data_root: Path | None = None) -> Path:
    return device_dir(device_id, data_root or default_data_root()) / CANDIDATES_DIRNAME


def candidate_path(device_id: str, run_id: str, data_root: Path | None = None) -> Path:
    safe_id = "".join(ch for ch in str(run_id) if ch.isalnum() or ch in "-_")
    if not safe_id:
        raise ValueError("runId 不能为空")
    return candidates_dir(device_id, data_root) / f"{safe_id}.json"


def profile_hash(bundle: dict, keys: tuple | list | None = None) -> str:
    """档案规范化哈希：candidate 记录 baseProfileHash，promote 前校验基线未被改动。

    keys 传入字段子集时只哈希这些字段——自动校准的 promote 守卫用几何/身份子集：
    运行中经合法入口新增的内容（如输入法删除键的目标标记、SAFE PARK）不应
    误杀提升，真正要挡的是几何/绑定基础被人改动。
    """
    selected = sorted(keys) if keys else [k for k in bundle if k != "updatedAt"]
    payload = {key: bundle.get(key) for key in selected}
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


V3_REQUIRED_IDENTITY_FIELDS = ("cameraBindingId", "sourceGeometryId")


def binding_fingerprint_v3(bundle: dict) -> str:
    """v3 bindingFingerprint：七字段对象的规范化 JSON SHA-256（附件 §7）。

    精确输入：deviceId,cameraId,cameraDeviceUniqueId,cameraBindingId,
    sourceGeometryId,daemonAddr,frame。缺失字段拒绝，不补 null。
    """
    missing = [key for key in ("deviceId", "cameraId", "cameraDeviceUniqueId",
                               "cameraBindingId", "sourceGeometryId", "daemonAddr")
               if not str(bundle.get(key) or "").strip()]
    frame = bundle.get("frame") or {}
    if not isinstance(frame.get("width"), int) or not isinstance(frame.get("height"), int):
        missing.append("frame")
    if missing:
        raise ValueError(f"v3 bindingFingerprint 缺少字段：{', '.join(missing)}")
    payload = {
        "deviceId": bundle["deviceId"],
        "cameraId": bundle["cameraId"],
        "cameraDeviceUniqueId": bundle["cameraDeviceUniqueId"],
        "cameraBindingId": bundle["cameraBindingId"],
        "sourceGeometryId": bundle["sourceGeometryId"],
        "daemonAddr": bundle["daemonAddr"],
        "frame": {"width": frame["width"], "height": frame["height"]},
    }
    return _canonical_sha256(payload)


def _canonical_sha256(value) -> str:
    """附件 §3 规范：UTF-8、键排序、紧凑分隔、不转义 Unicode、禁止 NaN。"""
    import hashlib
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def motion_calibration_fingerprint(bundle: dict) -> str:
    """Shared identity for observed geometry and calibrated motion readiness.

    v3（schemaVersion=3）：精确输入 {"contract_version":"motion-calibration.v3",
    "geometry":{schemaVersion,deviceId,daemonSession,cameraId,cameraDeviceUniqueId,
    cameraBindingId,sourceGeometryId,frame,camera,screen,xy,z,safePark}}——
    排除 bindingRevision/updatedAt/acceptance 等诊断/历史字段，字段缺失拒绝。
    v2 保持原 profile_hash 输入不变。两版均完整包含 xy，因此可选的
    xy.clickPointPolicy 新增、变更或移除都会撤销旧机械/观测证据指纹。
    """
    if int(bundle.get("schemaVersion") or 0) == 3:
        geometry_keys = ("schemaVersion", "deviceId", "daemonSession", "cameraId",
                         "cameraDeviceUniqueId", "cameraBindingId",
                         "sourceGeometryId", "frame", "camera", "screen",
                         "xy", "z", "safePark")
        missing = [key for key in geometry_keys if key not in bundle]
        if missing:
            raise ValueError(f"v3 motion 指纹缺少字段：{', '.join(missing)}")
        geometry = {key: bundle[key] for key in geometry_keys}
        geometry["schemaVersion"] = 3
        return _canonical_sha256({
            "contract_version": "motion-calibration.v3",
            "geometry": geometry,
        })
    return profile_hash(bundle, keys=(
        "schemaVersion", "deviceId", "daemonSession", "cameraId",
        "cameraDeviceUniqueId", "bindingRevision", "frame",
        "camera", "screen", "xy", "z", "safePark",
    ))


def save_candidate(candidate: dict, data_root: Path | None = None) -> dict:
    """candidate 原子落盘（临时文件替换）。必须携带 runId/deviceId。"""
    run_id = str(candidate.get("runId") or "").strip()
    device_id = str(candidate.get("deviceId") or "").strip()
    if not run_id or not device_id:
        raise ValueError("candidate 必须携带 runId 与 deviceId")
    candidate = dict(candidate)
    candidate["updatedAt"] = now_iso()
    path = candidate_path(device_id, run_id, data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(candidate, ensure_ascii=False, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    return candidate


def load_candidate(device_id: str, run_id: str, data_root: Path | None = None) -> dict | None:
    path = candidate_path(device_id, run_id, data_root)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def list_candidates(device_id: str, data_root: Path | None = None) -> list[dict]:
    """全部 candidate 摘要（按创建时间升序）：文件名即 runId。"""
    root = candidates_dir(device_id, data_root)
    if not root.exists():
        return []
    items = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            items.append({
                "runId": data.get("runId") or path.stem,
                "state": data.get("state"),
                "phase": data.get("phase"),
                "iteration": data.get("iteration"),
                "createdAt": data.get("createdAt"),
                "lastFailure": data.get("lastFailure"),
            })
    return items

def import_legacy(device_id: str, data_root: Path | None = None,
                  *, ime_device_id: str = "k40-01") -> dict:
    """把旧档案作为"兼容输入"并入新档案：目标照搬（validated=false 待重测），
    旧机械↔屏幕映射只作参考记录，全部不直接获得验收状态。"""
    from drivers.target_archive import load_legacy_targets

    root = data_root or default_data_root()
    bundle = load_bundle(device_id, root) or new_bundle(device_id, ime_device_id=ime_device_id)
    notes: list[str] = []

    # Explicit migration is the only path that reads the old targets.json.
    # Runtime target resolution uses the bundle as its single source of truth.
    legacy_targets = load_legacy_targets(device_id, root)
    for name, entry in legacy_targets.items():
        bundle["targets"][name] = {
            "nx": entry.get("nx"), "ny": entry.get("ny"),
            "targetMode": entry.get("targetMode", "tap"),
            "validated": False,
            "source": "legacy_targets",
            "note": "旧档案导入，需通过视觉目标测试后才能用于自动任务",
        }
    if legacy_targets:
        notes.append(f"targets.json 导入 {len(legacy_targets)} 个目标（待重测）")

    # 旧 screen_ime_mappings.json（机械↔屏幕仿射）：仅作参考，不带 validated
    mappings_path = root / "screen_ime_mappings.json"
    if mappings_path.exists():
        try:
            mappings = json.loads(mappings_path.read_text(encoding="utf-8"))
            legacy_map = mappings.get(device_id)
            if legacy_map and all(k in legacy_map for k in ("a", "b", "c", "d", "e", "f")):
                bundle.setdefault("legacy", {})["robotToScreenReference"] = {
                    **{k: legacy_map[k] for k in ("a", "b", "c", "d", "e", "f")},
                    "note": "旧 screen_ime_mappings.json 参考，不作为门禁依据",
                }
                notes.append("screen_ime_mappings.json 已并入 legacy 参考")
        except Exception:
            pass

    # 旧摄像头→机械标定：同样只作参考
    legacy_calib_path = device_dir(device_id, root) / "calibration.json"
    if legacy_calib_path.exists():
        try:
            legacy_calib = json.loads(legacy_calib_path.read_text(encoding="utf-8"))
            if legacy_calib.get("affine"):
                bundle.setdefault("legacy", {})["cameraToRobotReference"] = {
                    "affine": legacy_calib["affine"],
                    "session": legacy_calib.get("session"),
                    "note": "旧 camera→robot 标定参考，新链路为 camera→screen→robot",
                }
                notes.append("calibration.json 已并入 legacy 参考")
        except Exception:
            pass

    if notes:
        bundle.setdefault("legacy", {})["importedAt"] = now_iso()
        bundle["legacy"]["notes"] = notes
    return save_bundle(bundle, root)


# ---------- 数学辅助 ----------

def affine_invertible(m: dict) -> bool:
    try:
        det = float(m["a"]) * float(m["e"]) - float(m["b"]) * float(m["d"])
    except (KeyError, TypeError, ValueError):
        return False
    return abs(det) > 1e-12


_QUAD_X_KEYS = ("qx2", "qxy", "qy2")
_QUAD_Y_KEYS = ("px2", "pxy", "py2")


def _has_quadratic(m: dict) -> bool:
    """robotToScreen 是否携带二次项（倾斜触点滑移补偿，2026-09-04 方案 A）。

    旧档案只有仿射 6 键，一律按纯仿射处理——兼容是硬约束。
    """
    return all(m.get(key) is not None for key in _QUAD_X_KEYS + _QUAD_Y_KEYS)


def screen_from_robot(m: dict, rx: float, ry: float) -> tuple[float, float]:
    sx = m["a"] * rx + m["b"] * ry + m["c"]
    sy = m["d"] * rx + m["e"] * ry + m["f"]
    if _has_quadratic(m):
        rx2, ry2, rxy = rx * rx, ry * ry, rx * ry
        sx += m["qx2"] * rx2 + m["qxy"] * rxy + m["qy2"] * ry2
        sy += m["px2"] * rx2 + m["pxy"] * rxy + m["py2"] * ry2
    return sx, sy


def robot_from_screen(m: dict, sx: float, sy: float) -> tuple[float, float]:
    """逆变换：屏幕像素 → 机械坐标（自动任务走位方向）。

    二次模型无闭式逆：从仿射解起步做 3 次 Newton 迭代；Jacobian 奇异或
    步长发散（>50mm）即回退当前解（fail-safe，绝不抛错中断业务）。
    """
    det = float(m["a"]) * float(m["e"]) - float(m["b"]) * float(m["d"])
    if abs(det) < 1e-12:
        raise ValueError("仿射变换不可逆")
    dx, dy = sx - float(m["c"]), sy - float(m["f"])
    rx = (float(m["e"]) * dx - float(m["b"]) * dy) / det
    ry = (-float(m["d"]) * dx + float(m["a"]) * dy) / det
    if not _has_quadratic(m):
        return rx, ry
    for _ in range(3):
        ex, ey = screen_from_robot(m, rx, ry)
        j11 = float(m["a"]) + 2.0 * float(m["qx2"]) * rx + float(m["qxy"]) * ry
        j12 = float(m["b"]) + float(m["qxy"]) * rx + 2.0 * float(m["qy2"]) * ry
        j21 = float(m["d"]) + 2.0 * float(m["px2"]) * rx + float(m["pxy"]) * ry
        j22 = float(m["e"]) + float(m["pxy"]) * rx + 2.0 * float(m["py2"]) * ry
        jdet = j11 * j22 - j12 * j21
        if not math.isfinite(jdet) or abs(jdet) < 1e-12:
            return rx, ry
        step_x = (j22 * (sx - ex) - j12 * (sy - ey)) / jdet
        step_y = (-j21 * (sx - ex) + j11 * (sy - ey)) / jdet
        if not (math.isfinite(step_x) and math.isfinite(step_y)) \
                or abs(step_x) > 50.0 or abs(step_y) > 50.0:
            return rx, ry
        rx += step_x
        ry += step_y
    return rx, ry


def _flatten_homography(homography) -> list[float]:
    """Accept both OpenCV's nested 3x3 list and the wire-format flat list."""
    if isinstance(homography, (list, tuple)) and len(homography) == 3 \
            and all(isinstance(row, (list, tuple)) for row in homography):
        homography = [item for row in homography for item in row]
    if not isinstance(homography, (list, tuple)) or len(homography) != 9:
        raise ValueError("单应矩阵必须包含 9 个元素")
    values = [float(item) for item in homography]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("单应矩阵包含非有限值")
    return values


def apply_homography(homography: list | tuple, x: float, y: float) -> tuple[float, float]:
    """将摄像头像素投影到屏幕像素，并拒绝退化/非有限结果。"""
    try:
        values = _flatten_homography(homography)
    except (TypeError, ValueError) as exc:
        raise ValueError("单应矩阵格式无效") from exc
    try:
        den = values[6] * float(x) + values[7] * float(y) + values[8]
        if abs(den) < 1e-12:
            raise ValueError("单应矩阵投影分母接近 0")
        sx = (values[0] * float(x) + values[1] * float(y) + values[2]) / den
        sy = (values[3] * float(x) + values[4] * float(y) + values[5]) / den
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("单应矩阵格式无效") from exc
    if not all(math.isfinite(value) for value in (sx, sy)):
        raise ValueError("单应矩阵投影结果非有限")
    return sx, sy


# ---------- 运行前门禁（实施方案 §11） ----------

def _fail(checks: list, code: str, message: str) -> None:
    checks.append({"code": code, "message": message})


def input_acceptance_deferred(bundle: dict | None) -> bool:
    """输入法待验事实；机械资格或非布尔延后标志均不能获得输入资格。

    自动校准的独立机械资格（模型锁+五点独立验证）只覆盖动作门禁；
    输入/发送域仍以 E 输入法验收为准（2026-09-04 拍板：发送门禁保持
    关闭，ready 后补记解锁）。
    """
    if not bundle:
        return False
    acceptance = bundle.get("acceptance") or {}
    deferred = acceptance.get("deferred") or {}
    return (bundle.get("status") == MOTION_READY_STATUS
            or ("debug" in deferred and deferred["debug"] is not False))


def automatic_calibration_verified(bundle: dict | None) -> bool:
    """仅承认已提升、同 run 的模型锁与五点独立验证。"""
    if not isinstance(bundle, dict) or bundle.get("status") not in {"ready", MOTION_READY_STATUS}:
        return False
    auto = bundle.get("autoCalibration") or {}
    validation = bundle.get("validation") or {}
    locks = bundle.get("modelLocks") or {}
    park = bundle.get("safePark") or {}
    z = bundle.get("z") or {}
    finite = lambda value: type(value) in (int, float) and math.isfinite(value)
    points = validation.get("points") or []
    tolerance = validation.get("tolerancePx")
    return (
        auto.get("state") == "PROMOTED"
        and bool(auto.get("runId"))
        and auto["runId"] == validation.get("runId")
        and validation.get("passed") is True
        and finite(tolerance) and tolerance > 0
        and len(points) == 5
        and all(isinstance(point, dict) for point in points)
        and {point.get("targetId") for point in points}
            == {f"validation-{index}" for index in range(1, 6)}
        and all(point.get("passed") is True
                and finite(point.get("touchTargetErrorPx"))
                and 0 <= point["touchTargetErrorPx"] <= tolerance for point in points)
        and all((locks.get(key) or {}).get("state") == MODEL_LOCK_LOCKED
                and type((locks.get(key) or {}).get("version")) is int
                and locks[key]["version"] > 0
                for key in ("cameraToScreen", "armAffine"))
        and z.get("validated") is True
        and (bundle.get("xy") or {}).get("validated") is True
        and (bundle.get("camera") or {}).get("validated") is True
        and park.get("confirmed") is True
        and all(finite(park.get(key)) for key in ("parkX", "parkY", "parkZ"))
        and finite(z.get("zSafe")) and park["parkZ"] == z["zSafe"]
    )


def validate_calibration_bundle(
        bundle: dict | None, *,
        device_id: str | None = None,
        camera_id: str | None = None,
        camera_device_unique_id: str | None = None,
        binding_revision: int | None = None,
        daemon_addr: str | None = None,
        frame_width: int | None = None,
        frame_height: int | None = None,
        daemon_session: str | None = None,
        camera_binding_id: str | None = None,
        source_geometry_id: str | None = None,
        estop_locked: bool = False,
        screen_orientation: str | None = None,
        screen_width: int | None = None,
        screen_height: int | None = None,
        screen_edge_deviation_px: float | None = None,
        required_targets: tuple | list = (),
        wechat_send: bool = False,
        require_input_acceptance: bool = False,
        motion_result_unknown: bool = False) -> list[dict]:
    """运行前统一校准门禁：返回失败列表（空 = 放行）。

    参数即"当前现场事实"：调用方负责采集 daemon 会话、屏幕方向/尺寸、
    摄像头画面屏幕边缘偏差（未检测到传 None）与本次任务所需目标。
    """
    checks: list[dict] = []
    if not bundle:
        _fail(checks, "calibration_bundle_missing",
              "统一校准档案不存在：请先完成统一校准向导")
        return checks
    _schema_version = int(bundle.get("schemaVersion") or 0)
    if _schema_version not in (V2_SCHEMA_VERSION, BUNDLE_SCHEMA_VERSION_V3):
        _fail(checks, "calibration_bundle_version_unsupported",
              f"档案版本不受支持：{bundle.get('schemaVersion')}")
        return checks
    if _schema_version == BUNDLE_SCHEMA_VERSION_V3 and any(
            not str(bundle.get(key) or "").strip()
            for key in V3_REQUIRED_IDENTITY_FIELDS):
        _fail(checks, "calibration_bundle_version_unsupported",
              "v3 档案缺少 cameraBindingId/sourceGeometryId：记录缺字段为协议不支持，"
              "不得补 null 续用")
        return checks
    # FIX-07：v3 档案的当前安装/来源几何事实一旦可确认，必须与档案一致；
    # 换装/几何变化使资格失效（仅全局诊断 revision 不在此列）。
    if _schema_version == BUNDLE_SCHEMA_VERSION_V3:
        for current, saved, code, label in (
                (camera_binding_id, bundle.get("cameraBindingId"),
                 "camera_installation_changed", "相机安装身份"),
                (source_geometry_id, bundle.get("sourceGeometryId"),
                 "source_geometry_changed", "来源几何")):
            if current is not None and str(current) != str(saved):
                _fail(checks, code, f"{label}变化：档案 {saved}，当前 {current}")

    # 1. 设备、摄像头、daemon 与帧尺寸绑定必须完整且匹配
    identity_fields = {
        "deviceId": bundle.get("deviceId"),
        "cameraId": bundle.get("cameraId"),
        "cameraDeviceUniqueId": bundle.get("cameraDeviceUniqueId"),
        "bindingRevision": bundle.get("bindingRevision"),
        "daemonAddr": bundle.get("daemonAddr"),
        "bindingFingerprint": bundle.get("bindingFingerprint"),
    }
    frame = bundle.get("frame") or {}
    if not all(value not in (None, "") for value in identity_fields.values()) \
            or frame.get("width") in (None, "") or frame.get("height") in (None, ""):
        _fail(checks, "calibration_binding_incomplete",
              "校准档案未完整绑定机械臂、摄像头、daemon 和帧尺寸")
    else:
        try:
            saved_fingerprint = binding_fingerprint(
                device_id=identity_fields["deviceId"],
                camera_id=identity_fields["cameraId"],
                camera_device_unique_id=identity_fields["cameraDeviceUniqueId"],
                daemon_addr=identity_fields["daemonAddr"],
                frame_width=int(frame["width"]), frame_height=int(frame["height"]),
            )
            if saved_fingerprint != identity_fields["bindingFingerprint"]:
                _fail(checks, "calibration_binding_corrupt",
                      "校准档案绑定指纹与档案字段不一致")
        except (TypeError, ValueError):
            _fail(checks, "calibration_binding_corrupt", "校准档案绑定字段格式无效")

    comparisons = (
        (device_id, bundle.get("deviceId"), "device_binding_changed", "机械臂身份"),
        (camera_id, bundle.get("cameraId"), "camera_binding_changed", "逻辑摄像头"),
        (camera_device_unique_id, bundle.get("cameraDeviceUniqueId"),
         "camera_unique_id_changed", "物理摄像头"),
        (daemon_addr, bundle.get("daemonAddr"), "daemon_address_changed", "daemon 地址"),
    )
    for current, saved, code, label in comparisons:
        if current is not None and str(current) != str(saved):
            _fail(checks, code, f"{label}变化：档案 {saved}，当前 {current}")
    if camera_device_unique_id in (None, ""):
        _fail(checks, "camera_unique_id_unavailable",
              "当前物理摄像头唯一标识不可确认：拒绝使用校准档案")
    if binding_revision is not None:
        try:
            revision_match = int(binding_revision) == int(bundle.get("bindingRevision"))
        except (TypeError, ValueError):
            revision_match = False
        if not revision_match:
            _fail(checks, "camera_binding_revision_changed",
                  f"摄像头绑定版本变化：档案 {bundle.get('bindingRevision')}，当前 {binding_revision}")
    else:
        _fail(checks, "camera_binding_revision_unavailable",
              "当前摄像头绑定版本不可确认：拒绝使用校准档案")
    for current, saved, label in (
            (frame_width, frame.get("width"), "宽"),
            (frame_height, frame.get("height"), "高")):
        if current is not None and (saved is None or int(current) != int(saved)):
            _fail(checks, "camera_resolution_changed",
                  f"摄像头画面{label}变化：档案 {saved}px，当前 {current}px")

    current_fingerprint_fields = (
        device_id, camera_id, camera_device_unique_id, daemon_addr,
        frame_width, frame_height,
    )
    if all(value is not None for value in current_fingerprint_fields):
        try:
            current_fingerprint = binding_fingerprint(
                device_id=str(device_id), camera_id=str(camera_id),
                camera_device_unique_id=str(camera_device_unique_id),
                daemon_addr=str(daemon_addr), frame_width=int(frame_width),
                frame_height=int(frame_height),
            )
            if current_fingerprint != bundle.get("bindingFingerprint"):
                _fail(checks, "binding_fingerprint_changed",
                      "当前设备、摄像头、daemon 或分辨率组合与校准档案不一致")
        except (TypeError, ValueError):
            _fail(checks, "binding_fingerprint_changed", "当前绑定事实格式无效")

    # 2026-10-03 规格 §4.6：acceptance/deferred/deleteValidated 等 E 标志
    # 全部退出校验——不再决定输入、发送或机械 ready；机械证据要求不变。
    motion_only = bundle.get("status") == MOTION_READY_STATUS
    auto_verified = automatic_calibration_verified(bundle)

    # 2. 历史机械资格保持原有约束；自动 READY 以同轮独立验证为准。
    if bundle.get("status") not in {"ready", MOTION_READY_STATUS}:
        _fail(checks, "bundle_not_ready",
              f"档案状态为 {bundle.get('status') or '未知'}：请先完成校准与验收")
    if motion_only and not auto_verified:
        validation = bundle.get("validation") or {}
        points = validation.get("points") or []
        tolerance = validation.get("tolerancePx")
        locks = bundle.get("modelLocks") or {}
        park = bundle.get("safePark") or {}
        z = bundle.get("z") or {}
        finite = lambda value: type(value) in (int, float) and math.isfinite(value)
        complete = (
            z.get("validated") is True
            and (bundle.get("xy") or {}).get("validated") is True
            and (bundle.get("camera") or {}).get("validated") is True
            and all((locks.get(key) or {}).get("state") == MODEL_LOCK_LOCKED
                    and type((locks.get(key) or {}).get("version")) is int
                    and locks[key]["version"] > 0
                    for key in ("cameraToScreen", "armAffine"))
            and validation.get("passed") is True
            and bool(validation.get("runId"))
            and finite(tolerance) and tolerance > 0
            and len(points) == 5
            and all(isinstance(point, dict) for point in points)
            and {point.get("targetId") for point in points}
                == {f"validation-{index}" for index in range(1, 6)}
            and all(point.get("passed") is True
                    and finite(point.get("touchTargetErrorPx"))
                    and 0 <= point["touchTargetErrorPx"] <= tolerance for point in points)
            and park.get("confirmed") is True
            and all(finite(park.get(key)) for key in ("parkX", "parkY", "parkZ"))
            and finite(z.get("zSafe")) and park["parkZ"] == z["zSafe"]
        )
        if not complete:
            _fail(checks, "motion_qualification_incomplete",
                  "机械独立资格缺少完整模型锁、独立验证、Z 或停车位证据")
    elif bundle.get("status") == "ready" and (bundle.get("autoCalibration") or {}).get("state") == "PROMOTED" and not auto_verified:
        _fail(checks, "motion_qualification_incomplete",
              "自动校准缺少完整的模型锁、独立验证或安全停车位")

    # 3. daemon session 一致
    bundle_session = bundle.get("daemonSession")
    if not bundle_session:
        _fail(checks, "daemon_session_unbound", "档案未绑定 daemon 会话：请重新校准")
    elif daemon_session is None:
        _fail(checks, "daemon_session_unavailable",
              "当前 daemon 会话不可确认：拒绝使用校准档案，请重新连接并自检")
    elif bundle_session != daemon_session:
        _fail(checks, "daemon_session_changed",
              "机械臂会话已变化：Z/XY 校准失效，必须重新确认 hoverZ 并重采 XY 样本")

    # 4. 屏幕方向与尺寸
    screen = bundle.get("screen") or {}
    if screen_orientation and screen.get("orientation") \
            and screen_orientation != screen.get("orientation"):
        _fail(checks, "screen_orientation_changed",
              f"屏幕方向变化：档案 {screen.get('orientation')}，当前 {screen_orientation}")
    size_pairs = ((screen_width, screen.get("screenWidth"), "宽"),
                  (screen_height, screen.get("screenHeight"), "高"))
    for current, saved, label in size_pairs:
        if current and saved and int(current) != int(saved):
            _fail(checks, "screen_size_changed",
                  f"屏幕{label}变化：档案 {saved}px，当前 {current}px")

    # 5. 屏幕边缘
    if not (screen.get("edgeModel") or {}).get("corners"):
        _fail(checks, "screen_edge_not_calibrated", "屏幕边缘基准未采集")
    elif screen_edge_deviation_px is None:
        # 无法核验（检测失败/场景不适配：键盘打开、光线变化等）不再硬性阻断
        # 业务——绑定指纹/会话/帧尺寸等门禁仍然生效；真实偏差超限依然硬拦。
        # 边缘核验在手机校准页等可控画面下才可靠（2026-09-04 真机事故）。
        pass
    else:
        tolerance = float((screen.get("edgeModel") or {}).get("tolerancePx")
                          or DEFAULT_EDGE_TOLERANCE_PX)
        if float(screen_edge_deviation_px) > tolerance:
            _fail(checks, "screen_edge_changed",
                  f"屏幕边缘偏差 {screen_edge_deviation_px:.1f}px 超过容差 {tolerance:.0f}px")

    # 6. Z 参数完整、在安全范围且已验收
    z = bundle.get("z") or {}
    missing_z = [k for k in Z_PARAM_KEYS if z.get(k) is None]
    if missing_z:
        _fail(checks, "z_parameters_not_validated",
              f"Z 参数缺失：{', '.join(missing_z)}")
    else:
        try:
            check_feed(float(z["tapFeed"]), axis="Z")
            check_feed(float(z["deleteFeed"]), axis="Z")
        except ValueError as exc:
            _fail(checks, "z_parameters_out_of_range", f"Z 参数不安全：{exc}")
        hold = float(z["deleteHoldMs"])
        if not DELETE_HOLD_MIN_MS <= hold <= DELETE_HOLD_MAX_MS:
            _fail(checks, "z_parameters_out_of_range",
                  f"长按停留 {hold:.0f}ms 超出 {DELETE_HOLD_MIN_MS}~{DELETE_HOLD_MAX_MS}ms")
        if not z.get("validated"):
            _fail(checks, "z_parameters_not_validated", "Z 点击参数尚未通过触点回报验收")
        # 2026-10-03 规格 §4.6：deleteValidated 取消资格作用；删除进给/长按
        # 时长的存在性与范围检查仍保留（上方），真实清空靠当次删除动作核对。

    # 7. XY 映射存在、可逆、残差合格且已验收
    xy = bundle.get("xy") or {}
    m = xy.get("robotToScreen")
    if not m:
        _fail(checks, "xy_mapping_missing", "机械↔屏幕映射不存在：请完成 XY 校准")
    else:
        if not affine_invertible(m):
            _fail(checks, "xy_mapping_invalid", "机械↔屏幕映射不可逆（样本退化）")
        rms = xy.get("residualRmsPx")
        worst = xy.get("maxResidualPx")
        if rms is None or float(rms) > XY_RESIDUAL_RMS_MAX_PX:
            _fail(checks, "xy_residual_exceeded",
                  f"XY 拟合残差 RMS {rms}px 超过阈值 {XY_RESIDUAL_RMS_MAX_PX}px")
        if worst is not None and float(worst) > XY_RESIDUAL_MAX_PX:
            _fail(checks, "xy_residual_exceeded",
                  f"单点最大残差 {worst}px 超过建议值 {XY_RESIDUAL_MAX_PX}px")
        if not xy.get("validated"):
            _fail(checks, "xy_mapping_not_validated", "XY 映射尚未通过独立验证点验收")

    # 8. 摄像头映射
    camera = bundle.get("camera") or {}
    c2s = camera.get("cameraToScreen")
    if not c2s or not (c2s.get("homography") or []):
        _fail(checks, "camera_mapping_missing", "摄像头→屏幕映射不存在：请完成视觉校准")
    else:
        h = c2s.get("homography")
        try:
            values = _flatten_homography(h)
            determinant = (
                values[0] * (values[4] * values[8] - values[5] * values[7])
                - values[1] * (values[3] * values[8] - values[5] * values[6])
                + values[2] * (values[3] * values[7] - values[4] * values[6])
            )
            if abs(determinant) < 1e-12:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            _fail(checks, "camera_mapping_invalid", "摄像头单应矩阵格式无效或不可逆")
        rms = camera.get("reprojectionRmsPx")
        if rms is not None and float(rms) > CAMERA_REPROJECTION_MAX_PX:
            _fail(checks, "camera_residual_exceeded",
                  f"摄像头重投影残差 {rms}px 超过阈值 {CAMERA_REPROJECTION_MAX_PX}px")
        if not camera.get("validated"):
            _fail(checks, "camera_mapping_not_validated", "摄像头映射尚未通过目标点击验收")
        visual_verify = camera.get("visualVerify")
        if isinstance(visual_verify, dict) and visual_verify.get("passed") is False:
            _fail(checks, "camera_visual_verification_failed",
                  "摄像头→点击视觉验收未通过：不同位置存在区域性偏差，必须重新采集中心标靶并拟合")

    # 8b. v1.1 自动校准产物：model lock 与独立验证（仅在档案带自动校准痕迹时生效，
    #     手动流程档案无这些状态，门禁保持原语义）
    locks = bundle.get("modelLocks") or {}
    if isinstance(locks, dict) and any(
            (locks.get(key) or {}).get("state") not in (None, MODEL_LOCK_UNINITIALIZED)
            for key in ("cameraToScreen", "armAffine")):
        for key, label in (("cameraToScreen", "摄像头单应"),
                           ("armAffine", "机械臂映射")):
            lock = locks.get(key) or {}
            if lock.get("state") != MODEL_LOCK_LOCKED:
                _fail(checks, "model_not_locked",
                      f"{label}模型未锁定（当前 {lock.get('state') or 'UNINITIALIZED'}）："
                      f"自动校准产物未完成锁定，不得作为 ready 档案")
        auto_validation = bundle.get("validation")
        if isinstance(auto_validation, dict) and auto_validation.get("points") \
                and not auto_validation.get("passed"):
            _fail(checks, "independent_validation_failed",
                  "自动校准独立验证未通过：该 candidate 不得进入 ready")

    # 9. 所需目标已登记且通过验收
    targets = bundle.get("targets") or {}
    for name in required_targets:
        entry = targets.get(name)
        if entry is None:
            _fail(checks, "target_not_validated", f"目标未登记：{name}")
        elif not entry.get("validated"):
            _fail(checks, "target_not_validated", f"目标未通过点击验收：{name}")

    # 10. 业务测试会话不属于机械臂校准门禁。
    # 每次实际发送仍由 业务/插件完成目标、正文和发送后效果证据核对；
    # 通用平台这里只负责机械臂与输入法的校准事实。

    # 11. 运动可用性
    if estop_locked:
        _fail(checks, "motion_blocked", "急停锁定中：人工复位前拒绝一切自动任务")
    if motion_result_unknown:
        _fail(checks, "motion_result_unknown",
              "上一机械动作结果未知：必须人工确认位置后才能继续")

    return checks
