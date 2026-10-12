"""S3（启用前剩余修复 2026-10-08）：动态聊天行最终版本与绑定校验，驱动级反例。

执行单：docs/2026-10-08-启用前剩余修复执行与验收.md §4-S3 与 §3 统一几何规则。
修前红态见 reports/.../remaining-review-TGN2fx/standards/test_dynamic_final_binding.py
（1 过/2 红）。本文件覆盖 S3-01..04 的驱动级部分；S3-05 插件整链见
test_pre_enable_s3_plugin_chain.py。

隔离：沿用 tests.test_layout_observation.offline_only 严格 offline guard；
真实布局匹配/投影/真实 DaemonArmDriver 保留；替身仅：进程内假传输
（tests.test_layout_target_driver.Transport）、假 OCR（TextEngine）、
假姿态门/假运动验证（target_rig 内联替身）。临时 SQLite，零现场 IO。
"""
from __future__ import annotations

import asyncio
import copy
import time

import pytest

from adapters.vision.ocr import OcrEngineError, OcrWord
from drivers.arm import DriverError
from tests.test_layout_observation import offline_only  # noqa: F401
from tests.test_layout_target_driver import motion, target_rig
from tests.test_layout_text_location import TextEngine


def locate(rig, *, target_id="conversation_item", text="Ann"):
    """真实共享布局 OCR 定位一行，构造与插件相同形态的动态目标请求。"""
    rig.camera.ocr_engine = TextEngine()
    located = asyncio.run(rig.camera.locate_text(text, region="account", exact=True))
    assert located["found"] is True
    return {
        "target_id": target_id,
        "target_ref": {"source": "camera_ocr", **located},
        "_layout_binding": copy.deepcopy(
            rig.runtime.layout_observation.execution_binding("arm-01")),
    }


def select_adaptation_v3(rig):
    updated = copy.deepcopy(rig.parts["ops_svc"].resolve_adaptation("arm-01", 2))
    updated["version"] = 3
    rig.select(updated)


def change_region_pointer(rig):
    rig.parts["region_store"].cas_pointer("arm-01", 1, 2, "f" * 64, "offline-change")


def bump_device_binding(rig):
    rig.runtime.devices.get("arm-01")["binding_revision"] += 1


FAULTS = {"adaptation": select_adaptation_v3,
          "region": change_region_pointer,
          "binding": bump_device_binding}


# ---------- S3-01：合法例 + 走位后/接触前失效零接触 ----------

def test_unchanged_dynamic_row_control(target_rig):
    rig = target_rig
    result = asyncio.run(rig.arm.click(locate(rig)))
    assert result["target_source"] == "camera_ocr"
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


def test_fresh_recheck_after_slow_travel_still_succeeds(target_rig):
    """走位后复核（1 秒窗口）在复核成功时不误伤合法动态点击。"""
    rig = target_rig
    rig.transport.after_send = lambda line: (
        time.sleep(1.05) if line.startswith("XY ") else None)
    result = asyncio.run(rig.arm.click(locate(rig)))
    assert result["ok"] is True
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


@pytest.mark.parametrize("fault", ["adaptation", "region", "binding"])
def test_dynamic_row_invalidated_after_xy_keeps_travel_zero_contact(
        target_rig, fault):
    """已走 XY 后换适配/粗框/设备绑定 → 拒绝接触，保留已走位事实，零 TAP。"""
    rig = target_rig
    target = locate(rig)
    changed = []

    def after(line):
        if not line.startswith("XY "):
            return
        FAULTS[fault](rig)
        changed.append(fault)

    rig.transport.after_send = after
    with pytest.raises(DriverError) as caught:
        asyncio.run(rig.arm.click(target))
    assert changed == [fault]
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))
    # 已走位、未接触：不得再标 motion_not_started。
    assert getattr(caught.value, "motion_not_started", False) is False


@pytest.mark.parametrize("fault", ["adaptation", "region", "binding"])
def test_dynamic_row_invalidated_in_transport_lock_zero_contact(
        target_rig, fault):
    """传输互斥锁内、sendall 前换适配/粗框/设备绑定 → 最终 guard 拒绝，零 TAP。"""
    rig = target_rig
    target = locate(rig)
    changed = []

    def before(line):
        if not line.startswith("TAP ") or changed:
            return
        FAULTS[fault](rig)
        changed.append(fault)

    rig.transport.before_send = before
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert changed == [fault]
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


@pytest.mark.parametrize("fault", ["none", "adaptation", "region", "binding"])
@pytest.mark.parametrize("action", ["click", "double_tap"])
def test_dynamic_final_guard_runs_inside_actual_transport_lock(target_rig, fault, action):
    from drivers.daemon_arm import DaemonTCPTransport

    rig = target_rig
    target = locate(rig)
    responder = rig.transport

    class Socket:
        pending = b""

        def settimeout(self, timeout):
            pass

        def sendall(self, data):
            assert transport._lock.locked()
            self.pending = (responder.cmd(data.decode().strip()) + "\n").encode()

        def recv(self, size):
            data, self.pending = self.pending[:size], self.pending[size:]
            return data

        def shutdown(self, how):
            pass

        def close(self):
            pass

    transport = DaemonTCPTransport("127.0.0.1:1")
    transport._sock = Socket()
    original = transport.cmd
    contacts = []

    def cmd(line, *, timeout=None, input_claim=None):
        if line.startswith(("TAP ", "TAP2 ")):
            assert input_claim and callable(input_claim.get("geometry_gate"))
            gate = input_claim["geometry_gate"]

            def mutate_then_guard():
                assert transport._lock.locked()
                contacts.append(line)
                if fault != "none":
                    FAULTS[fault](rig)
                gate()

            input_claim = {**input_claim, "geometry_gate": mutate_then_guard}
        return original(line, timeout=timeout, input_claim=input_claim)

    transport.cmd = cmd
    rig.arm._transport = transport
    try:
        if fault == "none":
            assert asyncio.run(getattr(rig.arm, action)(target))["ok"] is True
            assert len([line for line in motion(rig) if line.startswith("TAP")]) == 1
        else:
            with pytest.raises(DriverError) as raised:
                asyncio.run(getattr(rig.arm, action)(target))
            assert getattr(raised.value, "motion_not_started", False) is False
            assert any(line.startswith("XY ") for line in motion(rig))
            assert not any(line.startswith("TAP") for line in motion(rig))
        assert len(contacts) == 1
        assert not transport._lock.locked()
    finally:
        transport.close()


@pytest.mark.parametrize("fault", ["adaptation", "region", "binding"])
def test_dynamic_row_invalidated_before_parse_never_moves(target_rig, fault):
    """The original locating context must survive until parsing, including binding."""
    rig = target_rig
    target = locate(rig)
    FAULTS[fault](rig)
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == []


def test_expired_original_location_cannot_borrow_fresh_frame_time(target_rig):
    rig = target_rig
    target = locate(rig)
    time.sleep(2.1)
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == []


@pytest.mark.parametrize("fault", ["moved", "duplicate", "missing"])
def test_same_page_row_is_rechecked_after_travel(target_rig, fault):
    rig = target_rig
    target = locate(rig)
    changed = []

    def after(line):
        if not line.startswith("XY ") or changed:
            return
        words = {
            "moved": [OcrWord("Ann", 12, 120, 40, 16, .99)],
            "duplicate": [OcrWord("Ann", 12, 20, 40, 16, .99),
                          OcrWord("Ann", 12, 120, 40, 16, .99)],
            "missing": [],
        }[fault]
        rig.source.image[300:320, 130:175] = 90
        rig.camera.ocr_engine = TextEngine(words=words)
        changed.append(True)

    rig.transport.after_send = after
    with pytest.raises(DriverError) as caught:
        asyncio.run(rig.arm.click(target))
    assert changed
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))
    assert not getattr(caught.value, "motion_not_started", False)


@pytest.mark.parametrize("fault", ["adaptation", "region", "binding"])
def test_dynamic_row_invalidated_before_first_motion_zero_motion(target_rig, fault):
    """传输锁内、首条动作指令 sendall 前换适配/粗框/设备绑定 → 完全未动。"""
    rig = target_rig
    target = locate(rig)
    changed = []

    def before(line):
        if not changed:
            FAULTS[fault](rig)
            changed.append(fault)

    rig.transport.before_send = before
    with pytest.raises(DriverError) as caught:
        asyncio.run(rig.arm.click(target))
    assert changed == [fault]
    assert motion(rig) == []
    assert getattr(caught.value, "motion_not_started", False) is True


@pytest.mark.parametrize("fault", ["adaptation", "binding"])
def test_dynamic_row_without_task_binding_still_pinned_and_rejected(
        target_rig, fault):
    """客户端不带 _layout_binding 也不能绕过：驱动现场固定快照后照样拒绝。"""
    rig = target_rig
    target = locate(rig)
    target.pop("_layout_binding")
    changed = []

    def after(line):
        if not line.startswith("XY ") or changed:
            return
        FAULTS[fault](rig)
        changed.append(fault)

    rig.transport.after_send = after
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert changed == [fault]
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


def test_double_tap_dynamic_row_rejected_at_contact_keeps_travel(target_rig):
    """TAP2 接触前换设备绑定 → 双击零接触，已走位事实保留。"""
    rig = target_rig
    target = locate(rig)

    def before(line):
        if line.startswith("TAP2 "):
            bump_device_binding(rig)

    rig.transport.before_send = before
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.double_tap(target))
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


# ---------- S3-03：来源时效/缺来源/重放/观测变化 ----------

def test_dynamic_row_without_source_sha_rejected_before_motion(target_rig):
    """缺必要来源（frameSha256）的裸坐标 → 解析前明确拒绝，零动作。"""
    rig = target_rig
    target = locate(rig)
    target["target_ref"].pop("frameSha256")
    with pytest.raises(DriverError, match="frameSha256"):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == []


def test_dynamic_row_replayed_frame_seq_rejected(target_rig):
    """定位帧序超前于当前推流（重放/回拨证据）→ 拒绝，旧行坐标不得续鲜。"""
    rig = target_rig
    target = locate(rig)
    target["target_ref"]["frameSeq"] = 10 ** 9
    with pytest.raises(DriverError, match="帧序"):
        asyncio.run(rig.arm.click(target))
    assert motion(rig) == []


def test_dynamic_row_re_shot_generation_after_xy_zero_contact(target_rig):
    """重拍证据换了来源代际：新流不得让旧行坐标自动续鲜 → 零接触。"""
    rig = target_rig
    rig.source.last_source_generation_id = "re-shot-generation"
    target = locate(rig)
    changed = []

    def after(line):
        if not line.startswith("XY ") or changed:
            return
        rig.source.last_source_generation_id = "re-shot-generation-2"
        changed.append(True)

    rig.transport.after_send = after
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert changed == [True]
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


def test_dynamic_row_page_changed_after_xy_zero_contact(target_rig):
    """走位后页面已变化 → 不能唯一确认旧落点，拒绝接触。"""
    rig = target_rig
    target = locate(rig)

    def after(line):
        if line.startswith("XY "):
            rig.source.image = rig.frame("chat_ime_hidden")

    rig.transport.after_send = after
    with pytest.raises(DriverError):
        asyncio.run(rig.arm.click(target))
    assert any(cmd.startswith("XY ") for cmd in motion(rig))
    assert not any(cmd.startswith("TAP") for cmd in motion(rig))


def test_new_observation_recovers_after_rejected_attempt(target_rig):
    """拒绝后，合法新任务/新观测（重新定位）仍通过；旧点不被复用。"""
    rig = target_rig
    stale = locate(rig)
    select_adaptation_v3(rig)
    with pytest.raises(DriverError):
        # 解析前适配已换：旧任务目标连同旧快照一起拒绝。
        asyncio.run(rig.arm.click(stale))
    assert motion(rig) == []
    fresh = locate(rig)
    result = asyncio.run(rig.arm.click(fresh))
    assert result["ok"] is True
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


def test_duplicate_rows_are_never_located_for_dynamic_click(target_rig):
    """同页同名多行 → 现有定位规则拒绝不唯一目标，驱动侧零动作。"""
    rig = target_rig
    rig.camera.ocr_engine = TextEngine(words=[
        OcrWord("Ann", 12, 20, 40, 16, .99),
        OcrWord("Ann", 12, 120, 40, 16, .99)])
    with pytest.raises(OcrEngineError):
        asyncio.run(rig.camera.locate_text("Ann", region="account", exact=True))
    assert motion(rig) == []


# ---------- S3-04：在用组合逐一可用，未用组合明确拒绝 ----------

def test_mark_read_row_camera_ocr_in_use_combo(target_rig):
    """快速恢复在用组合 mark_read_row＋camera_ocr：真实驱动可执行。"""
    rig = target_rig
    result = asyncio.run(rig.arm.click(locate(rig, target_id="mark_read_row")))
    assert result["target_source"] == "camera_ocr"
    assert sum(cmd.startswith("TAP ") for cmd in motion(rig)) == 1


def test_gesture_point_camera_vision_in_use_combo_with_provider(target_rig):
    """调试台在用组合 gesture_point＋camera_vision（带布局解析器）仍可执行。"""
    rig = target_rig
    result = asyncio.run(rig.arm.double_tap({
        "target_id": "gesture_point", "intervalMs": 120,
        "target_ref": {"source": "camera_vision", "cameraX": 320, "cameraY": 240,
                       "frameW": 1280, "frameH": 720}}))
    assert result["target_display"] == "视觉手势点"
    assert sum(cmd.startswith("TAP2 ") for cmd in motion(rig)) == 1


@pytest.mark.parametrize("target_id,source", [
    ("conversation_item", "camera_vision"),
    ("conversation_item", "vision_back_button"),
    ("mark_read_row", "camera_vision"),
    ("mark_read_row", "vision_back_button"),
    ("gesture_point", "camera_ocr"),
    ("gesture_point", "vision_back_button"),
])
def test_unused_dynamic_combinations_are_explicitly_rejected(
        target_rig, target_id, source):
    rig = target_rig
    ref = {"cameraX": 320, "cameraY": 240, "frameW": 1280, "frameH": 720}
    if source == "camera_ocr":
        ref.update(frameSha256="a" * 64, frameSeq=1,
                   coordinateVersion="calibrated_camera.v1")
    with pytest.raises(DriverError, match="未被产品使用"):
        asyncio.run(rig.arm.click({
            "target_id": target_id,
            "target_ref": {"source": source, **ref}}))
    assert motion(rig) == []
