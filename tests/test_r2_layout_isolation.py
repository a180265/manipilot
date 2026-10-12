"""R2-25：布局适配服务按 Runtime 隔离。

修前红测（快照 tests/test_r2_layout_isolation_prefix.py，走生产装配
`LayoutAdaptationService.shared`）已证明：同进程两个不同临时根、相同
device_id 的服务经进程级单例串库——B 能查到 A 的会话，B 发布续排 A 的
版本号，且服务无 close()（pytest 退出码 1，失败于"B 不得看见 A 的会话"
目标断言；日志 logs/r2-25-prefix-red.log）。

修后：`shared` 单例按卡片要求移除；正式装配由 Runtime.layout_bundle 持有
适配服务/Store/状态库/锁（本文件 LayoutBundle 镜像该形状）。断言：
版本、指针、会话、预览、作者和文件全部隔离；A 关闭 B 仍可用；各自重启
从各自库恢复。测试更正理由与修前重证见 reports/.../R2-D1.md。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "..")

import pytest

from runtime.layout_adaptation import (
    PAGE_STATES,
    AdaptationError,
    LayoutAdaptationService,
)
from tests.layout_r2_helpers import (
    DEFAULT_BINDING,
    DEFAULT_GEOMETRY_ID,
    FakeCalibrationProvider,
    FakeCaptureProvider,
    FakeClock,
    LayoutBundle,
    build_page_frames,
    build_template_assets,
)


def _make_bundle(root: Path, clock) -> LayoutBundle:
    return LayoutBundle(root, clock=clock,
                        capture_provider=FakeCaptureProvider(
                            build_page_frames(build_template_assets()),
                            binding=DEFAULT_BINDING,
                            source_geometry_id=DEFAULT_GEOMETRY_ID),
                        calibration_provider=FakeCalibrationProvider())


def _full_publish(bundle: LayoutBundle, device: str, principal: str) -> dict:
    svc = bundle.adaptation_svc
    sid = svc.start_session(device, {
        "template_ref": {"id": "tpl-1", "version": 1},
        "principal_id": principal,
        "installation_binding": DEFAULT_BINDING})["session_id"]
    revision = 1
    for state in PAGE_STATES:
        capture = svc.capture(device, {
            "client_request_id": str(uuid.uuid4()),
            "owner_kind": "session", "owner_id": sid,
            "expected_owner_revision": revision,
            "page_state": state, "controlled_test_confirmed": True})
        view = svc.write_page(device, sid, state, {
            "client_request_id": str(uuid.uuid4()),
            "expected_session_revision": revision,
            "source_evidence_ids": capture["source_evidence_ids"],
            "local_overrides": []})
        revision = view["session_revision"]
    preview = svc.preview(device, {"capture_session_id": sid})
    return svc.publish(device, {
        "client_request_id": str(uuid.uuid4()),
        "session_id": sid, "preview_id": preview["preview_id"],
        "principal_id": principal, "expected_revision": 0,
        "accepted_items": preview["accepted_items"]})


def test_r2_25_two_runtime_bundles_isolated_same_device_id(tmp_path):
    clock = FakeClock()
    root_a, root_b = tmp_path / "runtime-a", tmp_path / "runtime-b"
    bundle_a = _make_bundle(root_a, clock)
    bundle_b = _make_bundle(root_b, clock)
    try:
        svc_a, svc_b = bundle_a.adaptation_svc, bundle_b.adaptation_svc
        device = "arm-01"  # 两个 Runtime 中相同的 device_id
        rec_a = _full_publish(bundle_a, device, "op-a")

        # 版本/指针/会话隔离：B 查不到 A 的任何资料
        assert svc_b.get_version(device, rec_a["record"]["version"]) is None
        assert svc_b.get_current(device) is None
        # B 独立操作：自己的版本 1（不接 A 的序列、不见 A 的文件）
        rec_b = _full_publish(bundle_b, device, "op-b")
        assert rec_b["record"]["version"] == 1
        assert rec_b["record"]["sha256"] != rec_a["record"]["sha256"]
        assert rec_b["record"]["reviewed_by"] == "op-b"
        assert svc_a.get_version(device, 1)["reviewed_by"] == "op-a"
        assert svc_b.get_version(device, 1)["reviewed_by"] == "op-b"
        # 文件隔离：各自 data_root 下各自的版本文件
        assert (root_a / "data" / "devices" / device
                / "layout_adaptation" / "versions" / "1.json").exists()
        assert (root_b / "data" / "devices" / device
                / "layout_adaptation" / "versions" / "1.json").exists()
        # 指针隔离：各自 revision=1、指向各自摘要
        pa = bundle_a.state.adaptation_pointer_get(device)
        pb = bundle_b.state.adaptation_pointer_get(device)
        assert pa["revision"] == 1 and pb["revision"] == 1
        assert pa["sha256"] == rec_a["record"]["sha256"]
        assert pb["sha256"] == rec_b["record"]["sha256"]

        # A 关闭只释放自己；B 仍可用（可继续创建会话/查询）
        bundle_a.close()
        sid_b2 = svc_b.start_session(device, {
            "template_ref": {"id": "tpl-1", "version": 1},
            "principal_id": "op-b",
            "installation_binding": DEFAULT_BINDING})["session_id"]
        assert svc_b.get_session(device, sid_b2) is not None
        with pytest.raises(AdaptationError, match="service_closed"):
            svc_a.start_session(device, {
                "template_ref": {"id": "tpl-1", "version": 1},
                "principal_id": "op-a",
                "installation_binding": DEFAULT_BINDING})

        # 各自重启从各自库恢复（不借另一实例内存弥补）
        bundle_b.close()
        ra = _make_bundle(root_a, clock)
        rb = _make_bundle(root_b, clock)
        try:
            assert ra.adaptation_svc.get_version(device, 1)["sha256"] \
                == rec_a["record"]["sha256"]
            assert rb.adaptation_svc.get_version(device, 1)["sha256"] \
                == rec_b["record"]["sha256"]
            assert ra.adaptation_svc.get_current(device)["version"] == 1
            assert rb.adaptation_svc.get_current(device)["version"] == 1
            # 重启后各自续排：下一个版本都是 2（互不知晓）
            assert ra.state.next_adaptation_version(device) == 2
            assert rb.state.next_adaptation_version(device) == 2
        finally:
            ra.close()
            rb.close()
    finally:
        bundle_a.close()
        bundle_b.close()


def test_r2_25_process_level_shared_singleton_is_gone():
    """进程级 `shared` 单例不再是可用装配（回归守卫：禁止复活）。"""
    assert not hasattr(LayoutAdaptationService, "shared"), \
        "进程级 shared 单例是 R2-25 缺陷根源，不得复活"
