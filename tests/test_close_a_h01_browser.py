"""close-A：H01 页面层（R4-08-D）——采样位移超限的可见告警与留痕。

链路：注入采样超限场景（真实持久 candidate 格式，经 cb.save_candidate 落
调试台临时数据根）→ 真实调试台子进程 /api/wizard/state（正式端点）→
真实 Chrome 页面显示（过程/终局同源 poseWarnings）→ run 记录保留 →
同数据根重启调试台进程后重载页面内容一致。

文案边界（不得混写）：
- 真实测得超限 → “采样姿态超限提示” + 位移 Npx；
- 无法测得 → “采样姿态未核实提示” + “姿态未核实”（无编造数值）。
仅告警不中断不作废不强制重做；绑定/帧来源变化仍硬拒（由
test_r6_s1_h01_binding_and_restore 回归覆盖，页面不放宽）。
"""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.close_a_stack import CloseAStack, ControlServer

REPO = Path(__file__).resolve().parents[2]
REPORTS = REPO / "reports/wx-console-v1.10/repair-round7/logs"
NODE = "/usr/local/bin/node"
FRONTEND = REPO / "frontend"
MJS = Path(__file__).with_name("close_a_browser_region.mjs")


def _run_dir() -> Path:
    run_id = os.environ.get("CLOSE_A_RUN_ID") or f"close-a-h01-{uuid.uuid4().hex[:6]}"
    path = REPORTS / run_id
    if path.exists():
        path = REPORTS / f"{run_id}-{uuid.uuid4().hex[:4]}"
    (path / "browser").mkdir(parents=True, exist_ok=True)
    return path


def _chrome_available() -> bool:
    if not Path(NODE).exists():
        return False
    if not (FRONTEND / "node_modules/playwright-core/package.json").exists():
        return False
    return Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").exists()


pytestmark = pytest.mark.skipif(
    not _chrome_available(), reason="缺 node/playwright-core/Chrome 之一")


def _seed_pose_warning_run(data_root: Path, device: str = "arm-01") -> str:
    """注入采样超限场景：真实 candidate 持久格式（事件进 run 记录）。

    两条告警：① 真实测得超限（6.1px > 4px 阈值）；② 无法测得（无位移数值）。
    run 终态 STOPPED（不作废、不强制重做；告警不中断 run 的终局留痕）。
    """
    from drivers import calibration_bundle as cb
    run = {
        "runId": f"run-h01-{uuid.uuid4().hex[:8]}",
        "deviceId": device,
        "state": "STOPPED",
        "phase": "DONE",
        "iteration": 1,
        "createdAt": "2026-10-06T00:00:00Z",
        "events": [
            {"ts": "2026-10-06T00:01:00Z", "phase": "SAMPLE",
             "event": "pose_gate_skipped",
             "detail": {"category": "pose_sampling_warning",
                        "categoryDisplay": "采样姿态超限提示",
                        "stage": "sampling", "stageDisplay": "采样姿态复核",
                        "reason": "采样位移超限（真实注入：基准四角 vs 当前四角 6.1px）",
                        "measuredDisplacementPx": 6.1, "thresholdPx": 4.0}},
            {"ts": "2026-10-06T00:02:00Z", "phase": "SAMPLE",
             "event": "pose_gate_skipped",
             "detail": {"category": "pose_sampling_warning",
                        "categoryDisplay": "采样姿态未核实提示",
                        "stage": "post_travel", "stageDisplay": "走位后复核",
                        "reason": "当前帧无法定位手机四角（真实注入：无法测得）",
                        "measuredDisplacementPx": None, "thresholdPx": 4.0}},
        ],
    }
    cb.save_candidate(run, data_root)
    return run["runId"]


@pytest.fixture()
def h01(tmp_path):
    stack = CloseAStack(tmp_path, devices=("arm-01",))
    # 注入必须在调试台进程启动前落盘（向导状态/候选按数据根读取）
    run_id = _seed_pose_warning_run(stack.console.data_dir)
    stack.console.start()
    control = ControlServer(stack).start()
    run_dir = _run_dir()
    yield {"stack": stack, "control": control, "run_dir": run_dir, "run_id": run_id}
    control.stop()
    stack.close()


def _run_mjs(h01, phase: str, extra_base: str | None = None):
    run_dir = h01["run_dir"]
    report = run_dir / f"browser-{phase}.json"
    proc = subprocess.run(
        [NODE, str(MJS), phase, h01["stack"].console.base, str(report),
         str(run_dir / "browser"), str(FRONTEND), h01["control"].base]
        + ([extra_base] if extra_base else []),
        cwd=str(FRONTEND), capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, (
        f"浏览器脚本 {phase} 失败：\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    data = json.loads(report.read_text(encoding="utf-8"))
    failed = [c for c in data["cases"] if c["status"] != "passed"]
    assert not failed, f"{phase} 存在失败用例：{json.dumps(failed, ensure_ascii=False)[:2000]}"
    return data


def test_close_a_h01_page_warnings_and_retention(h01):
    """R4-08-D：页面可见告警（两类文案不混写）、run 保留、仅告警不中断。"""
    data = _run_mjs(h01, "h01")
    detail = next(c["detail"] for c in data["cases"]
                  if c["case_id"] == "h01-warnings-visible")
    text = detail["text"]
    # 文案边界：超限条目必须带 6.1px；未核实条目必须显示“姿态未核实”
    assert "采样姿态超限提示" in text and "6.1px" in text
    assert "采样姿态未核实提示" in text and "姿态未核实" in text
    kept = next(c["detail"] for c in data["cases"] if c["case_id"] == "h01-run-kept")
    assert kept["warnings"] >= 2, kept
    assert kept["failureVisible"] is False, "告警不是失败：不得显示失败框"
    assert kept["runId"] == h01["run_id"], "run 记录必须保留（不作废）"


def test_close_a_h01_new_process_reread_consistent(h01):
    """R4-08-D：同数据根重启调试台进程后，页面显示的告警内容一致。"""
    first = _run_mjs(h01, "h01")
    text_before = next(c["detail"]["text"] for c in first["cases"]
                       if c["case_id"] == "h01-warnings-visible")
    # 同数据根重启调试台进程（页面重载读新进程的正式向导状态）
    h01["stack"].restart_console()
    second = _run_mjs(h01, "h01-restart")
    text_after = next(c["detail"]["text"] for c in second["cases"]
                      if c["case_id"] == "h01-warnings-visible")
    assert text_after == text_before, "新进程重读后页面告警内容必须一致"
