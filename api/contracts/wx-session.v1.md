# WX 会话执行操作契约 v1

日期：2026-09-11。外部 `wx-wechat-adapter >= 1.4.1` 提供，不启用兼容托管器。
新增字段向后兼容：未填写控制模式的打开任务仍为人工操作，旧持久会话也按人工控制处理。

统一通过 `POST /v1/tasks`，外层为 `task_id`、`idempotency_key`、
`device_id`、`wechat_account_id`、`external_conversation_id`、
`expires_at`、`correlation_id`、`contract_version: wx-session.v1` 和 `payload`。
账号和外部会话由 WX 数据库关联获得，不能拿内部会话编号冒充外部编号。
输入字段 schema 见插件清单，各任务不接受额外字段。
三种任务的 `payload` 均必须携带 `wechat_account_id` 和
`external_conversation_id`；通用运行时只保留通用任务外层字段，
插件以 payload 内的账号和外部会话为执行依据，WX 两处值保持一致。

| task_type | 中文名称 | payload | 完成条件 |
|---|---|---|---|
| wx_session_open | 打开指定的聊天 | session_id、conversation_id；可选 conversation_title、control_mode、navigation_profile | 校验导航档案、确认列表顶部后查找；打开并确认会话界面、提供前后截图动作证据、点击 input_box、保留会话、删除键悬停 |
| wx_session_send | 人工发送当前聊天 | session_id、conversation_id、outbox_id、text、text_hash；其余 Outbox 元数据见 schema | 输入法确认完整原文、发送可证实、停靠；不返回主界面 |
| wx_session_exit | 按人工要求退出聊天 | session_id、conversation_id | 核对当前会话、点击 back_button、确认主界面、按档案回顶、清除会话、停靠 |

`control_mode` 中文名称为“执行控制模式”：`manual` 为人工控制（默认），
`automatic` 为自动控制。它只用于执行会话归属，不是白名单、业务身份或发送许可。
由 WX 判断是否允许自动进入聊天；插件只接收已授权任务，不读取 WX 数据库、代码、
客户画像、名单、消息推理或销售规则。

WX 持久保存每台设备当前执行会话和操作任务；提交前写记录。
同一请求编号不能重复动作，超时只查询原任务；未知结果不自动重发。
不同会话不得占用同一设备；人工发送期间不能退出；网页返回不等于物理退出。
插件保留原私有状态键 `manual_session` 以兼容升级，值内保存执行会话编号、
外部会话编号、账号编号、daemon 会话及 `control_mode`。
状态按设备隔离；控制器重启后旧会话禁止使用。
所有动作通过能力门面和短任务租约；等待人工期间不持有长期硬件租约。
异常恢复只停止，不擅自退出。手动发送不经过 RAG、草稿审批或销售事实门禁。

## 自动发送复用已打开会话

`chat-send.v1.payload.session_id` 是可选的“执行会话编号”。
不携带时保持原有从主界面打开、输入、发送、返回、停靠计划；
打开后增加输入前的会话核对，聊天不匹配或缺少可靠标题证据时禁止输入和发送。
设备已持有任何会话时拒绝另起发送，不得抢占人工或自动会话。
携带时必须匹配当前设备持有的自动会话、账号、外部会话和 daemon 会话。
对应计划为：

`check_session → check_account → confirm_conversation → focus_input → input_text
→ send_and_capture → return_main → return_chat_list_top → clear_session → park_ime_delete_key`。

不会重复打开聊天。`wx_session_send` 仍允许人工向当前已持有的人工或自动会话发送，
并保留聊天；`wx_session_exit` 为明确退出操作。自动发送不得借用人工会话。
自动发送执行异常时保留会话并停止，不能跳过核对、重复发送或自动换人。

## 打开动作证据

2026-10-17 插件1.8.1兼容扩展：`open_conversation/focus_input/return_main` 在原任务内
支持目标区域内微移。默认中心，只有两帧新鲜证据明确证明未生效且原目标稳定才补点，
每个目标最多三次。未知结果不补点；发送、删除、候选栏和一次性标读授权不扩展。
各步骤 `detail` 兼容新增 `click_retry`（效果、原因、尝试证据与次数）、
`contact_count`（实际接触次数，未知为null）与 `contact_count_status`。
提交已接受不代表完成；WX按原任务查询结果，旧回执不猜一次接触。
请求外壳及payload不变，区域坐标仍由Fleet侧管理。

`GET /v1/tasks/{task_id}` 的 `step_log` 中，成功的 `open_conversation` 完成记录包含：

```json
{
  "phase": "done",
  "action": "open_conversation",
  "outcome": "success",
  "detail": {
    "selection_evidence": {
      "action_id": "打开任务编号",
      "target_id": "WX 下发的外部会话编号",
      "before_screenshot": "点击前证据编号",
      "after_screenshot": "点击后证据编号",
      "result": "成功"
    }
  }
}
```

证据编号由受控截图存储生成，不是 WX 本机文件路径。动作成功只证明点击后进入会话界面；
WX 必须在打开任务完成后读取新画面并独立核对客户身份，再产生自身的聊天对象选择事件。
点击后界面为 `unknown` 或截图证据不完整时报告结果未知，不返回成功证据、不继续输入。
插件不代替 WX 确认身份，不把任务中的目标名称当作真实识别结果。

输入法路由、在线状态与执行确认由 Arm Fleet 负责。daemon 设备有效校准档案的
`imeDeviceId` 优先于旧配置占位编号；档案设备、摄像头和 daemon 会话必须一致。
不得选择“唯一在线手机”作为回退。`requires_input` 是插件任务输入门禁声明，
默认 false；发送任务为 true，在提交和执行前均由 Arm Fleet 检查。
`requires_keyboard_active=false` 表示任务会先点击输入框，再由机械臂侧输入服务等待并核对键盘激活；
这不等于 WX 检查输入法在线。

## 聊天列表滚动导航

推荐使用 `navigation_profile`，WX 通过只读 `gesture-profile.v1` 查询取得编号和版本，
不接收或持久化手势坐标。档案在调试台完成人工标记、两方向测试和明确启用。
改动标定、设备会话或档案版本会使旧引用失效。配置后行为为：

`按档案确认列表顶部 → 当前画面 OCR → 未命中则滑动一次 → 等待画面稳定
→ 新画面与 OCR 核对 → ... → 找到后点击并核对会话`

每次滑动最多执行一次，`max_swipes` 上限为 10；滑动结果或前后截图无法确认时立即停止，
不自动再次滑动、不自动点击，任务报告结果未知并转人工。滑动的 `start`/`end` 必须是当前摄像头
画面的 `cameraX/cameraY/frameW/frameH`，不能填写手机屏幕绝对坐标。

需要回顶时在调试台启用 `return_to_top`、登记顶部专属文字及验证矩形、回顶手势。
发送完成的自动路径和明确退出路径会执行 `return_chat_list_top`；只有重新 OCR 找到顶部锚点
且文字位于登记矩形内才算成功；常驻“微信/WeChat”标题不能作为顶部标志。
滑动 ACK 不能代替顶部确认。`wx_session_send` 仍保持当前聊天，通常由后续
`wx_session_exit` 完成回顶；若回顶失败，任务不报告完整成功并保留人工处理状态。

示例（编号和版本须使用当前设备查询结果；截止时间由调用方在提交时生成）：

```json
{
  "task_type": "wx_session_open",
  "task_id": "wx-open-001",
  "idempotency_key": "wx-open-001",
  "device_id": "arm-01",
  "wechat_account_id": "wx-account-01",
  "external_conversation_id": "customer-001",
  "expires_at": "2026-09-11T23:59:00+08:00",
  "contract_version": "wx-session.v1",
  "correlation_id": "wx-ui-001",
  "payload": {
    "session_id": "wx-session-001",
    "conversation_id": "customer-001",
    "wechat_account_id": "wx-account-01",
    "external_conversation_id": "customer-001",
    "conversation_title": "客户甲",
    "control_mode": "manual",
    "navigation_profile": {"profile_id": "wx_chat_list", "version": 1}
  }
}
```

外部系统提交后通过 `GET /v1/tasks/{task_id}` 查询 `step_log`。重点检查
`open_conversation`、`return_chat_list_top` 的 `outcome` 和 `detail.attempts`，
不要只依据 HTTP 接收成功判断机械臂动作完成。

## 发送证据与现场边界

历史实现曾在真机发送后统一保守报告未知；这是早期缺少专用发送证据时的限制，
不是当前插件 1.6.0 的全部实现状态。当前已有页面/区域登记、启动加载、标题核对及
新增己方气泡证据检查，规则见 `screen-proof.v1.md`。

真机不得使用仿真文本或仿真发送确认。截图、点击完成或全帧 OCR 命中正文均不足以证明发送。
只有当前目标、批准原文、输入框清空、新增己方气泡及连续新帧等证据满足检查时才确认发送；
证据不足仍报告未知并保留现场，不生成虚假发送编号、不重发、不继续自动退出。
发送已经证实但返回/停靠失败时保留发送事实，单独报告收尾待核对。
人工发送保留聊天，自动发送才按其任务计划退出。

软件具备上述检查能力不等于当前设备的页面档案、主题布局、实际点击或发送已经现场验收。
真实图像识别与手机动作仍须逐设备验证；WX 新画面身份核对和插件本地执行证据
属于不同职责，不能用一方通过替代另一方。旧仿真测试或历史部署记录不作为当前真机通过证明。

兼容说明：未配置档案的设备保留原有“仅查找可见项、不滑动”路径。
旧直接调用的 `navigation` 仍按严格参数校验接受，但不能与档案引用同时传入，
不能使用常驻标题证明到顶；WX 正式路径不使用内联坐标。
