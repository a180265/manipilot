# gesture-validation.v1 — 校准后自动手势自检任务

版本：`gesture-validation.v1`（规范第 12.1/22.12 节）。

- `task_type = gesture_self_test`，插件 `gesture-validation-adapter`。
- payload 必填 `binding / actions / rules_version`；可选 `vision_only`（布尔，
  默认 false，2026-09-30 拍板）：视觉优先——跳过手势页会话与 ClientHello
  心跳，滑动效果以相机前后帧光流判定并发证。仅纯滑动集（只含
  `scroll_up / scroll_down`）生效；混合动作集一律回页面采样路径。
  平台提交端点对纯滑动集默认置 true。
- `actions` 为内置动作唯一数组：`scroll_up / scroll_down / system_back_left /
  system_back_right / system_home / double_tap`。
- `binding.gesture_profile_version = 0` 仅允许出现在本任务（尚无自动档案时自举）；
  通用路径与自动导航不得用 0 绕过资格。
- 任务超时 600s、物理动作上限 36、每候选重复验证 3 次，由插件清单 limits 显式声明。
- 结果 detail 结构见规范第 22.16 节（budget/action_results/evidence_refs）。

## NAV07 生产装配边界

- 自检插件通过 `StepContext.data_root` 读取 bundle、坐标桥、模板及保存 run；
  证据文件只从 `StepContext.evidence_root` 读取。模块 `DATA_ROOT` 仅保留兼容，
  不作生产回退。证书仍只能通过 `device.automatic_gesture_profile(op=publish)` 发布。
- 请求包含 Home 时，在整个 run 开始前检查当前完整绑定和服务端登记的 launcher
  模板（`NavigationTemplates` 产物），以及校准域裁剪的 SHA-256、尺寸、纹理、区域
  和证据归属。预检、每次动作前及匹配时均通过 `ctx.get_evidence` 核对平台
  证据记录；该只读接口只返回本设备记录的深副本，缺失/跨设备返回空。
  数据库记录的设备、任务、路径或类型变化即使图片未变也必须拒绝，不能仅凭文件
  哈希发证。缺少模板返回 `launcher_template_missing`；模板绑定失效返回
  `navigation_template_binding_changed`；内容或裁剪失效返回
  `navigation_template_invalid`。这些前置失败均为明确失败、零手势动作，不发布证书。
- Home 的 `verify_page` 使用本次动作后经 `ctx.capture_evidence` 生成的截图，
  校验任务/设备归属、哈希、解码尺寸及新鲜度（不超过 5 秒且晚于动作前帧），
  再按当前相机到屏幕单应变换矫正，复用 `verify_top_template(min_anchors=3)`。
  动作前及验证时重新检查当前绑定/模板；不导入其他外部插件，不直接访问硬件。
  当前 CameraFrameBroker 的证据记录可能没有 `camera_id`，来源由 ctx 的受控
  设备相机能力和当前设备绑定校验；已有非空相机编号必须匹配。
- 桌面未命中、图像过期/不可解码或动作后模板失效，都不能推断手机仍在测试页，
  不返回可继续枚举候选的 `confirmed=False`。验证器返回未知，由 Runner 结合
  控制器/手机触点归约；有接触而页面未证实时为 `unknown`，不继续、不重放。
  Back 不借用桌面证据充当父页证据：无父页验证器时继续已有手机提交事件通道。
- 插件不点击推测坐标、不自动拉起测试 App，也不宣称已恢复采样页。每次 Home
  试验仍必须经过手机针对新 trial 的 `prepare_home → ready`；没有新的 ready
  就停止，不再下发手势。连续三次认证不豁免这一条件。
- 执行回调被受控门禁拒绝且异常明确标记 `motion_not_started=True` 时，只撤销
  本次预留动作计数，记录 `not_started/unverified` 并终结采样会话，不枚举下一候选。
  未标记的驱动异常仍保留 unknown 和保守计数；不能由“没收到成功”推断没有运动。

隔离验收入口为 `tests/test_gesture_validation_wiring.py`：真实插件、Runner、
能力发布、`gestures_view` 和导航 resolver，仅手机协调器、机械臂驱动、相机为
Fake；全部运行根和证据根使用临时目录。软件测试不代表现场部署或真机效果验收。
