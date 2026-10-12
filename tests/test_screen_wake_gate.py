"""The black-frame exception is restricted to the dedicated wake gate."""
from copy import deepcopy

import cv2
import numpy as np
import pytest

from runtime import calibration_gate
from tests.test_live_calibration_gate import ready_bundle, run_gate
from tests.test_screen_wake_driver import wake_frame


class WakeCamera:
    def __init__(self, *, change=None):
        self.change = change
        self.sequence = 0

    async def capture(self):
        self.sequence += 1
        frame = wake_frame(ready_bundle(), sequence=self.sequence)
        if self.change == "frozen":
            frame["frozen"] = True
        elif self.change == "stale":
            frame["source_captured_at"] = "2020-01-01T00:00:00Z"
        elif self.change == "wrong_camera":
            frame["device_unique_id"] = "other"
        return frame


def test_wake_gate_accepts_fresh_black_frame_without_changing_calibration(monkeypatch):
    bundle = ready_bundle()
    before = deepcopy(bundle)
    checks, _, _ = run_gate(
        monkeypatch, camera=WakeCamera(), bundle=bundle, screen_wake=True, wechat_send=False)
    assert checks == []
    assert bundle == before
    ordinary, _, _ = run_gate(
        monkeypatch, camera=WakeCamera(), bundle=bundle, wechat_send=False)
    assert {item["code"] for item in ordinary} == {"camera_waiting"}


def test_wake_gate_valid_dark_frame_waits_even_when_pose_is_requested(monkeypatch):
    """仍为有效暗屏（require_phone=True）时允许等待，不报错、不强制四边共识。"""
    bundle = ready_bundle()

    class BlackCamera(WakeCamera):
        captures = 0

        async def capture(self):
            self.captures += 1
            frame = await super().capture()
            ok, encoded = cv2.imencode(
                ".png", np.zeros((frame["height"], frame["width"], 3), np.uint8))
            assert ok
            frame["image_bytes"] = encoded.tobytes()
            return frame

    camera = BlackCamera()
    checks, frame, _ = run_gate(
        monkeypatch, camera=camera, bundle=bundle,
        screen_wake=True, require_pose=True, wechat_send=False)
    assert checks == []
    assert frame["source_frame_seq"] == 1
    assert camera.captures == 1, "暗屏等待不得为凑齐四边共识而丢弃首帧"


def test_wake_gate_lit_frame_requires_verified_pose(monkeypatch):
    """亮屏帧必须验证实时姿态：定位不到手机几何即拒绝，不借暗屏例外放行。"""
    import asyncio

    from drivers.screen_edge import ScreenEdgeError

    def no_edges(*_args, **_kwargs):
        raise ScreenEdgeError("no locatable edges")

    monkeypatch.setattr(calibration_gate, "EDGE_CONSENSUS_INTERVAL_S", 0)
    monkeypatch.setattr(calibration_gate, "locate_screen_quad", no_edges)
    bundle = ready_bundle()

    class Frames:
        def __init__(self, lit: bool):
            self.lit = lit
            self.sequence = 0

        async def capture(self):
            self.sequence += 1
            frame = wake_frame(bundle, lit=self.lit, sequence=self.sequence)
            frame.pop("_screen_pose", None)
            return frame

    options = dict(
        device_id="arm-01", camera_id="cam-01", daemon_addr="127.0.0.1:8795",
        daemon_session="session-01", estop_locked=False, bundle=bundle,
        wechat_send=False)
    checks, _, _ = asyncio.run(calibration_gate.check_live_calibration(
        screen_wake=True, require_pose=True, camera=Frames(lit=True), **options))
    assert "screen_pose_unavailable" in {item["code"] for item in checks}

    # 对照：普通动作通道面对有效暗帧仍是 camera_waiting，不因唤醒例外放行。
    checks, _, _ = asyncio.run(calibration_gate.check_live_calibration(
        camera=Frames(lit=False), **options))
    assert {item["code"] for item in checks} == {"camera_waiting"}


@pytest.mark.parametrize("change,code", [
    ("frozen", "camera_unavailable"), ("stale", "camera_unavailable"),
    ("wrong_camera", "camera_unique_id_changed"),
])
def test_wake_gate_keeps_camera_faults(monkeypatch, change, code):
    checks, _, _ = run_gate(
        monkeypatch, camera=WakeCamera(change=change), screen_wake=True, wechat_send=False)
    assert code in {item["code"] for item in checks}


@pytest.mark.parametrize("overrides,code", [
    ({"estop_locked": True}, "motion_blocked"),
    ({"daemon_session": "replacement"}, "daemon_session_changed"),
    ({"motion_result_unknown": True}, "motion_result_unknown"),
])
def test_wake_gate_keeps_nonvisual_safety_checks(monkeypatch, overrides, code):
    checks, _, _ = run_gate(
        monkeypatch, camera=WakeCamera(), screen_wake=True, wechat_send=False, **overrides)
    assert code in {item["code"] for item in checks}


def test_dark_screen_wx_task_requeues_with_auto_wake_once(tmp_path):
    """A6（2026-10-01 拍板）：WX 任务零动作遇暗屏（camera_waiting）→
    代提一次 screen_wake + 原任务退回队列重试一次；第二次暗屏走失败路径；
    总开关 auto_wake_enabled=False 直接失败（不代提）。"""
    import asyncio
    from types import SimpleNamespace
    from datetime import timedelta
    from runtime.executor import TaskExecutor
    from runtime.screen_wake import is_screen_wake_task

    submitted: list[dict] = []
    order: list[dict] = []

    def make_runtime(auto_wake):
        from datetime import datetime, timedelta, timezone
        from drivers import calibration_bundle as cb
        from tests.test_live_calibration_gate import ready_bundle
        data_root = tmp_path / "data"
        cb.save_bundle(ready_bundle(), data_root)
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        device = {"camera_quality": "waiting", "status": "busy",
                  "enforce_input_gate": False, "input_acceptance_deferred": False,
                  "camera_id": "cam-01", "camera_device_unique_id": "uvc-01",
                  "binding_revision": 7, "expected_frame_width": 64,
                  "expected_frame_height": 48, "daemon_session": "sess-1"}
        events = SimpleNamespace(publish=lambda *a, **k: None)
        runtime = SimpleNamespace(
            config=SimpleNamespace(get=lambda key, default=None: auto_wake),
            devices=SimpleNamespace(
                get=lambda device_id: device,
                _set_status=lambda device_id, status, reason: device.update(
                    {"status": status}),
                _gesture_data_root=lambda device_id: data_root,
                eligibility=lambda device_id, **kw: {"blocking_checks": [
                    "device_busy", "camera_waiting"]}),
            estop=SimpleNamespace(active=False),
            leases=SimpleNamespace(
                get=lambda device_id: {"task_id": "wx-dark-1",
                                       "lease_id": "lease-1",
                                       "expires_at": future},
                release=lambda device_id, lease_id: order.append({"action": "release"})),
            store=SimpleNamespace(get_task=lambda task_id: None,
                                  _save_task_progress=lambda task: None),
            events=events,
            reporter=SimpleNamespace(
                send_task_result=lambda task: asyncio.sleep(0)),
            submit_task=lambda body, **kw: (
                submitted.append(body), order.append({"action": "submit"}),
                {"task_id": body["task_id"]})[2],
            _save_task_progress=lambda task: None)
        return TaskExecutor(runtime)

    def dark_task(**extra):
        return {"task_id": "wx-dark-1", "device_id": "arm-01", "source": "wx",
                "task_type": "wx_list_navigate",
                "lease_id": "lease-1", "plan": None, "step_log": [],
                "action_call_count": 0, "capability_call_count": 0,
                "stop_result_unknown": None, "cancellation_handled": False,
                "status": "busy", **extra}

    entry = {"requires_input": False}

    # 1. 第一次暗屏：先退队/释放租约/置 ready，再提交 wake（P1-1 顺序契约：
    #    原任务持租约在途时提交必命中 lease_conflict+device_busy）
    executor = make_runtime(True)
    task = dark_task()
    done = asyncio.run(executor._finish_unstarted_camera_wait(task, entry))
    assert done is not None and done["status"] == "queued" and done["wake_retried"] is True
    assert len(submitted) == 1 and submitted[0]["task_type"] == "screen_wake"
    assert submitted[0]["payload"]["confirmed"] is True
    assert submitted[0]["correlation_id"] == "wx-dark-1"
    # 顺序：release 必须先于 wake 提交（生产 screen_wake.validate_preconditions
    # 会对在途任务+持有租约结构性拒绝）
    actions = [row["action"] for row in order]
    assert actions.index("release") < actions.index("submit"), actions
    # 代提成功留痕
    assert any("暗屏唤醒任务已提交" in row["note"] for row in task["timeline"])

    # 2. 代提失败：timeline 留痕，任务保持 queued（将按暗屏失败处理，不循环）
    def failing_submit(body, **kw):
        raise RuntimeError("plugin unavailable")
    executor2 = make_runtime(True)
    executor2._runtime.submit_task = failing_submit
    task_f = dark_task()
    done_f = asyncio.run(executor2._finish_unstarted_camera_wait(task_f, entry))
    assert done_f["status"] == "queued" and done_f["wake_retried"] is True
    assert any("暗屏唤醒代提失败" in row["note"] for row in task_f["timeline"])

    # 3. 第二次暗屏（wake_retried 已置）：走失败路径，不再代提
    executor._runtime.devices._set_status("arm-01", "busy", "重新排队")
    task2 = dark_task(wake_retried=True)
    done2 = asyncio.run(executor._finish_unstarted_camera_wait(task2, entry))
    assert done2["status"] == "failed" and done2["error_category"] == "camera_waiting"
    assert len(submitted) == 1

    # 4. 总开关关闭：不代提，直接失败
    executor_off = make_runtime(False)
    executor_off._runtime.devices._set_status("arm-01", "busy", "重置")
    task3 = dark_task()
    done3 = asyncio.run(executor_off._finish_unstarted_camera_wait(task3, entry))
    assert done3["status"] == "failed" and len(submitted) == 1


def test_expired_wake_retried_wx_task_fails_with_reporter_callback():
    """P2-1（第五轮复审）：暗屏代提重试群体的 queued 任务过期时——失败化
    （error_category=task_expired）并给 WX 回报（reporter 收到 failed），
    不再零回调；非 wake_retried 任务维持 expired 原语义。"""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from runtime.runtime import ArmFleetRuntime

    reported: list[dict] = []
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    saved: dict[str, dict] = {}

    def make_runtime(task):
        store = SimpleNamespace(
            get_task=lambda task_id: None,
            _save_task_progress=lambda t: None,
            save_task=lambda t: saved.update({t["task_id"]: t}),
            all_tasks=lambda: [task])
        runtime = ArmFleetRuntime.__new__(ArmFleetRuntime)
        runtime.store = store
        # 第六轮 S1 派发收尾读 executor 的早期收尾重试队列（_dispatch_once
        # 开头）；__new__ 部分桩补空队列即可——无待收尾项，不触及本测试
        # 的过期语义。
        runtime.executor = SimpleNamespace(_pending_early_finalize={})
        # pre_enable_maintenance 属性读 self.config（维护入口接线后）；部分
        # 桩补空 config（get 返回默认值=False=维护关闭，保持本测试语义）。
        runtime.config = SimpleNamespace(get=lambda key, default=None: default)
        runtime.events = SimpleNamespace(publish=lambda *a, **k: None)
        runtime.devices = SimpleNamespace(
            hold_unknown=lambda device_id, task_id, message: None)
        runtime.reporter = SimpleNamespace(
            send_task_result=lambda t: reported.append({"status": t["status"]}) or asyncio.sleep(0))
        runtime.estop = SimpleNamespace(active=False)
        runtime.screen_wake = __import__("runtime.screen_wake", fromlist=["screen_wake"])
        runtime._exec_tasks = {}
        runtime.list_tasks = lambda device_id=None: []
        return runtime

    def expired_task(**extra):
        base = {"task_id": "wx-exp-1", "device_id": "arm-01", "source": "wx",
                "task_type": "wx_list_navigate", "step_log": [],
                "created_at": "2026-10-01T00:00:00+00:00",
                "expires_at": past, "status": "queued"}
        base.update(extra)
        return base

    # 1. wake_retried 群体：过期→failed（task_expired）+ reporter 回报
    runtime = make_runtime(expired_task(wake_retried=True))
    asyncio.run(runtime._dispatch_once())
    final = saved["wx-exp-1"]
    assert final["status"] == "failed" and final["error_category"] == "task_expired"
    assert reported and reported[-1]["status"] == "failed"

    # 2. 非 wake_retried：维持 expired 原语义，无回调
    reported.clear()
    saved.clear()
    runtime2 = make_runtime(expired_task())
    asyncio.run(runtime2._dispatch_once())
    final2 = saved["wx-exp-1"]
    assert final2["status"] == "expired"
    assert not reported
