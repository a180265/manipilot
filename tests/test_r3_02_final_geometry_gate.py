"""R3-02 红测：实时几何直到最终接触仍有效（Fleet 侧）。

映射（acceptance-matrix.json 同步）：
- R3-02-A 候选/删除各一次合法接触；候选 task 绑定非空；删除允许 null
- R3-02-B 规划后到 TAP 前注入：隐藏/新View/新seq/超1s/nonce过期/凭据撤销/
  绑定变化 → TAP 0，走位事实保留（不伪装"完全未运动"）
- R3-02-C 同 nonce 不授权第二次点击；规划读取原子一致（验证的快照=使用的快照）；
  跨设备几何不可用
- R3-02-E 并行长轮询可取消（不阻塞事件循环）

Android 失效报文（R3-02-5/D）由 C 包在 Android 树实施；本文件覆盖服务端
"收到失效报文后旧请求被拒"的服务侧语义（新 seq/隐藏即取代旧快照）。

替身：FakeDaemon 传输、手机状态 provider、几何会话经真实协议握手/上报。
未替换：Runtime 几何门、驱动 click/租约/传输锁、认领链、坐标逆映射。
"""
from __future__ import annotations

import asyncio
import sys
import uuid

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from drivers.arm import DriverError
from tests.test_r2_live_geometry_driver import (
    ARM_DEVICE, CANDIDATE_CENTER, DELETE_CENTER, DISPLAY, GEOM_TOKEN,
    IME_DEVICE, LiveGeometryEnv, _click_params, _expected_robot,
    _snapshot_body)


@pytest.fixture()
def live_env(tmp_path):
    env = LiveGeometryEnv(tmp_path)
    try:
        yield env
    finally:
        env.close()


def _report(env, *, seq, view=None, nonce=None, candidate_visible=True,
            candidate_bounds=(54, 2160, 1026, 2280), task_id=None,
            geometry_valid=True):
    body = _snapshot_body(
        env.session["runtime_session_id"], seq, nonce,
        view_instance_id=view or env._view,
        candidate_visible=candidate_visible,
        candidate_bounds=candidate_bounds)
    body["current_task_id"] = task_id
    body["geometry_valid"] = geometry_valid
    if not geometry_valid:
        for t in body["targets"]:
            t["visible"] = False
            t["enabled"] = False
            t["clickable"] = False
            t["bounds"] = None
        body["keyboard_bounds"] = None
    env.geometry.report(IME_DEVICE, body, GEOM_TOKEN)


def _start_phone(env, *, task_id="t-r3-2-ime", candidate_bounds=(54, 2160, 1026, 2280)):
    """手机侧线程：领取 nonce 并回答一次快照（真实协议），回答后退出。"""
    import threading
    import time as _time
    sid = env.session["runtime_session_id"]

    def phone():
        deadline = _time.monotonic() + 10
        while _time.monotonic() < deadline:
            claimed = env.geometry.poll_next(IME_DEVICE, sid, 1, GEOM_TOKEN)
            if claimed is not None:
                env._seq += 1
                _report(env, seq=env._seq, nonce=claimed["request_nonce"],
                        candidate_bounds=candidate_bounds, task_id=task_id)
                return
            _time.sleep(0.005)

    t = threading.Thread(target=phone, daemon=True)
    t.start()
    return t


async def _plan(env, target_id="ime_candidate_bar"):
    return await env.runtime._ime_live_geometry_target(ARM_DEVICE, target_id)


def _tap_count(env):
    return len([line for line in env.daemon.commands if line.startswith("TAP")])


# ---------------------------------------------------------------- A 合法链

def test_r3_02_a_candidate_with_task_binding_and_delete_null_task(live_env):
    async def scenario():
        env = live_env
        phone = _start_phone(env, task_id="t-r3-2-ime")
        ref = await _plan(env, "ime_candidate_bar")
        phone.join(timeout=5)
        assert ref["currentTaskId"] == "t-r3-2-ime", "规划快照必须携带原任务引用"
        gate = env.runtime._geometry_gates.get(ref["nonce"])
        assert callable(gate), "规划必须寄存 TAP 边界最终几何门（R3-02-3）"
        ref = dict(ref, finalGeometryGate=gate)
        result = await env.driver.click(_click_params(dict(ref), "ime_candidate_bar"))
        expected = _expected_robot(CANDIDATE_CENTER)
        assert result["resolved_robot_xy"] == pytest.approx(list(expected))
        assert _tap_count(env) == 1
        # 删除键：task 允许 null（原契约）
        phone2 = _start_phone(env, task_id=None)
        ref2 = await _plan(env, "ime_delete_key")
        phone2.join(timeout=5)
        await env.driver.click(_click_params(dict(ref2), "ime_delete_key"))
        assert _tap_count(env) == 2

    asyncio.run(scenario())


def test_r3_02_a2_candidate_without_task_binding_rejected_at_planning(live_env):
    """候选 task 缺失（空原任务）：规划门拒绝，零接触。"""

    async def scenario():
        env = live_env
        phone = _start_phone(env, task_id=None)
        with pytest.raises(DriverError) as caught:
            await _plan(env, "ime_candidate_bar")
        phone.join(timeout=5)
        assert caught.value.motion_not_started is True
        assert _tap_count(env) == 0

    asyncio.run(scenario())


# ---------------------------------------------------------------- B 边界失效

def _assert_zero_tap_walk_preserved(env, caught):
    assert isinstance(caught, DriverError)
    # 走位事实保留：错误不得声称"完全未运动"（走位已发生）
    assert getattr(caught, "motion_not_started", None) is not True, \
        "TAP 边界拒绝不得把已发生的走位伪装为完全未运动"
    assert env.daemon.mpos[0] != pytest.approx(2.0), "走位应已发生（TAP 前）"
    assert _tap_count(env) == 0, "边界失效 → TAP 0"


def test_r3_02_b_hidden_report_between_plan_and_tap(live_env):
    async def scenario():
        env = live_env
        phone = _start_phone(env)
        ref = await _plan(env)
        phone.join(timeout=5)
        # 手机隐藏键盘：新 seq、目标不可见（正式协议失效形态）
        env._seq += 1
        _report(env, seq=env._seq, candidate_visible=False, geometry_valid=False)
        ref = dict(ref, finalGeometryGate=env.runtime._geometry_gates[ref["nonce"]])
        with pytest.raises(DriverError) as caught:
            await env.driver.click(_click_params(dict(ref), "ime_candidate_bar"))
        _assert_zero_tap_walk_preserved(env, caught.value)

    asyncio.run(scenario())


def test_r3_02_b_new_view_between_plan_and_tap(live_env):
    async def scenario():
        env = live_env
        phone = _start_phone(env)
        ref = await _plan(env)
        phone.join(timeout=5)
        env._seq += 1
        env._view = str(uuid.uuid4())  # 新 View（重建/销毁）
        _report(env, seq=env._seq, view=env._view)
        ref = dict(ref, finalGeometryGate=env.runtime._geometry_gates[ref["nonce"]])
        with pytest.raises(DriverError) as caught:
            await env.driver.click(_click_params(dict(ref), "ime_candidate_bar"))
        _assert_zero_tap_walk_preserved(env, caught.value)

    asyncio.run(scenario())


def test_r3_02_b_snapshot_age_over_1s(live_env, monkeypatch):
    async def scenario():
        env = live_env
        phone = _start_phone(env)
        ref = await _plan(env)
        phone.join(timeout=5)
        # 可控时钟：TAP 边界的"当前时间"推后 1.5 秒（快照年龄超 1 秒）
        import runtime.ime_geometry as geom_mod
        real_now = geom_mod._now
        monkeypatch.setattr(geom_mod, "_now",
                            lambda: real_now() + 1.5)
        ref = dict(ref, finalGeometryGate=env.runtime._geometry_gates[ref["nonce"]])
        with pytest.raises(DriverError) as caught:
            await env.driver.click(_click_params(dict(ref), "ime_candidate_bar"))
        _assert_zero_tap_walk_preserved(env, caught.value)

    asyncio.run(scenario())


def test_r3_02_b_nonce_expired_over_2s(live_env, monkeypatch):
    async def scenario():
        env = live_env
        phone = _start_phone(env)
        ref = await _plan(env)
        phone.join(timeout=5)
        import runtime.ime_geometry as geom_mod
        real_now = geom_mod._now
        monkeypatch.setattr(geom_mod, "_now",
                            lambda: real_now() + 2.5)
        ref = dict(ref, finalGeometryGate=env.runtime._geometry_gates[ref["nonce"]])
        with pytest.raises(DriverError) as caught:
            await env.driver.click(_click_params(dict(ref), "ime_candidate_bar"))
        _assert_zero_tap_walk_preserved(env, caught.value)

    asyncio.run(scenario())


def test_r3_02_b_credential_revoked(live_env):
    async def scenario():
        env = live_env
        phone = _start_phone(env)
        ref = await _plan(env)
        phone.join(timeout=5)
        assert env.runtime.revoke_phone_credential(IME_DEVICE) >= 1
        ref = dict(ref, finalGeometryGate=env.runtime._geometry_gates[ref["nonce"]])
        with pytest.raises(DriverError) as caught:
            await env.driver.click(_click_params(dict(ref), "ime_candidate_bar"))
        _assert_zero_tap_walk_preserved(env, caught.value)

    asyncio.run(scenario())


def test_r3_02_b_binding_changed(live_env):
    async def scenario():
        env = live_env
        phone = _start_phone(env)
        ref = await _plan(env)
        phone.join(timeout=5)
        env.runtime.devices._devices[ARM_DEVICE]["ime_device_id"] = "k40-9"
        ref = dict(ref, finalGeometryGate=env.runtime._geometry_gates[ref["nonce"]])
        with pytest.raises(DriverError) as caught:
            await env.driver.click(_click_params(dict(ref), "ime_candidate_bar"))
        _assert_zero_tap_walk_preserved(env, caught.value)

    asyncio.run(scenario())


# ---------------------------------------------------------------- C 一次性/原子/跨设备

def test_r3_02_c_same_nonce_never_authorizes_second_click(live_env):
    async def scenario():
        env = live_env
        phone = _start_phone(env)
        ref = await _plan(env)
        phone.join(timeout=5)
        gate = env.runtime._geometry_gates[ref["nonce"]]
        await env.driver.click(
            _click_params(dict(ref, finalGeometryGate=gate), "ime_candidate_bar"))
        assert _tap_count(env) == 1
        # 同 nonce 重放（同 seq 回执不续鲜）：第二次点击必须拒绝
        with pytest.raises(DriverError):
            await env.driver.click(
                _click_params(dict(ref, finalGeometryGate=gate),
                              "ime_candidate_bar"))
        assert _tap_count(env) == 1, "同 nonce 不授权第二次点击"

    asyncio.run(scenario())


def test_r3_02_c_planning_uses_validated_snapshot_atomically(live_env):
    """规划"验证后另取 latest"缺口：验证 seq=N 后、读取前到达 seq=N+1（候选
    位置不同），规划必须返回它验证过的那份快照，而不是 latest 的新坐标。"""

    async def scenario():
        env = live_env
        geometry = env.geometry
        phone = _start_phone(env, candidate_bounds=(54, 2160, 1026, 2280))
        original_latest = geometry.latest_snapshot

        def latest_then_competing(ime_device_id):
            # 竞争写入者：在规划读取 latest 前推送新 seq、候选移动到新位置
            env._seq += 1
            _report(env, seq=env._seq,
                    candidate_bounds=(500, 2160, 700, 2280))
            return original_latest(ime_device_id)

        geometry.latest_snapshot = latest_then_competing
        try:
            ref = await _plan(env)
        finally:
            geometry.latest_snapshot = original_latest
        phone.join(timeout=5)
        expected_x = (54 + 1026) / 2.0
        assert ref["cameraX"] == pytest.approx(expected_x), (
            "规划使用了验证后另取的 latest 快照（坐标漂移到新 seq）", ref["cameraX"])

    asyncio.run(scenario())


def test_r3_02_c_cross_device_geometry_unusable(live_env):
    from runtime.ime_geometry import ImeGeometryError
    env = live_env
    phone = _start_phone(env)
    ref = asyncio.run(_plan(env))
    phone.join(timeout=5)
    sid = env.session["runtime_session_id"]
    rec = next(r for r in env.geometry._nonces.values()
               if r["snapshot_seq"] is not None)
    nonce = rec["nonce"]
    # 另一手机（无会话）：同一 nonce 不可用
    with pytest.raises(ImeGeometryError):
        env.geometry.consume_for_click(
            "k40-other", nonce, target_id="ime_candidate_bar",
            expected_task="t-r3-2-ime", expected_session=sid,
            expected_display={"width_px": 1080, "height_px": 2400})


# ---------------------------------------------------------------- E 并行长轮询可取消

def test_r3_02_e_async_poll_cancellable(live_env):
    async def scenario():
        env = live_env
        task = asyncio.create_task(env.geometry.poll_next_async(
            IME_DEVICE, env.session["runtime_session_id"], 5, GEOM_TOKEN))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(scenario())
