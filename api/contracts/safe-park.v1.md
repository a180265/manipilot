# 校准安全停车能力（safe-park.v1）

`arm.park` v1.0.0 的中文名称为“机械臂回到已确认安全停车位”。它是高风险、可中断的
设备写能力，仅按设备当前校准档案的 `safePark` 移动，不接受坐标或接触参数。
插件须在 manifest 声明该能力和机械臂资源，并持有指定设备的有效任务租约。

请求参数为 `{}`，禁止附加参数。真实驱动要求校准和 daemon 会话有效、
`safePark.confirmed` 严格为 true、停车位会话与当前校准会话相同，且 XYZ 均为有限值。
仍检查实时画面、绑定、设备资格、停止/急停、任务归属、租约和动作上限；输入开关关闭
不单独阻止纯停车。该能力不读取 UI 目标档案，不要求当前页面或布局有键盘删除键。
它不能代替正式校准，不创建目标、不修复旧档案、不授权输入或发送。

动作复用 daemon 运动租约、分段、会话锚点和终点核验；不发送 TAP/TAP2。
到位回执包含 `ok=true`、`park=true`、`arrived=true`、
`role=calibration_safe_park`、`tap_executed=false` 及 `position`。
停车位缺失、过期或无效时明确拒绝并标明 `motion_not_started`；已开始后断连、
超时或到位证据不足按结果未知处理，不自动重试，不把之前已经确认的业务动作回滚。
仿真驱动返回相同无接触语义并记录独立 `park` 动作。

校准安全停车、业务 UI 悬停和硬件回零分别留证；`arm.home` 的平台安全状态检查
不能冒充本能力。能力注册默认清单在 Fleet 正常重注册时刷新；显式配置的设备能力
清单仍须由部署方明确增加 `arm.park`，不自动扩权。

绑定输入法的原文输入链在完整 `inserted` 回执后通过 `arm.park {}` 回停车位，再允许
后续画面核对与发送；使用该输入链的插件须声明 `arm.park`。候选栏点击显式失败或结果
未知时立即取消输入候选，不进入后续 60 秒输入等待，也不重试。旧驱动 `ok=true` 仍兼容，
但显式 `ok=false`、`controller_completed=false` 或 `outcome_hint=unknown` 不得视为完成。
普通点击中 `CameraMotionVerifier` 的帧差采用 `observation_only` 语义；
`motion_verified` 保留实际观测值，不因指令完成而改写。若回执同时为
`controller_completed=true`、`motion_observation_only=true`、`outcome_hint=success`，
即使 `motion_verified=false` 也仅确认控制器指令及终点动作完成，不证明触屏接触或业务生效。
文字是否完整由输入法回执确认，页面与点击效果由 Nano 判断。
停车失败、异常或到位未知均返回结果未知并禁止发送，
不会将已输入文字回滚或重新输入。本条沿用 v1 能力、租约与错误语义，不新增物理权限。
