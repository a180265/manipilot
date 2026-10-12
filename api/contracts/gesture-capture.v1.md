# gesture-capture.v1 — 手机连续触摸采样协议

版本：`gesture-capture.v1`（规范第 5 节，冻结于《自动手势闭环与WX导航完整实施规范-2026-09-15》）。

## 边界

- 手机侧只采样与上报，不请求任何机械动作；机械动作由 Fleet 自检任务在 Ready 握手与租约齐备后下发。
- 挂载于 8788，路径见规范第 5.2 节路由表；错误外壳沿用 `{"error","message"}`。
- 不增加旧 `/touch` 字段，不复用旧校准 `samples`。

## 对象

- `ClientHello`：登记客户端能力与页面元数据；`capabilities` 首版允许
  `gesture_trace.v1 / scroll_fixture.v1 / back_fixture.v1 / double_tap_fixture.v1 / home_fixture.v1`。
- `PageMetadata`：root/display 尺寸、原点、insets、density；`legacyCalibrationSessionId`
  仅 `legacy-calibration` 页非空（旧 cal- 会话），`coordinateSpaceVersion` 固定 2。
- `Command`：`kind ∈ {open_fixture, reset_list, prepare_back, prepare_home, prepare_double_tap, finish}`；
  `issuedAt/expiresAt` 固定相差 10000ms；寿命由服务端单调时钟控制。
- `Ready`：不可变命令确认；同 commandId 内容不同 → 409 `ready_payload_conflict`。
- `Trace`：`coordinateSpace=calibration_root_px.v1`、`clockDomain=android_uptime_ms`；
  事件 tMs 相对本 trial 第一个 DOWN；`endReason ∈ {up,cancel,overflow,multi_touch,page_lost,timeout}`；
  `scroll` 非空时 ScrollEvidence 必填；`recognition` 仅双击非空。
- `ScrollEvidence`：原生滚动偏移 + 行号互校验；`boundary ∈ {top,bottom,neither}`。
- `PageEvent`：`kind ∈ {page_resumed,page_paused,page_stopped,page_destroyed,back_started,back_cancelled,back_committed}`；
  `callbackSource ∈ {lifecycle,on_back_invoked,on_back_animation,on_back_pressed}`。
- `Receipt`：`traceId/payloadHash/received=true/acceptedForEvaluation/reasonCode`。

## 去重与冲突

- trace 主键 `sessionId+trialId+traceId`；同主键同内容返回相同 Receipt。
- 同主键不同 payload hash → 409 `trace_payload_conflict`，试验转 unknown。

详见规范第 5、22.2、22.3 节。
