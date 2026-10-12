# 人工唤醒屏幕契约

版本：`screen-wake.v1`。这是通用人工任务，不是微信业务、解锁或自动导航。
独立插件：`screen-wake-adapter 1.0.0`；能力：`arm.wake_screen`。
安装插件并不授予设备能力，也不解除暂停、急停或其他门禁。

## 只读资格

`GET /v1/devices/{device_id}/screen-wake`：

```json
{
  "contract_version": "screen-wake.v1",
  "device_id": "arm-01",
  "binding": null,
  "usable": false,
  "blocking_reasons": [
    {"code": "calibration_unavailable", "message": "已验证唤醒点不可用"}
  ],
  "target": null,
  "effect_status": "unverified"
}
```

有效目标由 `drivers.wake_screen.resolve_wake_target(bundle)` 只读解析，返回
`validation_point_id`、归一化 `point:{x,y}` 和完整 `binding`。
`validation_point_id` 指向保存的实际锚点/验证点记录（规范 §9.1）：优先指向
本轮"确认点击参数/测试点击"锚点（已发布档案键 `wakeTapAnchor`，由校准向导
提升时写入），旧档没有完整点击锚点时按确定性规则（最接近屏幕中心、距离相同
按编号排序）指向同轮通过的独立验证点。两类来源都不满足时不执行唤醒，
不能创建中心默认点。不使用默认中心点，不修改校准、停车位、手势证书或页面
状态。响应中的目标仅供查看，调用方不得回传坐标或机械参数。

清晰画面返回 `usable=false`、`screen_not_waiting / 当前画面无需唤醒`。
只有新鲜黑屏等待允许人工请求；冻结、无帧、过曝、断线、当前运动或停止结果不明、会话或绑定
变化、占用、暂停、隔离和急停均不豁免。查询不领取租约、不执行动作，
也不保证请求真正执行时仍然具备资格。

已终态的历史任务（包括未人工核对的 `unknown`）保留原状态和审计记录，
不单独作为新唤醒请求的阻断条件，也不要求补签历史动作成功。唤醒仍需当前
有效校准及已通过验证点，并复核设备、控制器、租约和新鲜相机帧。
此规则不解除当前暂停/隔离、运动或停止未知状态，不重放历史请求，也不改变
WX 自身在途操作及原唤醒请求只能查询结果的约束。

这里的会话是机械控制器校准会话，不是空闲聊天页面的持久记录。已打开但无在途
操作的聊天可以保留原样人工唤醒，不退出、不清除聊天上下文；实际执行租约、
未知任务、暂停/隔离等仍阻断。WX 负责自身 pending 聊天/发送/导航操作的门禁。

## 提交

统一使用 `POST /v1/tasks`，全部必填：

| 字段 | 约束 |
| --- | --- |
| task_id / idempotency_key / correlation_id | 非空稳定编号；同一请求不得换编号重放 |
| task_type | `screen_wake` |
| capability | `arm.wake_screen` |
| contract_version | `screen-wake.v1` |
| device_id | 与绑定相同 |
| expires_at | 带时区时间戳；服务端截短至提交起最多 60 秒 |
| payload | 仅 `{"binding":完整绑定,"confirmed":true}` |

可选审计字段：`operator_id`、`reason`。HTTP 外壳可沿用通用接口的 `source`，
其不是唤醒契约内容。必须由操作者明确确认“不解锁”；不允许自动监听、定时
唤醒、失败重试、任意坐标、下压、速度、间隔或解锁参数。当前角色/确认字段
不等于独立身份认证，部署仍须有可信客户端与访问控制。

同请求重提返回原任务。原请求摘要与原编号保持绑定，截短有效期不破坏幂等。
过期不会补做；WX 应查询原 `GET /v1/tasks/{task_id}`，不得换编号再次双击。

## 执行与结果

运行时集中使用 `is_screen_wake_task` 检查完整身份、独立插件、写任务性质、
人工确认、闭合参数及有效期。仅该任务能豁免 `camera_waiting`，不伪装为只读任务。
领取、执行和真正动作前仍检查其他安全条件、原租约和完整绑定；不要求 IME 在线。
画面检查等异步等待结束后、每段指令进入传输前再次检查取消、有效期和原租约，
不得依赖进入驱动时的旧快照。权限撤销后不继续双击，不使用新租约完成旧请求。
动作开始后且仍持有原租约时允许新帧恢复清晰，以完成同一次双击的必要抬回；
不得因此再次双击、解锁、OCR、返回主页或归位。

`step_log` 中已结束步骤的 `detail` 符合 Schema 的 `resultDetail`：

- `binding`：原请求完整绑定。
- `controller_completed`：控制器是否完整确认；不是独立实体运动或手机效果证明。
- `tap_count`：控制器确认的下压次数，完整双击为 2。
- `motion_not_started`：明确证明本次动作尚未开始。
- `effect_status`：仅 `awake` 或 `unverified`，不新增机械状态枚举。
- `before_evidence` / `after_evidence`：真实平台截图记录映射出的
  `evidence_id / sha256 / captured_at`；不可用为 `null`，不补造证据。
- `frame_observations`：驱动原始观察映射，含
  `source_frame_seq / quality / sha256 / captured_at`。缺失或不合法为 `[]`。

未进入动作的取消/过期/前置检查使用 `wake_preflight`、`is_action=false`
记录真实拒绝结论，截图字段为 `null`；这不是伪造执行步骤或亮屏证据。

`awake` 只表示观察到新帧从 `waiting` 恢复为 `clear`，不表示已解锁、
已进入微信或可发送。插件同时要求不同的前后截图编号/摘要、合法帧观察、
递增来源帧序号及不同帧摘要；只有驱动确认亮屏且这些证据满足时才输出 `awake`。
同一秒的合法截图时间可能相同，不应伪造更精细时间；来源帧序号用于证明新帧。
WX 可进一步保守核验原任务关联和证据，但不能把 `confirmed + unverified`
误解为机械失败或据此重放。

明确动前拒绝为 `failed`；控制器双击完整确认为 `confirmed`，即使手机仍黑屏。
回包丢失、停止不明、部分动作或运行中断为 `unknown`，设备暂停并转人工。
执行已开始而收尾未落定的重启任务不重新进入执行器，保留原步骤证据并转未知核对。

收尾不调用插件恢复/清理或设备归位，不伪造 `home_verified`。只释放原任务租约；
被替换的租约及新任务状态不受影响。等待未亮屏不会撤销校准或已有安全证明，
也不会清除其他暂停/隔离/急停状态。当前运动或停止未知状态仍阻止下一动作，
仅保留在历史任务里的未知结论不替代当前物理安全状态。

## 集成与验证

驱动须实现 `async wake_screen(params, *, execution_guard)`，且只接受原
`{"binding":dict,"confirmed":True}`；动前拒绝异常须有
`motion_not_started=True`，发送指令后不确定不能携带该标记。
`execution_guard` 是平台传入的同步检查回调，不是 HTTP 参数，也不写入插件任务。
驱动在异步等待后及实际指令发送前调用它；仅动作事务能断言是否尚未运动。
新运行时、驱动、契约、设备能力声明和独立插件必须配套发布；本源码改动不操作
现场服务或硬件，不修改配置及已安装插件。

隔离测试：`tests/test_screen_wake_contract.py`、
`test_screen_wake_runtime.py`、`test_screen_wake_lifecycle.py`、
`test_screen_wake_integration.py`、`test_screen_wake_runtime_review.py`。
后两组使用真实目标解析器、真实 daemon
驱动和校准门禁，连接随机本机端口的 fake daemon，覆盖一次双击、未亮屏、
动前拒绝和回包丢失；所有校准档案在临时目录，不代表手机现场验收。
