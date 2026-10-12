# probe-path-observation.v1 — 探头实体路径证据

版本：`probe-path-observation.v1`（规范第 22.14 节）。

- 由只读能力 `camera.measure_probe_path` 返回；复用 `drivers/probe_motion.py`
  的登记实体跟踪，不修改旧核验方法返回值。
- 字段：`contract_version / plan_id / binding / frame_seqs / points / verified /
  reason_code`；points 每项 `frame_seq / x_camera_px / y_camera_px / at_ms`。
- 至少 3 点、最大采样间隔 300ms、按现有 trace_verdict 核对预期 XY 位移；
  证据缺失即不支持边界探测。
- 该证据证明实体运动，不证明触屏接触。
