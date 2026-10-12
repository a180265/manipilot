# 单步滑动机器合同与接线

版本：`calibrated-scroll-package.design.v1`。从属主文第 5/6 节及本目录 README。
设计对象已闭合；代码接线、离线软件验证、现场样本验证是实施/验收状态，不是产品设计缺口。
本次仅改本目录，不改变已发布 `screen-gesture.v1`、旧证书、主文或父相机附件。

## 1. 文件与入口

| 文件 | 职责 |
| --- | --- |
| `contract.schema.json` | task_request/payload、task_ack/task_view、capability、result、review_request/list/response、HTTP 错误、可信内部调用与 OCR |
| `records.schema.json` | 完整配置、计划、驱动回执、逐动作记录、核对幂等回执、两种表行、引用闭包、纯归并输入/输出 |
| `observation.schema.json` | binding/source_frame/frame/pair/chain；review_request 兼容原附件定义 |
| `routes.json` / `routes.schema.json` | HTTP、可信调用、Store 与证据对象到 schema fragment 的机器映射 |
| `fixtures.json` / `package-fixtures.json` | 独立完整例及派生例、结构/语义反例、状态转换例 |
| `validate_fixtures.py` / `semantic_validator.py` | 无运行时依赖的本目录校验入口及纯跨字段检查 |

所有实例对象关闭额外字段；没有扩展字典、自由 `detail`、任意状态或客户端机械参数。
JSON 拒绝重复键、NaN/Infinity，整数拒绝布尔和浮点词法，时间必须带时区。
schema 的 `$id` 是注册标识，不是联网地址；发布时注册整个包，不现场拉取 schema。
只有 `task_request.policy_context` 和错误外壳 `error.detail` 可以省略；
其余对象按各自 required 传齐，允许 null 不等于允许缺字段。

| 调用入口 | 请求 -> 响应 | 行为 |
| --- | --- | --- |
| `GET /v1/devices/{id}/calibrated-scroll` | 无 body/query -> capability | 只读，不取新证明、不写校准/证书、不做动作 |
| `POST /v1/tasks`，task_type=calibrated_scroll | task_request -> task_ack，HTTP 200 | 沿用调度与插件，接纳不代表执行成功；重复同请求返回原任务 |
| `GET /v1/tasks/{task_id}`，新类型分支 | 无 body/query -> task_view | 只读终态结果；不重新取图/测量，不刷新到期时间 |
| `GET /v1/devices/{id}/calibrated-scroll/reviews` | 无 body/query -> review_list | 按 issue_id 排序返回本设备所有 review_required/reviewed 历史，不清问题 |
| `POST /v1/devices/{id}/calibrated-scroll/reviews/{issue_id}/review` | review_request -> review_response | 人工核对、CAS、幂等；绝不执行或恢复 |
| `execute_calibrated_scroll(ctx=可信上下文, **internal_call)` | internal_call -> result | 外部任务和聊天内部一步共用，内部不新建/释放外层租约 |
| `ctx.call_capability("camera.measure_scroll", measurement_request)` | measurement_request -> chain | 新模式只读取当前动作持久证据，不接受调用方阈值 |
| `aggregate_scroll_chain(**aggregate_request)` | aggregate_request -> aggregate_result | 纯函数，不生成 task/lease/frame 身份，不再次取图 |

父附件拥有 `GET /v1/devices/{id}/camera-source-frame` 的完整来源响应契约。
父 schema 的 source_frame_view 提供原图 metadata 与固定 evidence_id，
`GET /v1/devices/{id}/camera-source-frame/{evidence_id}/image` 提供该证据原JPEG。
WX 通过指定 source_evidence_id 派生 screen-source-observation.v2；G03直接消费同一原子
source snapshot，不需要 HTTP 自调用。两者均不得另查旧观察v1再拼来源。
它是新来源读取入口，不把字段强塞到旧 `screen-observation` 或 `camera-view.v1`。
运行中的滑动不是先请求图片、再调用这个接口补 metadata：broker 必须返回同一原子帧包，
由本动作直接归档。source_frame、JPEG 字节、桥序号、源序号/PTS、绑定和几何原子一致；
归档后的 sha256 对应实际保存的原字节。若需要重新编码，另建派生证据并保留原引用，
不可把旧 sha256 贴给新图。父接口只读证据不是任意客户端能上传给新任务的许可。

`routes.json` 只注册本包拥有的入口；父来源接口的 schema 注册及原子性由 camera-binding
附件负责。本包不另造竞争的来源外壳。路径 id 先做非空解析，再按权限和持久归属核对。

## 2. 接纳、权限与幂等

外部请求公共字段沿用 task_id/task_type/contract_version/device_id/idempotency_key/
correlation_id/expires_at，payload 仅 action 与 binding。expires_at=null 表示无客户端显式
到期时间，仍受平台任务时限、取消和观察时限约束；查询、恢复、核对不得延长它。
有明确 expires_at 时，入队及最后认领都检查。执行前失败三态结果为 failed/not_started；
排队取消/过期/接管且尚未进入动作准备时，task_view 可为 cancelled/expired/takeover、
result=null，不伪造一次滑动结果。认领后不能以取消/过期终态盖掉 unknown。

policy_context 只允许 reason/client_request_id，提供时两者必填。省略时由服务端使用
固定“校准直驱单步滑动”说明及 idempotency_key 构造审计上下文；不接受 role/operator_id。
调用者身份、来源、插件能力及设备权限来自受信认证/本机管理通道，不从 source、
submitted_by、operator_id 等请求字段取值。API 要先严格校验原 body，不能先 pop
非法字段后让请求通过。业务滑动仍由 WX 核对允许的列表页面；输入开关/IME键盘
不是底层滑动资格，但模型锁、独立机械验证、姿态、急停、原租约和所有暂停继续检查。

规范 JSON 为 `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
allow_nan=False)` 的 UTF-8；本包摘要使用该确定编码。无 Unicode 归一化，无显示四舍五入。
数值使用随版本冻结的表示，JSON number 不自动从整数重写为浮点。只有 OCR 文本匹配另做 NFC。
request_sha256 覆盖**完整原请求对象**，含 policy_context 是否存在、expires_at、诊断 revision；
重试不换内容、不新建动作编号。相同 task_id/idempotency_key 不同内容 409，
同内容返回原 task_ack/task_view，不再次认领。旧 screen-gesture 幂等规则不改变。

task 接纳事务预分配 action_instance_id，并把固定 step_key 保存到原任务。
外部 step_key=`single-scroll`；内部一步使用父任务已有的持久步骤键，新合法步骤才分配新编号。
不能恢复时生成新 action_instance_id。内部请求摘要为父 task_id、step_key 与完整 internal_call
组成的规范对象；外部摘要直接对应完整 task_request。

## 3. 能力、结果与空值

binding 十个字段全部必填。唯一语义比较集合为：
`device_id,daemon_session,camera_id,camera_device_unique_id,camera_binding_id,
source_geometry_id,calibration_fingerprint,frame_width,frame_height`。
binding_revision 是第十个诊断/旧兼容字段，不单独决定新路径失效。不得拿“忽略 revision”
同时忽略 daemon、物理相机或校准摘要。实例 UUID 为小写；几何摘要为小写64位SHA-256。

新任务只接受经有效机械验证的 schemaVersion=3 bundle；其 cameraBindingId/sourceGeometryId
投影到两蛇形键。v3 motion fingerprint 排除全局 bindingRevision 并纳入两新字段，
完整输入及 bindingFingerprint 定义引用 camera-binding 第7节；v2 保持原算法。
v2 只供旧任务/迁移，新路径不能填字段即 ready，不改变机械模型数学和既有数值上限。
不可直接复用旧 require_binding 的整对象相等；新旧分支明确按服务端任务契约选择，
不能由客户端在 binding 内加 binding_contract 切换。

capability.usable 表示至少一个方向可用；调用者必须看所选方向的 usable。
顶层 blocking_reasons 为两个方向原因按 code 去重排序的并集，即使另一个方向可用也保留。
同 code 的 message 是中文概括，可与方向明细不同。binding=null 表示没有可提交的新路径绑定；
v2/无校准分别返回 calibration_schema_version=2/null，不补造 v3 binding。
只读可用性是当时快照，不预留租约、不跳过动作前复核。

motion_budget.status 为 within_window/exceeds_window/unproven，后者 estimated_total_ms=null，
其余为整数。不可计算或 >4400ms 使用 scroll_observation_window_insufficient；
UI 区分“完整时长待验证”“本版观察窗口不足”。不得用改速度或改路径让慢机构过关。
sampling_support.source_timing_proven 只能来自父附件本机注册证明；true 时
source_clock_id/clock_registration_id 必有值。未知或无证明时 false，不能填占位 UUID。
这两个 ID 不是认证凭据；真实 registry、helper 摘要/启动代际与 PTS 误差证据仍要核验。

| 对象/字段 | null 的唯一含义或约束 |
| --- | --- |
| result.config_sha256 | 动作前还未冻结配置；任何已认领结果必须有摘要 |
| result.binding | 不可空，保留原请求/本步骤绑定，不偷偷替换当前绑定执行 |
| motion_started | null=不能确定是否开始；false=确定未动作；true=确认开始 |
| safe_cleanup_confirmed | 不可空；false=未获安全收尾证明，不等于已证明不安全 |
| dx/dy/valid_points/inlier_ratio、chain 的路径/MAD | 缺对或坏对时 null，禁止0填缺测；可靠实测保留原精度 |
| direction_consistent | 未得到可检查的位移候选或静止为 null；正向 true，反向 false |
| before_evidence/after_evidence | 本动作没有对应合格帧为 null；不可用最后 during 冒充 after |
| observation/observation_roi | 尚未测量或没有合法ROI为 null；已测链必须有最终ROI；缺前帧可无 chain |
| ocr_summary/ocr_evidence | 未启用 OCR 为 null；启用而失败不能假装没提供，必须 unknown |
| driver_error_code | 无驱动错误为 null；否则保留已登记驱动类别作诊断，不由它推断成功 |
| pause_issue_id | 没有本次 unknown 问题为 null；unknown 必填，核对后历史结果仍保留 |
| claim/timeline/回执引用 | 尚未发生或不能证明为 null，不拿查询时间补齐 |

结果为完整 `result`，同时用于内部一步返回及 task_view.result，不套用聊天专属
task-result.v1，不伪造 provider_message_id/sent_at。旧 task_view 不改，新类型通过
专用投影返回，不能把整段内部 task、自由 step_log/detail/extras 直接暴露出去。
现有任务列表若包含此类型，其每项也用相同 task_view 投影；外层通用列表契约不改。

| 优先级 | 事实 | status / reason | 暂停与租约 |
| --- | --- | --- | --- |
| 1 | 认领后中断/机械无法确认 | unknown / interrupted_after_claim 或 mechanical_result_unknown | 机械保护、视觉核对；不重放 |
| 2 | 机械确认但安全收尾未确认 | unknown / safe_cleanup_unconfirmed | 保留原机械阻断/租约 |
| 3 | 机械与安全确认，观察未知 | unknown / chain.reason_code | 原子保存视觉暂停后，独立任务可释放租约 |
| 4 | 机械与安全确认，可靠静止 | failed / scroll_no_displacement | 不建视觉未知问题；自动调用方终止本轮 |
| 5 | 机械与安全确认，正向位移 | confirmed / null | 只证明本次内容滚动，不证明顶部/底部 |
| 6 | 未认领的校验/取消/到期失败 | failed / 明确原因 | 零滑动，aborted_before_motion |

顶层 reason_code 优先安全事实，chain.reason_code 保留首个观察失败，
其余稳定原因进入去重 diagnostic_codes；不覆盖驱动原错误。
每个 result.retryable 恒 false。核对不会修改历史 manual_review_required=true；
当前是否还需恢复由动作记录 manual_resume_required 和当前问题列表表示。
flow_status 记录光流候选，effect_status 还受 OCR、方向和完整采样约束。
机械 unknown 且无法建立完成栅栏时可保留 flow_status=displaced，但 effect_status=unknown。
收尾在后续执行器阶段失去确认时，可保留已证明的视觉效果，总结果仍 unknown。

## 4. 持久对象与事务

`scroll_action_records` 列沿用 README SQL，json TEXT 解码后按 action_record 校验；
action_row 是列与已解码 JSON 的导出格式，不把 SQL TEXT 当二次转义的 API 字符串。
额外建以下表/索引，只存已成功核对的回执，与原动作同事务提交：

```sql
CREATE UNIQUE INDEX IF NOT EXISTS scroll_action_step
  ON scroll_action_records(task_id, json_extract(json, '$.step_key'));
CREATE UNIQUE INDEX IF NOT EXISTS scroll_action_claim
  ON scroll_action_records(json_extract(json, '$.claim_id'))
  WHERE json_extract(json, '$.claim_id') IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS scroll_action_issue
  ON scroll_action_records(json_extract(json, '$.pause_issue_id'))
  WHERE json_extract(json, '$.pause_issue_id') IS NOT NULL;
CREATE TABLE IF NOT EXISTS scroll_review_receipts (
  device_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  issue_id TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  json TEXT NOT NULL,
  PRIMARY KEY (device_id, request_id)
);
```

review_row 对应该表；json 是 review_receipt，保存原请求、路径归属、旧/新 revision、
完整原响应。动作 review 保存同一不可变回执，二者必须一致，不是独立可编辑事实源。
SQLite 连接须支持现有 JSON1，缺支持在迁移时拒绝启动新任务，不悄悄跳过唯一索引。
事务用 BEGIN IMMEDIATE、校验行/JSON一致、state/revision CAS、原任务/插件/租约/取消/
当前所有阻断，提交且确保持久后才调用驱动。调用者不能指定任意表名或任意补丁。
records schema 是 Store 组装后的输入/输出数据合同，不意味着允许 HTTP 直接写整行。

| state | result / outcome | claim | 后继 |
| --- | --- | --- | --- |
| prepared | 都 null | null；motion=false/not_started | claimed 或 aborted_before_motion |
| claimed | 都 null；可追加本动作进度 | 必填，永不更换 | 自身追加进度、completed、review_required |
| completed | confirmed 或可靠静止 failed | 保留 | 无，历史只读 |
| review_required | 原 unknown | 保留 | reviewed |
| reviewed | 原 unknown 不变 | 保留 | 自身，仅显式独立 resume 可清恢复标记 |
| aborted_before_motion | failed/not_started | null | 无，历史只读 |

每次实际写 revision 精确+1；初次1。created_at/原请求摘要/方向/绑定/配置/计划/原租约/
步骤键不可变；lease_id 终态保留仅供审计，不意味着该租约仍被持有。
prepared 之前失败可直接建 aborted_before_motion 行，plan/config/clock 引用为 null；
prepared 必须三者已归档且有效。未生成计划不是伪造一份“空计划”。
认领后崩溃一律 review_required、motion_started=null，不因本地 swipe_call_count=0
或 driver_invoked_at 缺失推断没有调用。次数只作回执诊断，不授予补调权。
认领前的全部可拒绝检查必须结束；认领后拒绝/错误保守进入不可重放对账，不重进准备态。

prepare/claim/finalize/progress/review 写入职责与 CAS 规则由 README §7.2 固定。
独立 resume 使用 `resume_reviewed_scroll_actions(device_id, authenticated_context)`，
在同设备事务锁内确认所有视觉问题已 reviewed、无执行中动作、无输入/机械/急停等阻断，
仅清 reviewed.manual_resume_required，逐行 revision+1，不改原 result/review，不恢复旧任务。
重启扫描先于 recover_tasks。坏记录/未知版本/丢失证据/列冲突阻断，不当空表。

核对请求摘要覆盖 `{device_id,issue_id,request:完整review_request}`。
先校验身份/路径与严格结构，再在事务内查 `(device_id,request_id)` 原回执；
同摘要原样返回原 response，包括当时 remaining_visual_issue_ids，不能按新列表重算幂等响应。
客户端要当前列表须另 GET。异摘要409；无原回执才查 expected_revision 和本设备无在途动作。
新问题出现不妨碍仅核对指定旧问题，但新响应须列出其余未核对 issue，不批量清空。
操作者由认证上下文记录；机械问题核对、租约保留及 G01 输入核对彼此独立。

## 5. 参数、证据链与可信调用

config_snapshot.parameters 完整冻结旧 `scroll_observation` 和本包
`calibrated_scroll_observation`，常量均由 schema 锁定；不是客户端可调参数。
旧 stationary.frames/frame_interval_ms/min_unique_ocr_rows 仅保存原配置供审计，
新采样用200ms/完成后2对尾部；新 OCR 最少2唯一互证行，不使用旧顶部判定的3行。
config_sha256 只哈希 parameters；config_evidence.sha256 哈希完整 config_snapshot 原字节，
两者不能混用。plan_evidence 指向 plan_snapshot；不以 evidence_id 猜文件路径。

计划保存方向、默认归一化起终点、由当前校准得到的相机点、完整profile、自动时长、
机械行程、分阶段保守预算与安全检查事实。raw calibration 存为受控不可变 calibration
证据，按父附件的完整 v3 bundle 规则验证；不得以 plan 的安全布尔值替代真实机械证明。
其源格式由 calibration_bundle 所属模块定义，不在本包嵌套自由 JSON。
预算 basis_evidence 是绑定当前驱动/起始位置及保守时长证明的任务内归档引用；
无法取得可信证明就 capability.unproven，不能把虚构估值写为“已测”。

本包所有 evidence_ref 固定 `evidence_id,sha256,captured_at,task_id,
action_instance_id,device_id,kind`，拒绝 URL、绝对路径、客户端下载地址。
类型分别为 frame/plan/config/calibration/ocr/clock_registration/driver_receipt/budget。
plan/config/frame/ocr/driver_receipt 的内容格式均在本包；校准、时钟和预算原证明由
所属模块验证后生成任务内只读归档关联。clock_registration 的真实内容与登记号按父附件；
`clock_registration_id` 是该受控证明的 evidence_id，底层 registration_id 不改写。
frame_ref.captured_at 是 source_captured_at；其他 kind 是该证据内容固化时刻，
不能把固化时刻当作机械完成或源采集时间。
所有引用必须查受控 Store 的原字节并验证归属/类型/摘要；schema 不能证明文件真的存在。

执行服务端把 plan.start/end 增加 source=screen_normalized，并将自动计算时长映射为
durationMs，组成严格 swipe_call；profile 不从调用者传给驱动覆盖校准。
`arm.swipe` 仍只调用一次，真实驱动回包由运行时依据 gesture_motion_confirmed 归一化为
driver_receipt。routes 中 swipe 响应是此适配后的存档，不要求旧 arm.swipe 回包改格式。
新驱动绑定验证由可信任务上下文选择 v3 分支，不在嵌套 binding 放模式开关；
旧 v1/v2 驱动调用与旧机械结果语义原样保留。

observation_context 在准备时固定 screen_norm/list_roi/exclusion_regions/ocr_mode。
list_roi=null 采用默认ROI；否则仅取交集；排除区与采样过程中探头遮挡取并集，全链同 mask。
ocr_mode=none 不调用 OCR；trusted_endpoint_provider 只能由受信插件注册回调
`ctx.scroll_ocr_endpoints(measurement_request)`，采样结束后传本动作全部帧引用，
回调仅读取第0帧与最终after帧，返回严格 ocr_endpoint，不新取图、不执行动作。
最多等待 capture_timeout_ms=400，且受剩余任务截止限制；缺after、超时、异常、半端结果
均 ocr_endpoint_invalid，不降级到none。原文仅保存在受控OCR证据，公共结果只给计数摘要。

链保存有序 frame_evidence_ids、binding/planning/config/clock 身份；frame_count 与引用数相等。
1帧只允许未完整观察的 unknown；正常纯归并至少2帧；成功/静止至少4帧且完成后3帧构成2对尾部。
所有 pair 为相邻 i->i+1，不能删坏对；缺/坏对汇总为null，首个失败索引/原因保留。
纯函数返回 aggregate_result 的计算字段；运行时加归属、采样状态、帧引用、尾部，
只能把测量结论降为unknown，不能把unknown提升为成功。
`evidence_bundle.current_binding` 是**最后许可时记录的通道快照**，不是查询历史时的实时绑定；
历史复核不能因现在换机/时间经过而重写当时结果。

sampling_diagnostics.tick 从1到29，以 before.accepted_ns + tick*200ms 调度；
sample_index 是实际被接纳帧的连续序号，二者不能混用。scheduled/requested/finished
都是同源注册时钟纳秒。accepted 必须有帧引用与null原因；duplicate/timeout/cancelled/
skipped/rejected 不进入链，引用null并记录对应原因。skipped表示之前读取未排空或错过tick，
不发追赶读；超时400ms仍须确保底层退出后才能放行下一设备操作。prepared阶段重取前帧
只保留最终被接纳的 before，淘汰尝试走现有任务审计，不算有效链。

## 6. 错误

保持 Fleet 外壳 `{"error":{"key","title","suggestion","trace_id","detail"?}}`，
detail 仅可选文本，不是任意对象；HTTP 错误绝不带成功/重试动作指令。
错误出现于接纳前时零动作、零清除；已接纳执行失败走持久 result，不把未知仅变成HTTP错误。

| HTTP | key | 范围 |
| --- | --- | --- |
| 400 | invalid_request | 原 `/v1/tasks` 严格格式失败 |
| 422 | invalid_request | 新 review 严格格式失败，含未知字段 |
| 403 | permission_denied/capability_not_allowed | 无设备访问、提交或人工核对权限 |
| 404 | device_not_found/task_not_found/scroll_review_not_found | 不存在或路径设备与问题不匹配 |
| 409 | task_id_conflict/idempotency_conflict | 原任务编号或幂等内容冲突 |
| 409 | scroll_review_revision_conflict/scroll_review_request_conflict/device_busy | CAS/核对幂等冲突/设备有在途动作 |
| 409 | device_not_eligible | 接纳阶段通道/校准/暂停等门禁，细节用中文文本 |
| 410 | task_expired | 新请求已经过期；同编号原回执查询不续期 |
| 503 | storage_unavailable/scroll_record_invalid/estop_active | 存储不可用、损坏记录或急停，保护阻断 |

观察原因完整枚举及中文意义在 README §8；本包增加的稳定原因：
calibration_v3_required=需要有效第三版校准；binding_changed=当前身份不匹配；
capture_not_bounded=取帧不能有界退出；plan_invalid=当前默认路径不可规划；
model_lock_required/independent_validation_required=机械证明未满足；
mechanical_result_unknown/safe_cleanup_unconfirmed=机械/收尾未知；
manual_resume_required=核对后仍待单独恢复；storage_unavailable/record_invalid=持久化故障；
task_cancelled/task_expired=动作前取消/过期；driver_rejected=动作前驱动资格拒绝。
其余 reason_code 使用 schema 中明确列举的设备/租约/相机/权限/能力原因，中文含义沿用
`runtime/errors.py` 同名项；input_review_required/mechanical_review_required/
camera_session_error 分别表示并存输入核对/机械核对/相机会话阻断，internal_error为内部异常。
新增 code 必须版本化登记并给中文显示，不能随手增加自由字符串或吞成可重试失败。

## 7. 验证边界

验证器读取两份 fixture，展开 `$fixture` 引用和 `$sha256` 纯数据配方后再逐份严格校验；
这些配方不是合法 HTTP/持久记录字段。派生正例也可独立得到完整对象，没有 schema 外默认值。
每个本包入口响应及有body请求必须有正例；每个正例的每层对象注入未知字段必须拒绝。
结构反例与语义反例分别断言拒绝层，状态转换另外核对不可变历史/CAS/禁止重放。
典型例：task_request、capability_v2_readonly、result_stationary、result_visual_unknown、
recovered_claim、reviewed_record、bundle_global_revision_changes_only。
真实机械证明、图像字节存在性、时钟注册可信性、权限、并发与异步排空仍需实施隔离测试，
不能由合成 JSON 自证。

本轮获准运行纯文档离线校验器，实际结果见 README §10；未执行生产功能、服务、
硬件或 pytest 功能回归。复跑用 README §10 命令，依赖现有环境的 jsonschema/referencing，
不自动安装依赖。
如果没有这些库，记录环境前置缺失，不访问生产目录替代验证，不把它算作设计缺口。

## 8. 正文同步清单

只由主线程修改正文，本包不越界编辑：

1. 第5.2节：链接本包 contract.schema、routes；固定 task_ack/task_view、policy_context
   封闭子集、任务查询投影、HTTP错误、逐方向预算与采样能力；不再写“外壳待补”。
2. 第5.3/5.4节：新滑动需有效v3校准；v2旧算法保持；新身份比较只排除全局诊断revision，
   不排除设备/physical/daemon/calibration/尺寸；新增实例与来源几何是必填，不允许补字段即ready。
3. 第6.2.1节：来源获取引用父 `GET /v1/devices/{id}/camera-source-frame`；
   图像和source_frame来自同一原子真实采样，不能图片与metadata分两次取后拼接；
   老screen-observation不强插字段；clock注册证明归父附件，保留本包20ms/代际/所有时限。
4. 第6.3/6.4节：链接完整参数、chain.frame_evidence_ids、result schema及空值/终态矩阵，
   准备/动作/效果/安全收尾独立；配置摘要与证据原字节摘要不同；Unknown不重放。
5. 第6.5节：动作表完整JSON、单次claim、核对幂等回执表、列/JSON一致、核对/单独resume
   的机器格式齐全；查询和核对不改原unknown，先持久暂停再释放独立租约。
6. 第8/13节：C01改为“完整机器合同按包注册与接线”，新旧schema/驱动分支分别发布；
   文档离线自检结果与功能/现场验收分开；功能未实施/现场样本未测列实施验收项，
   不再算机器合同设计缺口。

## 9. 静态源码锚点

以下仅指实施位置，不表示这些代码已经支持新契约：

| 源文件/符号 | 要接入的合同 |
| --- | --- |
| `api/app.py:214 task_view`、`:936 submit_task` | 新类型投影、原body先严格校验、认证来源，旧类型保持 |
| `runtime/runtime.py:346 submit_task`、`:937 arm.swipe`、`:1149 camera.measure_scroll` | schema注册、原任务租约、一次claim、新测量分支 |
| `runtime/store.py:134 create_task` | 任务幂等复用；新增动作/回执事务与启动扫描 |
| `drivers/screen_gestures.py:30 binding_snapshot`、`:63 require_binding`、`:167 plan_gesture`、`:206 default_plans` | v3显式身份分支，仅两默认方向，不改数学 |
| `drivers/calibration_bundle.py:292 motion_calibration_fingerprint` | 父附件v2/v3摘要分支，新字段不靠补值获资格 |
| `drivers/daemon_arm.py:1603 swipe` | 复用机械路径，新绑定校验按可信上下文，不读旧证书模板 |
| `drivers/arm.py:49 gesture_motion_confirmed` | 原机械确认标准；不以controller_completed或光流放宽 |
| `drivers/scroll_observation.py:51 _track`、`:215 measure_pair_quality` | 保留逐对指标、新共享纯归并 |
| `runtime/evidence.py:20 capture_screenshot`、`drivers/camera_broker.py` | 已取原子帧归档，来源/哈希/动作归属与有界排空 |
| `runtime/calibration_gate.py:74 _source_binding_checks` | 新来源身份/注册时钟/采集当时新鲜度，旧分支不暗改 |
| `runtime/executor.py` 的所有 hold_unknown 分支、`runtime/devices.py:1275 hold_unknown` | 纯视觉暂停不能伪造机械未知，结果先于释租约 |
| `examples/plugins/screen-gesture-adapter/plugin.py` | 同插件新增任务与完整request_schema，旧v1不改 |
