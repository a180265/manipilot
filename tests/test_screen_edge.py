"""屏幕边缘检测单元测试：合成画面上的四边形检测、偏差计算与摘要（§8.1/§16）。"""
from __future__ import annotations

import math

import cv2
import numpy as np
import pytest

from drivers.screen_edge import (
    ScreenEdgeError,
    detect_screen_quad,
    locate_screen_quad,
    quad_deviation_px,
    quad_digest,
)


def _frame(x0=100, y0=80, x1=860, y1=460, size=(540, 960)) -> np.ndarray:
    """合成取流画面：黑底 + 亮屏矩形（模拟全屏亮着的校准页）。"""
    frame = np.zeros((size[0], size[1], 3), np.uint8)
    cv2.rectangle(frame, (x0, y0), (x1, y1), (200, 200, 200), -1)
    frame = cv2.GaussianBlur(frame, (5, 5), 0)  # 模拟镜头轻微失焦
    return frame


def test_detect_quad_corners_ordered_tl_tr_br_bl():
    quad = detect_screen_quad(_frame())
    corners = quad["corners"]
    assert len(corners) == 4
    tl, tr, br, bl = corners
    assert tl[0] < br[0] and tl[1] < br[1]
    assert tr[0] > bl[0] and tr[1] < bl[1]
    # 与合成矩形一致（±4px 容差，边缘因模糊略收缩）
    assert tl == pytest.approx([100, 80], abs=4)
    assert tr == pytest.approx([860, 80], abs=4)
    assert br == pytest.approx([860, 460], abs=4)
    assert bl == pytest.approx([100, 460], abs=4)
    assert quad["frameW"] == 960 and quad["frameH"] == 540


def test_detect_quad_rejects_dark_frame():
    with pytest.raises(ScreenEdgeError):
        detect_screen_quad(np.zeros((540, 960, 3), np.uint8))


def test_detect_quad_rejects_tiny_bright_spot():
    frame = np.zeros((540, 960, 3), np.uint8)
    cv2.rectangle(frame, (10, 10), (40, 40), (255, 255, 255), -1)  # 面积 <5%
    with pytest.raises(ScreenEdgeError):
        detect_screen_quad(frame)


def test_strict_phone_location_compensates_small_shift_but_rejects_large_shift():
    saved = detect_screen_quad(_frame())["corners"]
    small = _frame(x0=105, y0=83, x1=865, y1=463)
    located = locate_screen_quad(small, saved, require_fittable=True)["corners"]
    np.testing.assert_allclose(
        np.asarray(located) - np.asarray(saved),
        np.tile([5, 3], (4, 1)), atol=2)

    moved = _frame(x0=125, y0=80, x1=885, y1=460)
    assert locate_screen_quad(moved, saved)["corners"]
    with pytest.raises(ScreenEdgeError, match="无法可靠补偿"):
        locate_screen_quad(moved, saved, require_fittable=True)


def test_strict_phone_location_on_dim_screen_ignores_brighter_content():
    saved = [[150, 90], [489, 90], [489, 409], [150, 409]]
    image = np.zeros((480, 640, 3), dtype=np.uint8)
    image[93:413, 155:495] = 24
    for y in range(110, 390, 25):
        image[y:y + 9, 175:460] = 75
    located = locate_screen_quad(
        image, saved, require_fittable=True)["corners"]
    np.testing.assert_allclose(
        np.asarray(located) - np.asarray(saved),
        np.tile([5, 3], (4, 1)), atol=2)

    shifted = np.zeros_like(image)
    shifted[93:413, 170:510] = 24
    for y in range(110, 390, 25):
        shifted[y:y + 9, 190:475] = 75
    with pytest.raises(ScreenEdgeError, match="无法可靠补偿"):
        locate_screen_quad(shifted, saved, require_fittable=True)


def test_quad_deviation_zero_for_same_corners():
    quad = detect_screen_quad(_frame())
    assert quad_deviation_px(quad["corners"], quad["corners"]) == 0.0


def test_quad_deviation_measures_shift():
    base = detect_screen_quad(_frame())["corners"]
    # 整体平移 (12, 5)：每个角点位移 √(12²+5²)=13
    moved = detect_screen_quad(_frame(x0=112, y0=85, x1=872, y1=465))["corners"]
    deviation = quad_deviation_px(base, moved)
    assert deviation == pytest.approx(math.hypot(12, 5), abs=3)


def test_quad_deviation_inf_on_mismatch():
    assert quad_deviation_px([[0, 0]], [[0, 0]]) == float("inf")


def test_quad_digest_describes_geometry():
    quad = detect_screen_quad(_frame())
    digest = quad_digest(quad)
    # 四条直边交点保持合成矩形的实际宽高与中心。
    assert digest == "760x380px@(480,270)"
    assert quad_digest({"corners": []}) == "invalid"


def test_detect_screen_quad_rejects_border_touching_quad():
    """2026-09-04 真机事故：检测区域触碰画面边界=锁到背景亮区（键盘/光线
    场景高发），必须按检测无效拒绝，不能当屏幕四角参与边缘比对。"""
    import numpy as np
    from drivers.screen_edge import ScreenEdgeError, detect_screen_quad

    frame = np.zeros((720, 1280, 3), np.uint8)
    # 顶到画面右边界与上边界的"背景亮区"矩形（事故现场：[338,0]-[1279,215]）
    frame[0:215, 338:1279] = 255
    try:
        detect_screen_quad(frame)
        raise AssertionError("触碰边界的四边形必须被拒绝")
    except ScreenEdgeError as exc:
        assert "触碰画面边界" in str(exc)

    # 完整在画面内的屏幕矩形正常检出
    frame2 = np.zeros((720, 1280, 3), np.uint8)
    frame2[100:600, 160:1160] = 255
    quad = detect_screen_quad(frame2)
    assert quad["corners"]


# ---------- 刚性一致性判别（2026-09-05 真机误报定案） ----------

def test_classify_today_real_contamination_as_anomaly():
    """2026-09-05 真机数据：右上/右下被环境亮带外扩（+31/+24、+11/-3 px），
    左上/左下像素级不动——非刚性，必须判 anomaly（检测干扰）而非手机移动。
    手机实未移动（用户实测输入/删除零偏移；6 次 run 边缘基准完全一致）。"""
    from drivers.screen_edge import classify_quad_change
    saved = [[157, 113], [1155, 99], [1163, 567], [139, 564]]
    live = [[157, 113], [1186, 123], [1174, 564], [140, 566]]
    verdict = classify_quad_change(saved, live, tolerance_px=10.0)
    assert verdict["status"] == "anomaly"
    assert verdict["deviationPx"] > 30  # 原始偏差 39px 级
    assert verdict["movementPx"] < verdict["deviationPx"]
    assert "干扰" in verdict["detail"]


def test_classify_coherent_translation_as_moved():
    """整体平移=刚体位移：必须硬拦（screen_edge_changed 语义）。"""
    from drivers.screen_edge import classify_quad_change
    saved = [[157, 113], [1155, 99], [1163, 567], [139, 564]]
    live = [[p[0] + 18, p[1] + 14] for p in saved]
    verdict = classify_quad_change(saved, live, tolerance_px=10.0)
    assert verdict["status"] == "moved"
    assert verdict["movementPx"] > 10
    assert verdict["anomalyPx"] < 2


def test_classify_rotation_as_moved():
    """面内旋转：四角位移方向各异但整体刚性，判 moved。"""
    from drivers.screen_edge import classify_quad_change
    saved = np.array([[157, 113], [1155, 99], [1163, 567], [139, 564]], float)
    theta = math.radians(2.0)  # 2° → 角点位移 ~20px
    c, s = math.cos(theta), math.sin(theta)
    ctr = saved.mean(axis=0)
    rot = np.array([[c, -s], [s, c]])
    live = ((saved - ctr) @ rot.T + ctr).tolist()
    verdict = classify_quad_change(saved.tolist(), live, tolerance_px=10.0)
    assert verdict["status"] == "moved"


def test_classify_uniform_scale_as_anomaly():
    """整体缩放：本装置手机刚体+摄像头固定，物理上不可能缩放——
    实测（2026-09-23）检测器在暗页会锁定手机边框（比屏幕亮区大 6-7%），
    形态恰为整体外扩。判 anomaly（检测锁定边界漂移），不判 moved。"""
    from drivers.screen_edge import classify_quad_change
    saved = np.array([[157, 113], [1155, 99], [1163, 567], [139, 564]], float)
    ctr = saved.mean(axis=0)
    live = (((saved - ctr) * 1.03) + ctr).tolist()  # +3% → 角点 ~15px
    verdict = classify_quad_change(saved.tolist(), live, tolerance_px=10.0)
    assert verdict["status"] == "anomaly"
    assert "缩放" in verdict["detail"]


def test_classify_translation_plus_heavy_asymmetric_contamination_is_anomaly():
    """真平移 20px + 右对角大幅不对称污染（+21,+30 / +21,+3）：整体已无
    刚性解释（平移不齐、拟合残差超容差）——归入"无法核实"歧义带跳过
    （2026-09-05 二次定案后的语义：只拦可被刚体解释的位移；真平移+重度
    污染的极端混合场景接受为已知歧义，由运行期触点验收兜底）。"""
    from drivers.screen_edge import classify_quad_change
    saved = [[157, 113], [1155, 99], [1163, 567], [139, 564]]
    live = [[177, 127], [1196, 143], [1204, 584], [159, 580]]  # 整体+20,+14 再右外扩
    verdict = classify_quad_change(saved, live, tolerance_px=10.0)
    assert verdict["status"] == "anomaly"


def test_classify_small_noise_ok_and_bad_corner_count_moved():
    from drivers.screen_edge import classify_quad_change
    saved = [[157, 113], [1155, 99], [1163, 567], [139, 564]]
    live = [[158, 114], [1156, 100], [1164, 566], [140, 565]]
    assert classify_quad_change(saved, live, 10.0)["status"] == "ok"
    assert classify_quad_change(saved, saved[:3], 10.0)["status"] == "moved"


def test_classify_single_edge_content_change_as_anomaly():
    """键盘打开/通知栏下拉：一条边的两角同向量内移、对边两角静止——
    平面刚体运动有两点不动即恒等，几何上不可能是手机位移。"""
    from drivers.screen_edge import classify_quad_change
    saved = [[157, 113], [1155, 99], [1163, 567], [139, 564]]
    # 通知栏下拉 80px：顶边两角同向量下移，底边静止
    live = [[157, 193], [1155, 179], [1163, 567], [139, 564]]
    v = classify_quad_change(saved, live, 10.0)
    assert v["status"] == "anomaly"
    # 键盘场景常见：顶边压暗 25px
    live2 = [[157, 138], [1155, 124], [1163, 567], [139, 564]]
    assert classify_quad_change(saved, live2, 10.0)["status"] == "anomaly"
    # 单侧环境亮区均匀并入（右边两角同向量 +40）：同形态
    live3 = [[157, 113], [1195, 99], [1203, 567], [139, 564]]
    assert classify_quad_change(saved, live3, 10.0)["status"] == "anomaly"


def test_classify_asymmetric_heavy_contamination_is_anomaly():
    """右侧两角大幅外扩且向量不一致、左对角完全静止：相似变换无法整体解释
    （真绕左缘旋转可被拟合解释→moved；这个形态拟合残差超容差）——判检测
    干扰。2026-09-05 二次定案：中位数平移超容差但逐角残差超容差≠一致平移。"""
    from drivers.screen_edge import classify_quad_change
    saved = [[157, 113], [1155, 99], [1163, 567], [139, 564]]
    live = [[157, 113], [1195, 99], [1203, 620], [139, 564]]  # TR(+40,0) BR(+40,+53)
    v = classify_quad_change(saved, live, 10.0)
    assert v["status"] == "anomaly"


def test_classify_three_corner_contamination_today_real_data():
    """2026-09-05 真机二次误报原始数据（tolerance=8）：TR/BR 右侧照旧污染、
    BL 被左下新亮区拽 (-31,+12)——三个角污染把中位数平移拖到 8.14px 刚超
    容差，但 TL 像素级不动=手机没动。必须判 anomaly（此前误判 moved 导致
    screen_edge_changed 39.8px 拦门禁）。"""
    from drivers.screen_edge import classify_quad_change
    base = [[159, 112], [1155, 99], [1163, 567], [139, 565]]
    live = [[159, 112], [1186, 124], [1174, 564], [108, 577]]
    v = classify_quad_change(base, live, 8.0)
    assert v["status"] == "anomaly"
    assert v["deviationPx"] > 39  # 原始偏差仍在，但不再透传给门禁


def test_classify_coherent_translation_with_single_outlier_blocks():
    """真平移 24px + 单角中庸外扩(+7,+5)：相似拟合可解释（残差 ≤容差）且
    刚性位移超容差 → moved 硬拦（平移证据确凿时污染不豁免）。"""
    from drivers.screen_edge import classify_quad_change
    saved = [[157, 113], [1155, 99], [1163, 567], [139, 564]]
    live = [[177, 127], [1183, 113], [1190, 581], [166, 585]]  # 整体+20,+14 再 TR+7,+5
    v = classify_quad_change(saved, live, 10.0)
    assert v["status"] == "moved"


def test_classify_rotation_about_bottom_edge_blocks():
    """绕底边旋转：顶边两角移动向量 x 同向（旋转分量）——不是单边内容
    变化形态，必须拦。"""
    from drivers.screen_edge import classify_quad_change
    saved = [[157, 113], [1155, 99], [1163, 567], [139, 564]]
    # 绕 (651, 565) 旋转 3°：TL(+25,-21) TR(+24,+25) BR(0,+26) BL(0,-26)
    import numpy as np
    s = np.array(saved, float)
    theta = math.radians(3.0)
    c, sn = math.cos(theta), math.sin(theta)
    ctr = np.array([651.0, 565.0])
    rot = np.array([[c, -sn], [sn, c]])
    live = ((s - ctr) @ rot.T + ctr).tolist()
    v = classify_quad_change(saved, live, 10.0)
    assert v["status"] == "moved"


def test_classify_left_edge_rotation_blocks():
    """绕左缘支点 ~1° 旋转（伪装面最大的真位移形态）：四角平移残差全部
    压在容差附近（~9.9px<10px），必须仍判 moved（系统性阈值 0.5x 容差
    + 相似变换拟合裁决）。"""
    from drivers.screen_edge import classify_quad_change
    import numpy as np
    saved = [[157, 113], [1155, 99], [1163, 567], [139, 564]]
    theta = math.radians(1.0)
    c, s = math.cos(theta), math.sin(theta)
    ctr = np.array([148.0, 335.75])  # 左缘中点
    q = np.array(saved, float) - ctr
    rot = np.array([[c, -s], [s, c]])
    live = (q @ rot.T + ctr).tolist()
    v = classify_quad_change(saved, live, 10.0)
    assert v["status"] == "moved"
    assert v["deviationPx"] > 15


def test_classify_subdegree_edge_rotation_blocks():
    """绕边 0.6° 旋转（对角位移 ~11px=1.1x 容差）不得钻进单边豁免：
    配对向量差阈值 0.35x 容差把漏判窗口压到 <容差。"""
    from drivers.screen_edge import classify_quad_change
    import numpy as np
    saved = [[157, 113], [1155, 99], [1163, 567], [139, 564]]
    theta = math.radians(0.6)
    c, s = math.cos(theta), math.sin(theta)
    ctr = np.array([148.0, 335.75])
    rot = np.array([[c, -s], [s, c]])
    live = ((np.array(saved, float) - ctr) @ rot.T + ctr).tolist()
    v = classify_quad_change(saved, live, 10.0)
    assert v["status"] == "moved"
