"""L01 验收（phone-search-region.v1）：pixel_rect 校验/保存/预览/读取/CAS。

R2-15 修订（2026-10-04）：rect 冻结域为 source_norm {x,y,width,height}；
服务端从证据链读取来源身份（不自报字段）；save 携带服务端注入的 principal。
端到端行为由 tests/test_r2_region_chain.py 覆盖；本文件保留存储层语义检查。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "..")

from runtime.layout_region import (
    LayoutRegionService, LayoutRegionStore, RegionError,
    _pixel_rect_from_norm,
)


@pytest.fixture()
def svc(tmp_path):
    store = LayoutRegionStore(tmp_path / "layout.db")
    return LayoutRegionService(store), store


def test_pixel_rect_floor_ceil_and_min_4px():
    # 左上 floor、右下 ceil、右/下排他
    assert _pixel_rect_from_norm(
        {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
        640, 480) == {"left": 64, "top": 48, "right": 576, "bottom": 432}
    with pytest.raises(RegionError, match="至少 4"):
        _pixel_rect_from_norm(
            {"x": 0, "y": 0, "width": 0.001, "height": 0.001},
            100, 100)


def test_save_requires_valid_preview(svc):
    service, _store = svc
    with pytest.raises(RegionError, match="不存在"):
        service.save("arm-01", {"contract_version":
                                "phone-search-region-write.v1",
                                "correlation_id": "00000000-0000-4000-8000-000000000001",
                                "client_request_id": "00000000-0000-4000-8000-000000000002",
                                "expected_revision": 0,
                                "preview_id": "nope",
                                "region_digest": "d", "publish": True},
                     "operator")


def test_legacy_rect_domain_rejected(svc):
    """旧 left/top/right/bottom 域按冻结契约拒绝（R2-15 坐标域收口）。"""
    from runtime.layout_region import _parse_norm_rect
    with pytest.raises(RegionError, match="x,y,width,height"):
        _parse_norm_rect({"left": 0.1, "top": 0.1,
                          "right": 0.9, "bottom": 0.9})
