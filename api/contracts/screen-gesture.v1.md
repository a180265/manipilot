# 屏幕单次手势契约 v1

版本：`screen-gesture.v1`。本契约是生产任务入口的源码约定，不是现场已就绪或真机验收证明。
两项目各自随包保存本文件及同名 Schema，副本逐字节相同；运行时不跨项目导入代码或读文件。
插件源码位于 `examples/plugins/screen-gesture-adapter/`，本次不安装、启用插件或重启服务。

## 坐标与绑定

`screen-gesture.v1` 明确冻结坐标空间：**正向物理屏幕归一化坐标**。
左上角 `(0, 0)`，右下角 `(1, 1)`；x 向右，y 向下。
坐标覆盖完整物理屏幕，不是原始相机图、裁剪图、键盘局部、机械坐标或调试台坐标。
WX 只提供 `{x, y}`；允许边界 0 和 1，不代表设备一定能安全触及边缘。
实际可达性、校准、Z、限速、下压、抬起及停车策略由 Fleet 驱动拒绝或执行。
禁止插件换算坐标、调用驱动 API、补猜参数、裁剪越界点或改写校准。
插件仅导入 `drivers.arm.DriverError` 这一接口异常类型以区分动作前拒绝，不取得驱动对象。

任务 payload 及可用元数据的 `binding` 必须恰好包含以下八个字段，不允许遗漏、
空值或附加字段；阻断元数据的唯一例外见下节：

| 字段 | 中文含义与约束 |
|---|---|
| device_id | 设备编号，非空文本，与请求路径及任务设备严格相同 |
| daemon_session | 当前控制器会话，非空文本，重连后旧绑定不得沿用 |
| camera_id | 摄像头通道编号，非空文本 |
| camera_device_unique_id | 物理摄像头唯一编号，非空文本 |
| binding_revision | 摄像头绑定版本，非负整数，不接受布尔值或浮点数 |
| calibration_fingerprint | 全部相关校准及 Z 参数的 SHA-256，小写 64 位十六进制 |
| frame_width / frame_height | 当前绑定原始帧尺寸，正整数，不接受布尔值或浮点数 |

摘要由 Fleet 生成并在执行前核对；不是 WX 计算，也不能用 `screen-observation.v1`
只覆盖相机变换的摘要替代。帧尺寸用于绑定验证，不改变归一化点的物理屏幕语义。
JSON 不允许 NaN 或无穷大；所有数值参数拒绝布尔值，拒绝字符串转数值。

## 元数据预检

`GET /v1/devices/{device_id}/screen-gestures` 为只读、新鲜查询。
成功响应 Schema 为同名文件的 `#/$defs/metadata`，包含：

| 字段 | 中文含义 |
|---|---|
| contract_version / device_id / binding | 契约版本、请求设备、上述完整绑定 |
| usable / blocking_reasons | 校准、绑定、设备及外部会话等基础资格与阻断原因 |
| features | daemon 原样返回的 `motionFeatures` 字符串数组 |
| actions.double_tap | 双击资格 `{usable, blocking_reasons}` |
| actions.swipe | 滑动资格 `{usable, blocking_reasons}` |

草稿或尚未就绪时，HTTP 200 可合法返回 `usable: false, binding: null`，
保留 `blocking_reasons` 供调用方展示。`binding` 字段仍不可省略；
仅整体 `usable` 严格为 false 时允许 null，非空对象仍须完整校验。
`usable: true` 必须返回完整有效绑定；此例外不放宽任务 payload，也不允许提交动作。

整体 `usable` 不因某一个动作缺特性而封锁另一个动作。调用前必须同时满足：
整体 `usable is True` 且无基础阻断原因、所选 `actions[action].usable is True`
且无该动作阻断原因，以及所选动作的必需特性存在。
双击要求 `tap2.v1`；滑动要求 `swipe.v1`。
归一化双击先在目标上方预下压，再执行两次完整 3mm 往返（校准深度小于 3mm
则使用校准深度），最后回到校准悬停高度。触屏平面仍为 hoverZ+tapDepth。
默认抬起停顿 50ms，已保存或显式提供的停顿保持原值。
`tap2.feed-3000.v1` 表示仅双击宏支持 F3000；有此特性时双击使用 F3000，
否则仍用校准进给，并通过 `double_tap_feed` 和 `speed_note` 明示。
普通 Z 走位、单击与滑动 Z 进给上限不变。插件不得拆成两次点击。
3mm 释放距离和两次宏循环均不证明手机真正识别了两个触点；
`effect_status` 仍为 `unverified`。`contact_interval_min_ms` 只是匀速模型的
理论最短触点间隔，不包含加减速，不是实测值。

HTTP 非 200、旧服务 404、响应无法解析、缺字段、错误设备/版本/绑定或资格不明，
均不提交动作。不缓存元数据、不使用历史 ready 状态兜底、不自动连接或重新校准。
元数据基础条件无效时服务可以返回现有 HTTP 错误外壳；客户端不会使用错误中的旧绑定。

元数据只是预检，不是运动授权或锁。**平台负责在排队执行前、持有本任务租约时
再次检查 WX 外部执行会话占用，禁止抢占人工或自动 held session。**
插件清单声明可选的通用占用保护：

```json
{
  "screen_context_guards": [
    {"plugin_id": "wx-wechat-adapter", "state_key": "manual_session"}
  ]
}
```

平台只按清单声明，在任务提交及实际动作前拒绝本设备的非空占用；
不硬编码 WX 业务，不向插件或元数据响应暴露被保护的状态内容。
元数据基础资格也呈现该阻断。WX 必须先退出或明确释放已保留会话，再使用任意点手势。
该声明只约束本手势插件，不影响微信插件内部既有的 `arm.swipe`。
动作能力还须原子核对当前完整绑定及动作特性，避免预检后换绑或校准变更。
插件不读取其他插件私有状态，不持有 WX 执行会话，也不自动退出应用会话。
缺少这些驱动及运行时门禁时，不能单独安装此插件宣称生产已就绪。

## 任务提交

使用现有 `POST /v1/tasks` 通用外壳，不新增动作 HTTP 写接口。
根 Schema 校验请求外壳，插件清单内嵌同一 payload 规则，便于独立安装。

| 外壳字段 | 约束 |
|---|---|
| task_type / contract_version | 固定 `screen_gesture` / `screen-gesture.v1` |
| task_id / idempotency_key | 调用方事先持久化的稳定、非空编号；不自动生成 |
| device_id | 指定设备，与绑定相同 |
| payload | 下述双击或滑动对象 |
| expires_at | 带时区到期时间或 `null`；生产调用方应提供业务有效期限 |
| correlation_id | 稳定关联编号；WX helper 缺省使用 task_id |
| policy_context | 可选通用策略对象；不授予绕过物理门禁的权限 |

双击 payload 恰好包含 `action: "double_tap"`、`binding`、`point: {x, y}`，
可选 `interval_ms`，有限数值 50 到 500。
滑动 payload 恰好包含 `action: "swipe"`、`binding`、
`start: {x, y}`、`end: {x, y}`，可选 `duration_ms`，有限数值 150 到 5000。
滑动起终点不能相同。两个动作的点字段及时间参数不能混用；
任何 `source`、`target_id`、机械速度/深度/Z 覆盖等附加字段均拒绝。
省略时间参数时不写入默认值；滑动由驱动按行程和限速自动计算时长，
双击使用驱动的受控默认节奏。显式时长仍可能因所选行程超出速度限值而被拒绝。

插件只调用一次对应能力：

```text
arm.double_tap({
  point: {source: "screen_normalized", x, y},
  binding, intervalMs?
})
arm.swipe({
  start: {source: "screen_normalized", x, y},
  end: {source: "screen_normalized", x, y},
  binding, durationMs?
})
```

只有一条动作计划，`max_actions_per_task=1`。前后各一次受控截图；
不增加手动回零、删除键点击、应用退出或另一个停车动作。
清理无屏幕动作；失败恢复及取消仅请求停止，停止未确认不得谎报成功。
全部既有租约、安全收尾、安全停车位及校准规则保持平台所有。

## 结果与证据

`GET /v1/tasks/{task_id}` 是完整任务与步骤详情的查询入口。
任务状态沿用通用 `confirmed / failed / unknown` 等现有值；
此任务的 `confirmed` 中文为“机械动作已完成，手机效果未核验”，不是消息已发送。
插件不生成 `provider_message_id`、`sent_at`、客户/会话字段或业务效果证明。
现有 `task-result.v1` 回调不携带全部插件详情，不能以回调代替步骤详情查询，
也不能把本任务送入聊天 Outbox 的“已发送”业务对账。

已结束的手势步骤在 `step_log[]` 的 `phase: "done"` 项下提供 `detail`，
格式见 Schema 的 `#/$defs/resultDetail`：

| 字段 | 中文语义 |
|---|---|
| contract_version / action / action_display | 结果详情版本、动作及中文名称 |
| effect_status / effect_status_display | 恒为 `unverified` / “手机效果未核验” |
| mechanical_status / mechanical_status_display | `not_started` 未开始、`confirmed` 机械已完成、`unknown` 机械结果未知 |
| motion_verified | 驱动结构化机械核验布尔值；未获得有效回报时为 `null` |
| tap_count | 已完成双击宏的下压次数，严格整数；不是手机回报的真实触点数，缺失或非法为 `null` |
| swipe_executed | 滑动实际完成标记，严格布尔值；缺失或非法为 `null` |
| before_evidence / after_evidence | `{evidence_id, sha256, captured_at}` 受控引用；无法取得时为 `null` |
| manual_review_required | 机械结果未知或动作后证据不完整时为 `true` |

成功必须 `ok is True`、`motion_verified is True`，并分别满足严格整数
`tap_count == 2` 或 `swipe_executed is True`；unknown/stopped/cancelled 等矛盾回报拒绝。
`ok` 单独为真不够。截图必须属于本任务设备，前后不可复用同一证据编号。
截图不解释手机是否唤醒、是否回桌面、滑动方向是否被系统识别或是否误触应用。

动作前参数或取证失败：`failed`，未调用手势。
能力边界拒绝 `PluginViolationError`，以及 `DriverError` 中明确的动作前策略、
未校准、参数、限位或急停锁定拒绝，返回 `failed`、`mechanical_status: "not_started"`。
驱动保证运动下发后的结构化错误转为 `driver_disconnected`，不得伪装成动作前拒绝。
其余异常、停止、部分执行或机械完成标记不足：`unknown`，禁止重试。
机械已完成但后证据缺失：任务仍 `unknown`，保留 `mechanical_status: "confirmed"`
这一局部事实，人工核对，不重做动作。
平台取消、超时或更早的校验失败可能尚无完整 detail；详情缺失不能推定成功。
安全收尾状态另读 `cleanup_result`、`home_check`、租约字段，不把机械完成等同安全收尾完成。

## WX 调用

`ArmFleetClient.get_screen_gestures(device_id)` 返回已校验结构的当前元数据；
资格为 false 时可供展示，不能用于放行动作。
`submit_double_tap` / `submit_swipe` 每次重新读取元数据、检查所选动作并复制完整绑定。
调用方不能传缓存绑定、机械参数或绕过预检；本契约不改变既有聊天派发逻辑。

以下仅为调用方式，编号、设备、点和到期时间须由业务方明确授权并保存；不执行现场动作：

```python
result = client.submit_double_tap(
    device_id, {"x": 0.5, "y": 0.5},
    task_id=saved_task_id, idempotency_key=saved_idempotency_key,
    interval_ms=120, expires_at=saved_expires_at,
)
# 另一项独立授权任务使用另一组事先保存的编号：
result = client.submit_swipe(
    device_id, {"x": 0.5, "y": 0.98}, {"x": 0.5, "y": 0.35},
    task_id=saved_swipe_task_id, idempotency_key=saved_swipe_key,
    expires_at=saved_swipe_expires_at,
)
view = client.get_task(result["task_id"])
```

helper 返回沿用 `accepted / rejected_4xx / server_error / network_error / unparseable`，
并始终携带调用方的 `task_id`、`idempotency_key` 和 `effect_status: "unverified"`。
`accepted` 只表示任务被接受，仍要查询原任务。任何提交响应丢失、5xx、格式错误或超时，
最多一次 POST，不自动重发、不换编号、不刷新绑定后重放原手势。
调用方按已保存编号查询和人工核对；同编号/幂等键不能绑定不同请求。
这些 helpers 不提供持久化队列，也不自动接入客户、发送或主页巡检调度。

## 仿真验证与现场限制

专项测试为 `arm_fleet/tests/test_screen_gesture_plugin.py` 和
`sales_bot/tests/test_screen_gesture_calling.py`，只使用假能力、假传输及临时目录。
两项目可独立测试，联合测试比对契约副本。Schema 之外额外校验有限数值、
严格整数、设备相等、起终点不同和实际完成证据。
完整校准与 Z 摘要、normalized 驱动路径、特性支持及会话占用门禁必须同时满足；
现有全幅画面差核验器无法区分手机动画与机械运动，新增任意点路径和元数据
明确阻断此核验条件；须补可靠实体运动核验，不能以画面变化放行下压。
现场仍须确认新 daemon 特性及真实双击/滑动效果。本交付不改 live data、
已安装插件、现场校准或停车位，不连接硬件，不安装插件、不重启服务。
