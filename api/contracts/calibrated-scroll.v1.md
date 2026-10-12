# calibrated-scroll.v1 — 校准直驱上下滑动契约

规格来源：《2026-10-03-输入开关与校准直驱上下滑动修改规格》§5/§6/§7。版本 1.0。

## 任务

- `task_type: calibrated_scroll`，`contract_version: calibrated-scroll.v1`，经 `/v1/tasks` 提交，
  由 screen-gesture-adapter 插件承载；复用现有驱动、调度、租约、证据与回报。
- 客户端只选方向：`payload = {"action": "swipe_up"|"swipe_down", "binding": <能力查询返回的完整 v3 绑定>}`。
  `additionalProperties=false`：证书 ID、模板、机械坐标、Z/速度/时长覆盖、ready/跳门禁等字段一律 400。
- 绑定十字段：`device_id, daemon_session, camera_id, camera_device_unique_id,
  camera_binding_id, source_geometry_id, binding_revision, calibration_fingerprint,
  frame_width, frame_height`。动作前与当前校准做语义比较（相机全局诊断代际不在字段内）；
  不一致拒绝，不悄悄换绑执行。需要实际验证完成的 `schemaVersion=3` 校准；
  旧 v2 保持原检查，不得加两字段改版本续用。

## 能力查询

`GET /v1/devices/{device_id}/calibrated-scroll`（零动作、不发证、不写校准）：
`contract_version, device_id, binding|null, usable, actions.{swipe_up,swipe_down}.usable/blocking_reasons,
blocking_reasons, planning_version, calibration_version, round_budget{8 次/90 秒}, sampling_support`。

## 结果分离（§6.4）

`mechanical_status`(not_started/confirmed/unknown) 与 `effect_status`(not_started/displaced/stationary/unknown)
分开表达；`motion_started` false/true/null；`retryable` 恒为 false。终态映射：

| 场景 | 任务终态 | 机械 | 效果 |
| --- | --- | --- | --- |
| 动作前校准/帧/绑定失败 | failed | not_started | not_started |
| 机械确认 + 位移足够且方向正确 | confirmed | confirmed | displaced |
| 机械确认 + 可靠测量静止 | failed(`scroll_no_displacement`) | confirmed | stationary |
| 机械完成但方向相反/测量不足/遮挡 | unknown | confirmed | unknown |
| 机械完成与否无法确认 | unknown | unknown | 按测量实录 |

效果未知只允许查询原任务、查看新画面或人工处理；不得自动重放同义新任务。
`scroll_no_displacement` 不得进入发送/导航自动重试机制。

## 测量

- 观察区默认 ROI（屏幕归一化）`{x:0.10,y:0.20,width:0.80,height:0.60}`，经
  `camera.measure_scroll`（姿态对齐→单应→screen_norm）测量；
- 帧链归并统一走 `scroll_observation.aggregate_scroll_chain`：逐对保留点数/MAD、
  累计横移路径设限、首尾 OCR 矛盾判 unknown；
- 阈值不放宽：configs/gesture_defaults.json::scroll_observation 为唯一来源。
