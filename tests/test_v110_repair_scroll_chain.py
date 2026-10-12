"""v1.10 修复单 FIX-07/FIX-08 验收（工作包 B：相机与运动）。

FIX-07（自动校准实际产出可验证 v3）：
- F07-1：fake 相机/驱动跑完整 wizard 产出真实 v3 bundle，再经正式任务→插件
  →真实驱动完成恰一次滑动；仅相机登记 revision 变化仍通过；安装身份
  （cameraBindingId）/来源几何（sourceGeometryId）变化拒绝。
- F07-2：不执行任何输入/删除验收（deferred）也完成机械校准；E 文字任务计数 0。

FIX-08（单次动作连续光流链）：
- F08-1：机械动作未返回时已采 during 帧；一次 swipe、一个租约、底层并发读
  峰值=1；归档次数与物理读一一对应（无隐式补拍）。
- F08-2：断帧/换 generation/错方向/无稳定尾→unknown 持久暂停；重复 seq 只记
  诊断不进链；未知后设备被持锁，不补滑。
- F08-3：静止合格链只能 stationary（failed/scroll_no_displacement）；机械完成
  视觉失败保留 mechanical confirmed 真事实，不自动 home。
- F08-4：预算超限/无法证明→零滑动；未认领前重取过旧前帧，成功后仅认领一次。

全部使用临时目录、fake 相机/驱动与真实 Runtime/插件；不连接现场硬件。
"""
from __future__ import annotations

import asyncio
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from drivers import calibration_bundle as cb, screen_gestures
from runtime.calibrated_scroll import (
    CONTRACT_VERSION, SAMPLING_SUPPORT, binding_view, capability_view,
    motion_budget,
)
from runtime.errors import ApiError
from tests.helpers import make_device_configs
from tests.test_calibrated_scroll import (
    SCROLL_PLUGIN_SOURCE, _lease, _stable_session, _submit, _sync_session,
    attach_simulated_source_identity,
)
from tests.test_calibration_wizard import (
    auto_ready_env, wait_auto, CAM_W, CAM_H,
)
from tests.test_deferred_input_acceptance import motion_bundle

PLUGIN_ID = "screen-gesture-adapter"

# 安装 UUID（小写）与来源几何摘要（64 位小写 SHA-256）——由测试内可信绑定
# 事实提供，与 FIX-06 帧桥将提供的字段同构。
INSTALL_ID = "0afec001-1111-4111-8111-0000000000f7"
GEOMETRY_ID = "b" * 64


# ---------------------------------------------------------------------------
# 夹具：小帧仿真运行时（320x240，渲染 ~45ms，采样节奏确定）
# ---------------------------------------------------------------------------

CAMERA_TO_SCREEN_SMALL = [1080 / 320, 0, 0, 0, 2400 / 240, 0, 0, 0, 1]
SCREEN_CORNERS_SMALL = [[64, 24], [256, 24], [256, 216], [64, 216]]


def v3_bundle_small(*, robot_scale: float = 8.0,
                    install_id: str = INSTALL_ID,
                    geometry_id: str = GEOMETRY_ID) -> dict:
    """v3 档案：完整 v2 机械证据 + v3 安装/几何身份 + 小帧几何。

    robot_scale=e 系数决定机械行程：8.0 → 135mm（时长 2701ms，预算 4301ms
    within_window）；7.2 → 150mm（时长 3001ms，预算 4601ms exceeds_window）。
    """
    bundle = motion_bundle()
    bundle["schemaVersion"] = cb.BUNDLE_SCHEMA_VERSION_V3
    bundle["cameraBindingId"] = install_id
    bundle["sourceGeometryId"] = geometry_id
    bundle["camera"]["cameraToScreen"]["homography"] = list(CAMERA_TO_SCREEN_SMALL)
    bundle["screen"]["edgeModel"]["corners"] = [list(c) for c in SCREEN_CORNERS_SMALL]
    bundle["xy"]["robotToScreen"]["e"] = robot_scale
    bundle["frame"] = {"width": 320, "height": 240}
    bundle["bindingFingerprint"] = cb.binding_fingerprint(
        device_id=bundle["deviceId"], camera_id=bundle["cameraId"],
        camera_device_unique_id=bundle["cameraDeviceUniqueId"],
        daemon_addr=bundle["daemonAddr"], frame_width=320, frame_height=240)
    return bundle


async def _make_runtime(tmp_path, bundle: dict):
    """真实 Runtime + screen-gesture-adapter 插件 + 档案落盘（v3 直存）。"""
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime

    config = PlatformConfig(
        {"db_path": str(tmp_path / "fleet.db"), "evidence_dir": str(tmp_path / "evidence"),
         "plugin_dir": str(tmp_path / "plugins"), "hardware_mode": "simulation"},
        root=tmp_path)
    runtime = ArmFleetRuntime(config)
    runtime.plugins.install(SCROLL_PLUGIN_SOURCE, operator_id="操作员甲", reason="测试安装")
    runtime.plugins.activate(PLUGIN_ID, operator_id="操作员甲", reason="测试启用")
    await runtime.startup(device_configs=make_device_configs(1))
    root = tmp_path / "calibration"
    cb.save_bundle(bundle, root)
    runtime.devices.driver("arm-01").data_root = root
    device = runtime.devices.get("arm-01")
    device.update({
        "camera_id": bundle["cameraId"],
        "camera_device_unique_id": bundle["cameraDeviceUniqueId"],
        "daemon_session": bundle["daemonSession"]})
    camera = runtime.devices.camera("arm-01")
    camera.configure_simulation_frame(
        bundle["frame"]["width"], bundle["frame"]["height"],
        screen_corners=[tuple(c) for c in bundle["screen"]["edgeModel"]["corners"]])
    _sync_session(runtime, root)
    attach_simulated_source_identity(runtime, bundle)
    return runtime, root, bundle


def _payload(binding, action="swipe_up", task_id="scroll-1"):
    return {"task_id": task_id, "task_type": "calibrated_scroll",
            "contract_version": CONTRACT_VERSION, "device_id": "arm-01",
            "idempotency_key": task_id, "correlation_id": "corr-scroll",
            "expires_at": "2099-01-01T00:00:00Z",
            "payload": {"action": action, "binding": deepcopy(binding)}}


def _counting_swipe(runtime, *, scroll: int | None = 8, delay: float = 0.0,
                    side_effect=None, camera_ops=None):
    """包一层驱动 swipe：计数、注入内容滚动/延迟/副作用（仿真联动）。

    scroll 是"与上滑预期方向一致"的内容位移幅值：按手势起终点自动翻转
    符号（上滑 dy<0、下滑 dy>0），方向失配用例显式传反号。
    """
    driver = runtime.devices.driver("arm-01")
    camera = runtime.devices.camera("arm-01")
    calls = {"n": 0}
    original = driver.swipe

    async def swipe(gesture=None, *args, **kwargs):
        calls["n"] += 1
        if scroll:
            going_up = True
            if isinstance(gesture, dict) and {"start", "end"} <= set(gesture):
                going_up = float(gesture["start"]["y"]) > float(gesture["end"]["y"])
            try:
                camera.scroll_by(scroll if going_up else -scroll)
            except Exception:
                pass
        if camera_ops is not None:
            try:
                camera_ops()
            except Exception:
                pass
        if side_effect is not None:
            outcome = side_effect(calls)
            if asyncio.iscoroutine(outcome):
                await outcome
        if delay:
            await asyncio.sleep(delay)
        return await original(*([gesture] if gesture is not None else []),
                              *args, **kwargs)

    driver.swipe = swipe
    return calls


def _detail(runtime, task_id):
    stored = runtime.get_task(task_id)
    for step in reversed(stored.get("step_log") or []):
        if step.get("detail"):
            return step["detail"]
    return {}


def _chain_frames(runtime, task_id):
    records = [r for r in runtime.evidence.for_task(task_id)
               if (r.get("sampling") or {}).get("broker_seq") is not None]
    return records


def _pause_state(runtime, detail):
    """R2-07：视觉暂停已迁入滑动动作存储（scroll_action_records 表）——
    旧 KV 旁路键（scroll_pause:*，无读取方）删除。"""
    issue_id = detail.get("pause_issue_id")
    if not issue_id:
        return None
    from runtime.calibrated_scroll import get_scroll_action_store

    store = get_scroll_action_store(runtime)
    for record in store.issues("arm-01"):
        if record.get("pause_issue_id") == issue_id:
            return {
                "pause_issue_id": issue_id,
                "reason": record.get("result", {}).get("pause_reason")
                or record.get("reason_code"),
                "pause_scope": record.get("pause_scope"),
                "mechanical_status": record.get("mechanical_status"),
                "state": record["state"],
            }
    return None


# ---------------------------------------------------------------------------
# F07-1：wizard 产真实 v3 → 正式任务 → 插件 → 驱动恰一次滑动
# ---------------------------------------------------------------------------

# 向导 fake 的手机四角（内缩到可被真实轮廓检测的边距）与同域的
# 屏幕→相机映射：绿色标靶、档案 edgeModel、运行时仿真渲染共用这一几何。
WIZARD_QUAD = [[180.0, 45.0], [780.0, 45.0], [780.0, 495.0], [180.0, 495.0]]


def _cam_into_quad(sx, sy):
    import tests.test_calibration_wizard as tw

    x0, y0 = WIZARD_QUAD[0]
    x1, y2 = WIZARD_QUAD[2]
    return (x0 + sx * (x1 - x0) / tw.SCREEN_W, y0 + sy * (y2 - y0) / tw.SCREEN_H)


def _wizard_v3_env(tmp_path, monkeypatch):
    """fake 相机/驱动跑完整自动校准，绑定事实携带安装/几何身份 → 真实 v3。"""
    import tests.test_calibration_wizard as tw
    from types import SimpleNamespace

    def git_revision(args, **kwargs):
        assert args == ["git", "rev-parse", "HEAD"]
        return SimpleNamespace(stdout="offline-fixture\n")

    # Only revision metadata is synthetic; the wizard and binding facts stay real.
    monkeypatch.setattr("subprocess.run", git_revision)

    env = auto_ready_env(tmp_path)
    # 手机四角与标靶映射一致地内缩：档案 edgeModel = 仿真渲染四角 = 可检测轮廓
    monkeypatch.setattr(tw, "cam", _cam_into_quad)
    env.quad = {"corners": [list(c) for c in WIZARD_QUAD],
                "frameW": CAM_W, "frameH": CAM_H}
    base_facts = dict(env.wizard._binding_facts())
    env.wizard._binding_facts = lambda: {
        **base_facts, "cameraBindingId": INSTALL_ID, "sourceGeometryId": GEOMETRY_ID}
    env.wizard.auto_start()
    wait_auto(env, lambda v: v["state"] == "PROMOTED", desc="提升 motion_ready")
    bundle = cb.load_bundle("arm-01", tmp_path)
    return env, bundle


def test_f07_1_wizard_produces_v3_and_scroll_executes_exactly_once(tmp_path, monkeypatch):
    env, wizard_bundle = _wizard_v3_env(tmp_path, monkeypatch)

    # 真实校准产物：schemaVersion=3 + 安装 UUID + 来源几何 + 完整机械模型
    assert wizard_bundle["schemaVersion"] == 3
    assert wizard_bundle["cameraBindingId"] == INSTALL_ID
    assert wizard_bundle["sourceGeometryId"] == GEOMETRY_ID
    assert wizard_bundle["status"] == cb.MOTION_READY_STATUS
    assert cb.automatic_calibration_verified(wizard_bundle)
    assert ((wizard_bundle.get("camera") or {}).get("cameraToScreen") or {}).get("homography")

    async def scenario():
        runtime = None
        try:
            from runtime.config import PlatformConfig
            from runtime.runtime import ArmFleetRuntime
            config = PlatformConfig(
                {"db_path": str(tmp_path / "fleet.db"),
                 "evidence_dir": str(tmp_path / "evidence"),
                 "plugin_dir": str(tmp_path / "plugins"), "hardware_mode": "simulation"},
                root=tmp_path)
            runtime = ArmFleetRuntime(config)
            runtime.plugins.install(SCROLL_PLUGIN_SOURCE, operator_id="操作员甲",
                                    reason="测试安装")
            runtime.plugins.activate(PLUGIN_ID, operator_id="操作员甲", reason="测试启用")
            await runtime.startup(device_configs=make_device_configs(1))
            # 向导产出的档案就是运行时唯一事实源（同一数据根，不复制不重写）
            runtime.devices.driver("arm-01").data_root = tmp_path
            device = runtime.devices.get("arm-01")
            device.update({"camera_id": wizard_bundle["cameraId"],
                           "camera_device_unique_id": wizard_bundle["cameraDeviceUniqueId"],
                           "daemon_session": wizard_bundle["daemonSession"]})
            camera = runtime.devices.camera("arm-01")
            corners = wizard_bundle["screen"]["edgeModel"]["corners"]
            camera.configure_simulation_frame(
                wizard_bundle["frame"]["width"], wizard_bundle["frame"]["height"],
                screen_corners=[tuple(c) for c in corners])
            attach_simulated_source_identity(runtime, wizard_bundle)
            _sync_session(runtime, tmp_path)
            binding = binding_view(runtime, "arm-01")
            assert binding is not None
            assert binding["camera_binding_id"] == INSTALL_ID
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = _counting_swipe(runtime, scroll=12)

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f07-1")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f07-1")
            assert done["status"] == "confirmed", (done.get("failure_reason"), detail)
            assert detail.get("mechanical_status") == "confirmed"
            assert detail.get("effect_status") == "displaced"
            assert detail.get("direction_consistent") is True
            assert swipes["n"] == 1, "正式链路必须恰一次滑动"
            assert detail.get("frame_chain_evidence"), "必须有连续观察帧链"

            # 仅相机登记 revision 变化：v3 语义比较忽略 revision → 仍通过
            bumped = cb.load_bundle("arm-01", tmp_path)
            bumped["bindingRevision"] = int(bumped["bindingRevision"] or 0) + 5
            cb.save_bundle(bumped, tmp_path)
            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_down",
                                                             task_id="f07-1b")))
            done = await runtime.executor.execute(task["task_id"])
            assert done["status"] == "confirmed", (done.get("failure_reason"),
                                                   _detail(runtime, "f07-1b"))
            assert _detail(runtime, "f07-1b").get("direction_consistent") is True
            assert swipes["n"] == 2

            # 安装身份变化（换相机安装）：旧绑定拒绝，零滑动
            reinstalled = cb.load_bundle("arm-01", tmp_path)
            reinstalled["cameraBindingId"] = "0afec001-1111-4111-8111-0000000000f8"
            cb.save_bundle(reinstalled, tmp_path)
            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f07-1c")))
            done = await runtime.executor.execute(task["task_id"])
            assert done["status"] == "failed"
            assert done.get("error_category") == "binding_changed"
            assert swipes["n"] == 2, "安装变化后不得下发滑动"

            # 来源几何变化：同样拒绝
            regressed = cb.load_bundle("arm-01", tmp_path)
            regressed["cameraBindingId"] = INSTALL_ID
            regressed["sourceGeometryId"] = "c" * 64
            cb.save_bundle(regressed, tmp_path)
            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f07-1d")))
            done = await runtime.executor.execute(task["task_id"])
            assert done["status"] == "failed"
            assert done.get("error_category") == "binding_changed"
            assert swipes["n"] == 2, "几何变化后不得下发滑动"
        finally:
            if runtime is not None:
                await runtime.shutdown()

    asyncio.run(scenario())


def test_f07_2_calibration_completes_without_input_acceptance(tmp_path, monkeypatch):
    """F07-2：延后输入/删除验收完成机械校准；E 文字任务与输入计数为 0。"""
    env, bundle = _wizard_v3_env(tmp_path, monkeypatch)
    assert env.ime.tasks_created == [], "延后路径不得创建任何 E 输入/删除验收任务"
    assert bundle["status"] == cb.MOTION_READY_STATUS
    assert cb.automatic_calibration_verified(bundle)
    assert bundle["acceptance"]["debug"] is False
    assert (bundle["acceptance"]["deferred"] or {}).get("debug") is True
    run_id = bundle["autoCalibration"]["runId"]
    run = env.wizard._auto.get("run")
    candidate = __import__("drivers.calibration_bundle", fromlist=["cb"]).load_candidate(
        "arm-01", run_id, tmp_path)
    assert candidate is not None and candidate.get("deferInputAcceptance") is True
    # 机械校准完成后，凭完整现场事实通过机械门禁（零动作）
    checks = cb.validate_calibration_bundle(
        bundle, device_id="arm-01", daemon_session=bundle["daemonSession"],
        camera_id=bundle["cameraId"],
        camera_device_unique_id=bundle["cameraDeviceUniqueId"],
        binding_revision=bundle["bindingRevision"], daemon_addr=bundle["daemonAddr"],
        frame_width=bundle["frame"]["width"], frame_height=bundle["frame"]["height"])
    assert checks == [], checks
    assert bundle["schemaVersion"] == 3


# ---------------------------------------------------------------------------
# F08-1：动作未返回已采 during；一次 swipe；并发读峰值 1；归档=物理读
# ---------------------------------------------------------------------------

def test_f08_1_during_frames_sampled_while_motion_pending(tmp_path, monkeypatch):
    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = _counting_swipe(runtime, scroll=8, delay=0.55)
            broker = runtime.devices.camera_broker("arm-01")

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f08-1")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f08-1")
            assert done["status"] == "confirmed", (done.get("failure_reason"), detail)
            assert swipes["n"] == 1, "一次动作只允许一次 swipe"

            timeline = detail["timeline"]
            returned_ns = timeline["driver_returned_ns"]
            frames = detail["frame_chain"]
            during_before_return = [f for f in frames
                                    if f["phase"] in ("during", "after")
                                    and f["captured_monotonic_ns"] < returned_ns]
            assert during_before_return, "机械动作未返回时必须已采到 during 帧"
            assert all(f["captured_monotonic_ns"] >= timeline["driver_invoked_ns"]
                       or f["phase"] == "before" for f in frames)
            # 完成栅栏语义：栅栏后至少 3 帧，最后一帧为 after
            fence_ns = timeline["completion_fence_ns"]
            post = [f for f in frames if f["captured_monotonic_ns"] > fence_ns]
            assert len(post) >= 3 and frames[-1]["phase"] == "after"

            # 一个租约一次动作；底层物理读全程并发峰值 = 1
            assert broker.max_concurrent_grabs == 1
            assert broker._active_grabs == 0, "采样结束后不得遗留底层读取"

            # 归档次数与物理读一一对应（每帧唯一 broker_seq，无隐式补拍）
            archived = _chain_frames(runtime, "f08-1")
            seqs = [r["sampling"]["broker_seq"] for r in archived]
            assert len(seqs) == len(set(seqs)) == broker.grab_count
            # 链帧与归档一一对应；诊断帧不冒充链帧
            chain_ids = set(detail["frame_chain_evidence"])
            assert chain_ids <= {r["evidence_id"] for r in archived}
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# F08-2：断帧 / 换代 / 错方向 / 无稳定尾 / 重复 seq / 持久暂停不补滑
# ---------------------------------------------------------------------------

def test_f08_2_frame_gap_returns_unknown_and_pauses(tmp_path, monkeypatch):
    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = _counting_swipe(runtime, scroll=8)
            broker = runtime.devices.camera_broker("arm-01")
            original_capture = broker.capture
            state = {"calls": 0}

            async def stalled_capture():
                state["calls"] += 1
                if state["calls"] == 2:  # 第一个链 tick：读取之间注入 700ms 空档
                    await asyncio.sleep(0.7)
                return await original_capture()

            broker.capture = stalled_capture

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f08-2a")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f08-2a")
            assert done["status"] == "unknown"
            assert detail.get("reason_code") == "frame_gap_exceeded"
            assert detail.get("mechanical_status") == "confirmed", "机械事实不得被视觉失败覆盖"
            assert detail.get("safe_cleanup_confirmed") is True
            assert detail.get("manual_review_required") is True
            pause = _pause_state(runtime, detail)
            assert pause and pause["reason"] == "frame_gap_exceeded"
            assert pause["pause_scope"] == "automatic_actions"
            assert swipes["n"] == 1, "观察失败禁止补滑"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_f08_2_generation_change_returns_unknown(tmp_path, monkeypatch):
    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            # R2-08 契约：代际属于采集源——仿真在渲染现场换代（驱动注入点），
            # broker 侧代际只是诊断身份，不再构成动作链
            camera = runtime.devices.camera("arm-01")
            swipes = _counting_swipe(
                runtime, scroll=8,
                side_effect=lambda calls: camera.restart_source_session())

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f08-2b")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f08-2b")
            assert done["status"] == "unknown"
            assert detail.get("reason_code") == "source_frame_replayed"
            assert detail.get("mechanical_status") == "confirmed"
            generations = {f["generation"] for f in detail["frame_chain"]}
            assert len(generations) == 1, "换 generation 的帧不得进入链"
            assert _pause_state(runtime, detail)["reason"] == "source_frame_replayed"
            assert swipes["n"] == 1
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_f08_2_wrong_direction_returns_unknown_direction_mismatch(tmp_path, monkeypatch):
    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = _counting_swipe(runtime, scroll=-8)  # 内容向下 = 上滑反向

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f08-2c")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f08-2c")
            assert done["status"] == "unknown"
            assert detail.get("flow_status") == "displaced", "客观位移事实保留"
            assert detail.get("direction_consistent") is False
            assert detail.get("mechanical_status") == "confirmed"
            assert detail.get("safe_cleanup_confirmed") is True
            pause = _pause_state(runtime, detail)
            assert pause and pause["reason"] == "direction_mismatch"
            assert swipes["n"] == 1, "方向不符禁止自动反向或补滑"

            # 持久暂停挡住下一动作：新任务拿不到租约或执行被持锁拦截，
            # 无论如何不产生第二次滑动（重启同理，不补滑）
            try:
                task2 = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                                  task_id="f08-2c2")))
                done2 = await runtime.executor.execute(task2["task_id"])
                assert done2["status"] in {"failed", "unknown", "cancelled"}
            except ApiError as exc:
                assert "租用" in str(exc), f"意外的接纳错误：{exc}"
            assert swipes["n"] == 1, "未知问题未核对前，禁止任何新滑动"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_f08_2_unstable_tail_returns_unknown_after_not_settled(tmp_path, monkeypatch):
    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            camera = runtime.devices.camera("arm-01")
            scroller = None

            def start_scroller(_calls=None):
                nonlocal scroller

                async def roll():
                    for _ in range(14):
                        await asyncio.sleep(0.12)
                        camera.scroll_by(3)  # 完成栅栏后惯性滚动不停止

                scroller = asyncio.create_task(roll())

            swipes = _counting_swipe(runtime, scroll=8, side_effect=start_scroller)

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f08-2d")))
            try:
                done = await runtime.executor.execute(task["task_id"])
            finally:
                if scroller is not None:
                    scroller.cancel()
                    try:
                        await scroller
                    except asyncio.CancelledError:
                        pass
            detail = _detail(runtime, "f08-2d")
            assert done["status"] == "unknown"
            assert detail.get("mechanical_status") == "confirmed"
            assert detail.get("settle_tail", {}).get("settled") is False
            assert detail.get("settle_tail", {}).get("completion_fence_ns") is not None
            pause = _pause_state(runtime, detail)
            assert pause and pause["reason"] == "after_not_settled"
            assert swipes["n"] == 1, "尾部未稳禁止补滑"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_f08_2_duplicate_seq_only_diagnostic_not_chain(tmp_path, monkeypatch):
    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = _counting_swipe(runtime, scroll=8)
            camera = runtime.devices.camera("arm-01")
            original_capture = camera.capture_sync
            state = {"calls": 0}

            def replay_capture():
                state["calls"] += 1
                if state["calls"] == 3:
                    # 下一次物理读复用前一帧的源序号（同 seq 伪装新帧）：
                    # 注入点在采样源（渲染现场），不是消费端计数
                    camera._source_capture_seq -= 1
                return original_capture()

            camera.capture_sync = replay_capture

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f08-2e")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f08-2e")
            assert done["status"] == "confirmed", (done.get("failure_reason"), detail)
            dup = [d for d in detail["sampling_diagnostics"]
                   if d.get("result") == "duplicate"]
            assert dup, "重复 seq 必须记录采样诊断"
            chain_ids = set(detail["frame_chain_evidence"])
            assert all(d["evidence_id"] not in chain_ids for d in dup), \
                "重复 seq 不得进入链"
            seqs = [f["seq"] for f in detail["frame_chain"]]
            assert len(seqs) == len(set(seqs)), "链内序号必须严格递增不重复"
            # 重复 seq 不等同质量失败：链仍然成立并成功
            assert detail.get("effect_status") == "displaced"
            assert swipes["n"] == 1
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# F08-3：静止=failed 不设暂停；机械完成视觉失败保留真事实、不自动 home
# ---------------------------------------------------------------------------

def test_f08_3_stationary_chain_maps_to_failed_no_displacement(tmp_path, monkeypatch):
    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = _counting_swipe(runtime, scroll=None)  # 内容纹丝不动

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_down",
                                                             task_id="f08-3a")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f08-3a")
            assert done["status"] == "failed"
            assert done.get("error_category") == "scroll_no_displacement"
            assert detail.get("mechanical_status") == "confirmed"
            assert detail.get("effect_status") == "stationary"
            assert detail.get("settle_tail", {}).get("settled") is True
            assert detail.get("pause_issue_id") is None, "静止不注册视觉未知暂停"
            assert detail.get("manual_review_required") is False
            assert detail.get("retryable") is False
            assert swipes["n"] == 1
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_f08_3_visual_failure_keeps_mechanical_facts_and_never_homes(tmp_path, monkeypatch):
    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = _counting_swipe(runtime, scroll=-8)

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f08-3b")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f08-3b")
            assert done["status"] == "unknown"
            assert detail.get("mechanical_status") == "confirmed", "纯视觉未知不得伪造机械未知"
            assert detail.get("mechanical_status_display") == "机械动作已完成"
            assert detail.get("safe_cleanup_confirmed") is True
            assert detail.get("motion_started") is True
            driver = runtime.devices.driver("arm-01")
            assert not any(entry.get("action") == "home" for entry in driver.action_log), \
                "视觉未知不得自动归位"
            assert swipes["n"] == 1
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# F08-4：预算超限/无法证明 → 零滑动；前帧重取后仅认领一次
# ---------------------------------------------------------------------------

def test_f08_4_budget_exceeded_zero_swipe(tmp_path, monkeypatch):
    """150mm 行程 → 时长 3001ms → 预算 4601ms > 4400ms：查询与执行都拒绝。"""
    async def scenario():
        runtime, root, bundle = await _make_runtime(
            tmp_path, v3_bundle_small(robot_scale=7.2))
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            view = capability_view(runtime, "arm-01")
            assert view["usable"] is False
            for action in ("swipe_up", "swipe_down"):
                assert view["actions"][action]["usable"] is False
                assert view["actions"][action]["motion_budget"]["status"] == "exceeds_window"
                assert view["actions"][action]["motion_budget"]["estimated_total_ms"] == 4601
                codes = {item["code"] for item in view["actions"][action]["blocking_reasons"]}
                assert "scroll_observation_window_insufficient" in codes
            swipes = _counting_swipe(runtime, scroll=None)

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f08-4a")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f08-4a")
            assert done["status"] == "failed"
            assert done.get("error_category") == "scroll_observation_window_insufficient"
            assert detail.get("motion_budget") == {"status": "exceeds_window",
                                                   "estimated_total_ms": 4601}
            assert swipes["n"] == 0, "预算超限必须零动作"
            assert not detail.get("frame_chain_evidence"), "预算拒绝不得开链采样"
            assert detail.get("before_evidence") is None
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_f08_4_unproven_budget_zero_swipe(tmp_path, monkeypatch):
    async def scenario():
        import runtime.calibrated_scroll as cs
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            original_plan = cs.plan_action

            def plan_without_duration(rt, device_id, action):
                plan = original_plan(rt, device_id, action)
                return {k: v for k, v in plan.items() if k != "durationMs"}

            monkeypatch.setattr(cs, "plan_action", plan_without_duration)
            swipes = _counting_swipe(runtime, scroll=None)
            assert motion_budget({"calculated": True}) == {
                "status": "unproven", "estimated_total_ms": None}

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f08-4b")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f08-4b")
            assert done["status"] == "failed"
            assert done.get("error_category") == "scroll_observation_window_insufficient"
            assert detail.get("motion_budget") == {"status": "unproven",
                                                   "estimated_total_ms": None}
            assert swipes["n"] == 0
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_f08_4_stale_before_frame_retaken_then_claimed_once(tmp_path, monkeypatch):
    """未认领前发现前帧过旧：在预检预算内重取；成功后仅认领/滑动一次。"""
    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = _counting_swipe(runtime, scroll=8)
            camera = runtime.devices.camera("arm-01")
            original_capture = camera.capture_sync
            state = {"calls": 0}

            def stale_first_capture():
                state["calls"] += 1
                frame = original_capture()
                if state["calls"] == 1:
                    # 首帧按 600ms 前的采样时刻接纳（渲染现场注入）→
                    # 年龄>400ms，必须重取；旧采样不因新读取续鲜
                    frame["source_captured_monotonic_ns"] -= int(0.6 * 1e9)
                return frame

            camera.capture_sync = stale_first_capture

            task = _lease(runtime, _submit(runtime, _payload(binding, "swipe_up",
                                                             task_id="f08-4c")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "f08-4c")
            assert done["status"] == "confirmed", (done.get("failure_reason"), detail)
            stale = [d for d in detail["sampling_diagnostics"]
                     if d.get("tick") == 0 and d.get("result") == "rejected"
                     and d.get("reason") == "source_frame_stale"]
            assert stale, "过旧前帧必须留下淘汰诊断"
            chain = detail["frame_chain"]
            assert chain and chain[0]["evidence_id"] != stale[0]["evidence_id"], \
                "淘汰尝试不得充当链前帧"
            assert swipes["n"] == 1, "重取成功后仅认领一次"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# 纯函数边界：motion_budget 与采样支持常量
# ---------------------------------------------------------------------------

def test_motion_budget_boundaries():
    within = motion_budget({"durationMs": 2700})
    assert within == {"status": "within_window", "estimated_total_ms": 4300}
    boundary = motion_budget({"durationMs": 2800})
    assert boundary["status"] == "within_window"  # 4400ms 恰好可观察
    assert boundary["estimated_total_ms"] == SAMPLING_SUPPORT["observation_budget_ms"]
    over = motion_budget({"durationMs": 2801})
    assert over["status"] == "exceeds_window"
    assert motion_budget(None)["status"] == "unproven"
    assert motion_budget({})["status"] == "unproven"
    assert motion_budget({"durationMs": 0})["status"] == "unproven"


def test_v3_bundle_small_is_planable_and_within_budget(tmp_path):
    bundle = v3_bundle_small()
    binding = screen_gestures.binding_snapshot_v3(bundle)
    assert binding["camera_binding_id"] == INSTALL_ID
    assert motion_budget({"durationMs": 2701})["estimated_total_ms"] == 4301
