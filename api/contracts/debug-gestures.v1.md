# 调试台单次手势接口

版本：debug-gestures.v1，日期：2026-09-11。
兼容扩展：2026-09-14，增加校准后只读计算和归一化测试。
当日后续修订：按操作者要求将本机调试台改为人工观察模式。
仅用于本机调试台的人工试验，不是 WX 跨项目动作入口。
沿用现有调试服务的来源、设备上下文和运动权限检查；
不新增远程身份权限，不应将本机调试端口直接暴露到公网。

## 同设备操作互斥（2026-10-17 兼容扩展）

调试台按设备对整次操作互斥，覆盖校准参数测试的布防、检测、接触、等待手机
回报与保存。期间同设备的运动请求、再次测试、重新开始校准、保存点击参数、
确认/清除停车位和连接/复位请求立即返回 HTTP 409；不等待上一请求完成，
也不排队补发。不同设备可以独立操作。共享安装锁仍用于阻止安装绑定同时修改，
`z-confirm-safe` 确认点击参数也须持有该锁。

错误契约沿用 `debug-gestures.v1` 错误结构：

```json
{"ok":false,"error":{"code":"debug_operation_busy","message":"当前设备正在执行调试或校准操作，请等待本次结果；本请求未执行，也不会排队补做。","details":{}}}
```

`debug_operation_busy` 中文名称为“设备调试操作进行中”，仅表示当前请求未执行，
不表示上一操作成功或失败。调用方展示提示并等待原请求结果；不得自动重试。
操作正常返回或异常结束均释放请求互斥；异常本身仍保留原错误码和校准失败状态。
既有来源、设备上下文、运动权限和自动校准活动门禁不因互斥而放宽。
`/api/stop`、`/api/estop`、向导停止/取消和只读查询不取得本互斥锁；
停止与急停仍沿用原校验和执行路径。

## 读取资格与结果

`GET /api/gestures/state?deviceId=...&contextId=...`
返回设备、校准/相机/会话绑定快照 `binding`、一次性 `requestId`、
有效期 `expiresInMs`、`blocked`、`blockingReasons` 和 `lastResult`。
请求编号由服务端签发、仅在当前上下文有效，不由调用方自造。

响应不明时附原 `requestId` 查询。服务端可撤销仍未消费的资格，
然后明确报告 `requestStatus=not_accepted`（未受理、未执行）；
迟到的旧 POST 将被拒绝。已执行的未知动作不能改写为未受理。

## 测试一次

`POST /api/gestures/test`。通用必填字段：
`deviceId`、`contextId`、`requestId`、`binding`、`frameSeq`、
`confirmed=true`（已人工确认）、`action`。
`operator` 为兼容可选字段；省略时后端记为“调试台”，页面不要求填写操作者。
这只是审计来源，不构成身份认证，也不改变动作安全门禁。

| 动作 | 额外字段 | 含义 |
|---|---|---|
| `double_tap` | `point`、`intervalMs` | 双击一个视觉点，抬起停顿 50~500ms 整数 |
| `swipe` | `start`、`end`、`durationMs` | 一次起点到终点滑动，时长 150~5000ms 整数 |

每个点仅含 `cameraX`、`cameraY`、`frameW`、`frameH`。
禁止提交机械坐标或覆盖 `tapDepth`、`tapFeed`、`hoverZ`、`feed`。
坐标必须落在当前已校准屏幕安全范围；相机、分辨率、校准和会话必须匹配，
帧必须新鲜。动作不排队、不抢租约、不重试，不写校准档案。

返回 `outcome`：`pending`（进行中/未确认）、`confirmed`（机械动作有证据）、
`executed`（控制器完整执行、实体效果由现场观察）、`failed`（未执行）、
`unknown`（执行状态未知）。`executed` 是本机调试接口的新枚举，消费者须识别；
不能转换成正式任务的 `confirmed`。
`evidence.motionConfirmed` 不等于手机已唤醒或滚到目标；
`effectStatus=manual_confirmation_required` 表示手机效果仍需人工确认。
相同编号相同请求仅返回原记录，同编号修改参数拒绝。旧资格永不重放。

人工观察模式不要求登记探头照片。照片缺失、亮度或外观变化仅产生
`evidence.observationNote`，不阻断本次已预校验的完整双击/滑动。
每段控制器回包仍须 Idle、位置与命令终点一致，显式失败、停止、断连、
急停、占用、校准失效、相机绑定/有效帧异常仍不能报完成。
`evidence.controllerCompleted` 与 `motionConfirmed` 分开：
前者只证明控制器完成回包，后者才是独立实体影像证据。
`executed` 不需要再人工释放，可另按按钮测试；不会自动重发原动作。
此模式由本机服务设置，不接受请求字段开关，不改变 WX 正式任务策略。
控制器完整完成后保留相同校准下的标记，方便继续调试。资格过期不使按钮
变灰：用户点击时先核对原计划，再读取新资格，同设备、上下文、绑定、
未阻断且没有未核对 unknown/pending 时，只替换请求编号。
任何刷新失败均不提交运动、不清除未知结果、不自动重试。

## 人工核对

`POST /api/gestures/review`。字段仅为：
`deviceId`、`contextId`、`requestId`、`daemonSession`、`operator`、`reason`、
`confirmed=true`。现场确认设备静止、探头和手机情况后才能提交。
`operator` 同样可省略，由后端记为“调试台”；核对原因和明确确认仍必填。

服务端要求当前会话一致、没有运动或租约、位置状态为 Idle，并再次检查。
只追加 `review` 记录、撤销旧资格并允许重新标记的新试验；
原 `unknown/pending` 保留，不改写成功、不重放、不重连、不复位急停，
不修改校准、不恢复业务任务。设备仍不可确认时保持阻断。

## 读取证据

`GET /api/gestures/evidence` 带 `deviceId`、`contextId`、`requestId` 和
`phase=before|after`。仅返回当前试验记录实际拥有的画面。
服务端保存单设备最近一次结果与有限前后帧，操作摘要进入原有审计记录。

外部应用仍使用 `/v1/tasks` 和插件能力 `arm.double_tap`、`arm.swipe`；
WX 使用 `screen-gesture.v1` 提交任意点手势，列表导航使用
`gesture-profile.v1` 登记的页面配置，不调用上述调试入口。

## 校准后自动计算

`GET /api/gestures/plan?deviceId=...&contextId=...` 是只读查询，不下发运动、
不创建测试记录或票据，不构造/恢复校准向导，不改写 H/XY/Z/SAFE PARK。

| 字段 | 含义 |
|---|---|
| `calibrationReady` | 当前设备、会话、相机上下文与 ready 档案匹配 |
| `binding`、`calibrationFingerprint` | `screen-gesture.v1` 完整绑定及校准摘要，包括 SAFE PARK |
| `frameSeq` | 计算时有效相机帧编号 |
| `profile` | 只读 `hoverZ`、`tapDepth`、`tapFeed`，来自已有校准 |
| `presets` | 中心双击、上下左右滑、底部上滑候选的独立计算结果 |
| `actions` | `double_tap`、`swipe` 各自的 `usable` 和 `blockingReasons` |
| `blockingReasons` | 未就绪、摄像头、占用、当前会话未知动作等阻断原因；不含探头照片匹配 |

每个预设包含 `id`、`label`、`action`、`calculated`、`blockingReasons`、
`effectStatus=unverified`；成功时还包含完整绑定、只读机械参数、
归一化 `point` 或 `start/end`、自动 `intervalMs` 或 `durationMs`，
以及 `points` 内对应的相机画面坐标。失败只影响该项，不制造默认坐标。

坐标以手机正向屏幕为准，上滑是 y 减小，与相机画面旋转无关。
双击抬起停顿默认 50ms，调试台可在 50~500ms 内以 10ms 步长调节，
接口接受此范围内的整数；这不是手机回报的两次触点间隔。
双击在同一校准触屏平面执行两次 3mm 往返（深度不足 3mm 时取实际深度），
先预下压、最后回到校准悬停高度。支持 `tap2.feed-3000.v1` 的 daemon
仅双击宏使用 F3000；旧服务仍使用校准速度，界面明确显示实际进给及未加载提示，
不会自动重启定位。滑动时长根据已校准行程、
协议最小/最大进给及 150~5000ms 范围计算，通常不短于 600ms，
短行程必要时自动缩短。距离、边界及非线性轨迹不可靠均拒绝。
底部候选从 y=0.99 逐步向内寻找已测物理边界内的路径，不移动任意用户点、
不改变校准边界，不能把候选解释为“返回桌面已通过”。

`POST /api/gestures/preview` 只读预览任意点，字段严格为：
`deviceId`、`contextId`、`action`，双击带 `point`，滑动带 `start/end`。
输入点仍为 `{cameraX,cameraY,frameW,frameH}`；返回与预设相同的完整计算项，
另带 `deviceId/contextId`。使用同一 H/XY/Z 及正式驱动边界规则反算，
尺寸不符不缩放，任意点越界不裁剪。运动特性或核验器缺失不阻止只读计算。

## 归一化测试

原 `/api/gestures/test` 兼容增加 `coordinateSpace="screen_normalized"`；
点仅为 `{x,y}`。滑动时长必须等于当前服务器重新计算的结果；
双击允许提交上述范围内的 `intervalMs`，触屏深度、悬停高度只读当前校准；
双击宏速度按上述服务特性选择，普通点击校准进给不变。
通用 `binding` 仍使用 `/api/gestures/state` 的票据绑定，新增
`screenCalibrationFingerprint` 与计划完整摘要一致。后端自行生成正式驱动完整绑定。

新模式只走正式任意点驱动，要求双击/连续滑动特性、
既有权限、现场门禁、当前帧、有效一次性票据。特性不满足时，
在写 pending 或释放租约前拒绝，不转用旧相机点击、连续单击或旧宏兜底。
ACK-only、停止、取消、双击次数不为 2、滑动执行未确认或帧证据不完整，
均不能报告 `confirmed`。完整且一致的控制器回包但只有影像证据不足时，
调试台报告 `executed`，并保留 `motionConfirmed=false`。
当前会话执行状态未知的动作仍阻断，不由重新计算清除。
2026-09-14 完成判据统一：通用调试与正式任务使用
`drivers.arm.gesture_motion_confirmed`；`aborted/unknown`、矛盾 outcome/status、
非法布尔值同样拒绝。这只核对机械完成，不替代手机效果或应用业务验收。

调试页监测 ready 与完整校准输入变化自动重新计算，但不会自动执行。
校准、上下文、请求票据或计算版本变化立即使旧点/预览/确认失效，迟到响应丢弃。
双击按钮触发单次双击，不再要求填写操作者；滑动预设须先选择方向，
再点击执行，底部上滑模式本身已明确方向。执行状态未知仍须单独人工核对；
仅影像证据不足不要求人工解锁。
执行前刷新只读计划，绑定和点位不变才用新鲜帧提交一次；
不重新解释或移动原标记，不更换校准绑定。404 显示接口未加载，不回退旧执行链。

## 划线与命名保存

画面划线直接产生滑动起终点，双击模式下点画面直接登记双击位置；
标记和预览不执行机械动作。名字只表示操作者计划的用途，
“返回桌面”“返回上一级”等名称不会自动变成效果确认或业务授权。

`GET /api/gestures/saved?deviceId=...&contextId=...` 返回
`{deviceId,contextId,gestures:[...]}`。每项沿用
`saved-screen-gesture.v1` 公共元数据，附加只读 `plan` 供画面显示；
失效项 `plan=null`，保留原版本和绑定，不自动迁移到新校准。

`POST /api/gestures/saved` 字段为 `deviceId`、`contextId`、
`binding`（计划的完整屏幕绑定）、`label`、`action`，
滑动带归一化 `start/end`，双击带归一化 `point` 和 `interval_ms`。
更新已有项还须提供 `gesture_id`、`expected_version`；
`operator` 可省略，默认“调试台”。不接受机械参数或手填滑动时长。

保存不占运动租约、不创建或消耗测试票据、不解除未知结果，
只写独立手势档案。选中后仍经原 `/test` 执行；
运动禁用不妨碍同一有效校准下的只读绘制与保存。
WX 从公开的 `/v1/devices/{device_id}/saved-screen-gestures` 读取
版本化动作快照，经原 `/v1/tasks` 提交，绝不调用调试执行接口。
