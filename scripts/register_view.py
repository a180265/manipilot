"""摄像头↔屏幕注册：识别网格页十字标记，建立"图像像素 ↔ 机器坐标"映射。

流程：手机打开 http://<本机IP>:8791/grid（绿色十字网格页）→ 运行本脚本：
1. 从取流桥抓当前帧；
2. 检测绿色十字标记的图像位置（HSV 绿色过滤 + 连通域）；
3. 与页面已知坐标 (/gridpoints) 按"旋转/镜像 8 种朝向 + 单应残差最小"自动匹配；
4. 求单应 image→page，再经屏幕标定 (A⁻¹) 得 image→machine；
5. 保存 configs/view_register.json，并在原图标注检测结果供人工核验。

用法：.venv/bin/python scripts/register_view.py [--frame /path/to/frame.png]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import cv2
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from drivers.screen_calibration import page_to_machine  # noqa: E402

FRAME_BRIDGE_URL = "http://127.0.0.1:8790/latest.jpg"
OUTPUT = PROJECT_ROOT / "configs" / "view_register.json"


def grab_frame() -> np.ndarray:
    """经 HttpFrameSource 单实现取帧（Auto_CS#6）：旧单路兼容放行缺头。"""
    from drivers.real_camera import HttpFrameSource

    return HttpFrameSource(FRAME_BRIDGE_URL, allow_legacy=True)()


def detect_green_crosses(frame: np.ndarray) -> list[tuple[float, float]]:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (35, 60, 60), (95, 255, 255))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    n, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask)
    points: list[tuple[float, float]] = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        # 十字标记：面积 ~500，外接框接近正方形（70×70），过滤背景/杆状噪声
        if 200 < area < 1500 and 30 < w < 110 and 30 < h < 110 and 0.5 < w / h < 2.0:
            points.append((float(centroids[i][0]), float(centroids[i][1])))
    merged: list[tuple[float, float]] = []
    for x, y in points:
        if all((x - mx) ** 2 + (y - my) ** 2 > 625 for mx, my in merged):
            merged.append((x, y))
    return merged


def normalize(points: np.ndarray) -> np.ndarray:
    return (points - points.mean(axis=0)) / (points.std() + 1e-9)


def transform_points(pts: np.ndarray, k: int, flip: bool) -> np.ndarray:
    p = pts.copy()
    for _ in range(k):
        p = np.stack([p[:, 1], -p[:, 0]], axis=1)
    if flip:
        p[:, 0] = -p[:, 0]
    return p


def match_orientation(detect_norm: np.ndarray, page_norm: np.ndarray, page_pts: np.ndarray):
    """8 种旋转/镜像朝向下最近邻配对，返回 (残差, 每个检测点对应的页面坐标)。"""
    best = None
    for k in range(4):
        for flip in (False, True):
            tp = transform_points(page_norm, k, flip)
            dist = np.linalg.norm(detect_norm[:, None, :] - tp[None, :, :], axis=2)
            idx = dist.argmin(axis=1)
            res = float(dist[np.arange(len(detect_norm)), idx].mean())
            if best is None or res < best[0]:
                best = (res, idx)
    idx = best[1]
    return page_pts[idx], best[0]


def main() -> int:
    frame_path = None
    if "--frame" in sys.argv:
        frame_path = Path(sys.argv[sys.argv.index("--frame") + 1])
    frame = cv2.imread(str(frame_path)) if frame_path else grab_frame()
    if frame is None:
        print("无法读取帧")
        return 1

    with urllib.request.urlopen("http://127.0.0.1:8791/gridpoints", timeout=3) as response:
        grid = json.loads(response.read().decode("utf-8"))
    points = grid.get("points") or []
    if len(points) < 6:
        print("gridpoints 为空：请先在手机上打开网格页 http://<本机IP>:8791/grid")
        return 1
    page_pts = np.array([(p["x"], p["y"]) for p in points], dtype=np.float32)

    detect = detect_green_crosses(frame)
    print(f"检测到绿色十字 {len(detect)} 个（期望 {len(points)}；被触针遮挡的十字会缺失）")
    if len(detect) < 6:
        print("有效标记不足 6 个，无法可靠注册")
        return 1

    det = np.array(detect, dtype=np.float32)
    # 8 种旋转/镜像朝向 × 最近邻配对（允许检测缺失），选对称残差最小的组合
    det_n = normalize(det)
    page_n = normalize(page_pts)
    best_pairs, best_score = None, 1e18
    for k in range(4):
        for flip in (False, True):
            tp = transform_points(page_n, k, flip)
            dist = np.linalg.norm(det_n[:, None, :] - tp[None, :, :], axis=2)
            idx = dist.argmin(axis=1)
            dmin = dist[np.arange(len(det_n)), idx]
            score = float(np.mean(np.clip(dmin, 0, 0.25)))
            if score < best_score:
                best_score, best_pairs = score, idx
    pairs = [(i, int(best_pairs[i])) for i in range(len(det))]
    # 去重：同一网格点只保留一个检测
    assign: dict[int, int] = {}
    for i, j in pairs:
        if j not in assign:
            assign[j] = i
    keep = [(i, j) for j, i in assign.items()]
    det_sel = np.array([det[i] for i, _ in keep], dtype=np.float32)
    matched_page = np.array([page_pts[j] for _, j in keep], dtype=np.float32)
    print(f"配对成功 {len(det_sel)} 对")
    matched_page, match_res = match_orientation(normalize(det), normalize(page_pts), page_pts)
    print(f"朝向匹配平均偏差 {match_res:.3f}（归一化）")

    H, _ = cv2.findHomography(det_sel.reshape(-1, 1, 2), matched_page.reshape(-1, 1, 2))
    proj = cv2.perspectiveTransform(det_sel.reshape(-1, 1, 2), H).reshape(-1, 2)
    residuals = np.linalg.norm(proj - matched_page, axis=1)
    print(f"image→page 单应残差: max {residuals.max():.1f}px, rms {np.sqrt((residuals**2).mean()):.1f}px")

    cal = json.load(open(PROJECT_ROOT / "configs" / "screen_calibration.json"))
    machine_pts = np.array([page_to_machine(cal, (p[0], p[1])) for p in matched_page], dtype=np.float32)
    H_im2mach, _ = cv2.findHomography(det_sel.reshape(-1, 1, 2), machine_pts)

    OUTPUT.write_text(json.dumps({
        "H_image_to_page": H.tolist(),
        "H_image_to_machine": H_im2mach.tolist(),
        "residual_max_px": float(residuals.max()),
        "n_points": len(det_sel),
        "registered_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        "note": "仅对当前摄像头/手机相对位置有效；任一移位后需重新注册",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    vis = frame.copy()
    for d, p in zip(det_sel, matched_page):
        cv2.circle(vis, (int(d[0]), int(d[1])), 10, (0, 0, 255), 2)
        cv2.putText(vis, f"({p[0]:.0f},{p[1]:.0f})", (int(d[0]) + 12, int(d[1])),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
    out = Path("/tmp/register_vis.png")
    cv2.imwrite(str(out), cv2.resize(vis, (960, 540)))
    print(json.dumps({
        "注册点数": len(det),
        "单应残差px": {"max": round(float(residuals.max()), 1), "rms": round(float(np.sqrt((residuals**2).mean())), 1)},
        "保存至": str(OUTPUT.relative_to(PROJECT_ROOT)),
        "可视化": str(out),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
