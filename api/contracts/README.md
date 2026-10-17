# Arm Fleet API Contracts

本页按当前源码整理；历史加固日期不代表当前已部署版本或真机验收结果。

### 运行代码只读核对（runtime-build.v1）

`GET /v1/system/runtime-build` 返回本次 API 工厂启动时冻结的进程编号、采集时间、
Python 版本及关键模块摘要。`source_sha256_at_start` 是当时磁盘源码摘要；
`loaded_functions_sha256` 来自进程实际已加载函数/方法的代码对象，两者不能互相替代。
函数摘要仅在相同解释器和文件路径下比较。响应不读取设备、不修改库、不获取租约；
不包含源码正文或凭据，也不授予校准、动作、输入或托管资格。
它是新增只读诊断接口，不更改既有状态接口；旧服务没有此接口时不能伪造新版本已生效。

### 启用前维护与画面诊断（v1 兼容新增）

`ARM_FLEET_PRE_ENABLE=1` / `pre_enable_maintenance=true` 用于受控刷新后的停用状态，
默认关闭。`GET /v1/system/status` 新增同名布尔字段及 `operation_mode_display` 中文说明。
该模式不自动恢复任务、派发、清理租约、对账输入或回调，不挂载手机输入法协议，
不自动发现设备或拉起 daemon；真机只接纳既有 daemon 与帧桥绑定，
但不调用机械臂连接、自检或心跳，只在启动时采样一次相机质量。
设备质量的采样时刻见 `last_camera_check_at`，不能当作持续更新的现场监测。
HTTP 只开放明确列出的状态、设备、任务、告警、审计及原始来源图读取；
其余入口（含有副作用的 GET）统一 HTTP 503 `pre_enable_maintenance`。
`POST /v1/emergency-stop` 保留原权限和确认规则；复位、启用和配置写入不开放。
解除模式必须另外授权并受控重启，不能靠接口切换。它不是安全认证或登录机制。

设备概览、绑定视图、`device.updated` 兼容新增
`camera_quality_reason_code`（字符串或 null）、`camera_quality_note`（固定中文或 null）、
`camera_quality_metrics`（对象）。仅用于诊断，不改变原质量枚举或动作门禁。
原因码为 `screen_roi_not_found`、`camera_capture_failed`、
`camera_quality_check_failed`、`camera_frame_decode_failed`、
`camera_calibration_binding_mismatch`、`camera_calibration_screen_missing`、
`camera_quality_geometry_invalid`、`camera_detail_insufficient`。
恢复清晰时清除旧原因；未知异常使用固定中文，不透出原始异常正文、路径、URL 或凭据。
指标只允许有限非负的 `brightness / texture / blur_variance`，最多保留两位小数。
取流异常仍保留原异常路径，不以新增诊断字段静默改变既有安全状态。

2026-09-11 任务创建与插件边界加固保持现有契约版本兼容：
任务编号、幂等键和请求内容绑定；同请求重提返回原结果，旧编号用于
新请求返回 HTTP 409 `task_id_conflict`，不得覆盖或重做。
微信执行插件只用登记并验证有效的 `send_button` 发送，
不再从正文 OCR 回退推测发送按钮；目标失效时拒绝点击。
能力调用必须匹配持久任务、插件、设备及原租约，并受资源和物理动作预算限制。
插件仍是可信进程内代码，不是第三方不可信代码安全沙箱。

插件源码版本见 [`wx-wechat-adapter/manifest.json`](../../examples/plugins/wx-wechat-adapter/manifest.json)；
它不证明运行实例已安装或加载同一版本。

## 自动手势与导航契约

以下契约已有源码和同名 JSON Schema，不再只是后续预留：

- [automatic-gesture-profile.v1](automatic-gesture-profile.v1.md)：自动手势及导航的只读资格与发布状态。
- [gesture-capture.v1](gesture-capture.v1.md)：手机连续触摸采样、去重与冲突。
- [gesture-validation.v1](gesture-validation.v1.md)：校准后的自动手势自检任务。
- [gesture-program.v1](gesture-program.v1.md)：受校准和权限约束的单指手势程序。
- [navigation-template.v1](navigation-template.v1.md)：桌面及顶部模板登记。
- [probe-path-observation.v1](probe-path-observation.v1.md)：探头实体路径证据。
- [wx-auto-navigation.v1](wx-auto-navigation.v1.md)：微信自动导航任务。

WX 的按设备导航选择与 Fleet 的动作资格分开管理；默认关闭不等于没有实现。
完整使用边界见[自动手势闭环使用方法](../../docs/自动手势闭环-使用方法.md)。

## 既有任务与观察契约

[debug-gestures.v1](debug-gestures.v1.md) 记录调试台人工单次双击/滑动、一次性资格、
未知结果人工核对和画面证据。它是本机调试接口，不是 WX 的跨项目任务入口。

[screen-gesture.v1](screen-gesture.v1.md) 和同名 Schema 定义正式任务入口的任意点双击及两点滑动。
WX 只传正向物理屏幕归一化点和完整校准绑定，不传机械坐标、下压深度或停车参数。
读取逐动作资格后，经 `POST /v1/tasks` 提交稳定编号任务；重复请求不重复执行。
这是独立版本的坐标契约，不改变旧导航档案或聊天发送契约。插件清单可声明
`request_schema` 校验完整请求外壳，`screen_context_guards` 保护其他插件的屏幕会话。
机械完成与手机效果分开报告；源码和仿真通过不表示现场已加载或真机已验收。

[screen-wake.v1](screen-wake.v1.md) 定义独立人工唤醒写任务。位置仅从本次校准
已通过的验证点选取，客户端只提交绑定与确认；允许新鲜黑屏等待，不豁免
其他安全门禁。控制器双击完成、观察到亮屏、解锁是不同事实，该任务不解锁。

[saved-screen-gesture.v1](saved-screen-gesture.v1.md) 和同名 Schema 定义调试台划线或点选后保存的
通用命名手势。WX 读取编号、版本和完整动作快照，核对当前资格后仍通过
`screen-gesture.v1` 提交。保存不执行动作，名称不证明手机效果；
校准变化后失效，不自动重绑，不改变既有 `wx_chat_list` 导航业务。

[screen-proof.v1](screen-proof.v1.md) 定义设备级页面/区域档案、只读元数据和
`camera.observe_regions` 通用视觉证据。WX 只消费结果，不持有区域坐标。

[bounded-click.v1](bounded-click.v1.md) 定义 `arm.click` 的可选微移序号、
同目标安全范围和单次动作语义。业务效果核验及有限重试由插件承担。

[screen-observation.v1](screen-observation.v1.md) 和同名 Schema 补录现有
`GET /v1/devices/{device_id}/screen-observation` 的屏幕图像/OCR 响应及错误外壳，
包括校准无效的 HTTP 409。它不同于 `screen-proof.v1` 区域证据和
`observation-envelope.v1` 入站信封。两项目各自随包保存副本，不增加运行期跨包依赖；
Schema 补录不改变生产接口或既有消费校验，也不代表现场已经通过。

[task-review.v1](task-review.v1.md) 定义无物理动作的人工核对与插件上下文释放；
不改写 unknown、不恢复设备运动、不重发原任务。

[gesture-profile.v1](gesture-profile.v1.md) 定义校准手势档案的只读元数据与任务引用；
WX 只传档案编号和版本，不传坐标。档案可解析与真机完整业务就绪分别判断。

[wx-session.v1](wx-session.v1.md) 定义外部 WX 插件的指定聊天打开、人工发送及退出任务，不是平台内置托管功能。
打开任务默认人工控制，可显式指定自动控制；自动发送仅复用匹配的自动执行会话。
所有任务保持短租约；等待时不持有硬件锁；异常不自动退出或重发。
打开任务提供动作截图证据，WX 负责新画面的身份核对与允许名单；输入法由 Arm Fleet 管理。

[wx-list-navigation.v1](wx-list-navigation.v1.md) 定义旧档案引用的有限列表导航及人工释放边界；
与新 `wx-auto-navigation.v1` 并存，不能互相替换请求格式。

仅有 JSON Schema、无单独 Markdown 说明的契约：
[chat-send.v1](chat-send.v1.schema.json)、
[task-result.v1](task-result.v1.schema.json)、
[customer-message.v1](customer-message.v1.schema.json)、
[observation-envelope.v1](observation-envelope.v1.schema.json)、
[camera-view.v1](camera-view.v1.schema.json)和
[human-takeover.v1](human-takeover.v1.schema.json)。
其余契约的同名 Schema 与上述说明并列存放；以 Schema 和运行时校验核对字段。

这些 JSON Schema 是 WX 与 Arm Fleet 唯一的跨项目数据边界。修改字段时必须提升 `contract_version` 或提供向后兼容策略，并同步更新 M5/M6 的 release manifest。`camera-view.v1.schema.json` 定义只读摄像头查看会话返回值；它不授予机械臂控制权限，也不创建人工接管。`human-takeover.v1.schema.json` 约束接管请求的会话标识和确认令牌字段；操作者二次确认还须由交互层落实，WX 仍是 `human_takeover` 事件的事实源。

二次确认是接管交互的契约要求，**当前 WX 前端未实现**（2026-09-27 静态复核）：
[`CameraViewer.tsx`](../../../frontend/src/components/CameraViewer.tsx) 点击“人工接管”后
建立接管查看会话并直接请求接管，没有可取消的再次确认；
[`armFleet.ts`](../../../frontend/src/api/armFleet.ts) 固定填写 `confirmation_token: '确认执行'`。
Schema 和服务端权限校验仅检查令牌字面值，不能证明操作者在本次接管前完成二次确认；
不得将请求能通过校验写成该交互已验收。

实时查看的地址分两层，不能混用：Arm Fleet 服务的规范地址是 `POST /v1/camera/view-sessions`、`GET /v1/camera/view-sessions/{view_session_id}/stream`、`POST /v1/camera/view-sessions/{view_session_id}/renew` 和 `DELETE /v1/camera/view-sessions/{view_session_id}`；生产 WX 应通过自己的后端适配层暴露 `/v1/arm-fleet/camera/...` 并由服务端转发，浏览器不应直连 Arm Fleet 原生地址。当前本地 `frontend` 开发环境的 `/arm-api` 是 Vite 同源代理，仅用于开发联调；发布部署必须由 WX 网关或后端实现同等服务端转发，不能把开发代理当成生产鉴权边界。

`camera-view.v1.schema.json` 约束的是返回值，不定义创建、续期或关闭请求。
当前请求必填字段、可选 `camera_id`、默认值及错误行为应核对
[API 路由](../app.py)与[查看会话管理器](../../runtime/views.py)；
[前端与控制台实施规划](../../docs/前端与控制台实施规划.md)是交互要求，
不能充当请求与错误语义的完整契约。
查看会话的状态变化（有效、即将过期、已关闭、已过期、画面不可用或等待画面恢复）
不等于设备状态，也不产生人工接管事件；近黑等待时不应用旧帧确认业务效果。

所有协议键、状态键和任务类型在运营界面必须有中文显示，具体规则见
[术语与中文关键值规范](../../docs/术语与中文关键值规范.md)。
通用运行时和微信双向适配的实施顺序见
[通用硬件任务运行时规划](../../docs/通用硬件任务运行时规划.md)。
