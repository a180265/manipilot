# 框选、布局复用与 IME 完整契约附件

状态：已同步主文 v1.9 第 11.10-11.16 节，仍为拟实施契约附件，**不是已发布 API，也不是运行功能验收**。
本目录固化框选、保存指针、模板/适配、草稿/采集、预览/发布/复核、资料包及 IME 契约，
始终从属于同一主文。完整路由、资产闭环、幂等崩溃规则及主文待同步条目见
[contract-completion.md](contract-completion.md)，不另立主规格。
D06/D07 已确认：每台独立重新完成自动校准、四态一次整体确认、竖屏 Android 正常全宽项目键盘。
D08 已确认本机历史版本及导出，不实现 NAS 或自动外部备份。

## 范围

| 文件 | 已覆盖内容 |
| --- | --- |
| `common.schema.json` | ID/UUID/时间/有限数字、来源框、像素框、`dx/dy` 相对框、资源路径、四页面状态与几何模式枚举 |
| `phone-search-region.schema.json` | 不可变范围版本、指针、预览请求/响应、保存请求/响应、读取响应、离线发布前后关系 |
| `ime-geometry.schema.json` | 握手、长轮询、快照、上报响应、会话/nonce/接触前的离线关联检查 |
| `ui-layout-template.v1.schema.json` / `device-layout-adaptation.v1.schema.json` | 完整不可变模板/适配记录入口 |
| `layout.schema.json` | 四页、锚点/目标/区域、兼容性、逐项结果、修订、颜色资源、草稿/采集/会话/预览 |
| `api.schema.json` / `routes.json` / `wire.schema.json` | 38 条布局/IME路由，精确分页、请求/成功/错误及二进制下载 |
| `package.schema.json` | manifest闭合集、导出/下载、导入预览/提交及记录 |
| `records.schema.json` / `checks.schema.json` | 严格内部记录、提交意图、逐项复核、指针/幂等/崩溃/继承关系 |
| `typed-state.sql` | 单张六列状态表DDL；15类主键/归属/JSON/CAS映射见技术附件§7.1 |
| `fixtures/examples.json` | 示例资料和服务端测试上下文；不含真实客户内容 |
| `fixtures/cases.json` | 正例及单点/组合反例，固定期待的拒绝类别 |
| `fixtures/completion.py` | 每条公共接口的可展开完整正反例及固定合成PNG/JPEG/ZIP |
| `validate.py` | 严格 Draft07、跨对象引用、合成图片/ZIP与HTTP映射检查；禁止远程 `$ref` 获取 |

原README列出的模板/适配、草稿会话、完整记录、逐项字典及导入导出缺口已由本目录补齐。
抓页复用父级 camera-binding §5.1 的 camera-source-frame 原图证据及
screen-source-observation；不新增另一套原图/摆正API。粗框不要求ready校准，
四态抓页要求本台v3校准；历史读取/导出不依赖校准。
installation_binding 保持既有 camera_binding_id 字段，新几何指纹排除全局bindingRevision。
本附件中的 `publication_case/acceptance_case/contact_case/handshake_case` 是离线测试容器，
**不是 API**，其中身份、设备空闲、相机匹配、输入认领等事实只可来自服务端可信状态。

页面判定遵循主文最新规则：先列表/聊天，再键盘展开/收起，再由当前输入区域判断空/有字。
共同标题锚点不能让四个状态互相按总分竞争，IME taskId/typing 也不能证明微信编辑器有字或为空。
本目录只验证状态编码，没有执行图像分类或匹配。

## 离线验证

在 `/Users/ye/Desktop/WX` 下运行，使用已存在的环境，不安装依赖：

```sh
.venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py
```

查看某条例子展开后的完整 JSON：

```sh
.venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --show-case draft-preserves-existing-pointer
.venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --show-case ime-hidden-state
.venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --show-case http-draft_asset_upload-request
.venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --show-case closed-package-upload
.venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --show-route-schema package_import
```

验证独立 JSON 文件：

```sh
.venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --family ime --definition snapshot --input /absolute/path/snapshot.json
.venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --family region --definition record --input /absolute/path/region.json
```

资源对象可额外传 `--assets-root /absolute/path/package` 检查实际包内文件、符号链接和摘要。
不传该参数时只检查路径/媒体类型字典，不代表文件存在、能解码或尺寸正确。
原资源路径自测文件明确不是图片，只验证字节摘要和路径；新增fixture单独生成真实合成
PNG/JPEG及ZIP，检查解码、尺寸、元数据、包总数/总量、路径/摘要及引用闭合。
`--check-package` 只读指定包，限制展开量并在内存检查，不提取到文件系统、不联网、
不读生产数据库。生产并发防竞态解包按附件规定的L07实施。
原路径自测临时文件仅在本目录下生成，完成后删除。

`$fixture`、`set`、`delete` 仅用于复用测试数据；通过 `--show-case` 展开后才是对应 JSON。
测试从不自动修复输入，不为反例重算摘要来掩盖损坏。`--digests` 只输出参考摘要，不改文件。
时间均是固定测试数据，不表示现场发生过事件。

## 严格性

- 所有报文对象拒绝未声明字段。整数拒绝 `true/false` 和 `1.0`；数字拒绝非有限值。
  `NaN`、`Infinity`、`1e999`、重复 JSON 键、控制字符、未配对代理码点均拒绝。
- 时间必须带时区；UUID、摘要均为小写固定格式；空字符串不能替代 `null`。
  相机物理 ID 可包含原始路径式字符，但只当身份字符串，不用于拼文件名。
- Draft07 不能直接表达两个字段相加、摘要一致、设备绑定或超时，
  因此完整合规必须同时通过 `validate.py` 的语义检查；仅 `Draft7Validator.is_valid` 不够。
- 摘要的参考实现为 `canonical()`：Python JSON 排序键、UTF-8、紧凑分隔、不转义 Unicode、
  禁止非有限值；资料 `sha256` 不包含自身。跨语言实现必须通过同一固定向量。
  这不是声称已实现 RFC8785；不同浮点拼写不可各用一套序列化后宣称摘要相同。
- 相对框为 `{dx,dy,width,height}`：最终左上角等于锚点左上角加 `dx/dy`，
  四个数都使用完整正向屏幕归一化单位。最终出屏及引用检查已覆盖离线场景，
  运行时仍须 L04 使用当次锚点实例。
- 人工框不接受 `roi_corners`、屏幕四角或机械坐标。检测出的 `detected_quad` 是
  完整来源图 `source_px`、屏幕边界顺时针凸四边形，不能贴人工裁剪边。

## 与主文同步的核心字段

以下核心字段及规则已同步主文 v1.9，包括忙时保存草稿、完整版本读取、按请求编号查询结果、
粗框原因闭集、单通道绑定实例及 nonce 首次接收记录不可变。

### 1. 搜索范围的像素框

第 11.11.2 节 `phone-search-region.v1` 必填：

```text
pixel_rect:{left:int>=0,top:int>=0,right:int>0,bottom:int>0}
```

它是来源图边界坐标，右/下为排他边界，允许 `right=W/bottom=H`。
左上取 floor，右下取 ceil，须与 `rect` 和 `source_size` 逐值一致，两边至少 4 像素。
保留原始归一化 `rect`，不把取整后的像素框再反写为新归一化框、重复取整。
物理绑定和 `source_geometry_id` 必须一致；分辨率变化只可预览复核，不能重算后自行发布。

资料保存归 `device_id` 对应机械臂，不归摄像头。`installation_binding` 必填
`camera_id,camera_device_unique_id,camera_binding_id,arm_binding_id`；
其中 `camera_binding_id` 为单通道服务端持久 UUID，其余为非空身份字符串。
相机可独立更换，但换走再换回也是新绑定实例；普通重启和未变绑定的保存不换实例。
现有全局 `binding_revision` 不能代替它，避免修改另一通道就使所有范围待复核。
新字段同时进入不可变版本摘要与 region_digest；这是未发布契约的修订，不允许改运行旧文件补假通过。
旧资料缺字段只读归档，显式预览/复核后生成新版本；不得把当前实例填入旧档并继续执行。
绑定实例持久化、源帧/上下文/校准接线、快速切设备及长流中途换源按主文 11.10.4 和 T31-T34，
本目录的格式自检不证明这些运行行为已经实现。

### 2. 预览与读取/写入报文版本

第 11.11.3 节明确：

| 报文 | `contract_version` |
| --- | --- |
| 不可变范围 | `phone-search-region.v1` |
| GET 响应 | `phone-search-region-read.v1` |
| preview 请求/响应 | `phone-search-region-preview.v1` |
| PUT 请求/响应 | `phone-search-region-write.v1` |
| current 指针 | `layout-pointer.v1` |

Fleet 请求/响应均带 UUID `correlation_id`；IME 专用协议按原 §11.13 字段，
不把 Fleet 的“所有请求公共字段”规则不加区分地套给 IME。
GET 不使用 JSON body：`correlation_id` 固定放 `X-Correlation-Id` UUID 请求头，
`contract_version` 固定放 `X-Layout-Contract-Version` 请求头。
POST/PUT 公共字段放 JSON body，头/体冲突值必须拒绝；此处不改变输入 GET 原契约。
本目录的 JSON 校验不代替 HTTP 请求头接线测试。

预览响应增加 `rect,pixel_rect,created_at`。`source_binding` 精确字段为：

```text
device_id,installation_binding,source_size,source_geometry_id,evidence_id,
source_session_id,frame_seq,source_captured_at,server_received_at
```

来源会话用于证据新鲜度，不混入安装绑定。`expires_at=created_at+120秒`，
服务端仍用单调时钟控制过期；此处时间字段是展示/审计，不能由客户端延长。
`region_digest` 精确覆盖 `device_id,installation_binding,source_size,source_geometry_id,rect,pixel_rect`，
不覆盖每次变化的帧号；服务端另将 preview 绑定身份、原请求摘要和新鲜来源证据。
框选用的旧 `source_evidence_id` 与预览实际新帧 `source_binding.evidence_id` 可以不同，
但设备、物理绑定、来源几何必须一致，不能把旧画面的框套给新绑定。

`verified` 预览必须有四边形且 `reason_codes=[]`；`needs_review` 为
`detected_quad=null` 且原因非空，绝不选择多个候选之一假装通过。
GET 的 `selected/pending_draft` 在本附件是 `{id,version,sha256}|null`。
`GET /v1/devices/{id}/phone-search-region/versions/{version}` 返回完整不可变版本，
前端用其中的 `rect/pixel_rect` 恢复原框，不把引用误当作全部资料。
损坏指针/已选版本缺失应报 `layout_archive_invalid`，不能映射成 revision 0。
粗框预览 `reason_codes` 和粗框 GET 的 `reasons` 使用本地闭集：

```text
phone_not_located / multiple_screen_candidates / screen_outside_region /
dark_screen / occluded / source_unavailable / source_binding_changed /
geometry_changed / archive_invalid
```

闭集只定义在 `phone-search-region.schema.json`，不修改 `common.reason_codes`，
更不能套到 Fleet/IME 通用错误码。底层诊断须映射为上述值；
旧别名、未知码和一般权限/会话错误不能混入粗框原因数组。

### 3. 草稿与发布

`publish=false` 只保存新的不可变版本及草稿记录，`current.json` 全字段不变，
响应 `status=draft_saved`，`revision/selected_version` 返回仍生效的旧值。
因此可以同时显示“旧框可用”和“新草稿待复核”；不能用一个状态覆盖两者。
`publish=true` 须新鲜 verified 预览、设备空闲、CAS 成功，指针 revision 恰好加一，
指向此次保存的版本/摘要，响应 `status=verified`。

版本分配必须在同设备锁中以所有已保存版本最大值递增，**不能以指针 revision 分配版本**，
因为未发布草稿不增加指针 revision。本附件发布场景仅检查前后关系；
版本号未被历史草稿占用、幂等意图、崩溃对账须由真实 Store 实现和测试。
从历史草稿继续编辑或重新复核发布，保留原草稿，按新来源证据保存新版本，不改旧文件。
同请求编号重放原结果，不重走本附件的“新发布”场景，不再次分配版本。
未知回包时按原请求编号调用
`GET /v1/layout-operations/by-request/{client_request_id}`；
无需先拿到 operation_id，404 也不能证明写入未提交。

主文与附件均固定：草稿编辑及保存零动作，可在忙时保存；
发布/解除当前阻断必须空闲。忙时草稿不得切指针或解除问题。

### 4. IME 握手、长轮询和 seq

第 11.13.2 长轮询必填 `runtime_session_id`：

```text
GET .../geometry/requests/next?waitSeconds=20&runtime_session_id=<当前会话UUID>
```

鉴权及会话核对必须在领取 nonce **之前**完成；旧客户端不能把新实例的只读请求取走。
query 中 `waitSeconds` 规范化为严格整数 0..20，无请求响应 204、无 JSON 正文。
不能把空 `request_nonce` 或 `runtime_session_id` 用来表示 204。

`seq` 为当前 `runtime_session_id` 下全局严格递增序号，跨显示/隐藏/重建 View 不清零；
只有获得新的 runtime_session_id 才能从 1 开始。每个新 View ID 永不复用，
服务端保留本会话已退役 View ID 集合；旧 View 即使携带更大 seq 也不可复活。
同 seq 同规范化 JSON 摘要只重放原回执，**不得刷新 server_received_at、有效期或 nonce**。
服务端首次接受后保存不可变 `receipt`：

```text
runtime_session_id,view_instance_id,seq,snapshot_sha256,
server_received_at,received_monotonic_ms,
request_nonce,nonce_issued_at_ms,nonce_expires_at_ms
```

事件的三个 nonce 字段均为 null；请求式快照的期限为最初发出时间加 2000ms。
同 seq 重放时，`first_receipt` 与 `receipt_after` 必须全字段相同，
响应的 `server_received_at` 必须仍等于首次值，当前处理时间另存，不得覆盖首次值。
`contact_case` 不接受独立 `received_at_ms`，只从首次 receipt 计算快照年龄，
并交叉核对仍在服务端保存的原 nonce 发出时间。首次 receipt 缺失即拒绝重放，
不能把重放伪装成首次报告重新建一条有效记录。
反例覆盖：10500ms 首次接收，11600ms 收到相同 seq/摘要，重放回执可以返回，
但最后点击仍因年龄 1100ms 被拒绝；修改单调接收时间、墙上接收时间、
回执时间、nonce 发出时间或截止时间均单独拒绝。
服务重启失效所有会话、最高序号、nonce 和几何；客户端重握手获得新的 UUID。
同 client_instance_id 的幂等承诺仅限当前服务实例的未撤销会话。
已被新客户端替换的旧 client_instance_id 再来握手必须拒绝，不重新成为当前客户端。

客户端真实 View 尚未创建时，不伪造 View UUID 或用空值上报；
服务端为“无当前几何”，快照获取不可用。隐藏/销毁保留最近真实 View UUID 上报失效，
不强制清空 current_task_id，因为隐藏键盘不等于撤销待输入任务。

### 5. IME 有效性、nonce 和接触

快照四类测试状态：活动且几何有效、活动但布局失效、隐藏、销毁。
后两者必须 `keyboard_active=false,geometry_valid=false`；
任意 `geometry_valid=false` 都须键盘 bounds 为 null，所有目标 bounds 为 null、
visible=false、clickable=false。`enabled` 可反映原 View 属性，不据此恢复点击。
有效目标不得越出显示屏或键盘 bounds，clickable 还要求 visible 与 enabled 均为 true。

`reason=snapshot` 当且仅当 nonce 是非空 UUID；事件上报 nonce 必须为 null。
服务端 nonce 绑定设备、操作、当前会话和当前 View，首次答案最多等 1 秒，
总有效期最多 2 秒；最后接触时快照接收年龄最多 1 秒且仍是最高有效 seq。
新的 View/隐藏/失效事件先作废旧快照和 nonce，不能只更新某个按钮。
重复回执不能延寿；nonce 完成其一次操作后消费/失效，不能授权第二次点击。
接触前仍要当前相机/姿态、输入最后认领、安全区和原动作门禁。

空 taskId（null）允许几何预览和符合其它门禁的删除定位；
候选点击则必须与 Fleet owner 的非空 IME taskId 完全相同。
“几何有效”不等于输入已获许可、消息已发送，也不等于编辑器非空。

## 已核对但不扩大实施范围

`drivers/phone_localization.py::locate_phone_quality` 当前 `roi_corners` 分支是
`calibrated_roi_only`，不是人工框内主动检测；附件明确禁止混用。
当前 `adapters/screen_ime/routes.py/service.py` 没有本附件的几何接口；
项目 Android 服务存在键盘生命周期与候选/删除 View 位置，不能据此宣称几何协议已部署。
已看到主文补入必填 `keyboard_geometry_mode`、`dx/dy` 以及分层页面分类，三处不再报“遗漏”。

校验通过说明本目录机器格式、完整路由、合成资源与离线关系自洽。
没有执行真实发布、fsync 中断、幂等并发、相机检测、Android View、机械动作或部署验收；
这些是后续实施验收，不再列作未定义设计。
