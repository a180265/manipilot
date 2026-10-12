"""设置写入编排测试（2026-10-03 规格 §4.8.3 P02）。

覆盖：意图 prepared→applied+checkpoint、prepared 意图阻断并发新请求、
同编号重放、落盘前失败 aborted、崩溃后按文件摘要对账（after 完成确认/
before+关闭完成关闭/before+开启拒绝）、双摘要不匹配保持阻断（commit_unknown）。
"""
from __future__ import annotations

import sys
from copy import deepcopy

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime import input_settings as iset
from runtime.input_control import InputControl, InputControlError, canonical_digest
from runtime.store import Store

REQUEST = {"enabled": True, "ime_device_id": "k40-1", "expected_revision": 0,
           "operator_id": "操作员", "reason": "开启", "client_request_id": "req-1"}


@pytest.fixture()
def env(tmp_path):
    store = Store(tmp_path / "fleet.db")
    ctrl = InputControl(store, tmp_path / "fleet.db")
    root = tmp_path / "data"
    return ctrl, root


def _write(ctrl, root, request=None, **kw):
    merged = {**REQUEST, **(request or {}), **kw}
    return ctrl.write_settings("arm-01", data_root=root, request=merged,
                               calibration_ready=True)


def test_write_settings_applies_intent_and_checkpoint(env):
    ctrl, root = env
    result = _write(ctrl, root)
    assert result["outcome"] == "applied" and result["replayed"] is False
    assert result["settings"]["effective"] is True
    stored = iset.load_settings("arm-01", data_root=root)
    assert stored["enabled"] is True and stored["revision"] == 1
    guard = ctrl.ensure_guard("arm-01")
    assert guard["settings_checkpoint"]["revision"] == 1
    intents = [r for r in ctrl.records_for_device("arm-01", "settings_write")]
    assert len(intents) == 1 and intents[0]["state"] == "applied"
    assert ctrl.has_prepared_intent("arm-01") is None


def test_prepared_intent_blocks_other_requests_and_replays_same_id(env):
    ctrl, root = env
    import runtime.input_settings as iset_mod
    original_atomic = iset_mod._atomic_write
    def stalled_write(path, payload):
        raise RuntimeError("disk stalled (模拟落盘前中断)")
    iset_mod._atomic_write = stalled_write
    try:
        with pytest.raises(InputControlError, match="持久化失败"):
            _write(ctrl, root)
    finally:
        iset_mod._atomic_write = original_atomic
    # aborted：文件未被触碰，重试同编号返回原失败（不自动再执行）
    assert iset.load_settings("arm-01", data_root=root)["revision"] == 0
    intents = ctrl.records_for_device("arm-01", "settings_write")
    assert intents[-1]["state"] == "aborted"
    with pytest.raises(InputControlError, match="不自动再执行"):
        _write(ctrl, root, client_request_id="req-1")
    # 新编号被已对账完成的意图放行：正常写第二笔
    ok = _write(ctrl, root, client_request_id="req-2", expected_revision=0)
    assert ok["outcome"] == "applied"


def test_prepared_intent_blocks_different_request_until_reconciled(env, monkeypatch):
    ctrl, root = env
    # 制造 prepared 僵尸意图：落盘成功但 applied+checkpoint 未完成（崩溃窗口）。
    # R2-02 更正（§4.5）：applied 与 checkpoint 已合并为单事务
    # _apply_settings_commit，故障注入点随之从 mark_settings_write 更正。
    calls = {"n": 0}
    real_mark = InputControl._apply_settings_commit

    def crashing_mark(self, *args, **kwargs):
        if calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("crash before applied-mark")
        return real_mark(self, *args, **kwargs)
    monkeypatch.setattr(InputControl, "_apply_settings_commit", crashing_mark)
    with pytest.raises(RuntimeError):
        _write(ctrl, root)
    monkeypatch.undo()
    pending = ctrl.has_prepared_intent("arm-01")
    assert pending is not None, "崩溃窗口必须留下 prepared 意图"
    # 文件已等于 after：其它新请求被阻断
    with pytest.raises(InputControlError, match="不得越过"):
        _write(ctrl, root, client_request_id="req-other")
    # 同编号到达：对账——文件匹配 after → 重新确认 applied，沿用原快照
    reconciled = _write(ctrl, root, client_request_id="req-1")
    assert reconciled["outcome"] == "applied"
    assert reconciled["settings"]["revision"] == 1
    assert ctrl.has_prepared_intent("arm-01") is None


def test_reconcile_before_digest_closes_authorized_close_but_rejects_open(env):
    ctrl, root = env
    # 场景 A：before 状态 + 关闭请求崩溃 → 对账完成这次已授权的关闭
    _write(ctrl, root, client_request_id="open-1")  # 先开（r1）
    # R2-02 更正（§4.5）：注入点 mark_settings_write → _apply_settings_commit
    calls = {"n": 0}
    real_mark = InputControl._apply_settings_commit
    def crashing_mark(self, *args, **kwargs):
        if calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("crash (close)")
        return real_mark(self, *args, **kwargs)
    import runtime.input_control as ic_mod
    ic_mod.InputControl._apply_settings_commit = crashing_mark
    try:
        with pytest.raises(RuntimeError):
            _write(ctrl, root, request={**REQUEST, "enabled": False,
                                        "ime_device_id": None,
                                        "expected_revision": 1,
                                        "client_request_id": "close-1"})
    finally:
        ic_mod.InputControl._apply_settings_commit = real_mark
    pending = ctrl.has_prepared_intent("arm-01")
    assert pending is not None
    # 对账时文件仍是 before（开启状态）：关闭请求 → 完成关闭
    done = _write(ctrl, root, request={**REQUEST, "enabled": False,
                                       "ime_device_id": None,
                                       "expected_revision": 1,
                                       "client_request_id": "close-1"})
    assert done["outcome"] == "applied"
    assert iset.load_settings("arm-01", data_root=root)["enabled"] is False

    # 场景 B：意图已建、替换边界前进程消失（BaseException 逃过 except Exception）
    # → 意图保持 prepared、文件仍在 before：对账必须拒绝代替操作者开启。
    real_commit = iset.commit_update
    class ProcessVanished(BaseException):
        pass
    def vanishing_commit(*args, **kwargs):
        raise ProcessVanished("process died before replace (open)")
    iset.commit_update = vanishing_commit
    try:
        with pytest.raises(ProcessVanished):
            _write(ctrl, root, request={**REQUEST, "enabled": True,
                                        "expected_revision": 2,
                                        "client_request_id": "open-2"})
    finally:
        iset.commit_update = real_commit
    outcome = _write(ctrl, root, request={**REQUEST, "enabled": True,
                                          "expected_revision": 2,
                                          "client_request_id": "open-2"})
    assert outcome["outcome"] == "aborted", outcome
    assert iset.load_settings("arm-01", data_root=root)["enabled"] is False
    intent = [r for r in ctrl.records_for_device("arm-01", "settings_write")
              if r["client_request_id"] == "open-2"][-1]
    assert intent["state"] == "aborted" and intent["failure"]


def test_unmatched_file_digest_keeps_device_blocked(env, monkeypatch):
    ctrl, root = env
    _write(ctrl, root, client_request_id="seed")
    # 直接破坏文件（回滚旧版本/外部改写）→ 对账双摘要不匹配 → commit_unknown
    path = iset.settings_path("arm-01", root)
    path.write_text('{"contract_version": "device-input-settings.v1", "device_id": "arm-01",'
                    '"ime_device_id": null, "enabled": false, "revision": 9,'
                    '"revoked_through_revision": 9, "updated_at": null, "operator_id": null,'
                    '"reason": null, "client_request_id": null, "requests": {}}',
                    encoding="utf-8")
    # R2-02 更正（§4.5）：注入点 mark_settings_write → _apply_settings_commit
    calls = {"n": 0}
    real_mark = InputControl._apply_settings_commit
    def crashing_mark(self, *args, **kwargs):
        if calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("crash")
        return real_mark(self, *args, **kwargs)
    import runtime.input_control as ic_mod
    ic_mod.InputControl._apply_settings_commit = crashing_mark
    try:
        with pytest.raises(RuntimeError):
            _write(ctrl, root, request={**REQUEST, "enabled": False,
                                        "expected_revision": 9,
                                        "client_request_id": "close-x"})
    finally:
        ic_mod.InputControl._apply_settings_commit = real_mark
    # 崩溃后文件被外部改写（回滚/篡改）：既非意图 after 也非 before
    # （注意与崩溃前的 before 文件内容必须不同，否则会走"完成已授权关闭"分支）
    path.write_text('{"contract_version": "device-input-settings.v1", "device_id": "arm-01",'
                    '"ime_device_id": null, "enabled": false, "revision": 9,'
                    '"revoked_through_revision": 8, "updated_at": null, "operator_id": null,'
                    '"reason": null, "client_request_id": null, "requests": {}}',
                    encoding="utf-8")
    with pytest.raises(InputControlError) as caught:
        _write(ctrl, root, request={**REQUEST, "enabled": False,
                                    "expected_revision": 9,
                                    "client_request_id": "close-x"})
    assert caught.value.reason_code == "input_settings_commit_unknown"


def test_replay_returns_original_snapshot_without_second_intent(env):
    ctrl, root = env
    first = _write(ctrl, root)
    replay = _write(ctrl, root)
    assert replay["replayed"] is True and replay["outcome"] == "replayed"
    assert replay["settings"] == first["settings"]
    intents = ctrl.records_for_device("arm-01", "settings_write")
    assert len(intents) == 1, "重放不得产生第二次业务意图/审计"
