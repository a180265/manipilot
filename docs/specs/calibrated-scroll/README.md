# G03 校准直驱单步滑动实施包

附件版本：`calibrated-scroll-package.design.v1`；观察协议仍为 `calibrated-scroll-observation.v1`。
依附主规格：`docs/2026-10-03-输入开关与校准直驱上下滑动修改规格.md`
第 5、6、7、13 节；已在主文 v1.8 同步引用。本文不是第二份主规格。

**状态：待实施的技术合同，不是已有接口、发布记录或真机通过报告。**
本附件不设计微信导航、未读恢复、模板或手机粗框。主规格 S01-S26 保持有效。
本附件参数是保守初始配置，尚未现场验证；不更改机械速度、深度、校准和安全上限。

**机器格式入口：** [CONTRACT.md](CONTRACT.md) 给出公共外壳、可空/状态/错误、
内部表、引用闭包、发布接线及正文同步清单；[routes.json](routes.json) 是入口到 schema
的机器映射。`contract.schema.json`、`records.schema.json` 与原 `observation.schema.json`
共同构成完整严格合同，不再把任务/结果外壳留给实施者补设计。
共同来源身份、真实时钟注册与机械校准 v3 以 [camera-binding 附件](../camera-binding/README.md)
为唯一语义来源；本包不复制其实现，也不向滑动嵌套 binding 增加 `binding_contract`。

## 1. 实施结果与现状

实施完成后，一个滑动动作只执行一次，使用本次动作的完整连续帧链证明内容位移。
机械完成、视觉位移、机械安全收尾分别记录。看不清即未知并持久暂停，不能再滑一次试试。
静止只能证明观察区没有显著移动，永远不证明到顶或到底。

已逐项核对的现状：

| 当前位置 | 可以复用的部分 | 必须补的部分 |
| --- | --- | --- |
| `drivers/scroll_observation.py::_track` | LK 正反追踪、有效点、比例、dx/dy、纵向 MAD | 补原始点数、正反误差统计；异常统一返回未知 |
| `measure_pair_quality` | 相邻帧质量检查 | 目前不返回点数/MAD；失败时还会丢掉部分可诊断的位移 |
| `runtime/runtime.py::camera.measure_scroll` 分支 | 读取证据，手机姿态对齐，再用单应矩阵摆正 | 当前链只累加 dx/dy，未限制累计横移，未做首尾 OCR 互证 |
| `drivers/camera_broker.py::CameraFrameBroker` | 同一相机物理读锁、订阅、最新帧 | 取消 `to_thread` 不等于物理读退出；新观察必须有有界等待和读线程排空 |
| `drivers/real_camera.py::capture_sync` | 推流身份、帧序号、时间透传；哈希相同不直接当冻结 | 补真实采集时钟及代际信息，归档不得只保留本地编码时间 |
| `runtime/evidence.py::capture_screenshot` | 图片持久化、哈希校验 | 补源信息和动作上下文；当前 `camera.device_id` 不能当 broker 的 `camera_id` |
| `runtime/calibration_gate.py` | `_fresh_source_seq`、`_source_binding_checks` | 新路径更严格的取样时间与顺序核验，不能以事后当前时间重判历史结果 |
| `tools/camera_helper/Sources/CameraHelper/main.swift` | `captureOutput`、源帧序号 | `upload` 当前用上传时 `Date()` 填 `X-Captured-At`，不是采样 PTS |
| `runtime/executor.py::_finalize`、`devices.py::hold_unknown` | 现有机械未知保护 | 现有未知会同时使 `home_ok=false`；视觉未知不能错误升级为机械未知 |

不复用 `gesture_validation.py` 的试滑、反向复位、发证、参数搜索。
不以 `scripts/motor_debug_server.py::_measure_swipe_effect_vision` 作为新任务判定器。
`RealCameraDriver` 的通用 `captured_at` 目前是编码时刻，不能替代 `source_frame`。

## 2. 单一实现边界

待新增 `runtime/calibrated_scroll.py`，提供：

```python
async def execute_calibrated_scroll(
    *, ctx, action: str, binding: dict, action_instance_id: str,
    observation_context: dict | None = None,
) -> dict:
    ...
```

`ctx` 是已由正式运行时核验的任务、插件、设备、原租约上下文，不能来自 HTTP 请求体。
外部任务 `calibrated_scroll` 和外层聊天任务内部一步使用同一函数。
内部调用不得创建子任务或释放外层租约。
`observation_context` 只能由可信服务端插件生成，包含收紧 ROI、排除区、可选 OCR 证据；
不接受客户端坐标、证书、模板、机械参数或“跳过检查”。
其严格输入及 OCR 的后置取证方式见 `contract.schema.json#/definitions/internal_call`
和 `CONTRACT.md` 第 5 节；不能在动作前伪造尚不存在的 after OCR。

待新增纯归并函数放在 `drivers/scroll_observation.py`：

```python
def aggregate_scroll_chain(
    pairs: list[dict], *, frame_count: int, roi_px: dict,
    expected_direction: str, params: dict,
    ocr_endpoint: dict | None = None,
) -> dict:
    ...
```

它不取帧、不读库、不执行硬件、不判断微信页面，不再次对首尾图片做 LK。
返回值只包含 schema `chain` 中的位移/质量/方向/OCR/原因等计算字段；
task、device、帧引用、采样完成状态与尾部栅栏由运行时核验后组装完整 `chain`，
纯函数不能凭空生成这些归属字段。运行时采样失败可把最终 effect 降为 unknown，
不能反过来把纯函数的 unknown 提升为成功。
所有相邻帧都走 `measure_pair_quality`。**新路径即使只有两帧也走同一归并函数**，
不再分成两个结论标准。旧 `measure_displacement`/旧协议调用保留兼容，
不得悄悄改变旧 `screen-gesture.v1` 的语义。

正式 `camera.measure_scroll` 的新模式为服务端能力参数
`measurement_contract="calibrated-scroll-observation.v1"`，要求
`task_id/action_instance_id/evidence_ids` 与当前持久动作完全匹配。
老模式仍可只读诊断，但不得产出可供新滑动消费的合格结果。

## 3. 配置与采样时间线

### 3.1 唯一配置源

实施时在 `configs/gesture_defaults.json` 增加 `calibrated_scroll_observation`。
下表是确定的首版值。旧 `scroll_observation` 内 12 点、0.70 比例、1.5px 正反误差、
0.04 纵移、0.03 横移、0.02 MAD、静止 0.01、30 帧上限不变。
任务建立时冻结两段配置的完整快照与规范化 JSON SHA-256；运行中不热换阈值。

| 配置键 | 初始值 | 含义 |
| --- | --- | --- |
| `version` | `calibrated-scroll-observation.v1` | 本次观察规则版本 |
| `sample_interval_ms` | 200 | 每个动作固定 5Hz；不因效果差临时改频率 |
| `max_pair_gap_ms` | 400 | 相邻有效源采集时间及本地接收时间间隔都不得超出 |
| `before_wait_timeout_ms` | 2000 | 动作前等新源帧的总等待上限 |
| `before_max_age_ms` | 400 | 最后动作许可检查时，前帧最早可能采集时刻距当前的上限 |
| `capture_timeout_ms` | 400 | 单次 broker 取帧含排队的超时 |
| `observation_timeout_ms` | 5800 | 从 `before` 被接纳起到最后一帧的单调时钟总上限 |
| `motion_observation_budget_ms` | 4400 | 同一观察窗口内等待机械完成与安全收尾的上限 |
| `after_wait_timeout_ms` | 1000 | 机械完成后等合格后帧及稳定尾部的上限；不能突破总上限 |
| `settle_pair_count` | 2 | 完成后至少 3 帧形成连续 2 对静止质量合格尾部 |
| `max_pair_vertical_ratio` | 0.25 | 相邻帧位移过大即拒绝，不冒充可靠 LK |
| `reverse_noise_ratio` | 0.002 | 计算明显反向位移时，每对只扣除 ROI 高的 0.002 噪声预算 |
| `max_reverse_path_ratio` | 0.01 | 全链有效反向路径不得超过 ROI 高的 0.01 |
| `max_capture_uncertainty_ms` | 20 | 源采集时刻换算不确定度的上限，不是允许伪造的固定值 |
| `observation_roi` | `{x:0.10,y:0.20,width:0.80,height:0.60}` | `screen_norm` 内容范围 |

`max_frame_chain` 仍只从 `scroll_observation.max_frame_chain=30` 读取，不复制另一份。
200ms × 29 间隔 = 5800ms，帧数和墙钟双上限同时生效。
这是观察能力上限，不是延长旧驱动命令超时的许可。
已有更短的任务到期、取消、急停、安全超时优先。

**长计划提前拒绝：** 从现有计划和驱动阶段计算本次执行加安全收尾的保守时间预算。
不能只用 `durationMs` 代表悬停移动、下压、滑动、抬起和收尾的总时间。
预算无法计算或大于 4400ms 时，零动作返回 `scroll_observation_window_insufficient`。
不得缩短滑动、加速机械臂、舍弃中间帧或把观察拆成多次物理动作。
**4.4 秒可能拒绝正常、合法的慢速机构，这不是硬件故障，也不是用户同意改硬件时序。**
只凭当前 `automatic_swipe_duration` 的触屏时长，不能判断这台机构能否满足完整窗口。
能力查询必须返回 `usable=false` 并明确显示“本版观察窗口不足”或“完整时长待验证”；
不得把机构判不合格、让用户重新校准，或默认提高速度以迁就观察配置。
现场待样本至少包括悬停行程、下压/抬起、驱动验证等待、安全收尾和帧获取的分段耗时，
覆盖快慢机构与可变起始位置。拿到这些证据后再决定是否制定新的采样/窗口版本，
不能在实施时自行放宽上限。此限制是能力可用性与现场验收项，不是已验证的正常动作预算。

### 3.2 实施顺序

1. 取得原任务租约，登记动作编号、配置/校准/计划快照，检查设备暂停与安全条件。
2. 通过同一 broker 取得新源帧，在 2000ms 内完成来源、姿态、亮度及特征预检。
   预检只选特征，不需要第二次试滑；遮挡/全黑/过曝/无有效 ROI/不足 12 点时零动作。
3. 前帧合格后启动单个采样协程，保存 `sample_index=0, phase=before`。
   最后许可点重新检查绑定、前帧年龄、任务到期与取消，再持久认领单次调用。
   前帧超过 400ms 时在未认领前重新准备；认领后不得重进执行。
4. 采样以本地单调时钟 `t0+n*200ms` 调度，不以“上次请求返回后再睡 200ms”累计漂移。
   不启动重叠读，不补发挤在同一瞬间的追赶采样。
   相同源序号只记采样诊断，不进入链，不冒充新帧；仍消耗时间预算。
5. 仅调用一次 `ctx.call_capability("arm.swipe", server_plan)`。
   采样必须同时覆盖驱动内部准备/运动/收尾，不得等 `await swipe()` 返回才开始采样。
   任何有效帧对的时间间隔超过 400ms，观察作废，不能跨过断点继续累加。
6. 机械完成回执和安全收尾确认后记录完成栅栏，继续原有采样，不重新开一条链。
   找到完成后 3 帧且尾部 2 对均满足静止条件，最后一帧标 `after`；
   完成后但不是最后一帧的样本仍标 `during`。尾部只证明动画已稳定，不证明页面边界。
7. 1000ms 内没有稳定尾部、总时长到 5800ms 或第 30 帧后仍未完成：
   视觉未知；停止添加帧，禁止补滑。机械仍在执行时沿用现有有界停止/安全收尾，
   不因光流采样失败就绕过停止，不在无机械依据时随便移动到另一个位置。
8. 所有采样、归并、证据持久化结束后，先原子保存动作结果和必要暂停，再向任务回报。
   内部一步把结果交给外层；独立任务按第 7 节规则结束租约。

不提前从“观察到了足够位移”结束：必须有完成后的新帧和稳定尾部。
任一链段质量失败后即锁定本次观察失败，不删掉该段重新拼一条“成功链”。
可以保留随后已取得的帧作诊断，但不能改变这次失败结论或额外延长采样窗口。

### 3.3 取消和资源排空

不得在事件循环直接调用同步 `capture_screenshot`。
新增 `EvidenceStore.archive_camera_frame(frame, observation_context)` 归档已采到的不可变帧；
不允许归档时又取一张。`capture_screenshot` 老入口可委托它，但不自造观察上下文。
broker 的生产底层取帧必须能在 400ms 内结束，包括连接、完整读取和解码。
`HttpFrameProvider` 当前默认 3 秒且 socket 超时不是完整下载总期限，必须补总期限或
有界读取；不支持此能力的驱动对新滑动报能力不可用，不能假装异步超时已取消底层线程。

取消时停止新采样请求；等待已提交物理读真正退出，再释放订阅和本次采样句柄。
`asyncio.wait_for(asyncio.to_thread(...))` 单独使用不满足此要求。
必须复用 `RealCameraDriver.capture` 已有的 shield/drain 思路，在 broker 层也保护物理读。
不可取消地永久阻塞的提供器只能禁用该相机新观察并保留安全阻断，不能换句柄并发重开。
测试必须证明底层最大同时读取数为 1，采样任务/读线程没有留给下一动作继续工作。

## 4. 来源、时钟和逐动作证据

### 4.1 最小完整帧记录

`observation.schema.json` 的 `frame` 变体是**证据记录内的新增观察子对象**，
不是替换已有 screenshot 外壳。保存位置为 `evidence.json.scroll_observation`；
其中 `source_frame` 字段仍可同时作为通用证据的新增可选来源对象，
两处如都存在必须相等，不维护两份可独立修改的数据。
实施可只存一个规范对象并在查询时投影；旧 screenshot 缺少它仍可查看。

每一张必须包含：

- 来源原样透传：`device_id/camera_id/device_unique_id/binding_revision`、
  必填 `camera_binding_id/source_geometry_id`、
  `source_frame_seq/source_captured_at/source_received_at/source_legacy_mode/width/height`。
- 新增可靠时序：`source_generation_id/source_capture_seq/source_clock_id`、
  `source_captured_monotonic_ns/source_capture_uncertainty_ms/source_timing_provenance`。
- 服务端归属：`task_id/action_instance_id/sample_index/phase`，
  完整动作绑定、`planning_version/observation_version/config_sha256`。
- 文件事实：`evidence_id/sha256`。哈希基于归档后文件原字节，不能对重新编码文件比对旧哈希。
- 本地接纳事实：`accepted_monotonic_ns/accepted_at/freshness_checked_at`。

源字段来源必须是获准的物理相机提供器，不得从 URL 设备编号、请求 binding、
broker 对象属性或当前系统时间“补齐”。`source_legacy_mode` 必须是严格 `false`。
整数拒绝布尔值、负序号、小数和非有限值；时间必须含时区。
归档前校验源绑定，归档后再次读取文件核对哈希，测量前再验证记录与文件。

Fleet 对外来源读取走只读 `GET /v1/devices/{id}/camera-source-frame`，
完整响应格式归 camera-binding 附件；旧 `screen-observation` 不强插新闭合字段。
该接口给出 metadata 与固定原图证据；原字节通过该 evidence_id 的 image 入口读取。
WX 从指定 source_evidence_id 派生 `screen-source-observation.v2`，不另拍一帧拼来源。
G03 直接消费相同原子 source snapshot，无需 HTTP 自调用。
滑动内部仍只经同一 `CameraFrameBroker` 取帧：图像原字节、源元数据、采样 PTS/序号、
实例与几何来自采集回调产生并经帧桥原子接纳的**同一个不可变帧包**。
禁止先取图片、再调用来源接口取“当前 metadata”拼成证据；接口响应及 broker 返回值
均须绑定同一原子帧与原字节摘要。归档只消费该包，不再次采集、不补当前绑定。

新 binding 和 source_frame 必填小写 UUID `camera_binding_id`、小写 SHA-256
`source_geometry_id`，全局 `binding_revision` 仍必填非负整数，仅诊断与旧兼容。
新路径比较设备、逻辑/物理相机、实例、几何、daemon 会话、v3 校准摘要、原图尺寸；
全局 revision 变化只重读本通道，不得单独失效。帧对、整链和最后认领均使用这一规则，
不能仍调用旧 `require_binding` 的整段 JSON 相等分支。
原始请求幂等摘要和不可变历史记录仍包含 revision，不因身份比较规则而改写原报文。

新滑动只消费完成真实机械验证的 `schemaVersion=3` 校准 bundle：
新增 `cameraBindingId/sourceGeometryId`，v3 motion fingerprint 排除全局 `bindingRevision`
并纳入两新字段，完整摘要输入按 camera-binding 第 7 节。不改机械数学和安全门禁。
v2 保持原算法，只供旧任务/迁移；补字段、改版本号或复制旧 ready 不能获得新资格。

### 4.2 实际采集时刻，不能拿上传时刻顶替

仅 `source_frame_seq` 增加、HTTP 返回发生在机械完成后，不能证明图片拍摄在完成后。
当前 helper 的 `upload` 时间需要修正；本附件明确这是实施依赖，不声称已存在。

首版拟支持**同一 Fleet 宿主机的受控原生相机采样时钟**。
这是拟议能力前置条件，不是当前所有部署的事实。必须从受控注册与时钟转换证据核验；
不能仅因 URL 是回环地址、源码在同一个仓库或设备名称相同就断定满足。
不具备时 `usable=false / source_timing_unproven`，保留只读画面，标“能力未就绪”。
本轮只规定将来需要满足的接口语义，不实施或改造采集系统。
受控注册的实际证明由 camera-binding 第 6 节固定，包括本机 helper 自检、
`mach_absolute_time()` 时钟转换、启动代际和 PTS 误差来源；本包保存其受控证据引用。
`source_generation_id` 仍是每次物理采集启动的独立代际，不得用持久物理绑定 UUID 替代；
本包的 20ms、前帧/完成栅栏及全部取样时限不变。

满足能力的实现应遵守：

1. helper 在 `captureOutput` 从该 `CMSampleBuffer` 的 presentation timestamp 取得采样时刻，
   明确转换到系统 host clock，再到 Fleet 注册的共同单调时钟域。
   不能用回调开始、编码结束或上传开始时刻代替 PTS。
2. `source_clock_id` 由相机服务与 Fleet 的受控启动握手确定，绑定宿主机启动代际；
   `source_generation_id` 在每次物理采集会话启动时改变。
   `source_capture_seq` 是该代际真实采样递增编号，不是 HTTP GET 次数或归档编号。
3. 序号、PTS、变换误差界限和图片在采样回调内一次性绑定；重传同一图片保留原值。
   原生时钟转换不可证明或误差界限大于 20ms 时，来源不具新滑动资格。
4. 现有帧桥在接收源帧时保留这些字段；`X-Frame-Seq` 继续是桥序号。
   增加响应头 `X-Source-Generation-Id`、`X-Source-Frame-Seq`、`X-Source-Clock-Id`、
   `X-Source-Captured-Monotonic-Ns`、`X-Source-Capture-Uncertainty-Ms`、
   `X-Source-Timing-Provenance`；最后一项正式值为 `camera_pts_host_clock.v1`。
5. `RealCameraDriver` 原样透传。`source_captured_at` 改为真实采集时刻的带毫秒 UTC 投影，
   `source_received_at` 仍是帧桥首次接收时刻；重新请求不得刷新这两个时间。
   UTC 用于沿用新鲜度检查和人类查看，跨事件顺序以共同单调时钟及误差界限为准。
6. Chrome 兼容、远端时钟、秒精度旧 helper、legacy 单路只保留查看/旧诊断；
   首版缺少可信 PTS 时不能执行新滑动，不为它们添加“测试时跳过”分支。
   fake 提供器产生同样完整的可信模拟时钟事件，以隔离测试验证，不属于生产来源冒充。

还须修正桥端对同一源代际/采样序号的重复上传：它们是同一采样，即使桥序号改变也不能
成为新证据。启动代际变更、物理相机换绑或时钟域变更立即中断当前链。
不要求相邻源序号恰好 +1，但两种序号都必须严格增加。

### 4.3 完成栅栏与完整性

运行时在本地单调时钟记录：

```text
before_accepted_ns <= dispatch_claimed_ns <= driver_invoked_ns
mechanical_completed_ns
safe_cleanup_confirmed_ns
completion_fence_ns = max(mechanical_completed_ns, safe_cleanup_confirmed_ns)
```

`mechanical_completed_ns` 必须由原驱动回执规则得出，不能由内容光流得出。
机械未知时完成栅栏为 null；仍可测量已得帧，但不能报告整体成功。
正常后帧必须满足：

```text
source_clock_id == 本动作固定的可信时钟域
source_captured_monotonic_ns - uncertainty_ns > completion_fence_ns
accepted_monotonic_ns >= source_captured_monotonic_ns - uncertainty_ns
```

前帧需满足其最晚可能采集时刻不晚于 `driver_invoked_ns`，最后认领处核验新鲜度。
所有帧同任务、同动作、同设备、同物理相机、同绑定、同校准摘要、同源代际、同尺寸、
同规划/配置版本，且索引从 0 连续，两个序号和源采样时刻都严格递增。
每对源时刻间隔考虑两端误差界限后仍不得超过 400ms。
源时间按采集当时复用 `calibration_gate` 的 2 秒/未来 0.5 秒检查，
并满足本附件更严格的 400ms 前帧约束；后读历史不以“现在太旧”改写当时结果。

缺前帧/来源协议不完整：零动作。缺后帧/跨动作/坏哈希/采样中途换绑：已调用则未知。
图片哈希相同但采样身份不同可以是静止；同一采样换证据编号/重编码/刷新接收时间必拒绝。

## 5. 屏幕坐标与逐对测量

每帧按同一校准快照执行 `locate_screen_quad -> fit_pose -> align_screen`，
再用 `cameraToScreen.homography -> warpPerspective` 得到同尺寸屏幕平面。
当前姿态超出既有补偿范围立即未知，不允许放宽姿态门禁。
`align_screen` 不是屏幕单应矫正，两者都需要且各做一次。

ROI 固定为这次动作的 `screen_norm` 范围，取服务端有效列表范围与默认 ROI 交集；
无页面信息就用默认值。只排除可信的静态栏、探头遮挡和非滚动区。
本次排除区统一在屏幕平面取各采样遮挡的并集，对所有帧使用同一 mask。
并集使有效区域不足则未知，不能每对换一个有利区域或退回整幅相机画面。
归一化转像素统一 `left=floor(x*W), right=ceil((x+w)*W)` 等，
边界限制在屏幕内；宽高必须为正，所有阈值基于**最终同一 ROI 的像素 W/H**。

每对必须保留以下实测字段，见 schema `pair`：

| 字段 | 含义 |
| --- | --- |
| `pair_index/from_sample_index/to_sample_index` | 第 i 对只能是 i 到 i+1 |
| `corners/valid_points/inlier_ratio` | 原始角点数、双向状态及误差过滤后点数、未四舍五入比例 |
| `forward_backward_max_px` | 保留点最大正反误差；无保留点为 null |
| `dx_px/dy_px/mad_dy_px` | 保留点位移中位数及 `median(abs(dy_j-median(dy)))` |
| `quality_ok/reason_code` | 是否通过该对质量门槛；失败也保留已实际测到的指标 |

`cv2` 返回 None、形状不符、非有限坐标或异常时，该对未知，不能用 `0` 填充缺测。
角点不足时 `corners` 保留实际角点数；还未获得光流则点数 0、比例 0，dx/dy/MAD 为 null。
相邻帧重新选点，所以全链的点数/MAD不能假装来自一组跨全程共同追踪的点。

一对 `quality_ok=true` 同时要求：

```text
valid_points >= 12
inlier_ratio = valid_points / corners >= 0.70
保留点 forward_backward_max_px <= 1.5
abs(dx_px) <= 0.03 * W
mad_dy_px <= 0.02 * H
abs(dy_px) <= 0.25 * H
```

不要求每对 dy 达到 0.04H；许多可靠的小位移可以累计成有效滚动。
大跨度不通过时，只能利用**事先已覆盖运动的完整相邻帧链**，不能在动作结束后补拍中间帧，
不能对失败对插值成伪帧，也不能为绕过失败跳过帧、拆掉动作或要求失败首尾 LK 再成功。

## 6. 全链归并算法

### 6.1 验证与统计

先验证 `2 <= frame_count <= 30`、`len(pairs)=frame_count-1`、逐对索引连续，
以及第 4 节证据链完整性；无效证据不进入算法。按原始精度计算，最后显示时才格式化。

```text
dx = sum(pair.dx_px)
dy = sum(pair.dy_px)
horizontal_path = sum(abs(pair.dx_px))
vertical_path = sum(abs(pair.dy_px))
positive_path = sum(max(pair.dy_px - 0.002*H, 0))
negative_path = sum(max(-pair.dy_px - 0.002*H, 0))
reverse_path = min(positive_path, negative_path)
valid_points = min(pair.valid_points)
inlier_ratio = min(pair.inlier_ratio)
mad_dy_px = max(pair.mad_dy_px)
```

最后三个是“最差逐对摘要”，不是全链真实同一批点的统计，schema/界面必须如此命名说明。
缺对、单对质量失败或缺任一必需指标就不计算全链完整总量；已有片段留在 `pairs` 中，
顶层 dx/dy、路径和最差摘要为 null，避免部分总量被误当完整位移。

### 6.2 顺序固定，不能各调用方自行选结论

1. 任一对 `quality_ok=false`：整体 `unknown`，记录第一个失败对和原因。
2. `abs(dx)>0.03W` **或** `horizontal_path>0.03W`：`unknown / accumulated_horizontal_motion`。
   后一个条件用于阻止横向往返抵消；仅净横移小不够。
3. `reverse_path>0.01H`：`unknown / bidirectional_motion`，即使最终 |dy| 达标也不算成功。
   例如先上 80px 再下 80px，净位移 0，不是 stationary。
4. 计算候选分类：
   - `abs(dy)>=0.04H`：位移候选。
   - 只有同时满足 `abs(dx)<=0.01W`、`abs(dy)<=0.01H`、
     `horizontal_path<=0.01W`、`vertical_path<=0.01H`，
     **每对** `abs(dx_i)<=0.01W, abs(dy_i)<=0.01H, mad_i<=0.01H`，才是静止候选。
   - 其余 `unknown / between_thresholds`。明显运动抵消不变成静止。
5. 有首尾 OCR 时执行第 6.3 节，矛盾或提供了不足互证的首尾结果为未知。
6. 位移候选的符号：`swipe_up` 必须 `dy<0`；`swipe_down` 必须 `dy>0`。
   反向为 `effect_status=unknown / direction_consistent=false / direction_mismatch`；
   `flow_status=displaced` 仍保留客观测到位移的事实。没有可靠候选时方向为 null。
7. 正向候选 `effect_status=displaced/direction_consistent=true`；
   静止 `effect_status=stationary/direction_consistent=null`。
8. 任何成功/静止还须经过完成后稳定尾部和完整证据检查；采样状态失败覆盖候选。

尾部 2 对必须分别满足质量门槛及 dx/dy/MAD 的 0.01 静止条件；
不要求整个动作静止。惯性滚动尾部仍在动时继续**原有有限取帧**，不是继续滑动。

### 6.3 首尾 OCR

通用滑动 `ocr_endpoint=null` 完全合法，不调用 OCR，不要求页面登记。
服务端插件提供互证时必须包含这次**第 0 帧和最终 after 帧**的证据编号、
屏幕平面坐标、行中心和文本；别的时刻、别的任务、列表变换前的原图行不能直接使用。
原图 OCR 如需转换，应复用各自帧的姿态对齐和单应变换，并声明 `screen_px`，不能变换两次。
仅提供一端、误标坐标域或处理失败，返回 `ocr_endpoint_invalid`，不能偷偷退回未提供。

配对算法：

1. 文本执行 NFC 和两端空白去除；不模糊匹配、不用客户身份推测，不折叠不同内部字符。
2. 只比较在两端均恰好出现一次的非空文本；重复同名行不配对。
3. 所有可比唯一行，检查 `delta_y` 与全链 dy 的差不超过 `0.04H`，
   `delta_x` 与全链 dx 的差不超过 `0.03W`。
   任一有效可比行违反即 `ocr_rows_not_consistent`，不能只挑两条相合的行放行。
4. 位移候选还要求至少两条匹配行；超过噪声 `0.01H` 的 OCR 行移动不得与 dy 反向。
   静止候选要求至少两条匹配行，且每条各轴位移不超过 `0.01H/0.01W`。
5. 有 OCR 但唯一行不足/完全滚出画面：`ocr_rows_insufficient`，不是无滚动。
   本规则不是旧 `measure_stationary_list` 的“顶部认证”，不设置 `top_verified`。

只归档受控证据引用、匹配计数和判定；公共日志不输出客户原文。
OCR 不用于弥补不合格光流，也不用于把机械未知变成安全完成。

## 7. 结果、持久暂停与纯核对出口

### 7.1 三种事实，不能合并

终态仍遵从主规格第 6.4 节。补充：

- `mechanical_status` 取现有 `drivers/arm.py::gesture_motion_confirmed("swipe", result)` 和
  持久动作回执，不因 `controller_completed=true`、光流或上传成功自行放宽。
- `safe_cleanup_confirmed` 独立布尔量；停止、抬笔、停车、租约安全收尾需要的既有证明不能省。
  它不能由 `mechanical_status=confirmed` 单独推导。
- `motion_started` 使用 `false/true/null`：确定未动作、确认开始、崩溃/回执缺失不能确定。
  `null` 与 true 一样禁止重放；不要以“unknown”强行声称一定动过。
- `retryable` 始终 false。`stationary` 为 failed/`scroll_no_displacement`，
  不注册视觉未知暂停，但自动调用方必须结束本轮，不自动尝试另一同义任务。
- 机械未知、收尾未知仍走现有机械阻断与保留租约规则；
  纯视觉未知且收尾安全时，只建立视觉核对暂停，独立任务可释放租约，
  不调用 `hold_unknown` 把 `home_ok` 改成 false，不要求额外机械归位。

正式结果增加 `action_instance_id/observation_version/config_sha256/flow_status`、
`safe_cleanup_confirmed`、`pause_issue_id` 和 `observation`。
`observation` 使用 schema `chain`，包含逐对指标、整链摘要、OCR 摘要和失败码。
所有 `before_evidence/after_evidence/frame_chain_evidence` 来自本动作；`after_evidence`
不存在就为 null，不能借末个 during 替代。
动作前失败时 `observation=null`；动作后缺测记录 unknown，而不是 not_started。

### 7.2 新增持久动作表

实施时在 Fleet SQLite 新增 `scroll_action_records`，不把暂停只写进 task JSON：

```sql
CREATE TABLE IF NOT EXISTS scroll_action_records (
  task_id TEXT NOT NULL,
  action_instance_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  state TEXT NOT NULL,
  revision INTEGER NOT NULL,
  json TEXT NOT NULL,
  PRIMARY KEY (task_id, action_instance_id)
);
CREATE INDEX IF NOT EXISTS scroll_action_device_state
  ON scroll_action_records(device_id, state);
```

`state` 固定为 `prepared/claimed/completed/review_required/reviewed/aborted_before_motion`。
完整、可机验的 JSON 以 `records.schema.json#/definitions/action_record` 为准；
下表为阅读索引，不是另一个可省字段的序列化格式。数据库列与解码 JSON 双向一致。
`json` 字段表：

| 字段 | 类型及规则 |
| --- | --- |
| `record_version` | 固定 `calibrated-scroll-action.v1` |
| `task_id/action_instance_id/device_id` | 与键一致的非空文本 |
| `revision/state` | 从 1 单调递增；状态与列一致 |
| `origin/step_key/request_sha256` | 外部任务或父任务步骤、稳定步骤键、完整原请求规范摘要 |
| `action/binding/planning_version/observation_version/config_sha256` | 不可变；binding 为本包新增两字段的 v3 投影 |
| `plan_evidence/config_evidence/clock_registration_evidence` | 带归属、类型、SHA-256、时间的受控引用，不是自由字典 |
| `created_at/updated_at` | 带时区时间 |
| `lease_id/claim_id/claimed_at/driver_invoked_at/swipe_call_count` | 原租约和调用事实；认领前 claim 为 null；认领后 claim 永不复用，次数未知为 null |
| `motion_started/mechanical_status/safe_cleanup_confirmed` | 依第 7.1 节；准备态 false/not_started/false |
| `sample_evidence_ids` | 最多 30 个，按 sample_index 排序 |
| `timeline` | 完整 before/claim/invoke/mechanical/cleanup/fence/sampling_finished 时刻；未知 null，同时保存 source_clock_id |
| `sampling_diagnostics` | 固定 tick 的读取、重复、超时、取消、跳过、拒绝记录，不冒充有效帧 |
| `observation_context/ocr_evidence/driver_receipt_evidence` | 冻结的服务端观察范围及本动作 OCR/机械回执引用，可空条件见 CONTRACT |
| `observation` | schema chain 或 null |
| `task_outcome/reason_code` | 主规格终态，准备/执行中 null |
| `pause_issue_id/pause_scope` | 未暂停均 null；暂停 scope 固定 `automatic_actions` |
| `manual_resume_required` | 准备态 false；发生未知时 true，核对后仍为 true，仅独立恢复操作可 CAS 清除 |
| `result/review` | 终态完整结果 / 完整核对回执；原 unknown 永不改写 |

只用 Store 新的事务方法写该表，不暴露任意 table 字符串：
`prepare_scroll_action/claim_scroll_action/finalize_scroll_action/review_scroll_action`，
以及只追加本动作采样/回执的 `record_scroll_progress`。
`BEGIN IMMEDIATE` 内 CAS 检查原 state/revision、任务/设备归属、原租约、取消和所有暂停，
禁止读取旧对象后整段覆盖。配置证据引用创建成功后才能 prepared。

`action_instance_id` 由运行时预先分配并保存为外层 task 的固定步骤编号关联，
不是每次恢复时重新生成。外层多次合法搜索滑动各自新编号。
同一动作从 prepared 到 claimed 只成功一次；claimed 在调用驱动之前 fsync/提交。
claimed 后崩溃，不论是否已写 driver_invoked，都当可能已动作且不得重放。
认领失败调用计数为 0。没有认领凭据的直接 arm.swipe 不得冒充新滑动实现。

观察未知的动作结果和 `state=review_required/pause_issue_id` 在同一事务内先提交，
然后才发布结果/结束租约。调度、能力最后认领、设备 resume 均查询未核对记录，
不能只看设备 UI 的 `status=ready`。`device.visual_scroll_review_required` 和问题列表是
从该表生成的只读投影，不是另一份事实源。
若结果写库失败，当前运行时立即阻断设备；重启从 durable claimed 重建核对问题。
数据库不可读时不启动该设备自动任务。

### 7.3 启动、迁移与其他暂停共存

迁移只增表和证据可选字段，旧证据不补造 source，旧任务不改成新协议。
未知新 record_version、损坏记录或不一致列/JSON 必须阻断并报数据核对，
不能删除记录后当首次启动。不能降级到不识别这些暂停的旧版本继续执行。

启动顺序：加载/迁移 Store -> 扫描本表 -> 重建未核对阻断 -> 启动接纳/调度/
`Runtime.recover_tasks`。后者本身可能恢复执行，不能先调用它再扫描。

| 重启遇到的记录 | 处理 |
| --- | --- |
| prepared | 本次旧任务结束为动作前中断，不自动接着滑；不建已动作的视觉暂停 |
| claimed | 原子变 review_required，`motion_started=null`，机械未知；走现有机械未知保护 |
| review_required | 原样保留问题；即使重新校准或画面恢复也不能自动清除 |
| completed/aborted_before_motion/reviewed | 只读恢复，不重放；reviewed 不代表历史成功 |

视觉暂停不能清掉 G01 输入撤销核对、人工暂停、急停、机械未知、相机会话错误。
即使记录已经 reviewed，`manual_resume_required=true` 仍阻断新自动动作。
既有独立 `/resume` 必须先确认该设备所有视觉问题均 reviewed、没有其他安全阻断，
再在事务内清除这些 reviewed 记录的恢复标记；不得清除未核对记录或直接重放旧任务。
来源/相机重绑也不删除旧问题；核对历史问题后，新动作仍检查当前绑定和校准。
旧 generic task review、`/resume`、重新打开托管不能跨过未解决视觉问题。
可以查询、看新画面、停止、急停，以及完成原任务已授权的必要安全收尾；
不得借“人工新建”绕过视觉未知再试滑。

### 7.4 只核对，不执行的接口

以下均为**计划新增**：

```text
GET  /v1/devices/{device_id}/calibrated-scroll/reviews
POST /v1/devices/{device_id}/calibrated-scroll/reviews/{issue_id}/review
```

GET 只查表，返回 `contract_version="calibrated-scroll-review.v1", device_id, issues`。
每条含问题编号、原 task/action、revision、原 binding、原结果、受控帧引用，
以及 `mechanical_review_required` 和其他阻断原因；不取证、不执行、不解除任何问题。

POST 请求见 schema `review_request`，必须有
`request_id/expected_revision/task_id/action_instance_id/acknowledge_no_replay=true/note`。
路径 device/issue 与记录须一致；操作者来自认证上下文，不从 body 信任 operator_id。
确认含义是“已经查看本次未知情况，不重放本动作”，不是“我证明光流成功”。
人工可先通过已有只读看图接口查看当前画面；看图不倒改历史结果。

服务端在一个事务内检查身份、问题、revision 和当前没有执行中的本设备动作，
保存核对回执，仅把这一条变为 reviewed。
返回：

```text
contract_version = calibrated-scroll-review.v1
device_id, issue_id, task_id, action_instance_id
revision, reviewed_at, reviewed_by, original_outcome
issue_resolved = true
automatic_resume_performed = false
remaining_visual_issue_ids = [...]
other_blocking_reasons = [...]
```

响应中的 `issue_resolved` 只解除本条视觉暂停，不解除机械或输入问题。
无需为了核对视觉问题做一次归位，也不能借该接口填写 `home_ok=true`。
机械未知问题须走已有机械人工核对流程；这里可记录“已阅”但其机械阻断必须保留，
不能释放机械未确认的租约。
核对不恢复托管开关、不刷新任务到期、不重发原任务、不执行滑动/点击/输入。
下一次自动运行须由操作者按现有单独恢复入口明确恢复，并重新验证所有前置条件。

幂等：同 request_id 同规范请求摘要返回原核对响应，异内容 409；
expected_revision 不匹配、其他并发核对或新问题已出现但 request指向旧问题，禁止清空整台列表。
新问题出现不妨碍仅核对指定旧问题，但响应必须列出仍未解决的问题。
404 表示设备/问题不匹配，409 为 revision/执行中/请求冲突，422 为严格请求格式错误，
403 为无人工核对权限，503 为存储不可用。任何错误均为零动作、零问题清除。

## 8. 稳定错误码

下列为新观察原因码，公共错误外壳仍沿用 Fleet；UI 显示中文名称，不展示英文代替说明。
机械层已有错误保留，不被本表覆盖。未知优先记录首个失败，其他原因存诊断列表。

| code | 中文说明 |
| --- | --- |
| `scroll_observation_window_insufficient` | 计划超出本版可观察时长，未动作 |
| `source_metadata_missing` | 缺少可信来源字段 |
| `source_timing_unproven` | 采样时间或时钟域不能证明 |
| `source_binding_mismatch` | 帧与本动作设备/校准不符 |
| `source_frame_replayed` | 源采样重复、倒退或跨代际 |
| `source_frame_stale` | 采样当时画面过期 |
| `evidence_action_mismatch` | 证据属于别的任务或动作 |
| `evidence_hash_mismatch` | 图片与记录哈希不一致 |
| `frame_chain_invalid` | 帧数、索引、字段或相邻关系非法 |
| `frame_gap_exceeded` | 连续采样出现不可跨越的空档 |
| `capture_timeout` | 有界读取超时 |
| `observation_timeout` | 观察总时间耗尽 |
| `frame_chain_limit` | 帧数耗尽 |
| `after_frame_missing` | 没有机械完成后新帧 |
| `after_not_settled` | 完成后画面未稳定 |
| `sampling_cancelled` | 采样被取消 |
| `screen_pose_unavailable` | 手机姿态不能可靠补偿 |
| `roi_invalid` | 观察区或有效 mask 不足 |
| `insufficient_features` | 角点不足 |
| `insufficient_inliers` | 有效追踪点或比例不足 |
| `tracking_error` | LK 错误、非有限值或输出不完整 |
| `incoherent_motion` | 单对横移或 MAD 超限 |
| `pair_displacement_exceeded` | 单对跨度超出观察能力 |
| `accumulated_horizontal_motion` | 全链横向移动超限 |
| `bidirectional_motion` | 明显往返移动 |
| `between_thresholds` | 无法可靠区分位移或静止 |
| `ocr_endpoint_invalid` | OCR 不属于同一首尾帧/坐标无效 |
| `ocr_rows_insufficient` | 已提供 OCR 但唯一互证行不足 |
| `ocr_rows_not_consistent` | OCR 与光流矛盾 |
| `direction_mismatch` | 位移方向相反 |
| `scroll_no_displacement` | 机械完成但无显著内容位移 |
| `interrupted_after_claim` | 持久认领后中断，不能确定动作结果 |
| `scroll_review_required` | 仍有待人工核对的滑动问题 |

## 9. 逐文件实施顺序与退出条件

表中均是**后续实施工作**，本附件不修改这些运行文件。

| 顺序 | 文件/模块 | 必须完成与退出条件 |
| --- | --- | --- |
| C01 | `api/contracts/calibrated-scroll.v1.schema.json`、新观察/核对契约；本目录 schema | 按本包完整 contract/records/observation schema 发布与注册，落实 routes 映射、错误码、中文术语；不能直接把设计文件当已发布 API |
| C02 | `tools/camera_helper/Sources/CameraHelper/main.swift`、`scripts/camera_frame_server.py` | 真采集 PTS/同宿主时钟/源代际、同样本重传不变、headers 透传；先用合成消息测试，不能拿上传时间补造 |
| C03 | `drivers/real_camera.py`、`camera_broker.py`、`fake_camera.py` | 有界 broker 读取、真实源信息、取消排空；fake 同结构；并行读峰值 1 |
| C04 | `runtime/evidence.py`、`runtime/calibration_gate.py` | 归档已取帧，核验绑定/时钟/哈希/动作编号；老证据仍查看但无新任务资格 |
| C05 | `drivers/scroll_observation.py`、`configs/gesture_defaults.json` | 逐对完整指标、共享归并、OCR、固定配置；纯算法边界用例通过 |
| C06 | `runtime/store.py`、`runtime/calibrated_scroll.py` | 新表/CAS单次认领、采样编排、持久暂停、重启扫描、结果保存先于释放；动作不重放 |
| C07 | `runtime/runtime.py`、`runtime/executor.py`、`runtime/devices.py` | 接正式能力；视觉未知与机械未知分别收尾；调度和最后动作入口均防绕过；不能误删其他暂停 |
| C08 | `examples/plugins/screen-gesture-adapter/plugin.py`、`manifest.json` | 同插件新增 calibrated_scroll 分支，schema/能力/预算声明；旧 task_type 不变 |
| C09 | `api/app.py`、`runtime/errors.py`、`runtime/status.py` | 能力查询含采样协议/时间预算阻断；核对接口鉴权/CAS/幂等；中文状态与审计 |
| C10 | 调试台现有新滑动按钮接线 | 只调用正式任务，展示完整机械/视觉结果；核对是独立无动作操作，不接旧视觉判定器 |

C07 必须检查 `_finalize` 中不止一个 `hold_unknown` 调用点和普通失败的自动 recover 分支。
仅在顶部加一次条件不够；静止 failed 不应触发试滑/页面恢复，视觉未知也不得落到
通用自动归位来“治好画面”。专用终态辅助函数可以复用现有租约清理，不新建第二执行器。
外层插件使用可信的结构化结果报告纯视觉未知，不能允许客户端提供 `safe_cleanup_confirmed`
骗过机械门禁。

### 9.1 必须新增的无硬件测试入口

以下名称为待实现测试文件，不声明当前已存在或已通过：

```text
arm_fleet/tests/test_calibrated_scroll_observation.py
arm_fleet/tests/test_calibrated_scroll_sampling.py
arm_fleet/tests/test_calibrated_scroll_evidence.py
arm_fleet/tests/test_calibrated_scroll_recovery.py
arm_fleet/tests/test_calibrated_scroll_task.py
```

分别覆盖纯算法、fake clock/broker、正式归档、SQLite 崩溃/核对、正式任务能力链。
现有 `test_scroll_observation.py/test_camera_broker.py/test_fake_camera_scroll.py`
及相机帧桥测试作为邻接回归。所有测试用临时目录/临时 SQLite，不使用工作区 data 数据库，
不实例化真实 daemon、相机、手机、部署脚本或外部接口。

### 9.2 补充验收建议

使用附件编号 G03-Axx，交由主线程决定并入 S 系列的位置，避免重编号 S01-S26。
每项须同时断言 `driver.swipe_calls`、接触宏计数和输入调用数；停止/必要抬笔单列，
不能混进“滑动次数”掩盖补滑。

| 编号 | 夹具/故障 | 期望与动作次数 |
| --- | --- | --- |
| G03-A01 | 合格链，旋转/透视/小幅手机姿态变化 | 摆正后正向 displaced；swipe=1、接触宏=1、input=0；补充 S05/S08/S11/S12 |
| G03-A02 | 源字段缺失、上传时间冒充 PTS、旧时钟协议 | before 阶段 failed；swipe=0；补充 S04/S13/S24 |
| G03-A03 | 同图片不同真实采样；同采样换桥序号/证据编号 | 前者可 stationary，后者拒绝；按故障发生前后 0/1 次；S25 |
| G03-A04 | 同 task 不同 action、跨设备/绑定/代际、文件篡改 | 不混证；前拒绝/后 unknown；S13/S24 |
| G03-A05 | source_capture 早于完成但 HTTP 返回晚于完成 | 不作 after；超时 unknown，swipe=1；S13 |
| G03-A06 | source PTS 误差界限与完成栅栏相交 | after 不合格，不用 UTC 猜顺序；swipe=1 |
| G03-A07 | 每对 dy 小于 0.04H，累计达标 | 全链成功，不因单对 small 拒绝；S14 |
| G03-A08 | 单对跨度过大、间隙 401ms、链 31 帧 | unknown，无插值/抽帧/补滑；S14 |
| G03-A09 | 每对横移合法，累计净横移或路径超 0.03W | unknown；S26 |
| G03-A10 | 先上再下净零、少量多次移动累计抵消 | 不判静止；超过反向预算 unknown；S19/S26 |
| G03-A11 | 静止低净位移但某对 MAD 超静止线 | 不判 stationary；严格边界等号测试；S07/S26 |
| G03-A12 | 首尾 OCR 两条相合一条明显相反 | unknown，不挑有利行；缺 OCR 可合法判位移；S20/S26 |
| G03-A13 | 机械确认但完成后仍惯性滚动 | 有界等静止尾部；预算内不稳 unknown；swipe=1 |
| G03-A14 | 机械未知但内容正向移动 | 总结果 unknown；保留 displaced 实测，不释机械保护；S16/S22 |
| G03-A15 | 仅视觉未知、机械与安全收尾已确认 | 原子存视觉暂停后释独立租约，home_ok 不伪改；S23 |
| G03-A16 | 认领后崩溃、回调丢失、重复任务/同幂等请求 | 重启不重放；累计 swipe<=1，旧 task 查询不增次数；S15 |
| G03-A17 | 核对后重启、并发旧 revision 核对新问题 | 只清指定 issue；旧结果仍 unknown；所有核对 input/swipe=0 |
| G03-A18 | 取消时 broker 底层读仍进行 | 排空后再准入；max_concurrent_grabs=1；无孤儿读取；S14 |
| G03-A19 | 视觉问题同时有输入撤销/急停/机械未知 | 单项核对不解除其他阻断；所有核对零动作 |
| G03-A20 | 计划时间预算超限、无法估计 | 查询不可用/执行前拒绝；swipe=0，不调快机械参数 |
| G03-A21 | paused 投影丢失、generic resume/直接能力调用 | 持久 issue 仍挡在最后认领；swipe=0 |
| G03-A22 | 观察结果写库失败、提交后回报前进程死 | 当前阻断/重启从 claimed或review_required恢复；不放行下一动作 |

计时测试必须使用可推进的单调 fake clock，覆盖恰好阈值、超 1ms、
时钟回退、迟到帧、任务取消、采集线程释放；不靠真实 sleep 猜时序。
集成通过条件包括：S01-S26 全部仍成立、动作最多一次、每个新接口错误零动作、
所有新记录重启可恢复、普通旧证据仍只读可见。

## 10. 附件校验与同步记录

本目录四份 schema 均为 Draft-07；`routes.json` 映射所有本包入口。
`fixtures.json` 与 `package-fixtures.json` 覆盖公共/内部对象、绑定、引用、摘要与状态转换。
`validate_fixtures.py` 只读本目录文件，调用纯 `semantic_validator.py`，
不导入运行时、不联网、不读取相机/数据库，并对每个正例的所有嵌套对象注入额外字段验证拒绝。
它检查设计不变量，不证明生产来源可信、硬件安全、LK 算法或异步采样实现通过。

纯文档自检命令，从 WX 根目录运行：

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-wx/bin/python arm_fleet/docs/specs/calibrated-scroll/validate_fixtures.py
```

以下技术条款已同步到主文 v1.8；清单保留供实施追踪：

1. 第 6.2.1 节：增加真实采集 PTS/时钟域/源代际字段及 helper/帧桥依赖；
   旧上传时间不够证明动作后帧，不能只加 EvidenceStore 字段就宣称 S13 通过。
2. 第 6.3 节：引用本附件固定配置、归并函数、路径累计/OCR/完成后尾部规则。
3. 第 6.4 节：加入 action_instance_id、safe_cleanup_confirmed、motion_started 可空语义、
   逐对 observation、pause_issue_id；与完整 G02 任务/结果 schema 一起发布。
4. 第 5.4/7.2/8 节：采样窗口预检、持久认领、视觉暂停先于租约释放，
   executor 纯视觉分支及 source clock 支持文件。
5. 第 6.5 节：保留 S01-S26，引用本附件 G03-A01-A22，并明确 fixture 校验不是运行验收。
6. 第 13 节 G03：记录“实施设计和附件已补，待实现/样本验证”，
   不能写成光流功能已完成、所有手机均已可用或现场参数已验证。

本附件没有剩余产品选项。真实样本、慢计划适配和相机时钟误差界限属于实施验证门槛，
不能用让用户猜一个阈值来替代测试。
本轮按用户澄清运行纯文档自检，结果在下方记录；未运行生产功能、服务或硬件。
公共机器格式与父相机附件的新增正文同步点见 `CONTRACT.md` 第 8 节。

本轮实际自检结果：

```text
PASS: 4 schemas; 16 entry mappings; 69 positives; 61 schema negatives;
48 semantic negatives; 13 transitions; 712 nested extra-property rejections.
```

这只是纯文档 schema/样例/跨字段/状态转换自检通过；没有执行 Fleet/WX 生产功能、
启动服务、读取生产数据、连接硬件或声称现场样本通过。未来功能回归仍按第9节实施。
