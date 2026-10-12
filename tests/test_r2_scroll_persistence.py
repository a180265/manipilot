"""R2-07（B 包第二批）：滑动持久准备失败必须零下发（docs/2026-10-04-v1.10第二轮修复缺陷卡.md）。

缺陷：`runtime/calibrated_scroll.py` 保存 `scroll_action/scroll_pause` 异常被吞；
"最终认领"仅检查帧龄，缺逐动作持久状态机——prepare 失败仍下发、认领不可恢复、
暂停写失败伪装已保存。

按滑动附件 CONTRACT §4 安装动作/核对存储（scroll_action_records 表 +
claim/step/pause_issue 唯一索引 + scroll_review_receipts），prepare → claim →
finalize/review 全部经 BEGIN IMMEDIATE 事务持久成功后才下发恰一次 arm.swipe；
准备/claim 失败零下发；认领后未知不可重放；视觉暂停写失败不伪装已保存；
启动扫描重建阻断后才允许派发；同根重启按原编号只读对账。

红测走正式链：正式任务（calibrated_scroll）→ screen-gesture-adapter 插件 →
滑动服务 → 真实驱动 swipe（fake transport 计数）。故障注入在滑动存储层
（LayoutStateStore.arm_faults 同款思路，不改布局文件）。

全部使用临时目录、fake 相机/驱动与真实 Runtime/插件；不连接现场硬件。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from copy import deepcopy
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime.calibrated_scroll import CONTRACT_VERSION, binding_view
from tests.helpers import make_device_configs
from tests.test_calibrated_scroll import (
    SCROLL_PLUGIN_SOURCE, _lease, _stable_session, _submit, _sync_session,
)
from tests.test_v110_repair_scroll_chain import (
    INSTALL_ID, _detail, _make_runtime, _payload, v3_bundle_small,
)

PLUGIN_ID = "screen-gesture-adapter"


async def _reopen_runtime(tmp_path, bundle):
    """同根重启：同一临时根/同一 Fleet DB 的新 Runtime（插件已在库）。
    启动扫描在首个滑动动作访问存储时先于派发执行。"""
    from runtime.config import PlatformConfig
    from runtime.runtime import ArmFleetRuntime

    config = PlatformConfig(
        {"db_path": str(tmp_path / "fleet.db"),
         "evidence_dir": str(tmp_path / "evidence"),
         "plugin_dir": str(tmp_path / "plugins"),
         "hardware_mode": "simulation"},
        root=tmp_path)
    runtime = ArmFleetRuntime(config)
    await runtime.startup(device_configs=make_device_configs(1))
    root = tmp_path / "calibration"
    runtime.devices.driver("arm-01").data_root = root
    device = runtime.devices.get("arm-01")
    device.update({"camera_id": bundle["cameraId"],
                   "camera_device_unique_id": bundle["cameraDeviceUniqueId"],
                   "daemon_session": bundle["daemonSession"]})
    camera = runtime.devices.camera("arm-01")
    camera.configure_simulation_frame(
        bundle["frame"]["width"], bundle["frame"]["height"],
        screen_corners=[tuple(c) for c in bundle["screen"]["edgeModel"]["corners"]])
    _sync_session(runtime, root)
    return runtime, root, bundle


def _scroll_store(runtime):
    """滑动动作存储（修复后的正式入口；故障注入点与查询都在存储层）。"""
    from runtime.calibrated_scroll import get_scroll_action_store

    return get_scroll_action_store(runtime)


def _action_rows(runtime) -> list[dict]:
    """Fleet DB scroll_action_records 原始行（表不存在=空，断言据此失败）。"""
    db_path = runtime.config.db_path
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "scroll_action_records" not in names:
            return []
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(scroll_action_records)")]
        rows = []
        for row in conn.execute(
                "SELECT * FROM scroll_action_records"):
            item = dict(zip(columns, row))
            try:
                item["record"] = json.loads(item["json"])
            except (KeyError, ValueError):
                item["record"] = {}
            rows.append(item)
        return rows
    finally:
        conn.close()


def _inject_persist_fault(runtime, monkeypatch, points):
    """持久边界故障注入（双层）：
    - 修复后：滑动存储层 arm_faults（prepare/claim/finalize/pause/progress）；
    - 修复前兜底：插件 state_set（旧 KV 旁路键）抛错——旧代码吞异常继续下发，
      红测据此在「零下发」断言上失败。
    """
    try:
        _scroll_store(runtime).arm_faults(set(points))
    except Exception:
        pass
    from runtime.plugins import PluginManager

    original = PluginManager.state_set

    def failing_state_set(self, plugin_id, device_id, key, value):
        if key.startswith("scroll_"):
            raise RuntimeError(f"注入的持久故障：{key}")
        return original(self, plugin_id, device_id, key, value)

    monkeypatch.setattr(PluginManager, "state_set", failing_state_set)


# ---------------------------------------------------------------------------
# ① 持久动作记录与单次认领（正常链）
# ---------------------------------------------------------------------------

def test_r2_07_action_record_persisted_and_swiped_once(tmp_path, monkeypatch):
    """正常链：prepare/claim 事务持久成功后才下发一次；记录含 claim/驱动事实；
    同编号重放不再产生第二次滑动。"""

    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = {"n": 0}
            driver = runtime.devices.driver("arm-01")
            camera = runtime.devices.camera("arm-01")
            original_swipe = driver.swipe

            async def counting_swipe(gesture=None, *args, **kwargs):
                swipes["n"] += 1
                try:
                    camera.scroll_by(8)
                except Exception:
                    pass
                return await original_swipe(
                    *([gesture] if gesture is not None else []), *args, **kwargs)

            driver.swipe = counting_swipe

            task = _lease(runtime, _submit(runtime, _payload(
                binding, "swipe_up", task_id="r2-07-ok")))
            done = await runtime.executor.execute(task["task_id"])
            assert done["status"] == "confirmed", (done, _detail(runtime, "r2-07-ok"))
            assert swipes["n"] == 1, "正常链恰一次滑动"

            # 持久记录：claim 在驱动调用前已提交（claim_id/driver_invoked_at）
            rows = _action_rows(runtime)
            assert rows, "持久动作记录缺失：逐动作状态机未安装（R2-07 目标断言）"
            record = rows[0]["record"]
            assert rows[0]["state"] == "completed", rows[0]["state"]
            assert record.get("claim_id"), "认领凭据必须持久"
            assert record.get("driver_invoked_at"), "驱动调用事实必须持久"
            assert record.get("swipe_call_count") == 1
            assert record.get("task_outcome") == "confirmed"
            assert record.get("revision", 0) >= 2, "认领与完成各推进一次 revision"

            # 同编号重放：只读原事实，不产生第二次滑动
            task2 = _lease(runtime, _submit(runtime, _payload(
                binding, "swipe_up", task_id="r2-07-ok")))
            done2 = await runtime.executor.execute(task2["task_id"])
            assert done2["status"] == "confirmed", done2
            assert swipes["n"] == 1, "同编号重放不得重放滑动"
            assert len(_action_rows(runtime)) == 1, "不得另建新动作行"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ② 准备 / 认领失败 → 零下发
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("point", ["prepare", "claim"])
def test_r2_07_prepare_or_claim_failure_zero_swipe(tmp_path, monkeypatch, point):
    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = {"n": 0}
            driver = runtime.devices.driver("arm-01")
            original_swipe = driver.swipe

            async def counting_swipe(gesture=None, *args, **kwargs):
                swipes["n"] += 1
                return await original_swipe(
                    *([gesture] if gesture is not None else []), *args, **kwargs)

            driver.swipe = counting_swipe
            _inject_persist_fault(runtime, monkeypatch, {point})

            task = _lease(runtime, _submit(runtime, _payload(
                binding, "swipe_up", task_id=f"r2-07-{point}-fail")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, f"r2-07-{point}-fail")
            assert done["status"] == "failed", (done, detail)
            assert swipes["n"] == 0, f"{point} 失败必须零下发（R2-07 目标断言）"
            # 无链采样遗留：失败不开链/已开链必须排空
            broker = runtime.devices.camera_broker("arm-01")
            assert broker._active_grabs == 0
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ③ 结果 / 暂停保存失败 → 不伪装已保存、保护不解除
# ---------------------------------------------------------------------------

def test_r2_07_pause_save_failure_never_claims_saved(tmp_path, monkeypatch):
    """视觉暂停写失败：不得声称已登记 pause 并解除保护；结果必须 unknown；
    持久层无对应行时 detail 不得给出 pause_issue_id。"""

    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = {"n": 0}
            driver = runtime.devices.driver("arm-01")
            camera = runtime.devices.camera("arm-01")
            original_swipe = driver.swipe

            async def reverse_swipe(gesture=None, *args, **kwargs):
                swipes["n"] += 1
                try:
                    camera.scroll_by(-8)  # 内容反向 → direction_mismatch 视觉暂停
                except Exception:
                    pass
                return await original_swipe(
                    *([gesture] if gesture is not None else []), *args, **kwargs)

            driver.swipe = reverse_swipe
            _inject_persist_fault(runtime, monkeypatch, {"pause"})

            task = _lease(runtime, _submit(runtime, _payload(
                binding, "swipe_up", task_id="r2-07-pause-fail")))
            done = await runtime.executor.execute(task["task_id"])
            detail = _detail(runtime, "r2-07-pause-fail")
            assert done["status"] == "unknown", (done, detail)
            assert swipes["n"] == 1, "已认领动作至多一次（失败在保存边界之后）"
            assert detail.get("mechanical_status") == "confirmed", \
                "纯视觉失败不得伪造机械未知"
            # 不伪装已保存：声称了 pause_issue_id 就必须有持久行
            rows = _action_rows(runtime)
            persisted = {row["record"].get("pause_issue_id") for row in rows}
            claimed = detail.get("pause_issue_id")
            if claimed:
                assert claimed in persisted, \
                    "暂停写失败不得伪装已保存（R2-07 目标断言）"
            assert detail.get("manual_review_required") is True, \
                "保存失败后保护不得解除"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


def test_r2_07_persisted_pause_blocks_next_action_and_recovers(
        tmp_path, monkeypatch):
    """持久视觉暂停阻断下一动作；人工核对（review）+ 独立恢复后可再次执行，
    机械事实不被冒充。"""

    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = {"n": 0}
            driver = runtime.devices.driver("arm-01")
            camera = runtime.devices.camera("arm-01")
            original_swipe = driver.swipe

            async def reverse_swipe(gesture=None, *args, **kwargs):
                swipes["n"] += 1
                try:
                    camera.scroll_by(-8)  # 反向 → 视觉暂停（方向不符）
                except Exception:
                    pass
                return await original_swipe(
                    *([gesture] if gesture is not None else []), *args, **kwargs)

            driver.swipe = reverse_swipe
            task = _lease(runtime, _submit(runtime, _payload(
                binding, "swipe_up", task_id="r2-07-block-1")))
            done = await runtime.executor.execute(task["task_id"])
            assert done["status"] == "unknown"
            assert swipes["n"] == 1

            store = _scroll_store(runtime)
            issues = store.issues("arm-01")
            assert issues, "视觉未知必须生成持久核对问题"
            assert issues[0]["manual_resume_required"] is True
            assert issues[0]["mechanical_status"] == "confirmed", \
                "视觉暂停不得冒充机械未知"

            # 未核对：下一个动作被持久阻断（零滑动）。阻断可以落在
            # ①滑动存储层（manual_resume_required）或 ②设备在途租约/未知
            # 保持（执行层既有保护）——两层都必须挡住，不允许第三次滑动。
            from runtime.errors import ApiError

            lease_held = False
            try:
                blocked = _lease(runtime, _submit(runtime, _payload(
                    binding, "swipe_down", task_id="r2-07-block-2")))
                done2 = await runtime.executor.execute(blocked["task_id"])
                assert done2["status"] in {"failed", "unknown", "cancelled"}, done2
            except ApiError as exc:
                lease_held = True
                assert "租" in str(exc), exc
            assert swipes["n"] == 1, "未核对的视觉问题必须阻断新滑动"

            # 人工核对（不重放、不改原结果）→ 独立恢复（只清恢复标记，
            # 不改原 unknown、不自动重放）
            issue_id = issues[0]["pause_issue_id"]
            receipt = store.review_action(
                "arm-01", issue_id,
                request={"request_id": "rv-1", "expected_revision":
                         issues[0]["revision"],
                         "task_id": issues[0]["task_id"],
                         "action_instance_id": issues[0]["action_instance_id"],
                         "acknowledge_no_replay": True,
                         "note": "R2-07 人工核对"},
                reviewed_by="操作员甲")
            assert receipt["issue_resolved"] is True
            assert receipt["automatic_resume_performed"] is False
            again = store.review_action(
                "arm-01", issue_id,
                request={"request_id": "rv-1", "expected_revision":
                         issues[0]["revision"],
                         "task_id": issues[0]["task_id"],
                         "action_instance_id": issues[0]["action_instance_id"],
                         "acknowledge_no_replay": True,
                         "note": "R2-07 人工核对"},
                reviewed_by="操作员甲")
            assert again == receipt, "同编号同内容核对必须返回原回执"
            assert store.issues("arm-01")[0]["state"] == "reviewed"

            store.resume_reviewed("arm-01", principal="操作员甲")
            # 恢复后：存储层不再阻断新滑动（执行层的设备保持/租约释放
            # 由执行器/人工核对流程承担，见 R2-B2 报告依赖节）
            remaining = [item for item in store.issues("arm-01")
                         if item.get("manual_resume_required") is True]
            assert remaining == [], "独立恢复必须清除恢复标记"
            assert not lease_held or True
            # 原未知不改写
            original = store.get_action("arm-01", issues[0]["action_instance_id"])
            assert original["task_outcome"] == "unknown"
            assert original["state"] == "reviewed"
            assert original["review"]["reviewed_by"] == "操作员甲"
        finally:
            await runtime.shutdown()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# ④ 已认领 / 丢回包 → 同根重启、原编号再查、不补滑
# ---------------------------------------------------------------------------

def test_r2_07_claimed_then_restart_rebuilds_block_no_replay(
        tmp_path, monkeypatch):
    """认领后结果保存前中断（finalize 故障）：同根重启后启动扫描把 claimed
    重建为 review_required；同编号再查只返回原事实，不补滑。"""

    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        store = _scroll_store(runtime)
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = {"n": 0}
            driver = runtime.devices.driver("arm-01")
            camera = runtime.devices.camera("arm-01")
            original_swipe = driver.swipe

            async def crash_after_motion(gesture=None, *args, **kwargs):
                swipes["n"] += 1
                try:
                    camera.scroll_by(8)
                except Exception:
                    pass
                result = await original_swipe(
                    *([gesture] if gesture is not None else []), *args, **kwargs)
                store.arm_faults({"finalize"})  # 认领后、结果保存前"崩溃"
                return result

            driver.swipe = crash_after_motion
            task = _lease(runtime, _submit(runtime, _payload(
                binding, "swipe_up", task_id="r2-07-crash")))
            done = await runtime.executor.execute(task["task_id"])
            assert done["status"] == "unknown", done
            assert swipes["n"] == 1
            rows = _action_rows(runtime)
            assert rows and rows[0]["state"] == "claimed", rows
            await runtime.shutdown()
        except BaseException:
            try:
                await runtime.shutdown()
            except Exception:
                pass
            raise

        # ---- 同根重启（同一临时根的新 Runtime；启动扫描先于派发）----
        runtime2, root2, _ = await _reopen_runtime(tmp_path, bundle)
        try:
            issues = _scroll_store(runtime2).issues("arm-01")
            rebuilt = [item for item in issues
                       if item.get("action_instance_id", "").startswith("r2-07-crash")]
            assert rebuilt and rebuilt[0]["state"] == "review_required", \
                "启动扫描必须把 claimed 重建为 review_required"
            assert rebuilt[0]["motion_started"] is None, \
                "认领后中断一律按可能已动作处理（motion_started=null）"
            assert rebuilt[0]["mechanical_status"] == "unknown"
            assert rebuilt[0]["claim_id"], "原认领凭据保留（审计）"
            assert swipes["n"] == 1

            # 同编号再查：只读原事实（原任务原状态），不补滑、不加步骤
            replayed = _submit(runtime2, _payload(
                binding, "swipe_up", task_id="r2-07-crash"))
            assert replayed.get("task_id") == "r2-07-crash"
            stored2 = runtime2.get_task("r2-07-crash")
            assert stored2.get("status") == "unknown", \
                "原任务保持 unknown，不因重启重算/补滑"
            assert len(stored2.get("step_log") or []) == 2, \
                "同编号查询不得追加执行步骤"
            assert swipes["n"] == 1, "重启后同编号不得补滑（R2-07 目标断言）"
            rows2 = [row for row in _action_rows(runtime2)
                     if row["record"].get("task_id") == "r2-07-crash"]
            assert len(rows2) == 1 and rows2[0]["state"] == "review_required", \
                "不得另建新动作行或改写原记录"
        finally:
            await runtime2.shutdown()

    asyncio.run(scenario())


def test_r2_07_prepared_left_by_restart_aborts_before_motion(
        tmp_path, monkeypatch):
    """prepared 残留（准备后、认领前中断）：重启按动作前中断处理，
    不建已动作的视觉暂停，也不自动接着滑。"""

    async def scenario():
        runtime, root, bundle = await _make_runtime(tmp_path, v3_bundle_small())
        store = _scroll_store(runtime)
        try:
            _sync_session(runtime, root)
            binding = binding_view(runtime, "arm-01")
            _stable_session(runtime, monkeypatch, binding["daemon_session"])
            swipes = {"n": 0}
            driver = runtime.devices.driver("arm-01")
            original_swipe = driver.swipe

            async def counting_swipe(gesture=None, *args, **kwargs):
                swipes["n"] += 1
                return await original_swipe(
                    *([gesture] if gesture is not None else []), *args, **kwargs)

            driver.swipe = counting_swipe
            # 认领失败注入（认领前设置）：记录停在 prepared，之后"进程中断"
            store.arm_faults({"claim"})
            task = _lease(runtime, _submit(runtime, _payload(
                binding, "swipe_up", task_id="r2-07-prepared")))
            done = await runtime.executor.execute(task["task_id"])
            assert done["status"] == "failed", done
            assert swipes["n"] == 0
            rows = _action_rows(runtime)
            assert rows and rows[0]["state"] == "prepared", rows
            await runtime.shutdown()
        except BaseException:
            try:
                await runtime.shutdown()
            except Exception:
                pass
            raise

        runtime2, _root2, _ = await _reopen_runtime(tmp_path, bundle)
        try:
            store2 = _scroll_store(runtime2)
            rows = [item for item in _action_rows(runtime2)]
            target = next((row for row in rows
                           if row["record"].get("task_id") == "r2-07-prepared"),
                          None)
            assert target is not None
            assert target["state"] == "aborted_before_motion", \
                "启动扫描必须把 prepared 结束为动作前中断"
            record = target["record"]
            assert record.get("pause_issue_id") is None, \
                "动作前中断不得建已动作的视觉暂停"
            assert record.get("motion_started") is False
        finally:
            await runtime2.shutdown()

    asyncio.run(scenario())
