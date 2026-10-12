# wx-auto-navigation.v1 — 微信自动导航任务

版本：`wx-auto-navigation.v1`（规范第 12.2/22.5/22.6 节）。

- `task_type = wx_auto_navigate`，新版外部 `wx-wechat-adapter` 注册。
- payload 必填 `navigation_id / binding / navigation_profile / action / step_index`。
- `action ∈ {search_next, search_previous, return_top, back_to_list, home}`；
  `target`（visible_name + external_conversation_id）仅 search 类允许；
  `session_ref`（session_id + control_mode + external_conversation_id）仅
  `back_to_list` 必填、其他动作禁止。
- `navigation_profile` 有且仅有 `profile_id/version/contract_version`，其中
  contract_version 固定 `automatic-gesture-profile.v1`。
- `expires_at` 必须为有效 UTC 时间，不允许 null。
- 每任务最多一次机械动作；search 先在当前帧精确找目标，命中即
  `target_found=true, controller_status=not_started`，不点击。
- 已完成步骤为 `step_log[].action=auto_navigate`，与插件实现一致；
  `detail.action` 仍为请求中的 `return_top/search_next` 等业务动作。
  WX 兼容读取旧步骤名 `automatic_navigation`，不改写历史回执。
  detail 其余字段见规范第 22.5 节表格。
- 沿用外部非发送任务的提交/轮询；不进聊天 Outbox 回调。

## 执行与人工核对

平台通过通用同步 `TaskPlugin.validate_preconditions` 钩子，在受理、出队及动作下发前
复核实时资格；异步规划后再次复核。插件负责导航业务条件，平台不写入微信页面逻辑。
关闭开关、校准/坐标桥变化、档案版本失效均拒绝后续物理动作；停止及安全归位不被该
业务钩子拦截。已进入驱动而无法确定结果时保留 `unknown`，不得按失败重新发送。

人工核对使用现有任务 review 接口。仅原终态任务、原设备、原 `navigation_id`、
人工确认主列表且无聊天占用时，才释放导航上下文并给私有台账添加核对记录。
原任务结果、步骤证据、校准及设备暂停/隔离状态不变；被核对轮次不可重启或追加动作。
操作员仍须按既有安全流程处理设备状态；后续导航必须使用新的轮次编号。
