"""屏幕反馈标定测试：仿射拟合、范围反解、Z 探针模板与策略。"""
from __future__ import annotations

import math

import pytest

from drivers.grbl import GrblPolicyError, validate_line
from drivers.real_arm import PRESET_TEMPLATES
from drivers.screen_calibration import CalibrationError, fit_affine, page_to_machine, screen_range_machine


def test_fit_affine_recovers_known_transform():
    """已知映射（旋转 90° + 缩放 12px/mm + 平移）应被精确恢复。"""
    # page = A·machine + offset；A 旋转 90°、12 px/mm：machine(x,y) → page(-12y, 12x)
    A = [[0.0, -12.0], [12.0, 0.0]]
    offset = [640.0, 360.0]
    machine = [(-10.0, -10.0), (0.0, -10.0), (10.0, -10.0), (10.0, 0.0), (10.0, 10.0),
               (0.0, 10.0), (-10.0, 10.0), (-10.0, 0.0), (0.0, 0.0)]
    page = [(A[0][0] * x + A[0][1] * y + offset[0], A[1][0] * x + A[1][1] * y + offset[1])
            for x, y in machine]
    calibration = fit_affine(machine, page)
    assert calibration["residual_max_px"] < 1e-6
    assert calibration["scale_px_per_mm"] == pytest.approx(12.0)
    assert calibration["rotation_deg"] == pytest.approx(90.0)
    # 反解：page 坐标还原回 machine
    px, py = page[0]
    mx, my = page_to_machine(calibration, (px, py))
    assert (mx, my) == pytest.approx(machine[0])


def test_fit_affine_handles_noise():
    """带 2px 内噪声时残差应保持小量，拟合不发散。"""
    machine = [(-10.0, -10.0), (0.0, -10.0), (10.0, -10.0), (10.0, 0.0),
               (10.0, 10.0), (0.0, 10.0), (-10.0, 10.0), (-10.0, 0.0)]
    page = [(480.0 + 12.0 * x, 320.0 + 12.0 * y) for x, y in machine]
    page[2] = (page[2][0] + 1.5, page[2][1] - 1.0)  # 人为噪声
    calibration = fit_affine(machine, page)
    assert calibration["scale_px_per_mm"] == pytest.approx(12.0, rel=0.01)
    assert calibration["residual_rms_px"] < 1.0


def test_fit_affine_rejects_degenerate_input():
    with pytest.raises(CalibrationError):
        fit_affine([(0.0, 0.0)], [(1.0, 1.0)])  # 点数不足
    with pytest.raises(CalibrationError):
        # 共线点：矩阵退化
        fit_affine([(0.0, 0.0), (1.0, 1.0), (2.0, 2.0)], [(0.0, 0.0), (10.0, 10.0), (20.0, 20.0)])


def test_screen_range_inset_from_viewport():
    machine = [(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0)]
    page = [(480.0 + 12.0 * x, 320.0 + 12.0 * y) for x, y in machine]  # 12px/mm 无旋转
    calibration = fit_affine(machine, page)
    screen_range = screen_range_machine(calibration, {"w": 400, "h": 400}, inset_mm=2.0)
    # 页面角 (0,0) → machine((0-480)/12, (0-320)/12) = (-40, -26.67)
    # 页面角 (400,400) → machine((400-480)/12, (400-320)/12) = (-6.67, 6.67)
    assert screen_range["machine_corners"]["page_origin"] == pytest.approx([-40.0, -26.67], abs=0.01)
    assert screen_range["machine_corners"]["page_bottom_right"] == pytest.approx([-6.67, 6.67], abs=0.01)
    assert screen_range["safe_min"] == pytest.approx([-38.0, -24.67], abs=0.01)
    assert screen_range["safe_max"] == pytest.approx([-8.67, 4.67], abs=0.01)


def test_z_probe_templates_registered_and_policy_compliant():
    """Z 探针 0.25mm 步进模板：已登记、策略放行、Z 探针对（下/抬）成对。"""
    for name in ("z_lower_0p25_conservative", "z_lift_0p25_conservative"):
        assert name in PRESET_TEMPLATES
        template = PRESET_TEMPLATES[name]
        for line in template:
            validate_line(line, distance_mode="G91")  # 不抛即合规
    assert "Z0.25 F30" in PRESET_TEMPLATES["z_lower_0p25_conservative"][1]
    assert "Z-0.25 F30" in PRESET_TEMPLATES["z_lift_0p25_conservative"][1]


def test_calibration_output_contract_keys():
    """拟合输出必须包含保存到 configs 的全部关键字段。"""
    machine = [(-10.0, -10.0), (10.0, -10.0), (10.0, 10.0), (-10.0, 10.0)]
    page = [(480.0 + 12.0 * x, 320.0 + 12.0 * y) for x, y in machine]
    calibration = fit_affine(machine, page)
    for key in ("A", "offset", "residual_max_px", "scale_px_per_mm", "rotation_deg", "n_points"):
        assert key in calibration
