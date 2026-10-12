"""B07（2026-10-03 规格 §6）：同机时钟登记与 helper 时钟证明核验（只读）。

- `--clock-proof --helper <可执行文件> [--json]`：由 Fleet 在本机启动 helper 的
  `--clock-proof` 子命令（不接受 HTTP 自报），前后 monotonic_ns 包住 helper 读取的
  host tick ns，连续 20 次全部位于包络内且往返 <=20ms；
- `--install-id <文件>`：输出 source_clock_id 指纹（mach-absolute:<摘要>），
  指纹输入固定 {host_installation_id,boot_seconds,boot_microseconds} 规范化 JSON；
- 校验 `time.get_clock_info("monotonic").implementation == "mach_absolute_time()"`，
  不满足即 source_timing_unproven（退出码 3）；
- `--pts-provenance` / `--capture-uncertainty-ms`：当前捕获格式的时间戳来源证明
  与已证不确定度上界。RTT 通过不等于 PTS 误差通过：缺证明或总 uncertainty>20ms
  时 clock_registration 不可用，不得动作（不把固定 0/20 或往返均值当测量证明）。

只做纯计算与本机 OS 读取；不启动相机、不触机械臂。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time

ROUND_TRIP_MAX_MS = 20
ENVELOPE_RUNS = 20
PTS_PROVENANCE_REQUIRED = "camera_pts_host_clock.v1"
TOTAL_UNCERTAINTY_MAX_MS = 20


def source_clock_id(host_installation_id: str,
                    boot_seconds: int, boot_microseconds: int) -> str:
    """§6：source_clock_id = mach-absolute:<指纹>；指纹为规范 JSON SHA-256。"""
    payload = json.dumps(
        {"host_installation_id": host_installation_id,
         "boot_seconds": int(boot_seconds),
         "boot_microseconds": int(boot_microseconds)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"mach-absolute:{digest}"


def host_fingerprint(host_installation_id: str) -> dict:
    """本机宿主指纹：安装 UUID + kern.boottime 秒/微秒（本机 OS 读取）。"""
    import subprocess as sp
    boot = sp.run(["sysctl", "-n", "kern.boottime"], capture_output=True,
                  text=True, check=True).stdout.strip()
    # 形如 { sec = 1690000000, usec = 123456 } ...
    import re
    seconds = int(re.search(r"sec\s*=\s*(\d+)", boot).group(1))
    micro = int(re.search(r"usec\s*=\s*(\d+)", boot).group(1))
    return {"host_installation_id": host_installation_id,
            "boot_seconds": seconds, "boot_microseconds": micro}


def monotonic_implementation() -> str:
    return time.get_clock_info("monotonic").implementation


def verify_helper_clock(helper: str, *, runs: int = ENVELOPE_RUNS,
                        round_trip_max_ms: int = ROUND_TRIP_MAX_MS,
                        command_prefix: list[str] | None = None) -> dict:
    """连续 runs 次：Fleet 前后 monotonic_ns 包住 helper 输出的 host tick ns。

    helper --clock-proof 输出一行 JSON {"host_tick_ns": <int>}。
    全部位于包络内且每次往返 <= 上限才算通过；不从成功子集挑次数。
    """
    if monotonic_implementation() != "mach_absolute_time()":
        return {"ok": False, "code": "source_timing_unproven",
                "reason": f"monotonic 实现不受支持：{monotonic_implementation()}"}
    samples = []
    for _index in range(runs):
        before = time.monotonic_ns()
        command = [helper, "--clock-proof"]
        if command_prefix:
            command = [*command_prefix, *command]
        proc = subprocess.run(command, capture_output=True,
                              text=True, timeout=10)
        after = time.monotonic_ns()
        if proc.returncode != 0:
            return {"ok": False, "code": "source_timing_unproven",
                    "reason": f"helper 退出码 {proc.returncode}"}
        try:
            tick = int(json.loads(proc.stdout.strip())["host_tick_ns"])
        except (ValueError, KeyError, TypeError) as exc:
            return {"ok": False, "code": "source_timing_unproven",
                    "reason": f"helper 输出无效：{exc}"}
        round_trip_ms = (after - before) / 1_000_000
        in_envelope = before <= tick <= after
        samples.append({"before_ns": before, "host_tick_ns": tick,
                        "after_ns": after, "round_trip_ms": round_trip_ms,
                        "in_envelope": in_envelope})
        if not in_envelope or round_trip_ms > round_trip_max_ms:
            return {"ok": False, "code": "source_timing_unproven",
                    "reason": (f"第 {_index + 1} 次越界/超时：往返 "
                               f"{round_trip_ms:.1f}ms，包络内={in_envelope}"),
                    "samples": samples}
    return {"ok": True, "runs": runs, "samples": samples,
            "round_trip_max_ms": round_trip_max_ms}


def build_clock_registration(envelope_result: dict, *,
                             source_clock_id: str | None,
                             pts_provenance: str | None,
                             capture_uncertainty_ms: int | None,
                             helper_path: str | None = None,
                             helper_sha256: str | None = None,
                             format_id: str | None = None) -> dict:
    """§6：clock_registration = 包络核验 + PTS 来源证明 + 总不确定度判定。

    往返（RTT）合格只是跨进程转换核验；采样不确定度必须来自当前捕获格式的
    时间戳来源证明上界 + 整数换算舍入误差。缺证明、总 uncertainty>20ms 或
    包络核验未过，registration 即不可用（不得动作），不从成功子集拼证据。
    R3-10：登记审计关联当前捕获格式（format_id）；帧桥受控核验归档同形记录。
    """
    registration = {
        "contract_version": "clock-registration.v1",
        "source_clock_id": source_clock_id,
        "timing_provenance_required": PTS_PROVENANCE_REQUIRED,
        "helper_path": helper_path,
        "helper_sha256": helper_sha256,
        "format_id": format_id,
        "envelope_ok": bool(envelope_result.get("ok")),
        "envelope_runs": envelope_result.get("runs"),
    }
    reasons = []
    if not envelope_result.get("ok"):
        reasons.append(str(envelope_result.get("reason") or "包络核验未通过"))
    if pts_provenance != PTS_PROVENANCE_REQUIRED:
        reasons.append(f"PTS 来源证明缺失或不受支持：{pts_provenance!r}")
    uncertainty_total = None
    if capture_uncertainty_ms is None:
        reasons.append("缺少捕获格式已证不确定度上界（不得当 0/20）")
    else:
        # +1ms 整数换算舍入裕量（ns→ms 进位），不采用往返均值或固定 20
        uncertainty_total = int(capture_uncertainty_ms) + 1
        if uncertainty_total > TOTAL_UNCERTAINTY_MAX_MS:
            reasons.append(
                f"总不确定度 {uncertainty_total}ms > {TOTAL_UNCERTAINTY_MAX_MS}ms")
    registration.update({
        "pts_provenance": pts_provenance,
        "capture_uncertainty_ms": capture_uncertainty_ms,
        "total_uncertainty_ms": uncertainty_total,
        "ok": not reasons,
        "reasons": reasons,
    })
    return registration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="相机来源时钟登记与核验（只读）")
    parser.add_argument("--clock-proof", action="store_true",
                        help="执行 helper 跨进程时钟包络核验（20 次全过）")
    parser.add_argument("--helper", help="CameraHelper 可执行文件路径")
    parser.add_argument("--install-id-file",
                        help="data_root/camera_source/host_installation_id 文件")
    parser.add_argument("--pts-provenance",
                        help="当前捕获格式的时间戳来源证明（固定 "
                             f"{PTS_PROVENANCE_REQUIRED}）")
    parser.add_argument("--capture-uncertainty-ms", type=int,
                        help="捕获格式已证采样不确定度上界（毫秒；不含 RTT）")
    parser.add_argument("--format-id",
                        help="当前捕获格式 format_id（fourcc:<8hex>;"
                             "size:<宽>x<高>;pixel-aspect:<n>/<d>，登记审计用）")
    parser.add_argument("--json", action="store_true", help="JSON 输出")
    args = parser.parse_args(argv)

    result: dict = {}
    if args.install_id_file:
        path = args.install_id_file
        raw = open(path, encoding="utf-8").read().strip()
        if len(raw) != 36 or raw != raw.lower():
            print(json.dumps({"ok": False,
                              "code": "source_timing_unproven",
                              "reason": "host_installation_id 缺失/非小写 UUID"}),
                  file=sys.stderr)
            return 3
        fingerprint = host_fingerprint(raw)
        result["source_clock_id"] = source_clock_id(**fingerprint)
        result["fingerprint"] = fingerprint
    if args.clock_proof:
        if not args.helper:
            print("--clock-proof 需要 --helper", file=sys.stderr)
            return 2
        result.update(verify_helper_clock(args.helper))
    if args.pts_provenance is not None or args.capture_uncertainty_ms is not None \
            or args.clock_proof:
        helper_digest = None
        if args.helper:
            helper_digest = hashlib.sha256(
                open(args.helper, "rb").read()).hexdigest()
        result["clock_registration"] = build_clock_registration(
            result if args.clock_proof else {"ok": False,
                                             "reason": "未执行包络核验"},
            source_clock_id=result.get("source_clock_id"),
            pts_provenance=args.pts_provenance,
            capture_uncertainty_ms=args.capture_uncertainty_ms,
            helper_path=args.helper,
            helper_sha256=helper_digest,
            format_id=args.format_id)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for key, value in result.items():
            if key != "samples":
                print(f"{key}: {value}")
    if result.get("ok") is False or \
            (result.get("clock_registration") or {}).get("ok") is False:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
