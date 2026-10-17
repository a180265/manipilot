"""机械臂 → 手机屏幕坐标的仿射拟合与映射注册表。

继承 AI_demo《K40屏幕校准与输入法清空设计》的约定：
- 校准采集 ≥3 个不共线的 (机械X/Y, 屏幕像素) 样本，拟合 screen = A·robot + b。
- 残差超阈值时判定失败，禁止写入正式映射。
- 映射按 deviceId 保存；机械臂控制器会话、手机夹具或摄像头改变后必须重新校准。
- MPos 只是控制器计算坐标，不能单独证明电机真实移动；正式校准仍需摄像头或现场确认。

纯 Python 实现（不依赖 numpy），macOS / Linux / Windows 通用。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

DEFAULT_RESIDUAL_THRESHOLD_PX = 3.0
_MIN_PIVOT_RATIO = 1e-9


class CalibrationMathError(Exception):
    """校准数学错误：样本不足、共线或奇异。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _solve3(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """带部分主元的高斯消元解 3x3 线性方程组；矩阵奇异时抛错。"""
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    size = 3
    for col in range(size):
        pivot_row = max(range(col, size), key=lambda r: abs(a[r][col]))
        if abs(a[pivot_row][col]) < _MIN_PIVOT_RATIO * max(
                abs(a[r][c]) for r in range(size) for c in range(size)):
            raise CalibrationMathError(
                "calibration_collinear", "校准样本共线或退化，无法拟合仿射变换")
        a[col], a[pivot_row] = a[pivot_row], a[col]
        for row in range(size):
            if row == col:
                continue
            factor = a[row][col] / a[col][col]
            for k in range(col, size + 1):
                a[row][k] -= factor * a[col][k]
    return [a[i][size] / a[i][i] for i in range(size)]


def fit_affine(samples: list[dict]) -> dict:
    """最小二乘拟合 screen = A·robot + b。

    samples: [{"robotX","robotY","screenX","screenY"}, ...]（至少 3 个不共线样本）。
    返回 {"a","b","c","d","e","f","residuals","residualRms"}，其中
    screenX = a*robotX + b*robotY + c；screenY = d*robotX + e*robotY + f。
    """
    if len(samples) < 3:
        raise CalibrationMathError("calibration_insufficient_samples",
                                   "校准至少需要 3 个不共线样本")
    m = [[0.0] * 3 for _ in range(3)]
    vx = [0.0] * 3
    vy = [0.0] * 3
    points = []
    for sample in samples:
        rx = float(sample["robotX"]); ry = float(sample["robotY"])
        sx = float(sample["screenX"]); sy = float(sample["screenY"])
        points.append((rx, ry, sx, sy))
        row = (rx, ry, 1.0)
        for i in range(3):
            for j in range(3):
                m[i][j] += row[i] * row[j]
            vx[i] += row[i] * sx
            vy[i] += row[i] * sy
    # 三点共线检查：三角形面积（叉积）接近零即共线
    if len(points) == 3:
        (x1, y1, _, _), (x2, y2, _, _), (x3, y3, _, _) = points
        area2 = abs((x2 - x1) * (y3 - y1) - (x3 - x1) * (y2 - y1))
        if area2 < 1e-6:
            raise CalibrationMathError("calibration_collinear",
                                       "三个校准样本共线，必须使用不共线的采样点")
    tx = _solve3(m, vx)
    ty = _solve3(m, vy)
    a, b, c = tx
    d, e, f = ty
    residuals = []
    for rx, ry, sx, sy in points:
        px = a * rx + b * ry + c
        py = d * rx + e * ry + f
        residuals.append(math.hypot(px - sx, py - sy))
    rms = math.sqrt(sum(r * r for r in residuals) / len(residuals))
    return {"a": a, "b": b, "c": c, "d": d, "e": e, "f": f,
            "residuals": residuals, "residualRms": rms,
            "sampleCount": len(samples)}


class ScreenRobotTransform:
    """screen = A·robot + b 的可逆封装（持久化与双向换算）。"""

    def __init__(self, a: float, b: float, c: float, d: float, e: float, f: float, *,
                 device_id: str = "", screen_width: int = 0, screen_height: int = 0,
                 orientation: str = "portrait", residual_rms: float = 0.0,
                 version: int = 1, created_at: str = ""):
        self.a, self.b, self.c = a, b, c
        self.d, self.e, self.f = d, e, f
        self.device_id = device_id
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.orientation = orientation
        self.residual_rms = residual_rms
        self.version = version
        self.created_at = created_at

    def screen_from_robot(self, robot_x: float, robot_y: float) -> tuple[float, float]:
        return (self.a * robot_x + self.b * robot_y + self.c,
                self.d * robot_x + self.e * robot_y + self.f)

    def robot_from_screen(self, screen_x: float, screen_y: float) -> tuple[float, float]:
        """逆变换：给定屏幕像素目标，求机械坐标（机械臂到屏幕操作使用该方向）。"""
        det = self.a * self.e - self.b * self.d
        if abs(det) < 1e-12:
            raise CalibrationMathError("calibration_singular", "仿射变换不可逆")
        dx, dy = screen_x - self.c, screen_y - self.f
        return ((self.e * dx - self.b * dy) / det, (-self.d * dx + self.a * dy) / det)

    def to_dict(self) -> dict:
        return {
            "deviceId": self.device_id, "a": self.a, "b": self.b, "c": self.c,
            "d": self.d, "e": self.e, "f": self.f,
            "screenWidth": self.screen_width, "screenHeight": self.screen_height,
            "orientation": self.orientation, "residualRms": self.residual_rms,
            "version": self.version, "createdAt": self.created_at,
            "formula": "screenX=a*robotX+b*robotY+c; screenY=d*robotX+e*robotY+f",
        }

    @classmethod
    def from_fit(cls, fit: dict, *, device_id: str, screen_width: int, screen_height: int,
                 orientation: str, version: int, created_at: str) -> "ScreenRobotTransform":
        return cls(fit["a"], fit["b"], fit["c"], fit["d"], fit["e"], fit["f"],
                   device_id=device_id, screen_width=screen_width,
                   screen_height=screen_height, orientation=orientation,
                   residual_rms=fit["residualRms"], version=version, created_at=created_at)

    @classmethod
    def from_dict(cls, data: dict) -> "ScreenRobotTransform":
        return cls(data["a"], data["b"], data["c"], data["d"], data["e"], data["f"],
                   device_id=data.get("deviceId", ""),
                   screen_width=int(data.get("screenWidth", 0)),
                   screen_height=int(data.get("screenHeight", 0)),
                   orientation=data.get("orientation", "portrait"),
                   residual_rms=float(data.get("residualRms", 0.0)),
                   version=int(data.get("version", 1)),
                   created_at=data.get("createdAt", ""))


class MappingRegistry:
    """按 deviceId 持久化仿射映射（JSON 文件，跨平台；服务重启不丢失）。"""

    def __init__(self, path: Path):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text(encoding="utf-8"))

    def save(self, transform: ScreenRobotTransform) -> dict:
        previous = self._data.get(transform.device_id)
        if previous is not None:
            transform.version = int(previous.get("version", 1)) + 1
        self._data[transform.device_id] = transform.to_dict()
        self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        return transform.to_dict()

    def get(self, device_id: str) -> dict | None:
        return self._data.get(device_id)

    def delete(self, device_id: str) -> bool:
        if device_id in self._data:
            del self._data[device_id]
            self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
            return True
        return False

    def all(self) -> list[dict]:
        return sorted(self._data.values(), key=lambda item: item.get("deviceId", ""))


def _solve_linear(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """带部分主元的高斯消元解 N×N 线性方程组；矩阵奇异时抛错。"""
    n = len(matrix)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    scale = max((abs(v) for row in matrix for v in row), default=1.0) or 1.0
    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot_row][col]) < _MIN_PIVOT_RATIO * scale:
            raise CalibrationMathError(
                "calibration_collinear", "样本退化（方程组奇异），无法拟合")
        a[col], a[pivot_row] = a[pivot_row], a[col]
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col] / a[col][col]
            for k in range(col, n + 1):
                a[row][k] -= factor * a[col][k]
    return [a[i][n] / a[i][i] for i in range(n)]


def fit_quadratic(samples: list[dict]) -> dict:
    """最小二乘拟合 screen = A·robot + b + 二次项（倾斜触点滑移补偿，2026-09-04）。

    每轴 6 系数：
        screenX = a*x + b*y + c + qx2*x² + qxy*x*y + qy2*y²
        screenY = d*x + e*y + f + px2*x² + pxy*x*y + py2*y²

    手机屏相对机械臂平面的固定倾斜带来的触点滑移是平滑低阶非线性，纯仿射
    在网格间隙点泄漏（实测 8~15px）；二次项把它吸收。≥6 个不退化样本即可
    求解（16 点网格余量充足）。返回与 fit_affine 同契约（额外携带二次键），
    二次键缺省时下游按纯仿射处理（旧档案完全兼容）。
    """
    if len(samples) < 6:
        raise CalibrationMathError("calibration_insufficient_samples",
                                   "二次拟合至少需要 6 个样本")
    m = [[0.0] * 6 for _ in range(6)]
    vx = [0.0] * 6
    vy = [0.0] * 6
    for sample in samples:
        rx = float(sample["robotX"]); ry = float(sample["robotY"])
        sx = float(sample["screenX"]); sy = float(sample["screenY"])
        row = (rx, ry, 1.0, rx * rx, rx * ry, ry * ry)
        for i in range(6):
            for j in range(6):
                m[i][j] += row[i] * row[j]
            vx[i] += row[i] * sx
            vy[i] += row[i] * sy
    coef_x = _solve_linear(m, vx)
    coef_y = _solve_linear(m, vy)

    def eval_x(x, y):
        return (coef_x[0] * x + coef_x[1] * y + coef_x[2]
                + coef_x[3] * x * x + coef_x[4] * x * y + coef_x[5] * y * y)

    def eval_y(x, y):
        return (coef_y[0] * x + coef_y[1] * y + coef_y[2]
                + coef_y[3] * x * x + coef_y[4] * x * y + coef_y[5] * y * y)

    residuals = []
    for sample in samples:
        rx = float(sample["robotX"]); ry = float(sample["robotY"])
        ex, ey = eval_x(rx, ry), eval_y(rx, ry)
        residuals.append(math.hypot(ex - float(sample["screenX"]),
                                    ey - float(sample["screenY"])))
    return {
        "a": coef_x[0], "b": coef_x[1], "c": coef_x[2],
        "qx2": coef_x[3], "qxy": coef_x[4], "qy2": coef_x[5],
        "d": coef_y[0], "e": coef_y[1], "f": coef_y[2],
        "px2": coef_y[3], "pxy": coef_y[4], "py2": coef_y[5],
        "residuals": residuals,
        "residualRms": math.sqrt(sum(r * r for r in residuals) / len(residuals)),
    }


def fit_calibration_model(samples: list[dict]) -> dict:
    """选取机械坐标 → 触点模型；独立验收数据不得传入本函数。

    仿射为基线，至少九点才比较二次模型。复杂模型必须在逐点留一的
    最大误差上更好（相同时比较 RMS），且训练 RMS、最大残差均不变差。
    留一拟合退化时保留仿射；基线本身不可拟合时仍抛 CalibrationMathError。
    返回原拟合契约，并附 modelSelection，便于保存选型证据。
    """
    tolerance = 1e-6

    def summarize(residuals: list[float]) -> dict:
        if not residuals or not all(math.isfinite(r) for r in residuals):
            raise CalibrationMathError("calibration_invalid_samples", "校准残差必须为有限值")
        return {"maxErrorPx": max(residuals),
                "rmsPx": math.sqrt(sum(r * r for r in residuals) / len(residuals))}

    def leave_one_out(fitter) -> dict:
        residuals = []
        for index, point in enumerate(samples):
            fit = fitter(samples[:index] + samples[index + 1:])
            x, y = float(point["robotX"]), float(point["robotY"])
            sx = (fit["a"] * x + fit["b"] * y + fit["c"]
                  + fit.get("qx2", 0) * x * x + fit.get("qxy", 0) * x * y
                  + fit.get("qy2", 0) * y * y)
            sy = (fit["d"] * x + fit["e"] * y + fit["f"]
                  + fit.get("px2", 0) * x * x + fit.get("pxy", 0) * x * y
                  + fit.get("py2", 0) * y * y)
            residuals.append(math.hypot(sx - float(point["screenX"]),
                                        sy - float(point["screenY"])))
        return {**summarize(residuals), "residualsPx": residuals}

    fit = fit_affine(samples)
    affine_training = summarize(fit["residuals"])
    selection = {
        "selectedModel": "affine", "sampleCount": len(samples),
        "basis": "training_leave_one_out_max_then_rms",
        "reason": "insufficient_samples_for_quadratic_validation",
        "affine": {"training": affine_training},
    }
    fit["modelSelection"] = selection
    if len(samples) < 9:
        return fit
    try:
        affine_validation = leave_one_out(fit_affine)
        selection["affine"]["leaveOneOut"] = affine_validation
    except CalibrationMathError as exc:
        selection.update(reason="affine_leave_one_out_degenerate", errorCode=exc.code)
        return fit
    try:
        quadratic = fit_quadratic(samples)
        quadratic_training = summarize(quadratic["residuals"])
        selection["quadratic"] = {"training": quadratic_training}
        quadratic_validation = leave_one_out(fit_quadratic)
        selection["quadratic"]["leaveOneOut"] = quadratic_validation
    except CalibrationMathError as exc:
        selection.update(reason="quadratic_validation_degenerate", errorCode=exc.code)
        return fit
    if any(quadratic_training[key] > affine_training[key] + tolerance
           for key in ("maxErrorPx", "rmsPx")):
        selection["reason"] = "quadratic_training_regression"
        return fit
    max_delta = quadratic_validation["maxErrorPx"] - affine_validation["maxErrorPx"]
    improves = (max_delta < -tolerance
                or (abs(max_delta) <= tolerance
                    and quadratic_validation["rmsPx"]
                    < affine_validation["rmsPx"] - tolerance))
    if improves:
        selection.update(selectedModel="quadratic", reason="quadratic_generalizes_better")
        return {**quadratic, "modelSelection": selection}
    selection["reason"] = "affine_generalizes_at_least_as_well"
    return fit
