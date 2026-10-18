# 摄像头身份、来源几何与校准迁移附件

从属主规格第 5、6、11.10、13 节。状态：拟实施设计，不是已发布接口。
本文固定跨附件共同语义；不是另一个产品方案。不改变 D01-D08，不新增远程相机或 NAS。
只读检查过本机 Python 单调时钟实现为 `mach_absolute_time()`；没有启动相机或机械臂。

## 1. 必须区分的四种变化

| 字段 | 产生与保存者 | 什么时候变 | 不能替代什么 |
| --- | --- | --- | --- |
| `camera_binding_id` | 相机服务，绑定文件每通道 UUID | 真正换绑、解绑后重绑、人工报告安装位变化 | 不用全局 revision、USB 序号、帧号或 daemon 会话代替 |
| `source_geometry_id` | 已登记来源几何的规范化 JSON SHA-256 | 采集格式、取景、裁剪、缩放、来源旋转/镜像变化 | CSS 预览变换不进入它；不是精确手机屏幕四角 |
| `source_generation_id` | 已注册采集进程的采集会话 UUID | 每次重新启动物理采集或改变来源几何 | 普通采集重启不强制使机械校准过期，但旧帧链立即不可续接 |
| `source_clock_id` | Fleet 本机时钟登记 | 宿主机启动代际或受控时钟实现变化 | 不信 HTTP 客户端自报“同机器”；不是上传时间 |

全局 `binding_revision` 继续单调递增，用于兼容旧链路和审计。新链路中它的变化
只触发重读当前通道事实；**不能单独使另一台设备的校准失效**。该字段不被删除、
归零或改解释为单通道修订号。

相机来源 `device_unique_id` 与 WX/滑动 binding 中 `camera_device_unique_id` 是同一个原始
物理身份字符串，映射时改键不改值。相机编号按实际登记值透传，不在边界偷偷转大小写/别名。
输入控制 owner 的 `binding_revision` 是 Fleet 单设备登记绑定修订，见 §7，
不能误改成相机服务的全局版本或取消其比较。

## 2. 机器格式与唯一比较规则

`contracts.schema.json` 是本附件新增对象的完整 Draft07 schema，所有对象关闭额外字段。
`examples.json` 给出完整正例；`validate.py` 校验跨字段、摘要、迁移和版本比较反例。
不存在的字段不可补 null；schema 明确允许 null 时才可用。
本文命名的 `calibration_identity` 是从档案提取的严格投影，不是允许把完整机械模型换成投影。

新机械任务绑定固定比较：

```text
device_id, camera_id, camera_device_unique_id, camera_binding_id,
source_geometry_id, daemon_session, calibration_fingerprint,
frame_width, frame_height
```

WX的Binding复用共同的设备/相机/daemon/校准身份，再比较
`wechat_account_id,coordinate_frame_hash`，共九项语义身份；宽高在source/visual中核对，
不往WX已封闭的Binding额外添加frame_width/frame_height。原租约/任务身份照旧检查。
范围只比较本来就属于它的设备/安装实例/几何/尺寸，不引入 daemon_session 使重启丢框。
除上表明确的诊断 revision 外，不能忽略其他绑定字段；不得使用整个 JSON 相等作新身份判据。
原 task/request 幂等摘要仍覆盖**完整原报文**，包含诊断 revision；重试原编号须重放原字节，
不能为了忽略 revision 比较而改变原请求内容。

实际来源 `binding_snapshot` 的物理标识、实例、尺寸、几何必须与新校准匹配；
记录缺字段为协议不支持，字段变化为换源，不能将两者当成旧相机暂时离线。

## 3. 来源几何不能填常量

`source_geometry` 固定字段：

```text
contract_version=source-geometry.v1
input_size:{width,height}
format_id:string
crop:{left,top,right,bottom}          # 原输入像素，右/下排他
rotation_degrees:0|90|180|270        # 先裁剪，再顺时针旋转
mirror_x:boolean, mirror_y:boolean  # 在旋转后的坐标中镜像
output_size:{width,height}          # 最后缩放后的实际 JPEG 尺寸
resampler:none|area|bilinear
zoom_milli:integer>=1000
stabilization:off
```

`format_id` 是 helper 从实际采集格式生成的稳定字符串，不是列表下标；首版生成规则固定为
`fourcc:<小写8位十六进制媒体子类型>;size:<原宽>x<原高>;pixel-aspect:<分子>/<分母>`，
分子分母为约分后的正整数。相关事实从捕获连接/实际格式读取，缺失即不具新模式资格。
几何明确不含曝光、JPEG 压缩率和帧号；它们不改变坐标域，但画质仍须当次检查。
实际 zoom 乘1000四舍五入只允许绝对量化误差<=0.0005，否则拒绝，不能将未知 zoom 当1000。
首版禁用电子防抖/自动切镜头/动态裁剪；检测到这些模式或不能证明已禁用则只读。
用户搬动物理相机仍须人工报变更，不能声称这些字段能检测三脚架移动。

几何摘要为 UTF-8、键排序、紧凑分隔、不转义 Unicode、禁止 NaN 的 JSON SHA-256。
几何全部数值为整数，避免跨语言 `1`/`1.0` 的摘要歧义。`crop` 必须在 input_size 内；
resampler=none 时旋转后尺寸必须等于 output_size；其他情况也不得假报实际解码尺寸。
同一生效来源会话内几何不变；变化先停止该会话并创建新会话，禁止给旧 JPEG 贴新摘要。

## 4. 路由与原子保存

下列相机路由只由既有受控 CameraHelper/后端调用，不暴露成可任意指定相机的 WX 按钮。
现有相机写接口只信任 body.operator，并不存在可直接沿用的可信认证。新接口固定采用
**同机受控 helper 身份**：首次显式安装生成32随机字节、64位小写hex token，保存到
`CAMERA_CONTROL_TOKEN_FILE` 指定的独立0600文件，父目录0700，拒绝符号链接/错误属主。
CameraHelper/Fleet后台由本机启动配置得到该路径；HTTP 带
`Authorization: Bearer <token>`，只接受loopback对端且无 Origin 的本机后台请求，
常量时间比较；不接受URL/body token，不下发浏览器、不记日志、不随资料导出。
缺失/损坏令牌拒绝服务能力，不自动生成替换；作者固定为服务端核验的
`camera-helper:<本机uid>`。此身份证明受信本机用户使用工具，不声称区分该用户启动的恶意程序。
新版本启用后旧浏览器绑定写接口403；兼容画面仍可读，不能借旧写接口绕过安装UUID。
JSON body 最大64KiB。全局 `request_id` UUID、CAS 与持久幂等先检查，再更改绑定。

| 路由 | 请求 definition | 成功 definition / HTTP | 拒绝与动作 |
| --- | --- | --- | --- |
| `GET /cameras/{id}/binding-v2` | 无 body/无 query | `binding_view` / 200 | 不存在404；坏档503；零动作 |
| `PUT /cameras/{id}/binding-v2` | `binding_write_request` | `binding_write_response` / 200 | 身份403；CAS/忙409；格式422；存储503；零动作 |
| `POST /cameras/{id}/binding-v2/renew` | `binding_renew_request` | `binding_write_response` / 200 | 同上；人工报告安装位置改变，不移动设备 |
| `POST /cameras/{id}/binding-v2/unbind` | `binding_remove_request` | `binding_write_response` / 200 | 同上；必须先停通道且设备无在途任务 |
| `GET /camera-binding-operations/{request_id}` | 无 body/无 query | `binding_operation_view` / 200 | 未知404不证明未提交；零动作 |
| `GET /cameras/{id}/capture-sessions/current` | 无 body/无 query | `capture_session_view` / 200 | 同机后台认证；只查当前服务启动代际/来源会话 |
| `POST /cameras/{id}/capture-sessions` | `capture_session_request` | `capture_session_response` / 200 | 实例过期409；源能力缺失503；不操作机械 |
| `POST /cameras/{id}/frame` | 原 JPEG + §5 必需头 | `frame_receipt` / 200 | 旧实例/重复异内容409，非法422，无能力503 |
| `POST /api/device-context` | 新分支 `debug_context_request` | `debug_context_response` / 200 | 旧请求走旧分支，新分支缺来源不得生成 ready 上下文 |

错误均为 `error` definition。对 frame 入站返回200只证明保存了来源帧，不证明校准或页面有效。
捕获会话先查 current，再携带当前 `server_instance_id`、新的 `client_instance_id`；
`replaces_generation_id` 为查到的旧代际，无旧会话时null。服务端在通道锁内比较上述事实，
不匹配409；同请求同内容且仍为当前会话返回原响应，旧client已被替换则409。
当前启动内保存请求摘要/原响应/retired_client_ids，不允许重发初始化夺回通道。
服务重启生成新的server_instance_id并清会话；旧请求的服务启动代际不匹配，
返回 `source_session_expired`，helper 重新查询并用新的client_instance_id握手；
物理绑定UUID不变。停止采集不刷新最后帧；会话仍可查询但不代表来源新鲜。
一个通道同一时刻只有一个获准来源；正常接管先停止旧采集、排空在途上传，
崩溃后的显式重启也须带预期旧代际。接管原子撤销旧代际并清帧，全部迟到上传拒绝。
来源进程断开不允许任意不同来源抢占；旧协议浏览器桥只能作为显式只读兼容模式。

错误 code→HTTP 唯一映射：404为 `camera_not_found/camera_operation_not_found`；
403为 `camera_permission_denied`；422为 `camera_request_invalid`；
503为 `camera_archive_invalid/camera_storage_failed/camera_commit_unknown/
source_timing_unproven/source_unavailable`；其余枚举均409。
`motion_started=false`只说明相机接口不驱动机械，不表示绑定未提交；
`camera_commit_unknown`必须查原request_id，禁止改编号盲写。新接口均不自动重试变更。
schema内无 `error.detail` 或自由扩展字典。请求摘要按本附件紧凑规范计算
`[method,path,body,principal_id]`，同编号不同路由/作者/正文均冲突。

### 4.1 绑定文件

沿用相机服务配置的 BINDINGS_PATH，不硬编码真实路径。文件形状为 `binding_file`：
schemaVersion=2、revision、bindings；每项保留现有驼峰字段，并加 `cameraBindingId`。
无绑定通道从数组删除，不用空物理 ID 占位。重复逻辑相机、重复物理相机和重复实例 UUID 拒绝。

每个写入在唯一相机进程中持全局配置短锁；后台输入/运动正在占用该设备时拒绝换绑。
同身份、同物理相机且没有 renew 意图的保存是无动作 no-op，不换 UUID、不递增 revision；
新的显示名称允许独立更新元数据并递增全局 revision，但不换实例、不影响校准。
真正换绑/renew 分配新 UUID，revision +1，清该通道旧帧/来源会话；其他通道实例原样保留。
解绑也 revision +1；重新绑定绝不复用曾出现的 UUID。busy 检查与实际绑定提交必须位于
Fleet 设备维护占用之内；不能先查空闲、释放保护后再写相机文件。

维护占用固定用同机 `data_root/installation_locks/{device_id}.lock` 文件锁：
Fleet、调试台的所有动作入口（含旧模式和自动校准）在认领前非阻塞取得共享锁，
持有至最后动作/安全收尾结束；相机绑定写取排他锁，文件不删除、不替换。
关闭输入、STOP/急停、只读查询不取此锁；获取失败立即busy，不排队等待。
次序为安装锁→现有动作/输入短锁；短锁内不等待安装锁。排他锁内再核对持久在途任务、
未解决输入/机械问题和实际daemon状态，任一未知拒绝；持有到绑定及操作记录提交完成。
只有锁兼容的服务版本全部部署后才开新写接口，旧进程未退不能声称保护已生效。
Fleet离线时CameraHelper只在本台daemon已明确断开、无持久在途/未知任务时允许绑定；
网络不可达不等于已断开。相机服务只读STATUS，绝不代为CONNECT/STOP/回零。
这允许设备已安全停用后离线A→B→A，仍持久生成不同安装UUID。
崩溃释放锁不清持久未知；恢复原保护后才可换绑，不以进程已退出当作机械已停。

持久操作记录保存为 BINDINGS_PATH 同目录 `binding_operations/{request_id}.json`，
结构为 `binding_operation`。同请求不同摘要409；完整原响应持久化后才能回成功。
先落 prepared 意图（含完整 before/after 及各自摘要），fsync，再写临时绑定文件并
原子替换/fsync目录，最后写 committed 及原响应。崩溃重启用磁盘摘要判断：
等于 after 则只补完成记录；等于 before 则标 aborted、不自动再写；其他为 unknown，
保护相关设备并人工核对。绝不自动重放机械动作，备份/操作记录不自动删除。
同通道存在prepared/unknown绑定意图时，停止签发新来源/可运动上下文，
绑定查询返回503 camera_commit_unknown；迁移意图覆盖全部通道。
真实换绑在提交保护内先停旧来源并清帧，任何提交失败保留该阻断，不能继续用内存旧绑定服务动作。
模板/范围历史查看不受此阻断；启动先完成意图对账，再启新来源接纳。
before_sha256/after_sha256是严格解析文件后的规范化JSON摘要，比较磁盘时也先同样解析；
下一节备份文件名则使用原字节摘要，两者不得混用。迁移committed的response为null，
查询通过kind=migrate/phase=committed明确完成；普通写入committed必须有原响应。

### 4.2 旧绑定一次性迁移

停用写入口和推流，取得维护占用；读旧原字节并计算摘要，再保存不可变备份
`binding_backups/{sha256}.json`（已存在须同摘要），版本1严格检查后才生成版本2候选。
所有 UUID 及 before/after 在上述 prepared 意图中先持久化；重跑使用同一候选，不另生成一套 UUID。
旧格式 `boundAt/boundBy` 为 null 时原样保留，不伪造旧作者或时间。格式损坏不是无绑定，
不能沿用当前 `_load_bindings` 吞异常后返回空表的行为。
新装无文件允许显式初始化空版本2；已登记但文件丢失为故障，不能再初始化覆盖。
迁移完成仅代表资料可读，新机械校准保持待复核，不将旧 ready 档补字段后继续动作。

## 5. 采集端、帧桥与调试预览闭环

`CameraHelper.fetchBindings/saveBinding` 新链路读取 `binding-v2`，保存完整通道身份，
而不是只保留 `[cameraID: uniqueID]`。原物理采集回调生成不可变包：JPEG、采样序号/PTS、
单通道绑定实例、几何摘要、采集代际；编码/上传只消费该包，不能上传时读取“当前绑定”补标。

`capture_session_request.geometry` 由实际管线生成；服务端核对物理来源/绑定/受控时钟登记，
返回同几何摘要和新的 source_generation_id。此 UUID 同时用于 G03 来源帧链。
真实启动采集后才能将该会话绑定到回调；启动失败终止会话，不输出空画面当有效。
以下头在原 `POST /cameras/{id}/frame` 与返回 JPEG 中对应传播：

| HTTP 头 | 规范字段 |
| --- | --- |
| `X-Camera-Binding-Contract` | 固定 `camera-binding.v2` |
| `X-Frame-Source` | 必需，固定 `camera_helper`；缺失或其他来源拒绝，不自动补标 |
| `X-Camera-Id` / `X-Device-Id` / `X-Camera-Unique-Id` | 逻辑相机 / 机械臂 / 物理相机 |
| `X-Camera-Binding-Id` | camera_binding_id |
| `X-Source-Geometry-Id` | source_geometry_id |
| `X-Source-Generation-Id` | source_generation_id |
| `X-Source-Frame-Seq` | source_capture_seq |
| `X-Source-Clock-Id` | source_clock_id |
| `X-Source-Captured-Monotonic-Ns` | source_captured_monotonic_ns |
| `X-Source-Capture-Uncertainty-Ms` | source_capture_uncertainty_ms |
| `X-Source-Timing-Provenance` | 固定 `camera_pts_host_clock.v1` |
| `X-Captured-At` | 真实采样时刻的 UTC 投影 |
| `X-Binding-Revision` / `X-Frame-Seq` / `X-Received-At` | 帧桥接纳时全局诊断修订 / 桥序号 / 首次接收时间 |
| `X-Frame-Width` / `X-Frame-Height` | 解码后真实尺寸 |

最后两行由帧桥产生，不接受上传者决定。响应头中的诊断修订保存的是该帧接纳快照，
不能从当前全局计数读取后贴到旧帧。旧响应字段不删，旧消费者继续原语义。
无 v2 头的旧上传仅进入兼容只读通道，不给新模式签发来源标识，也不覆盖活动 v2 会话。

帧桥在同一锁内校验绑定实例/来源会话/几何/序号，提交图片及全部元数据。
相同代际+序号+完整摘要重传返回原回执，不增加桥序号、不刷新时间；同号异内容409，
低序号409；缺新鲜度/采样时钟证明503。解码尺寸须等于登记 output_size。
保存 JPEG、元数据和身份必须属于同一原子快照；读图时再比较活动绑定实例，
不允许先读身份再读图片造成换绑竞态。任一换绑清帧、断旧 MJPEG 流。

调试台 v2 上下文携带同一单通道 UUID/几何；UI 切换代际隔离按主文 T31-T34。
加载范围/历史不要求有可运动上下文；取新预览要求新鲜合法来源，二者不能共用旧的强门禁。
WX `CameraViewer` 的低延迟预览不是可执行证据：框选/页面采集必须经 Fleet 来源证据接口，
不能截浏览器 `<img>` 后自己补来源。`camera-view.v1` 保持旧只读结构，不塞新必填字段。
新增来源信息通过本附件规范的只读快照及其他新契约取得，不强改公开旧屏幕观察协议。

### 5.1 Fleet 只读来源接口

以下是新增接口，均不移动、不输入、不改开关/模板；取证可保存只读证据，不代表动作授权。
由现有设备可见性及后台传输身份保护，原相机token只在Fleet后台使用，浏览器不持有。
传递证据ID而非任意URL/磁盘路径，禁止服务端请求用户指定地址。

| 路由 | 唯一输入 | 200 响应 |
| --- | --- | --- |
| `GET /v1/devices/{id}/camera-source-frame` | 无body/无query | `source_frame_view`，当前原图证据及完整source_frame |
| `GET /v1/devices/{id}/camera-source-frame/{evidence_id}/image` | 无body/无query | `image/jpeg`原字节；ETag为带引号jpeg_sha256，Cache-Control=no-store |
| `GET /v1/devices/{id}/screen-source-observation` | `screen_source_query`，必填唯一query `source_evidence_id`，无body | `screen_source_response`，只从指定原图摆正/识别 |

第一条不要求机械校准ready，否则首次框选无法进行。必须取得真实v2来源：
沿用采样距现在<=2秒、未来<=0.5秒的门槛；具体动作另执行更严的G03或WX新鲜度。
归档原JPEG与全部source_frame原子快照，证据类型 `camera_source_v2`；
同一来源代际+捕获序号重复读取复用原证据，不改采样/接收时间。受控本地
`source_frame_view`记录与原图一起保存到现有EvidenceStore，不导入客户/模板公共资产。
第二条核对设备归属和原字节摘要；即使证据已过实时期限，也可仅供查看，不续期。
原图最大8MiB，解码宽高均<=8192且总像素<=2400万；越界422，不先分配无界图像。
首次使用先保存文件/fsync、再保存数据库引用，失败无可用证据；孤立文件不可自动变成有效证据。

第三条要求本台当前v3机械校准、精确屏幕和指定原帧的绑定/几何均匹配；无法摆正503，
不能另拍一张图后沿用指定ID。`observation`是在当前screen-observation.v1已声明字段内的
严格投影，schema已逐字段封闭，旧端点/旧外层协议不改。`frame_sha256/frame_seq/
captured_at/物理身份/尺寸`必须对应外层source；摆正PNG的image_sha256独立计算。
`screen_to_camera`及`screen_to_calibrated_camera`来自同一帧现有屏幕姿态变换，
`coordinate_frame_hash`沿用WX已固定坐标域计算；不从模板旧图取矩阵。
OCR只能解析这张摆正图，截断/画质waiting按原规则拒绝动作；不由Fleet判断客户可否回复。
2026-10-09 契约勘误：clear 仍必须有两种逆矩阵及真实 OCR 元数据；
waiting 与旧观察端点一致，返回空 OCR/空引擎标识，不运行 OCR，也不返回
`screen_to_calibrated_camera`。等待态不能生成点击目标，不能用单位占位矩阵授予动作。
`observation.binding_revision` 保留归档接纳时的修订，不取当前计数覆盖；
`frame_seq` 对应帧桥 `source_frame_seq`，不是采集器 `source_capture_seq`。
内部辅助 `source` 不进入封闭的 observation，对外来源只在外层返回。
新WX用该外层source构造binding，并将内层observation交原识别器，
禁止先独立GET旧观察再凭设备ID把两份结果拼接。G03直接消费同一源快照，不需要走HTTP自调用。

新来源读接口错误使用Fleet已有 `{error:{key,title,suggestion,trace_id,detail?}}` 包装：
权限403 `camera_permission_denied`；无设备/证据404 `camera_not_found`；
非法query/超额资源422 `camera_request_invalid`；换绑/来源不匹配409
`source_binding_changed/source_geometry_changed`；来源过期/证据丢失/校准不具资格503
`source_unavailable/source_timing_unproven`。不会返回200空图。
`detail`只能字符串，错误均零动作。相机进程自己的§4错误结构不替代Fleet公共包装。
纳秒字段由Python后端按整数处理；前端只展示和转发证据ID，不用JS Number重算或回传纳秒证明。

## 6. 同机时钟的确定实现

首版限定受控 macOS 原生 CameraHelper 与 Fleet 在同宿主机，其他来源仅查看。
启动自检增加只读 `tools/verify_camera_clock.py --helper <已安装可执行文件> --json`：
由 Fleet 在本机实际启动该 helper 的 `--clock-proof` 子命令，不接受 HTTP 提交的自称报告。
helper 读取 `CMClockGetHostTimeClock`，PTS 用 `CMSyncConvertTime` 转到 host clock，
再用 `CMClockConvertHostTimeToSystemUnits`/`mach_timebase_info` 转纳秒；
Fleet 核对 `time.get_clock_info("monotonic").implementation == "mach_absolute_time()"`。
只支持这一明确转换；不支持的操作系统或 clock 实现报 `source_timing_unproven`。

宿主机指纹为本机安装 UUID 与 `kern.boottime` 秒/微秒的规范化对象摘要；
source_clock_id 为 `mach-absolute:<指纹>`。两个进程从本机 OS 读取相同启动事实，
不是服务器拿客户端 host_id 比一下就通过。已安装 helper 的路径/文件摘要随自检留证，
重装后重新验证；受信任本机管理员可以改程序不在来源认证威胁边界之外。
来源注册仅授予该本机受控 helper，不把 UUID 本身当作秘密或身份凭证。
本机安装UUID固定保存于 `data_root/camera_source/host_installation_id`，内容为小写UUID及一个换行，
0600、拒绝符号链接；只在显式首次安装时生成。缺失/坏值不临时重建；helper由受控启动配置读取
同一绝对路径。指纹精确输入为 `{host_installation_id,boot_seconds,boot_microseconds}`，
后两者为本机OS读取的整数，按§3规范JSON计算，不用本地时区字符串或进程启动时间。

自检连续20次用 Fleet 前后 monotonic_ns 包住 helper 读取的 host tick ns，
每次须位于前后边界内，往返<=20ms；超时即失败，不从成功子集挑20次。
这只检验跨进程时钟转换，不证明传感器 PTS 本身误差；采样 uncertainty 还须有
当前捕获格式的时间戳来源证明，取其已证上界与整数转换舍入误差之和，>20ms则不能动作，
不得将往返平均值、固定0或允许上限20冒充测量证明。证据归档 `clock_registration`，
G03 接触前/完成后帧次序仍逐次核对；自检通过不免除运行期新鲜度。

服务重启重新运行自检但同宿主启动代际 clock_id 不变；旧来源会话失效、重新握手取新帧。
宿主机重启 clock_id 变化，旧单调时间不能比较，所有在途动作只对账、不重放。

## 7. 机械校准 v3 与原输入配对

现代码 `calibration_bundle.BUNDLE_SCHEMA_VERSION=2`；
`motion_calibration_fingerprint` 包含 bindingRevision；`screen_gestures.require_binding`
比较完整 JSON；`calibration_gate/real_camera` 多处也检查全局修订。必须同包修改，不能只改 UI。

新自动校准写 `schemaVersion=3`，新增 `cameraBindingId/sourceGeometryId`，保留其余原机械字段。
`calibration_identity` schema 定义新增版本的严格身份投影。Z/XY/相机/屏幕模型、
独立验证、模型锁、安全停车及原数值上限继续复用当前校准验证函数的通用部分；
不得通过临时将3改成2来骗旧验证器。v2分支保持原字段、原摘要及机械/身份拒绝规则；
主文D01已要求删除的历史E输入资格不在“保持旧规则”范围内，不能借兼容恢复该门禁。
默认新建档案为 draft；v3 必须本轮真实自动校准完成才能 ready，删除 E 输入历史门禁不删除机械证明。

v3 `bindingFingerprint` 精确为以下字段对象的规范化 JSON SHA-256：
`deviceId,cameraId,cameraDeviceUniqueId,cameraBindingId,sourceGeometryId,daemonAddr,frame`。
v3 `motion_calibration_fingerprint` 精确输入：

```text
{"contract_version":"motion-calibration.v3","geometry":{
 schemaVersion,deviceId,daemonSession,cameraId,cameraDeviceUniqueId,
 cameraBindingId,sourceGeometryId,frame,camera,screen,xy,z,safePark
}}
```

geometry 是上述字段的原值对象，不是文本列名；字段缺失拒绝，旧几何值不被重算或改单位。
排除 `bindingRevision/updatedAt/acceptance` 等诊断/历史输入字段，不能据其变化重新签机械坐标。
合法 v3 校准、输入许可及当次页面/姿态仍分别检查。新上下滑和新 WX 导航只接受 v3；
旧 v1 任务在原有 v2 档案/上下文上按原契约对账，不能升级成新任务继续做。

输入 G01 owner 的 `binding_revision` **固定指 Fleet 本台设备登记修订**，
读取 `DeviceManager.binding_view(device_id).binding_revision`，来自 Store 的 devices 记录；
它不是相机全局修订，也不是另设的手机配对文件。当前 `devices.py::update_binding`
已有该单设备递增机制，继续沿用；手机身份从 `input_binding()` 及当前手机能力核对。
换手机必须在输入短锁/原绑定变更保护下关闭 input_settings 并撤销旧 owner，孤儿任务照旧核对；
只换相机不强制清掉用户已保存的输入选择，但旧owner/机械任务仍按实际绑定变化阻断。
其他设备相机的全局修订变化不修改本台设备登记修订。已有设备登记修订缺失/坏值为故障，
不能从相机revision补值。不要对所有名为 binding_revision 的代码机械替换成忽略比较。
单独取消旧 IME 的通道不能被相机失效堵住。

## 8. 发布和回退，按顺序执行

1. 保持业务派发关闭，备份输入/绑定/校准/模板/在途任务；操作员确认无在途运动再维护。
2. 先发布读取兼容 v1/v2 相机绑定、v2/v3 校准及新旧结果的服务代码，不开启新动作。
   每进程能力暴露其真实支持，不从安装包版本号猜接线完成。
3. 离线迁移绑定文件至v2；任何一步损坏/摘要冲突停止。升级 CameraHelper 并通过只读时钟/
   来源自检，取新鲜v2帧；旧helper仍可看画面但新能力不可用，不伪造v2头。
4. 调试台本台粗框预览/发布，用户启动本台新自动校准取得v3，再完成模板本台复核。
5. 新输入手机协议与 WX/Fleet 双侧 parser 同时就绪后，人工开启需要的开关和业务模式；
   没有取得该动作授权之前只运行 fake/副本验证。相机身份升级本身不会开启输入。
6. 回退先停止新任务派发，保留全部新记录和未知任务，使用仍懂新结果的只读服务对账。
   不把v3档改版本号覆盖回v2，不把绑定文件降级丢UUID。旧二进制读不了v2绑定/v3校准就
   不能接管真实动作；待新记录结案后只能人工重新建立旧格式校准，不能用旧备份恢复资格。

## 9. 文件改动与验收

| 步骤 | 真实修改入口 | 退出条件 |
| --- | --- | --- |
| B01 | `scripts/camera_frame_server.py` 的 load/persist/save/delete/accept/send | 同通道UUID稳定；换绑/renew更新；坏档不变空；原子意图对账 |
| B02 | `tools/camera_helper/Sources/CameraHelper/main.swift` 的 binding/capture/upload | 捕获时固定来源，重传不换身份；PTS转换；新旧能力明确 |
| B03 | `drivers/real_camera.py`、`camera_broker.py`、`runtime/evidence.py`、`api/app.py` | 图片/元数据原子传播及§5.1三条只读路由；不能重编码后沿用旧hash；低延迟预览不冒充证据 |
| B04 | `drivers/calibration_bundle.py`、`screen_gestures.py`、`runtime/calibration_gate.py` | v2/v3显式分支；v3 hash与身份不依赖全局修订；原机械安全不减 |
| B05 | `motor_debug_server.py`、`motor_debug_page.html`、WX查看入口 | 上下文版本和单通道实例一致；迟到回包不串机；T31-T34 |
| B06 | `runtime/devices.py`/输入owner装配、绑定变更路径 | 单设备登记修订不串用相机全局计数；换相机不偷关选择或恢复输入；换手机关闭撤销 |
| B07 | 新 `tools/verify_camera_clock.py` 及迁移测试、各消费者契约测试 | 升级/混合版本/回退拒绝矩阵通过，未测真机不显示可执行 |

隔离测试至少覆盖：一次换绑只影响一台；A→B→A即使Fleet离线也不可继承；
普通服务/采集重启保留安装UUID但不能续旧帧；同帧重传不续时；旧帧补新身份拒绝；
同全局revision但不同安装UUID拒绝；不同全局revision但安装/几何一致允许继续来源比较；
设备登记修订与相机修订同名但值不同，不被错误串用；坏档/断电/CAS/并发/迟到上下文。
这些行为落实为主文T31-T34及本附件验收矩阵，不是机器示例通过就算硬件已验证。

离线检查命令：

```sh
.venv-wx/bin/python arm_fleet/docs/specs/camera-binding/validate.py
```

该命令仅读文档样例并做纯计算，不读生产绑定或接触任何设备。
