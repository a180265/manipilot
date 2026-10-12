# task-review.v1：人工核对后释放插件上下文

此接口只承载人工核对声明，不产生物理动作、不改写已保存的执行结论。
平台不解释应用页面；具体应用上下文由任务所属插件核对。

`POST /v1/tasks/{task_id}/review-context`

请求：

```json
{
  "operator_id": "销售人员",
  "role": "operator",
  "reason": "已现场核对并手动返回应用首页",
  "correlation_id": "原任务编号",
  "client_request_id": "本次核对唯一编号",
  "expected_device_id": "arm-01",
  "expected_task_status": "unknown",
  "review_context": {
    "expected_session_id": "原执行会话编号",
    "observed_ui_state": "wechat_main"
  }
}
```

`review_context` 是插件定义的 JSON 对象。示例中的会话和页面字段只属于
WX 外部插件，通用平台不硬编码其语义。

成功返回 `contract_version=task-review.v1`、原任务/设备编号、原执行状态、
`context_released=true` 和 `reviewed_by`。原状态不会从 unknown 改成 confirmed。

前置条件：

- 运维角色与非空操作人、原因、关联编号、请求编号。
- 原任务属于给定设备，当前状态与预期完全一致，且已经终态。
- 原执行协程已结束，设备没有其他未结束任务，也没有其他任务租约。
- 插件实现 `review_task(task, ReviewContext, review_context)` 并明确允许释放。
- 同一请求编号只能对应同一任务、设备、预期状态和核对内容。

`ReviewContext` 仅提供插件自己的状态读写；不提供移动、点击、输入、
采集证据或改写观测状态的方法。状态改动在插件明确允许后才提交。
此接口不释放租约、不解除设备暂停或隔离、不更新校准资格。
这些仍由独立设备控制台的既有复检/恢复操作负责。

失败返回现有中文错误契约：状态已变/尚未结束/占用时 409，
只读角色 403，缺参数 400；旧插件默认拒绝上下文释放。
HTTP 响应丢失时只能重试同一个核对请求，不能重放原任务。

WX 的调用规则：

1. 用户必须先在手机上核对任务结果并手动返回微信首页。
2. 打开/退出任务选择不涉及发送；发送任务明确记录已发送或未发送。
3. WX 保存审计并归档关联待核对记录，关闭执行会话、撤销目标验证，
   停止本设备 WX 接收，等待用户复检后重新开始。
4. 不把人工声明伪造为摄像头发送证明，也不自动补发“未发送”的消息。
5. 原任务查不到或服务不在线时，不猜测原任务已结束，不放行恢复。

旧 `POST /v1/tasks/{task_id}/mark-reviewed` 保持原有仅标记语义，
不能替代本接口的插件上下文核对。
