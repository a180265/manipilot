# G02 输入机器契约附件（待实施）

唯一权威是 `../../2026-10-03-输入开关与校准直驱上下滑动修改规格.md`
§4.1-4.9；本附件已核对新增的 §4.9 迁移路线。**不是第二主文档，
不是已发布 API，不证明运行代码或手机端已支持。** 下表技术细则及迁移展开已由主文 v1.8
引用；如后续出现冲突须先同步正文/schema/反例再实施，不允许挑宽松版本运行。

## 文件与校验

| 文件 | 根或 `#/definitions/...` 入口 |
| --- | --- |
| `common.schema.json` | 共用标识、计数、时间、服务端身份、证据引用 |
| `input-settings.schema.json` | 根=`put_request`；`authorized_put/normalized_request/get_query/status/success/file/ledger_entry` |
| `input-record.schema.json` | 根=四种记录联合；`owner/effect/device_guard/settings_write` |
| `input-recovery.schema.json` | 根=`request`；`authorized_request/normalized_request/response/review_entry` |
| `fleet-error.schema.json` | 根=真实错误正文；`http_response` 是离线验证 HTTP 与正文配对的容器，不是额外线上包装 |
| `ime-control.schema.json` | 根=`heartbeat_v1`；心跳观察/响应、任务状态、单次开始、取消命令/确认、IME 错误 |
| `input-migration.schema.json` | 根=`baseline`；`legacy_file/legacy_entry/backup_manifest/version_inventory/maintenance_intent` |
| `fixtures/positive.json`、`intent.json`、`reviewed.json` | 可直接提取 `value` 的完整正例，均为虚构设备、任务和证据 |
| `fixtures/variants.json`、`transitions.json` | 从命名正例派生的正反例及状态迁移对照；`select` 取子对象，`edits` 改/删指定路径，`repeat` 只生成长度边界 |
| `fixtures/migration.json`、`migration-variants.json`、`migration-checks.json` | 旧档/备份/关闭基线/迁后写入和中断对照；`from_fixture` 复制已验证正例，避免重复全文 |
| `validate_contracts.py` | 仅读取本目录，校验 schema、fixtures、摘要/版本链/快照/追加回执等关系，不写文件、不导入运行时 |

在工作区根目录离线执行：

```sh
.venv-wx/bin/python arm_fleet/docs/specs/input-control/validate_contracts.py
# 加 --verbose 可逐项查看；不运行 pytest、服务或手机端构建。
```

统一 draft-07、每个对象 `additionalProperties:false`，字典用
`propertyNames + patternProperties`，不会放开任意记录。所有 `$ref` 仅解析本地已注册
URN，未注册引用直接失败。整数检查拒绝布尔及 `1.0`；后者属于解析层加严，
标准 JSON Schema 的数学整数本身不能区分 `1` 与 `1.0`。
时间同时检查格式、真实日历和时区，不依赖额外日期包。正例通过、反例被预期层拒绝才算通过。
这些检查只证明附件自洽，不是运行时并发、持久性、硬件、Android 或两侧解析验收。

## 实施边界

- GET 无查询参数、无动作、无修复、无默认落盘；`readonly/wx` 可读取。
  PUT/review 先验证形状，再显式鉴别人工 `operator/responder`；设置 PUT 接着查旧编号
  tombstone/迁移保留编号，再查新版幂等，最后检查 CAS。
  原始请求 schema 允许合法角色字符串以正确返回 403；`authorized_*` 才收窄权限。
  缺角色/非法类型返回 400。正文角色不是新增认证机制，系统关闭不走公开 PUT。
- 所有明确 nullable 字段仍须出现；关闭请求的手机可省略并规范为 null，
  `error.detail` 可省略但不可 null。标识接受 Unicode，不强制 ASCII；
  不做 NFC/NFKC 或大小写折叠，首尾空白拒绝，操作人/原因先检查原长度再 trim。
  协议长度按 Unicode 码点，不按 UTF-16 单元；两侧解析仍需验证。
- 保存成功原包装固定 `{settings,replayed:false}`，文件中永远存原快照；
  重放只改外层 replay 标志，不刷新 `observed_at`，不再审计/取消/写版本。
  关闭保留原授权手机，另在 audit 保留请求携带的旧手机。文件账本首版不清理。
- owner 的身份、摘要和授权版本不可变；仍须检查当前撤销边界、唯一绑定及绑定版本。
  此处 `binding_revision` 固定来自
  `DeviceManager.binding_view(device_id).binding_revision`，即 Store 中本台设备登记修订。
  它不是相机服务所有通道共用的 revision；不能跟随新相机语义取消这一比较，也不另建
  IME 配对文件。手机身份仍通过 `input_binding()` 及手机当前能力核对。
  换相机保留用户已保存选择不等于保留旧 owner 的执行资格；换手机仍关闭并撤销旧 owner。
  完整相机迁移边界见 `../camera-binding/README.md` §7。
  effect 各自认领，`completed` 不是业务效果成功；已认领/未知不可自动重发。
  `input_effect_not_started` 不推出 `motion_not_started`。原始回执保存在不可变证据引用中，
  `receipt.observations` 只追加，不用摘要替代原始证据保存。
- 人工核对只解决列出的可核对 issue，保留 resolved 问题及原审计；
  不能清提交/损坏/换绑问题，不能改 unknown、字符进度或伪造手机确认。
  必须另外证明原协程退出、旧 create/deliver/start 永久禁止、手机唯一匹配、
  无真实占用/机械未知；仅这些旧 issue 对应的 cancel_requested 不构成占用障碍。
  这些现场前置条件、SQL 列与 JSON 身份一致、证据真实性须运行时校验。
- Fleet IME 仍用原 camelCase 路径/字段及 `{error,message}`，不强改独立服务。
  `typing` 是单次开始许可，拒绝时不得回 `200 + cancelled`；迟到 displayed 可以回真实取消状态。
  ack 的 taskId 来自路径，必须核对手机/任务/commandId，输入协程退出后报告真实字符数。
  只有原手机 ack 才产生 `cancel_state=confirmed`；未交付 pending 的本地取消保留
  `repository_cancelled` 事实且无需手机握手（none），not_created 必须有原 create 已退出及查无记录证据。
- 缺失/其他控制版本的心跳仍可观察在线状态，但不通过 `heartbeat_v1` 兼容门禁；
  不能把 `currentTaskId=null`、超时或校准轮询 touch 当取消确认/新能力心跳。
  IME 仓储固定新增 nullable `input_task_control_version/input_control_reported_at/
  input_control_runtime_instance` 三列，仅真实带能力的当前绑定心跳更新。
  runtime_instance 是**服务端本次启动 UUID**，不是手机字段；历史心跳即使仍在
  `ONLINE_WINDOW=45s` 内也不能跨服务重启复用，必须等新心跳。touch、普通轮询、
  appVersion 和旧在线时间不补值、不续期；45秒不是手机停止阈值。
  G05 的 imeGeometryVersion 只走独立几何握手，不进入普通心跳，此封闭 schema 无须合并。
- `typedCharacterCount` 是项目 `GraphemeSplitter.split` 单元中成功 commit 的数量，
  取消协程停止并 join 后上报同一计数；不是 UTF-16 长度或 Python len(content)。
  正文 10000 上限才按 Unicode 码点；进度仍用现有 0..10000 协议范围，但不得把两者
  相等当完成证据。组合字符/emoji 不用于推算可安全重发的后半段，旧单位不明的进度原样留历史。
  非 IME 进度采用安全整数；本附件不设置机械阈值。

## 已同步技术字典

以下为主文 v1.8 第 4.8/4.9 节已采纳或明确引用的技术细则：

| 正文章节 | 新增字段或明确行为 |
| --- | --- |
| §4.1/4.8.2 | `requests[id]` 完整字段为 request_digest、normalized_request、call_context、before、after、audit、original_response；before/after 是五字段 saved_value，不递归复制账本。服务端 context 固定 origin/operator_id/role/operation_id，系统仅 `system_binding_change` 关闭。 |
| §4.1/4.8.2 | 摘要采用 UTF-8 `json.dumps(sort_keys=True,ensure_ascii=False,allow_nan=False)` 的默认空格分隔形式；不做 Unicode 折叠。请求摘要输入 `[contract_version,device_id,normalized_request,call_context]`；文件/checkpoint 摘要覆盖完整语义 JSON，非文件空白。文件不存在时的摘要输入为 `["input-settings-file-absent.v1"]`。其他语言必须逐字节对齐，不能改用无空格 JSON。 |
| §4.8.2/4.8.4 | slot 三元组第一项固定为字符串、序号从 0 起；IME 固定 `["ime:"+taskId,0,effect_kind]`。receipt 是 observations 数组，含来源、事件、原回执 evidence_ref/digest、时间、未交付证明及真实进度/命令号。 |
| §4.8.2/4.8.7 | guard 新增必填 `review_requests` 持久幂等账本；人工 resolution 引用 client_request_id，原请求、操作者、时间、原问题和原响应同事务保存。response 增加固定 contract_version，其他四个成功字段保持正文；重放返回完全相同结果，不新增 replay 字段。issue_ids 保留顺序，换顺序视为不同请求。 |
| §4.8.2/4.8.3 | settings_write 补齐 proposed_file、original_response、failure(stage/occurred_at/完整HTTP错误)，aborted 重放原失败。binding_change 固定 operation_id/old_ime_device_id/new_ime_device_id/phase，阶段 prepared/closed/reconciling/applied。 |
| §4.2.2 | blocking_reasons 固定闭集；沿用当前设备资格码，`calibration_due` 映射 calibration_not_ready，`ime_not_bound` 映射 input_binding_unavailable，加入 G01 问题码/启动核对/控制不兼容。新原因需同步契约，不静默丢弃。 |
| §4.8.5/4.9.3 | v1 心跳四字段齐全，观察入口兼容旧字段缺失，响应控制版本未知为 null。三列能力观察和服务端启动 UUID 已与主文对齐。Fleet ack 强制真实 split-unit 计数，不以 status=cancelled 冒充控制确认；具体 DB/手机升级未实施。 |
| §4.2.3/4.8.7 | 下表补人工核对/未装配错误键。现有 errors.py 的 input_settings_invalid=400 必须改为本端点 503；request_conflict 不能沿用“换编号重试”的旧建议。Fleet 与手机错误包装不可混用。 |
| §4.9.1/4.9.2 | 可选 migration_baseline、完整 legacy 白名单、维护意图与中断路线已按主文落 schema；下文明确新增技术字段、原键保留、备份编码和目标命令。无需再决定是否迁移或是否伪造历史。 |

## 错误与动作

全部设置/review 错误和成功都**不授权新输入、删除、发送或机械动作**；旧任务取消/只读对账继续。
以下 HTTP 对应 schema 固定；错误 title/suggestion 须中文，代理保留 HTTP、key、trace_id。

| HTTP | 键 | 提交/恢复含义 |
| --- | --- | --- |
| 400/403/404 | invalid_request、unsupported_contract / permission_denied / device_not_found | 本次未提交，先修请求/权限/设备 |
| 409 | revision_conflict、request_conflict、calibration_not_ready、input_binding_unavailable、input_binding_mismatch、input_revision_exhausted | 本次不新增提交；同编号原结果可能已存在；不能盲换编号重试 |
| 409 | legacy_request_replay_unavailable | 旧编号无论内容相同/不同均不重执，不返回当前值伪装原响应；权限之后、CAS之前拒绝 |
| 503 | input_settings_invalid、input_settings_write_failed、input_settings_commit_unknown | 分别为不可安全解释、确定替换前失败、提交未知；保留资料，最后一种按原请求对账 |
| 409 | input_recovery_conflict、input_recovery_busy、input_recovery_issue_not_reviewable | 核对 CAS/问题归属变化、真实占用/机械未知、问题不可由人工出口解决；不清阻断 |
| 503 | input_recovery_write_failed、input_recovery_commit_unknown、input_control_not_ready | 核对事务确定未提交、结果未知、尚未完成装配；未知只重放原请求 |
| 410 | input_acceptance_removed | 旧 E 写端点已移除，不签发资格 |

手机开始拒绝 `input_authorization_revoked/input_start_already_claimed/ime_control_incompatible`
为 409，未装配 `input_control_not_ready` 为 503，沿用手机 `{error,message}`。
既有 IME 参数、设备归属、task/command 不存在等错误继续原协议，未在此增量中重定义。

## 迁移契约

本节对应主文 §4.9，不是第二套路线；**只交付契约与离线检查，未实现或运行迁移工具**。
无 baseline 的新装文件仍从 revision=0 验证完整新账本，不允许空 requests。
有 baseline 时从其 `closed_saved_value` 开始，迁移刚结束的 requests 可以为空；
顶层当前值/维护审计必须等于基线，后续每次真实写入才追加完整请求及原响应。
baseline 和 tombstone 永久保留、后续普通写入不可改动。

`migration_baseline` 恰含 contract_version=`input-settings-migration.v1`、
revision_floor、closed_saved_value、operator_id、reason、client_request_id、
backup_manifest_ref、backup_manifest_sha256、legacy_request_ids、migration_id。
`legacy_request_ids` 是 **原键 -> SHA-256(完整旧 entry 的规范 JSON)** 字典，
不是复制 entry.digest，更不是伪造原请求/原响应。迁移本身不生成 HTTP 成功快照。

旧格式白名单只接受 `legacy_file/legacy_entry` 定义的精确字段集合：旧顶层无撤销字段，
entry 只有 enabled/revision/operator_id/at/digest。必须校验类型、时间、范围、唯一版本、
旧版本不超当前、按 revision 的时间不倒退，最新 entry 与当前值/审计/摘要一致。
历史 entry 缺原请求时只保存原件及摘要，不推断未记录的关联号/身份/手机/原因。
旧模块只在保存后产生正 revision/非空手机和审计；磁盘上的 revision=0 空白对象不作为合法
旧写入文件接受。只有真正新设备**缺文件**才走首次安装，不能拿空白对象绕过历史检查。

旧模块曾保存未 trim 的 requests 原键、却 trim 顶层 client_request_id。附件保留原键，
并将原键及其 trim 别名均作为拒绝标记；别名碰撞先核对，不选择性丢历史。
合法 PUT 先查现时权限，再查这些标记（409 legacy_request_replay_unavailable），
再查 migration_id/维护 client_request_id（409 request_conflict），之后才是新版幂等/CAS。
原编号同内容、改内容、旧 CAS 都不重新执行；不得由客户端自动换号再次开启。

### 目标命令

以下是**待实现接口**，路径与主文一致；本轮没有创建该工具，也不执行这些命令。
配置必须指向获准停写的维护资料，变量由维护人员显式指定，不使用现场默认配置。
apply 必填 `--reason/--client-request-id`，对应基线审计字段；已同步主文命令示例。

```sh
PYTHONPATH=arm_fleet .venv-wx/bin/python arm_fleet/tools/migrate_input_settings.py \
  --config /path/to/offline-config.json --device arm-01 --dry-run

PYTHONPATH=arm_fleet .venv-wx/bin/python arm_fleet/tools/migrate_input_settings.py \
  --config /path/to/offline-config.json --device arm-01 --apply \
  --migration-id "$MIGRATION_ID" --operator "$OPERATOR_ID" \
  --reason "迁移旧输入资料并关闭许可" --client-request-id "$MAINTENANCE_REQUEST_ID" \
  --backup-dir /path/to/controlled-backup

PYTHONPATH=arm_fleet .venv-wx/bin/python arm_fleet/tools/migrate_input_settings.py \
  --config /path/to/offline-config.json --device arm-01 --resume \
  --migration-id "$MIGRATION_ID"
```

### 执行顺序

1. 停止相关 Fleet/IME 写服务，工具从同一配置解析真实数据根、Fleet/IME DB，
   取得 G01 **全部资源排他锁**，保持到退出；不能只锁目标设置文件。
   dry-run 只读业务资料，报告格式、R、拟关闭版本、备份范围和阻断项，不生成意图/基线。
2. 严格验证旧文件及所有相关记录。R=max(旧 file/ledger revision、可信 owner.settings_revision、
   checkpoint.revision、已提交 intent.after_revision)。owner 超出旧文件、checkpoint
   版本/摘要与文件不符、已提交意图出现未来版本/同版本不同摘要、存在未对账 prepared 意图，
   均先核对并阻断，不能仅取 max 掩盖来源矛盾。版本来源从锁定资料重新读，不复用过时 dry-run。
3. 用 SQLite backup API 分别备份 Fleet/IME DB，不能只复制 `.db` 漏 WAL；
   原设置文件按字节复制。停写且全锁保证两库/文件处于同一稳定维护窗口。
   `backup_manifest` 固定设备/迁移编号、时间、旧文件语义摘要、三份 artifacts 与
   version_inventory（文件/账本版本、owner/已应用意图的编号/版本/记录摘要、原 checkpoint）。
   每个 artifact 保存 source_ref/backup_ref/sha256/byte_length/method；所有备份不可覆盖源文件。
4. 校验备份完整可读、SQLite 完整性和原字节摘要，先同步备份文件及目录。
   manifest 按上文规范 JSON 编码为 UTF-8、**不追加换行**，其原字节 SHA 与规范摘要相同；
   settings 文件的 raw SHA 与 semantic SHA 是不同用途，不互相替换。
   源记录摘要/版本清单由备份中的原记录复核。fixtures 的路径/原始文件摘要是虚构资料，
   附件脚本不打开真实 DB 或备份，不能将其通过视为真实备份验收。
5. 在 `{data_root}/maintenance/input-control/{SHA256([device_id,migration_id])}/intent.json`
   原子保存完整 `maintenance_intent=prepared`，含 before_digest/after_digest、
   backup_manifest_ref/sha256、完整清单和 proposed_file，fsync 文件和父目录。
   proposed_file 必须关闭，revision=revoked_through_revision=R+1，requests={}，
   保留旧授权手机；不创建 owner、IME 任务、旧请求响应或机械动作。
6. 原子替换并同步关闭文件后，在同一 Fleet DB 事务更新 checkpoint 和按 migration_id
   去重的迁移审计；不能改旧 owner 的许可、原效果或手机取消事实。
   DB 持久提交后才能把维护意图改成 committed 并同步，committed_at 才允许非 null。
   SQLite 无法确认提交、目录同步失败或回包丢失时保留待核对状态，不能倒回开启。
7. 正常启动先查维护目录；任何未完成/损坏意图均阻断输入，交维护工具 `--resume`，
   不自动执行迁移、开启或重放。committed 只解除迁移这一项阻断，仍须 G01 恢复扫描；
   孤儿旧任务、未知输入/发送和取消未确认保持原问题与真实计数，不因迁移完成清除。

### 中断与幂等

| 维护意图及现场 | 唯一处理 |
| --- | --- |
| prepared，文件=before，checkpoint=原值 | 仅显式 resume 继续**同一次**关闭文件，不改编号/版本/来源 |
| prepared，文件=after，checkpoint=原值 | 不再替换文件；resume 同事务补 checkpoint/迁移审计 |
| prepared，文件=after，checkpoint=目标值 | 只核对已有迁移审计并补 committed，仍未 committed 前不报成功 |
| prepared，文件其他/丢失/损坏，或 checkpoint 与上述均不匹配 | 停止并保持阻断；不拿拟提交文件覆盖未知资料 |
| 同迁移编号、同来源重复 apply/查询 | 只返回同一结果/待维护状态；未完成的推进必须用 resume |
| 同编号但源摘要、清单摘要、设备或维护身份/原因不同 | 拒绝并保留原意图，不新建一个同名迁移 |
| committed 后已有合法的新版本 | 查原迁移结果，验证当前基线/账本/checkpoint；绝不回写旧关闭文件 |
| committed 却文件/检查点回退 | 资料恢复事故，继续阻断，不重新迁移当新装 |

MAX=9007199254740991 **预留给关闭**：新开启若 after.revision==MAX，
在提交前返回 409 input_revision_exhausted；MAX-1 仍可关闭到 MAX。
保存值（包括原快照）或旧文件出现 enabled=true 且 revision=MAX，附件拒绝并要求维护阻断；
不能宣称“仍能关闭”。迁移 R=MAX 无法 R+1，同样阻断；R=MAX-1 只能生成关闭的 MAX 基线。
此后无新设置写入、无重置/回绕/强行清零；只能走将来的更大版本协议维护。

### 本次同步点

迁移主路线、旧编号 409、split-unit 计数、服务端启动 UUID/新心跳、G05 独立握手与 MAX
关闭预留均已对齐主文。本附件补齐的技术展开是：baseline 字段名、完整 manifest/版本清单、
维护目录定位及 prepared/committed 字段、manifest 精确编码、原旧键 trim 别名防复用、
维护审计两个 CLI 参数。主文第 4.9.2 节已引用本附件并同步示例，不再作为待定迁移方案。
实际工具、数据库升级、两侧解析和中断验收仍未实施；没有运行时或硬件验证结论。
