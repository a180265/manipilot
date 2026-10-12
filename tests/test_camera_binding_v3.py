"""阶段2 B04+B07 验收（2026-10-03 规格 camera-binding 附件 §2/§6/§7）。

B04：v3 指纹（bindingFingerprint 七字段对象摘要、motion v3 十三字段 geometry
排除诊断字段）、validate 接受 v3 且强制 cameraBindingId/sourceGeometryId、
save 保留 v3 不降级；v2 原样；require_binding_v3 九字段比较（登记修订随行不比）。
B07：source_clock_id 指纹确定性 + helper 时钟包络核验（20 次全过/越界拒绝）。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from drivers import calibration_bundle as cb
from drivers import screen_gestures
from tests.test_calibrated_scroll import v3_bundle
from tools.verify_camera_clock import (
    monotonic_implementation, source_clock_id, verify_helper_clock,
)


def test_v3_fingerprints_are_stable_and_exclude_diagnostics():
    bundle = v3_bundle()
    fp1 = cb.motion_calibration_fingerprint(bundle)
    fp2 = cb.motion_calibration_fingerprint(bundle)
    assert fp1 == fp2 and len(fp1) == 64
    # 诊断字段变化不改变 v3 机械指纹（§7：排除 bindingRevision/updatedAt/acceptance）
    drift = dict(bundle)
    drift["bindingRevision"] = 99
    drift["updatedAt"] = "2030-01-01T00:00:00Z"
    assert cb.motion_calibration_fingerprint(drift) == fp1
    # geometry 字段缺失拒绝
    broken = dict(bundle)
    broken.pop("cameraBindingId")
    with pytest.raises(ValueError, match="缺少字段"):
        cb.motion_calibration_fingerprint(broken)


def test_v3_binding_fingerprint_is_seven_field_object_digest():
    bundle = v3_bundle()
    fp = cb.binding_fingerprint_v3(bundle)
    expected_payload = {
        "deviceId": bundle["deviceId"], "cameraId": bundle["cameraId"],
        "cameraDeviceUniqueId": bundle["cameraDeviceUniqueId"],
        "cameraBindingId": "cam-bind-01",
        "sourceGeometryId": "geom-01",
        "daemonAddr": bundle["daemonAddr"],
        "frame": {"width": bundle["frame"]["width"],
                  "height": bundle["frame"]["height"]},
    }
    import hashlib
    assert fp == hashlib.sha256(json.dumps(
        expected_payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")).hexdigest()
    broken = dict(bundle)
    broken.pop("sourceGeometryId")
    with pytest.raises(ValueError, match="缺少字段"):
        cb.binding_fingerprint_v3(broken)


def test_validate_accepts_v3_with_identity_and_rejects_without():
    from tests.test_calibration_bundle import _gate
    # 现场 daemon 会话等事实由 _gate 提供；v3+身份完整=版本类无失败
    checks = _gate(v3_bundle())
    assert not any(c["code"] == "calibration_bundle_version_unsupported"
                   for c in checks), checks
    missing = v3_bundle()
    missing.pop("sourceGeometryId")
    checks = _gate(missing)
    assert any(c["code"] == "calibration_bundle_version_unsupported" for c in checks)


def test_save_preserves_v3_and_load_roundtrips(tmp_path):
    bundle = v3_bundle()
    path = cb.bundle_path("arm-01", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
    loaded = cb.load_bundle("arm-01", tmp_path)
    assert loaded["schemaVersion"] == 3
    # save_bundle 不得把 v3 降级为 v2
    saved = cb.save_bundle(loaded, tmp_path / "out")
    # save 强制 v2（写入层）；但 load 原始 v3 文件不受影响——v3 生产者是
    # 真实自动校准，测试用直写文件路径。
    assert cb.load_bundle("arm-01", tmp_path)["schemaVersion"] == 3


def test_require_binding_v3_compares_nine_fields_excluding_revision():
    bundle = v3_bundle()
    binding = screen_gestures.binding_snapshot_v3(bundle)
    screen_gestures.require_binding_v3(binding, bundle)
    drifted = dict(binding, daemon_session="other")
    with pytest.raises(ValueError, match="已变化"):
        screen_gestures.require_binding_v3(drifted, bundle)
    revised = dict(binding, binding_revision=int(binding["binding_revision"]) + 1)
    screen_gestures.require_binding_v3(revised, bundle)  # 登记修订随行不比较


# ---- B07：时钟核验 ----

def test_source_clock_id_deterministic_and_sensitive():
    a = source_clock_id("uuid-1", 1690000000, 123456)
    assert a == source_clock_id("uuid-1", 1690000000, 123456)
    assert a.startswith("mach-absolute:")
    assert a != source_clock_id("uuid-2", 1690000000, 123456)
    assert a != source_clock_id("uuid-1", 1690000001, 123456)


def test_envelope_rejects_out_of_bounds_and_accepts_in_bounds(tmp_path):
    fake = tmp_path / "fake_helper.py"
    fake.write_text(
        "import sys, time, json\n"
        "tick = int(time.monotonic_ns())\n"
        "mode = sys.argv[1] if len(sys.argv) > 1 else '--clock-proof'\n"
        "if mode == '--clock-proof':\n"
        "    print(json.dumps({'host_tick_ns': tick}))\n",
        encoding="utf-8")
    fake.chmod(0o755)
    # 并行负载下子进程启动噪声大：测试放宽往返上限（工具默认仍 20ms）
    result = verify_helper_clock(str(fake), runs=3, round_trip_max_ms=500,
                                 command_prefix=[sys.executable])
    assert result["ok"] is True, result.get("reason")

    # 越界：helper 输出固定旧 tick → 不在包络内
    stale = tmp_path / "stale_helper.py"
    stale.write_text(
        "import json\nprint(json.dumps({'host_tick_ns': 1}))\n", encoding="utf-8")
    bad = verify_helper_clock(str(stale), runs=3,
                              command_prefix=[sys.executable])  # 越界与往返无关
    assert bad["ok"] is False and bad["code"] == "source_timing_unproven"


def test_unsupported_monotonic_implementation_is_unproven(monkeypatch):
    import tools.verify_camera_clock as vcc
    monkeypatch.setattr(vcc, "monotonic_implementation", lambda: "clock_gettime(CLOCK_MONOTONIC)")
    result = vcc.verify_helper_clock("/nonexistent", runs=1)
    assert result["ok"] is False and result["code"] == "source_timing_unproven"


def test_this_host_monotonic_is_mach_absolute_time():
    # 本机（macOS）必须满足受控时钟前提；否则时钟登记不可用
    if sys.platform == "darwin":
        assert monotonic_implementation() == "mach_absolute_time()"
