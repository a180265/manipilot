# 人工通信核查与校准安全停车（operator-recovery.v1）

本契约独立于任务结果核对与业务页面归位。超时后原任务仍是结果未知；人工确认
设备位置不会把原任务改成成功，也不允许自动重复点击。通信核查只解除本次通信
故障锁，不发送恢复运动字节，不解除人工急停，不恢复设备或自动托管。

- `GET /v1/devices/{device_id}/operator-recovery`：新鲜只读连接、会话及位置；故障
  锁下可能没有位置。`session`、`recovery_id` 供确认请求绑定。
- `POST /v1/devices/{device_id}/recover-communication`：人工确认后核查通信，绑定
  `expected_session` 和 `recovery_id`；daemon 确认稳定 Idle 和协议同步后才清故障。
- `POST /v1/devices/{device_id}/park`：独立新动作，回当前会话校准的 `safePark`。
  不接受坐标，不点击、不回零，不把到位解释为手机已在业务首页。
- `POST /v1/devices/{device_id}/operator-recovery/review`：核对之前未知或重启遗留
  pending 操作，额外提交 `operation_request_id`。旧结果不改写，只加复核记录。
  须当前通信正常、Idle、位置可读；换会话须先完成当前会话有效校准与安全停车位。

所有写请求包含 `contract_version=operator-recovery.v1`、`expected_session`、
`operator_id`、`role`、`reason`、`correlation_id`、`client_request_id`、
`confirmation_token=确认执行`，以及 `confirmations` 四个严格布尔 true：
`arm_stopped`（机械臂已静止）、`probe_clear`（探头已离屏）、
`setup_unchanged`（没有人为挪动手机或机械臂）、`path_clear`（停车路径无遮挡）。
普通运维及以上角色可操作；WX 入口从可信人员会话取身份并校验 CSRF。
确认探头离屏是运动前提：现有停车路径先 XY 再 Z，不能在探头仍压屏时执行。

设备须已暂停、无执行中任务或协程、无未核对的 unknown 任务；保留租约只可在
原任务 unknown 已人工核对且协程退出后审计释放，再为本次操作申请新短租约。
全局急停、设备隔离、会话变化、无有效校准停车位均不能通过停车核查绕过。
停车逐段复核原会话、设备身份、绑定、暂停状态、急停及本次租约。

GET 响应包含 `can_recover_communication`、`can_park`、`can_review`，各动作的
`*_blocking_reasons` 与中文原因，`last_operation`、`pending_operations`。
每个待核对操作含 `can_review`；GET 可传 `client_request_id` 查询
`requested_operation`，避免其他人的新记录遮盖原请求。
停车未知之后允许独立通信核查以解除通信故障，但旧停车仍待复核，不能继续运动。

写响应包含设备、会话、请求编号、`operation`（`recover_communication` / `park` /
`operator_recovery_review`）、`status`（`pending` / `unknown` / `rejected` /
`confirmed`）、中文 `status_display`、`receipt` 和 `park_confirmed`。
只有 `ok/park/arrived=true`、`tap_executed=false`、
`role=calibration_safe_park` 的完整停车回执才确认到位。

操作在物理下发前持久登记 pending，设备级互斥；相同请求编号与内容只返回原记录，
不同内容冲突，不自动重放。进程重启的 pending 保守视为待人工核对。
回包丢失后客户端只读查询原编号，不新建编号重试；明确前置拒绝且编号未认领时，
错误响应顶层 `not_submitted=true`。未知、在途、冲突、网络失败不得假定未执行。
所有完成路径保持设备暂停，旧任务结果及业务发送事实不变。
