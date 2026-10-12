"""D4c：通用画面位移测量（规范第 9.2、22.14 节）。

合成图验证：真位移、真静止、阈值之间 unknown、无法追踪≠没滚动、
探头遮挡排除、横向位移拒绝、OCR 行互证、静止测量的独立阈值。
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from drivers import scroll_observation as so

W, H = 320, 640
ROI = {"x": 16, "y": 32, "width": 288, "height": 576}


def textured(seed: int = 7, w: int = W, h: int = H) -> np.ndarray:
    """双向渐变背景 + 大尺度内容块，模拟聊天列表的多尺度结构。"""
    rng = np.random.default_rng(seed)
    img = (120 + ((np.arange(h)[:, None] // 36) * 6)
           + ((np.arange(w)[None, :] // 44) * 7)).astype(np.uint8)
    for _ in range(80):
        x, y = int(rng.integers(0, w - 110)), int(rng.integers(0, h - 50))
        cv2.rectangle(img, (x, y),
                      (x + int(rng.integers(30, 104)), y + int(rng.integers(14, 44))),
                      int(rng.integers(30, 255)), -1)
    return img


def rolled(img: np.ndarray, dy: int) -> np.ndarray:
    return np.roll(img, dy, axis=0)


@pytest.fixture(scope="module")
def base():
    return textured()


class TestMeasureDisplacement:
    def test_real_displacement_measured(self, base):
        result = so.measure_displacement(base, rolled(base, 24), ROI)
        assert result["status"] == "displaced"
        assert result["dy_px"] == pytest.approx(24, abs=2)

    def test_negative_displacement(self, base):
        result = so.measure_displacement(base, rolled(base, -26), ROI)
        assert result["status"] == "displaced"
        assert result["dy_px"] == pytest.approx(-26, abs=2)

    def test_true_stationary(self, base):
        result = so.measure_displacement(base, base.copy(), ROI)
        assert result["status"] == "stationary"
        assert abs(result["dy_px"]) < 2

    def test_between_thresholds_is_unknown(self, base):
        # 0.01*H=5.76 < 12 < 0.04*H=23：既不是静止也不是有效滚动
        result = so.measure_displacement(base, rolled(base, 12), ROI)
        assert result["status"] == "unknown"

    def test_untrackable_is_unknown_not_stationary(self, base):
        """帧跨度过大无法追踪：算法失败≠没滚动。"""
        different = textured(seed=99)
        result = so.measure_displacement(base, different, ROI)
        assert result["status"] == "unknown"
        assert result["reason_code"]

    def test_probe_exclusion(self, base):
        after = rolled(base, 24)
        # 探头遮罩盖住左侧一半，剩余特征仍可测
        mask = {"x": 16, "y": 32, "width": 144, "height": 576}
        result = so.measure_displacement(base, after, ROI, exclusions=[mask])
        assert result["status"] == "displaced"
        assert result["dy_px"] == pytest.approx(24, abs=2)

    def test_full_occlusion_is_unknown(self, base):
        mask = {"x": 16, "y": 32, "width": 288, "height": 576}
        result = so.measure_displacement(base, rolled(base, 24), ROI,
                                         exclusions=[mask])
        assert result["status"] == "unknown"

    def test_flat_image_is_unknown(self):
        flat = np.full((H, W), 128, dtype=np.uint8)
        result = so.measure_displacement(flat, flat.copy(), ROI)
        assert result["status"] == "unknown"

    def test_horizontal_motion_rejected(self, base):
        sideways = np.roll(base, 80, axis=1)
        result = so.measure_displacement(base, sideways, ROI)
        assert result["status"] == "unknown"

    def test_displacement_requires_two_ocr_rows(self, base):
        after = rolled(base, 24)
        rows_before = [{"text": "甲", "y": 100}, {"text": "乙", "y": 300},
                       {"text": "丙", "y": 500}]
        rows_after = [{"text": "甲", "y": 124}, {"text": "乙", "y": 324},
                      {"text": "丙", "y": 524}]
        confirmed = so.measure_displacement(base, after, ROI,
                                            ocr_rows_before=rows_before,
                                            ocr_rows_after=rows_after)
        assert confirmed["status"] == "displaced"
        assert confirmed["matched_ocr_rows"] == 3
        # OCR 行与光流矛盾：只有一行方向一致 → 不能判位移
        rows_bad_after = [{"text": "甲", "y": 124}, {"text": "乙", "y": 295},
                          {"text": "丙", "y": 490}]
        rejected = so.measure_displacement(base, after, ROI,
                                           ocr_rows_before=rows_before,
                                           ocr_rows_after=rows_bad_after)
        assert rejected["status"] == "unknown"

    def test_duplicate_names_not_force_matched(self, base):
        after = rolled(base, 24)
        rows_before = [{"text": "甲", "y": 100}, {"text": "甲", "y": 300}]
        rows_after = [{"text": "甲", "y": 140}, {"text": "甲", "y": 340}]
        result = so.measure_displacement(base, after, ROI,
                                         ocr_rows_before=rows_before,
                                         ocr_rows_after=rows_after)
        # 同名不强行配对
        assert result["status"] == "unknown"


class TestMeasureStationaryList:
    ROWS = ([{"text": "甲", "y": 100}, {"text": "乙", "y": 300},
             {"text": "丙", "y": 500}],)

    def test_stationary_with_ocr_rows(self, base):
        rows_before = [{"text": "甲", "y": 100}, {"text": "乙", "y": 300},
                       {"text": "丙", "y": 500}]
        result = so.measure_stationary_list(base, base.copy(), ROI,
                                            ocr_rows_before=rows_before,
                                            ocr_rows_after=list(rows_before))
        assert result["status"] == "stationary"

    def test_without_ocr_rows_not_stationary(self, base):
        result = so.measure_stationary_list(base, base.copy(), ROI)
        assert result["status"] == "unknown"

    def test_tiny_motion_between_thresholds(self, base):
        rows_before = [{"text": "甲", "y": 100}, {"text": "乙", "y": 300},
                       {"text": "丙", "y": 500}]
        rows_after = [{"text": "甲", "y": 106}, {"text": "乙", "y": 306},
                      {"text": "丙", "y": 506}]
        result = so.measure_stationary_list(base, rolled(base, 6), ROI,
                                            ocr_rows_before=rows_before,
                                            ocr_rows_after=rows_after)
        assert result["status"] == "unknown"

    def test_real_scroll_not_stationary(self, base):
        rows_before = [{"text": "甲", "y": 100}, {"text": "乙", "y": 300},
                       {"text": "丙", "y": 500}]
        rows_after = [{"text": "甲", "y": 130}, {"text": "乙", "y": 330},
                      {"text": "丙", "y": 530}]
        result = so.measure_stationary_list(base, rolled(base, 30), ROI,
                                            ocr_rows_before=rows_before,
                                            ocr_rows_after=rows_after)
        assert result["status"] == "displaced"


class TestParamsFromDefaults:
    def test_params_load_from_gesture_defaults(self):
        params = so.default_params()
        assert params["min_inliers"] == 12
        assert params["min_displacement_ratio"] == 0.04


class TestHygieneFixes:
    def test_empty_text_rows_do_not_count_as_corroboration(self, base):
        """R1 P2：空/纯空白文本行不构成互证（稀释 ≥2 唯一文本行门禁）。"""
        after = rolled(base, 24)
        rows_before = [{"text": "", "y": 100}, {"text": "  ", "y": 300}]
        rows_after = [{"text": "", "y": 124}, {"text": "  ", "y": 324}]
        result = so.measure_displacement(base, after, ROI,
                                         ocr_rows_before=rows_before,
                                         ocr_rows_after=rows_after)
        assert result["status"] == "unknown"
        assert result["reason_code"] == "ocr_rows_not_consistent"

    def test_exclusion_overhang_masks_only_roi_portion(self):
        """R1 P2：排除区横跨 ROI 边界时只遮蔽 ROI 内部分（不按截断后整宽）。"""
        roi = {"x": 100, "y": 100, "width": 200, "height": 200}
        mask = so._exclusion_mask((200, 200), roi,
                                  [{"x": 60, "y": 120,
                                    "width": 100, "height": 40}])
        column = mask[30, :]  # mask 为 ROI 局部坐标：区域 y∈[120,160) → 局部 [20,60)
        assert int((column == 0).sum()) == 60, "ROI 内只应有 60px 被遮蔽"
        row = mask[:, 20]
        assert int((row == 0).sum()) == 40
