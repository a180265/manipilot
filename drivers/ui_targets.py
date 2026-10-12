"""校准后 UI 固定目标扩展档案（ui_targets.json）—— WX 图形固定目标标定与
安全点击实施规格书 v1.0 §3.2/§3.3 的档案层。

与 calibration_bundle.json 严格分层：

    calibration_bundle.json  自动校准产出的几何/Z/绑定/ready 基线（本模块只读）
    ui_targets.json          校准完成后登记的业务固定目标（本模块唯一写入口）

规则（规格书 §3.2/§3.3/§5）：
- 登记只发生在 bundle 为 ready 之后；任何登记/测试/启停操作都不修改
  calibration_bundle.json 的几何、Z 参数或 ready 状态；
- 目标只存画面归一化坐标（nx, ny）；机械坐标运行期按当前标定现算；
- 每个目标绑定登记时的绑定指纹、几何哈希、daemon 会话、帧尺寸与屏幕方向；
  校准/换绑/分辨率/方向/会话变化后目标判"已失效（待复核）"，拒绝执行；
- validated 必须来自真实受控测试 + 操作者确认，不能因"已保存坐标"自动获得；
- 目标修改递增 version 并回到草稿，全部操作记审计；
- 业务运行时只读（resolve）；写入只经调试台登记入口。
"""
from __future__ import annotations

import json
import re
import tempfile
import os
from pathlib import Path

from drivers.daemon_protocol import (
    DEFAULT_TARGET_MODES,
    TARGET_DISPLAY_NAMES,
    TARGET_MODE_HOVER_ONLY,
    TARGET_MODE_TAP,
)
from drivers import calibration_bundle as cb

UI_TARGETS_FILENAME = "ui_targets.json"
ARCHIVE_SCHEMA_VERSION = 1

# 预置目标（规格书 §5.1）：模式与显示名由 daemon_protocol 固定，不允许改名。
# ime_candidate_bar 是 screen_input 业务链路的固定目标（2026-09-04 并入本层，
# 旧 bundle targets 兑底移除后候选栏点击只认这里）。
PRESET_TARGET_IDS = ("back_button", "send_button", "ime_delete_key", "ime_candidate_bar", "input_box")
# 动态会话目标保留字（§6.1）：必须携带摄像头感知坐标，不属于固定目标登记。
RESERVED_DYNAMIC_TARGET_IDS = ("conversation_item",)
# 与自动校准 AUTO_PROMOTE_GUARD_KEYS 的几何子集一致：重跑校准/换绑会变，
# 运行中经合法入口新增的 targets/acceptance 等字段不影响它。
GEOMETRY_HASH_KEYS = ("frame", "screen", "xy", "camera")

TARGET_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")

STATE_DRAFT = "draft"
STATE_TESTED = "tested"
STATE_ENABLED = "enabled"
STATE_DISABLED = "disabled"
STATE_DISPLAY = {
    STATE_DRAFT: "草稿",
    STATE_TESTED: "已测试待确认",
    STATE_ENABLED: "已启用",
    STATE_DISABLED: "已停用",
}
VALID_TARGET_MODES = (TARGET_MODE_TAP, TARGET_MODE_HOVER_ONLY)


class UiTargetError(ValueError):
    """档案层拒绝（登记校验 / 解析门禁）；code 供驱动层映射错误类别。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ---------- 路径与读写 ----------

def ui_targets_path(device_id: str, data_root: Path | None = None) -> Path:
    root = Path(data_root) if data_root else Path(__file__).resolve().parents[1] / "data"
    return root / "devices" / device_id / UI_TARGETS_FILENAME


def load_archive(device_id: str, data_root: Path | None = None) -> dict:
    path = ui_targets_path(device_id, data_root)
    if not path.exists():
        return {"schemaVersion": ARCHIVE_SCHEMA_VERSION, "deviceId": device_id,
                "updatedAt": None, "entries": {}, "audit": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        # 档案损坏按空处理是危险的：静默清空会让已启用目标凭空消失。
        raise UiTargetError(
            "archive_corrupted",
            f"固定目标档案损坏（{path.name}）：请人工修复或删除后重新登记")
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        raise UiTargetError("archive_corrupted", "固定目标档案结构无效：entries 必须是对象")
    data.setdefault("schemaVersion", ARCHIVE_SCHEMA_VERSION)
    data.setdefault("deviceId", device_id)
    data.setdefault("audit", [])
    return data


def _save_archive(device_id: str, archive: dict, data_root: Path | None = None) -> None:
    archive["schemaVersion"] = ARCHIVE_SCHEMA_VERSION
    archive["deviceId"] = device_id
    archive["updatedAt"] = cb.now_iso()
    path = ui_targets_path(device_id, data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(archive, ensure_ascii=False, indent=2)
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


def _append_audit(archive: dict, op: str, target_id: str, operator: str, detail: dict) -> None:
    archive.setdefault("audit", []).append({
        "ts": cb.now_iso(), "op": op, "targetId": target_id,
        "operator": operator or "unknown", "detail": detail,
    })


# ---------- 绑定快照与失效判定 ----------

def binding_snapshot(bundle: dict | None) -> dict:
    """登记时冻结的绑定事实（规格书 §3.2 目标条目绑定字段）。

    bundle 缺失（None）时按"全空快照"处理——已登记目标相对空快照必然
    失配，调用方据此判"已失效"，而不是崩溃或误判绑定一致。
    """
    bundle = bundle or {}
    frame = bundle.get("frame") or {}
    return {
        "calibrationFingerprint": bundle.get("bindingFingerprint"),
        "geometryHash": cb.profile_hash(bundle, keys=GEOMETRY_HASH_KEYS),
        "daemonSession": bundle.get("daemonSession"),
        "frame": {"width": frame.get("width"), "height": frame.get("height")},
        "screenOrientation": (bundle.get("screen") or {}).get("orientation"),
    }


def binding_mismatches(entry: dict, bundle: dict) -> list[str]:
    """当前档案与登记快照的失配清单（空 = 绑定仍一致）。"""
    current = binding_snapshot(bundle)
    checks = [
        ("calibrationFingerprint", "绑定指纹变化（设备/摄像头/分辨率换绑）"),
        ("geometryHash", "校准几何变化（重新标定）"),
        ("daemonSession", "daemon 会话变化（机械臂重启/串口重连）"),
        ("frame", "摄像头帧尺寸变化"),
        ("screenOrientation", "屏幕方向变化"),
    ]
    mismatches = []
    for key, display in checks:
        if entry.get(key) != current[key]:
            mismatches.append(f"{key}：{display}")
    return mismatches


def _require_ready_bundle(device_id: str, data_root: Path | None) -> dict:
    """登记门禁（§3.3 规则 1）：必须在自动校准完成（bundle=ready）之后。"""
    bundle = cb.load_bundle(device_id, data_root)
    if bundle is None:
        raise UiTargetError("calibration_not_ready",
                            "设备尚未生成统一校准档案：请先完成自动校准再登记固定目标")
    if bundle.get("status") != "ready":
        raise UiTargetError(
            "calibration_not_ready",
            f"统一校准档案状态为 {bundle.get('status') or '未知'}（非 ready）："
            "固定目标只能在自动校准完成并验收后登记")
    return bundle


# ---------- 登记入口（唯一写入路径，调试台专用） ----------

def validate_target_id(target_id: str) -> None:
    if target_id in RESERVED_DYNAMIC_TARGET_IDS:
        raise UiTargetError(
            "invalid_target_id",
            "conversation_item 是动态会话目标（需摄像头感知坐标），不能登记为固定目标")
    if target_id in PRESET_TARGET_IDS:
        return
    if not TARGET_ID_PATTERN.match(target_id or ""):
        raise UiTargetError(
            "invalid_target_id",
            "目标 ID 必须匹配 [a-z][a-z0-9_]{1,63}（小写蛇形，2~64 位）")


def register_draft(device_id: str, target_id: str, *, nx: float, ny: float,
                   target_mode: str | None = None, name_display: str | None = None,
                   owner_plugin: str | None = None, required_ui_state: str | None = None,
                   risk_level: str | None = None, operator: str = "",
                   data_root: Path | None = None,
                   registration_window: dict | None = None) -> dict:
    """保存/更新一个目标草稿（画面坐标来自调试台「仅标记」的 lastMark）。

    任何修改（含已启用目标的再标记）都回到草稿并递增 version——
    validated 只能源于真实测试与操作者确认（§3.2 规则）。

    registration_window（校准验收期登记窗口，由向导提供）：
    E 验收发生在 promote（ready）之前，删除键必须能在验收期先登记。
    窗口内放行登记（要求活动档案存在，状态不限），条目标记 bindPending，
    绑定快照为占位——promote/手动完成时由 rebind_pending 统一 re-stamp，
    避免 promote 改写几何字段导致快照必然失配。
    """
    validate_target_id(target_id)
    try:
        nx = float(nx); ny = float(ny)
    except (TypeError, ValueError):
        raise UiTargetError("invalid_position", "画面位置 nx/ny 必须是数字")
    if not 0 <= nx <= 1 or not 0 <= ny <= 1:
        raise UiTargetError("invalid_position", "画面位置必须是 0~1 的归一化坐标")
    bind_pending = bool(registration_window)
    if bind_pending:
        bundle = cb.load_bundle(device_id, data_root)
        if bundle is None:
            raise UiTargetError("calibration_not_ready",
                                "设备尚未生成统一校准档案：校准验收期登记也需要活动档案存在")
    else:
        bundle = _require_ready_bundle(device_id, data_root)
    preset = target_id in PRESET_TARGET_IDS
    if preset:
        target_mode = DEFAULT_TARGET_MODES[target_id]
        name_display = TARGET_DISPLAY_NAMES[target_id]
        owner_plugin = owner_plugin or "wx-wechat-adapter"
    else:
        if target_mode not in VALID_TARGET_MODES:
            raise UiTargetError("invalid_target_mode",
                                f"自定义目标必须显式指定动作模式 tap 或 hover_only（收到 {target_mode!r}）")
        if not name_display or not str(name_display).strip():
            raise UiTargetError("invalid_name_display", "自定义目标必须填写显示名称")
        if not owner_plugin or not str(owner_plugin).strip():
            raise UiTargetError("invalid_owner_plugin", "自定义目标必须填写所属插件")
        name_display = str(name_display).strip()
    archive = load_archive(device_id, data_root)
    old = archive["entries"].get(target_id) or {}
    entry = {
        "targetId": target_id,
        "nameDisplay": name_display,
        "nx": round(nx, 4), "ny": round(ny, 4),
        "targetMode": target_mode,
        "source": "manual_mark",
        "state": STATE_DRAFT,
        "ownerPlugin": owner_plugin,
        "requiredUiState": required_ui_state or None,
        "riskLevel": risk_level or None,
        "version": int(old.get("version") or 0) + 1,
        "markedAt": cb.now_iso(),
        "validatedAt": None,
        "enabledAt": None,
        "testEvidence": None,
        "testFailures": [],
        "preset": preset,
        **binding_snapshot(bundle),
    }
    if bind_pending:
        entry["bindPending"] = True
        entry["registeredVia"] = str((registration_window or {}).get("source") or "unknown")
        if (registration_window or {}).get("runId"):
            entry["registeredAtRun"] = registration_window["runId"]
    archive["entries"][target_id] = entry
    _append_audit(archive, "register_draft", target_id, operator,
                  {"version": entry["version"], "nx": entry["nx"], "ny": entry["ny"],
                   "targetMode": target_mode, "resubmit": bool(old),
                   "registrationWindow": registration_window})
    _save_archive(device_id, archive, data_root)
    return entry


def mark_test_result(device_id: str, target_id: str, *, passed: bool,
                     evidence: dict | None = None, operator: str = "",
                     data_root: Path | None = None) -> dict:
    """受控测试结果落档：passed=True 草稿→已测试待确认；失败保持草稿并留证据。"""
    archive = load_archive(device_id, data_root)
    entry = archive["entries"].get(target_id)
    if entry is None:
        raise UiTargetError("target_not_found", f"目标 {target_id} 尚未登记")
    if entry.get("state") != STATE_DRAFT:
        raise UiTargetError("invalid_state",
                            f"目标 {target_id} 状态为 {STATE_DISPLAY.get(entry.get('state'))}，"
                            "只有草稿可以执行受控测试确认")
    record = {"ts": cb.now_iso(), "operator": operator or "unknown",
              "evidence": evidence or {}}
    if passed:
        entry["state"] = STATE_TESTED
        entry["validatedAt"] = record["ts"]
        entry["testEvidence"] = record
        _append_audit(archive, "test_confirmed", target_id, operator,
                      {"version": entry["version"], "evidence": evidence or {}})
    else:
        entry.setdefault("testFailures", []).append(record)
        _append_audit(archive, "test_failed", target_id, operator,
                      {"version": entry["version"], "evidence": evidence or {}})
    _save_archive(device_id, archive, data_root)
    return entry


def set_enabled(device_id: str, target_id: str, *, enabled: bool,
                operator: str = "", data_root: Path | None = None) -> dict:
    """启用/停用业务调用（§4.2 三个独立动作的第三个）。"""
    archive = load_archive(device_id, data_root)
    entry = archive["entries"].get(target_id)
    if entry is None:
        raise UiTargetError("target_not_found", f"目标 {target_id} 尚未登记")
    state = entry.get("state")
    if enabled:
        if state not in (STATE_TESTED, STATE_DISABLED):
            raise UiTargetError(
                "invalid_state",
                f"目标 {target_id} 状态为 {STATE_DISPLAY.get(state)}："
                "必须先通过受控测试并确认，才能启用业务调用")
        if not entry.get("testEvidence"):
            raise UiTargetError("invalid_state", "目标缺少受控测试证据，不能启用")
        entry["state"] = STATE_ENABLED
        entry["enabledAt"] = cb.now_iso()
    else:
        if state != STATE_ENABLED:
            raise UiTargetError("invalid_state",
                                f"目标 {target_id} 未处于已启用状态，无需停用")
        entry["state"] = STATE_DISABLED
    _append_audit(archive, "enable" if enabled else "disable", target_id, operator,
                  {"version": entry["version"]})
    _save_archive(device_id, archive, data_root)
    return entry


def delete_target(device_id: str, target_id: str, *, operator: str = "",
                  data_root: Path | None = None) -> dict:
    """删除目标条目（审计保留）。预置目标删除后即回到"未登记"。"""
    archive = load_archive(device_id, data_root)
    entry = archive["entries"].pop(target_id, None)
    if entry is None:
        raise UiTargetError("target_not_found", f"目标 {target_id} 尚未登记")
    _append_audit(archive, "delete", target_id, operator,
                  {"version": entry.get("version"), "state": entry.get("state")})
    _save_archive(device_id, archive, data_root)
    return entry


def rebind_pending(device_id: str, final_bundle: dict, *, data_root: Path | None = None,
                   operator: str = "calibration_promote") -> dict:
    """校准提升/手动完成时，把验收期登记（bindPending）条目的绑定快照
    re-stamp 到最终档案。

    只触碰带 bindPending 标记的条目；正常登记（ready 后）的条目保持严格
    失效判定。E 验收先于 promote，期间登记的快照只是占位。
    """
    archive = load_archive(device_id, data_root)
    rebound = []
    for entry in archive["entries"].values():
        if entry.pop("bindPending", None):
            entry.pop("registeredAtRun", None)
            entry.update(binding_snapshot(final_bundle))
            rebound.append(entry["targetId"])
    if rebound:
        _append_audit(archive, "rebind_pending", ",".join(sorted(rebound)), operator,
                      {"count": len(rebound)})
        _save_archive(device_id, archive, data_root)
    return {"rebound": rebound}


def re_stamp_after_recovery(device_id: str, final_bundle: dict, *, data_root: Path | None = None,
                            operator: str = "recovery_attempt") -> dict:
    """F4（第七轮复审）：daemon 会话重置且 A1 复核全过后的 enabled 目标重签。

    与 rebind_pending 的区别：那是验收期 bindPending 草稿占位；本函数处理
    正常登记且已 enabled 的目标——几何/指纹已由恢复链独立复核（手机四边+
    锚点走位），因此把绑定快照 re-stamp 到新会话，保留 geometryHash 语义
    由 binding_snapshot 全量重算。未复核会话前的调用方无权调本函数。
    """
    archive = load_archive(device_id, data_root)
    stamped = []
    for entry in archive["entries"].values():
        if entry.get("state") != STATE_ENABLED:
            continue
        entry.update(binding_snapshot(final_bundle))
        stamped.append(entry["targetId"])
    if stamped:
        _append_audit(archive, "rebind_recovery", ",".join(sorted(stamped)), operator,
                      {"count": len(stamped)})
        _save_archive(device_id, archive, data_root)
    return {"stamped": stamped}


# ---------- 运行时解析（业务只读；规格书 §5.2 门禁） ----------

def load_entry(device_id: str, target_id: str,
               data_root: Path | None = None) -> dict | None:
    """读取单个目标条目；未登记返回 None（调用方回落旧档案链路）。"""
    try:
        archive = load_archive(device_id, data_root)
    except UiTargetError:
        # 档案损坏：宁可拒绝也不能当作"未登记"回落旧坐标。
        raise
    return archive["entries"].get(target_id)


def resolve_entry(entry: dict, bundle: dict, *, require_enabled: bool = True) -> dict:
    """门禁通过后返回旧档案格式的目标条目（供 target_archive.resolve_target 换算）。

    门禁（§5.2）：绑定一致 + 已启用。绑定失配=已失效（待复核），硬拒绝；
    未启用（草稿/待确认/已停用）同样拒绝——业务运行时只能用"已启用"目标。
    require_enabled=False 仅供调试台受控测试（草稿态）换算坐标，
    绑定门禁在任何路径都强制。
    """
    mismatches = binding_mismatches(entry, bundle)
    if entry.get("bindPending"):
        raise UiTargetError(
            "target_not_enabled",
            f"目标 {entry.get('targetId')} 在校准验收期登记、绑定尚未生效"
            "（待校准提升后自动生效）")
    if mismatches:
        raise UiTargetError(
            "target_stale",
            f"目标 {entry.get('targetId')} 已失效（待复核）：{'；'.join(mismatches)}。"
            "请到电机调试台重新标记并完成受控测试后再启用")
    if require_enabled and entry.get("state") != STATE_ENABLED:
        raise UiTargetError(
            "target_not_enabled",
            f"目标 {entry.get('targetId')}（{entry.get('nameDisplay')}）"
            f"状态为 {STATE_DISPLAY.get(entry.get('state'), entry.get('state'))}，"
            "未启用业务调用：请到电机调试台完成受控测试与启用")
    return {
        "name_display": entry.get("nameDisplay"),
        "nx": float(entry["nx"]), "ny": float(entry["ny"]),
        "targetMode": entry.get("targetMode") or TARGET_MODE_TAP,
        "target_source": "ui_fixed_target",
        "ui_target_version": entry.get("version"),
    }


# ---------- 调试台列表视图（§4.4 状态展示） ----------

def entry_view(entry: dict, bundle: dict | None) -> dict:
    """目标条目 + 派生状态/绑定复核结果（供页面展示，不做门禁）。

    bundle 缺失（被删/未生成）时一律按"已失效（待复核）"展示——
    没有校准事实就没有可用目标。bindPending（验收期登记）条目在提升前
    展示为"待校准完成"，不按失配判已失效（快照是占位）。
    """
    view = dict(entry)
    if view.pop("bindPending", None):
        view["derivedStatus"] = "草稿（待校准提升后生效）"
        view["businessUsable"] = False
        return view
    stale = binding_mismatches(entry, bundle or {})
    if stale:
        view["derivedStatus"] = "已失效（待复核）"
        view["staleReasons"] = stale
        view["businessUsable"] = False
    elif entry.get("state") == STATE_ENABLED:
        view["derivedStatus"] = STATE_DISPLAY[STATE_ENABLED]
        view["businessUsable"] = True
    else:
        view["derivedStatus"] = STATE_DISPLAY.get(entry.get("state"), "未知")
        view["businessUsable"] = False
    return view


def list_view(device_id: str, data_root: Path | None = None,
              registration_window: dict | None = None) -> dict:
    """调试台列表：注册门禁 + 全部目标（含派生状态与实时换算机械坐标）。

    registration_open = 档案 ready 或校准验收期登记窗口开启（E 验收先于
    promote，删除键等验收目标需在窗口内先登记，提升后绑定生效）。
    """
    from drivers import target_archive

    bundle = cb.load_bundle(device_id, data_root)
    bundle_ready = bundle is not None and bundle.get("status") == "ready"
    archive = load_archive(device_id, data_root)
    views: dict[str, dict] = {}
    robot = {"x": None, "y": None}
    for target_id, entry in sorted(archive["entries"].items()):
        view = entry_view(entry, bundle)
        if bundle_ready:
            try:
                profile = target_archive.load_profile(device_id, data_root)
                resolved = target_archive.resolve_target(view, bundle, profile)
                view["robotX"] = resolved["robotX"]
                view["robotY"] = resolved["robotY"]
                robot = {"x": resolved["robotX"], "y": resolved["robotY"]}
            except Exception:
                view["robotX"] = view["robotY"] = None
        else:
            view["robotX"] = view["robotY"] = None
        views[target_id] = view
    return {
        "deviceId": device_id,
        "registrationOpen": bundle_ready or bool(registration_window),
        "registrationWindow": registration_window,
        "bundleStatus": (bundle or {}).get("status"),
        "presets": {name: {"nameDisplay": TARGET_DISPLAY_NAMES[name],
                           "targetMode": DEFAULT_TARGET_MODES[name]}
                    for name in PRESET_TARGET_IDS},
        "targets": views,
        "auditTail": archive.get("audit", [])[-30:],
    }
