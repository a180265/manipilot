"""8792「点哪打哪」的屏幕边界闸门。

背景：调试台相机→机械换算曾直接 apply_homography 反解，屏幕外目标
不会被拒绝，探针被派到屏幕外撞上边框导致 X 轴丢步。修复后必须走
生产同款 point_from_camera（轮廓/边界/精度全套校验）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from motor_debug_server import _camera_point_to_robot  # noqa: E402
from tests.test_screen_gesture_driver import gesture_bundle  # noqa: E402


@pytest.fixture()
def bundle_with_model():
    bundle = gesture_bundle()
    return bundle, bundle["xy"]["robotToScreen"]


def test_in_screen_camera_point_resolves(bundle_with_model):
    bundle, model = bundle_with_model
    # 相机画面中心附近（屏幕中部）应正常反解
    rx, ry = _camera_point_to_robot(bundle, model, 640, 350)
    assert isinstance(rx, float) and isinstance(ry, float)


def test_off_screen_camera_point_rejected(bundle_with_model):
    bundle, model = bundle_with_model
    # 屏幕外（相机画面右上角落，映射到屏幕轮廓之外）必须拒绝，不得给出机械目标
    with pytest.raises(ValueError):
        _camera_point_to_robot(bundle, model, 1270, 10)


def test_far_off_screen_camera_point_rejected(bundle_with_model):
    bundle, model = bundle_with_model
    with pytest.raises(ValueError):
        _camera_point_to_robot(bundle, model, -40, 700)
