"""v1.10 修复单 FIX-01 验收（§4.4/§4.8.4/P01-P08）。

F01-1/2 核心：输入认领回调只允许在产生本次输入接触的命令（tap）sendall 前
执行一次；走位/抬笔/收尾命令不携带认领。认领失败=接触命令零下发。
F01-3：认领回调内重验失败（开关关闭）时接触 0 次。

复用 test_daemon_arm 的 FakeDaemon/夹具（真实 DaemonArmDriver + 真实
DaemonTCPTransport.cmd 边界）。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_daemon_arm import (FakeDaemon, make_driver,  # noqa: E402
                             write_device_files, write_ready_bundle,
                             enable_ui_target)


@pytest.fixture()
def env(tmp_path):
    write_device_files(tmp_path)
    write_ready_bundle(tmp_path)
    daemon = FakeDaemon().start()
    driver = make_driver(daemon, tmp_path)
    try:
        asyncio.run(driver.connect())
        yield daemon, driver, tmp_path
    finally:
        asyncio.run(driver.disconnect())
        daemon.stop()


def _claim_log():
    return {"callbacks": 0, "results": []}


def _make_claim(log, *, allow=True, reason="输入与发送未允许"):
    def callback():
        log["callbacks"] += 1
        if not allow:
            raise PermissionError(reason)
        log["results"].append("claimed")
    return {"callback": callback}


def test_f01_2_claim_fires_only_on_contact_command(env, monkeypatch):
    """走位/抬笔不带认领；tap（接触）恰一次认领。"""
    monkeypatch.setattr(
        __import__("drivers.daemon_arm", fromlist=["DaemonArmDriver"]).DaemonArmDriver,
        "_input_switch_allowed", lambda self, calib: True)
    daemon, driver, tmp = env
    enable_ui_target(tmp, "ime_candidate_bar", nx=0.5, ny=0.5)
    log = _claim_log()
    result = asyncio.run(driver.click(
        {"target_id": "ime_candidate_bar", "input_claim": _make_claim(log)}))
    assert result["ok"] is True
    assert log["callbacks"] == 1, "正常一次输入只消费一次认领"
    assert log["results"] == ["claimed"]


def test_f01_2_non_input_click_has_no_claim(env, monkeypatch):
    """非输入目标（无 input_claim）不产生认领消费——回归护栏。"""
    monkeypatch.setattr(
        __import__("drivers.daemon_arm", fromlist=["DaemonArmDriver"]).DaemonArmDriver,
        "_input_switch_allowed", lambda self, calib: True)
    daemon, driver, tmp = env
    enable_ui_target(tmp, "ime_candidate_bar", nx=0.5, ny=0.5)
    result = asyncio.run(driver.click({"target_id": "ime_candidate_bar"}))
    assert result["ok"] is True  # 无 input_claim 时行为与此前一致


def test_f01_1_claim_rejected_at_contact_zero_contact_sendall(env, monkeypatch):
    """F01-1：认领在接触命令 sendall 前拒绝 → 接触零下发；
    已走位事实保留（motion 已开始），错误按输入未开始语义抛出。"""
    daemon, driver, tmp = env
    enable_ui_target(tmp, "ime_candidate_bar", nx=0.5, ny=0.5)
    # 准入时开关开启；认领时刻回调拒绝=准入与认领之间"关闭先赢"
    monkeypatch.setattr(
        __import__("drivers.daemon_arm", fromlist=["DaemonArmDriver"]).DaemonArmDriver,
        "_input_switch_allowed", lambda self, calib: True)
    log = _claim_log()
    with pytest.raises(PermissionError):
        asyncio.run(driver.click(
            {"target_id": "ime_candidate_bar",
             "input_claim": _make_claim(log, allow=False,
                                        reason="输入与发送未允许")}))
    assert log["callbacks"] == 1
    # 接触命令（TAP）不得出现在 daemon 已受理命令中
    taps = [c for c in daemon.commands if c.startswith("TAP ")]
    assert taps == [], "认领拒绝后接触命令必须零下发"
    # 走位命令已经发生（真实运动事实保留，不谎称整个动作未开始）
    assert any(c.startswith("XY ") or c.startswith("Z ")
               for c in daemon.commands), "已执行的走位事实必须保留"


def test_f01_3_claim_reverify_switch_off_blocks_contact(env, monkeypatch):
    """F01-3：回调内按当前开关状态重验（关闭先赢=接触 0 次）。"""
    daemon, driver, tmp = env
    enable_ui_target(tmp, "send_button", nx=0.5, ny=0.9,
                     target_mode="tap", name_display="发送按钮")
    monkeypatch.setattr(
        __import__("drivers.daemon_arm", fromlist=["DaemonArmDriver"]).DaemonArmDriver,
        "_input_switch_allowed", lambda self, calib: True)
    calls = {"n": 0}

    def callback():
        calls["n"] += 1
        # 模拟认领时刻开关已被关闭（关闭先赢）
        raise PermissionError("输入与发送未允许（开关关闭）")

    with pytest.raises(PermissionError):
        asyncio.run(driver.click({"target_id": "send_button",
                                  "input_claim": {"callback": callback}}))
    assert calls["n"] == 1
    assert not [c for c in daemon.commands if c.startswith("TAP ")]
