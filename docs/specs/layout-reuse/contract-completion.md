# 第 11 节完整契约技术附件

本附件从属于 `../../2026-10-03-输入开关与校准直驱上下滑动修改规格.md` v1.9
第 11 节，不是第二份主规格。D01-D08 不变。这里把既定行为转录为字段、入口、
存储和离线反例；不声称功能已部署，不增加 NAS、后台发送、自动触屏或逐按钮实触。
与主文发生行为冲突时以第 11 节为准；第 12 节逐条列出应由主线程同步的技术补充。

## 1. 文件与使用

| 文件 | 权威范围 |
| --- | --- |
| `common.schema.json` | 沿用 ID、UUID、时间、摘要、坐标、安装绑定 |
| `phone-search-region.schema.json` / `ime-geometry.schema.json` | 沿用粗框、指针及 IME 已有机器契约 |
| `ui-layout-template.v1.schema.json` | 共享模板完整记录入口，引用 `layout:template` |
| `device-layout-adaptation.v1.schema.json` | 单台适配完整记录入口，引用 `layout:adaptation` |
| `layout.schema.json` | 模板/适配、兼容性、逐页逐项结果、资源、草稿、采集、会话、预览内部记录 |
| `api.schema.json` | 各路径的封闭业务字段和固定错误字典 |
| `package.schema.json` | 导入导出请求/结果、manifest、导入预览和导出记录 |
| `records.schema.json` | 问题、核对、操作、提交意图、资源索引和分页快照 |
| `typed-state.sql` | 第7.1节唯一新增状态表的六列SQLite DDL；不修改原review表 |
| `routes.json` | 38 条布局/IME接口的 method/path、逐个路径参数、query、body、版本及响应映射 |
| `wire.schema.json` | 根据以上文件机械展开的完整 HTTP request/response/error schema |
| `checks.schema.json` | 服务端可信上下文的离线关联场景，不是可提交 API |
| `fixtures/completion.py` | 固定合成图片/ZIP 和完整接口示例；原有 JSON fixtures 保留 |
| `validate.py` | 唯一离线入口，结构、语义、引用、图片/ZIP 及反例校验 |

下文 `layout:template` 表示 `layout.schema.json#/definitions/template`，其他 family 同理。
所有 schema 是 Draft07；所有引用只在显式本地 Registry 中解析，不联网、不动态读取任意文件。
`wire.schema.json` 是派生文件，不手改；修改路由/业务字段后运行 `--write-wire-schema`。
日常自检会比较派生文件与输入，过期即失败。

```sh
env PYTHONDONTWRITEBYTECODE=1 .venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py
env PYTHONDONTWRITEBYTECODE=1 .venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --show-case http-draft_asset_upload-request
env PYTHONDONTWRITEBYTECODE=1 .venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --show-case http-package_import_preview-request
env PYTHONDONTWRITEBYTECODE=1 .venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --show-route-schema session_page
env PYTHONDONTWRITEBYTECODE=1 .venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --family wire --definition adaptation_publish_request --input /absolute/path/request.json
env PYTHONDONTWRITEBYTECODE=1 .venv-wx/bin/python arm_fleet/docs/specs/layout-reuse/validate.py --check-package /absolute/path/test-package.zip
```

每个 route_id 都有 `http-<route_id>-request/response` 正例及
`http-<route_id>-error-<code>` 错误响应正例，还有拒绝多余 query/字段及缺字段的反例。
`--show-case` 输出普通完整 JSON，没有占位 dict、生成指令或 `$fixture`。
示例生成器只生成固定正例；反例在其后修改，绝不为损坏对象重算资料摘要。
ZIP 攻击例仅重算上传运输摘要，使校验能继续检查包内坏数据，不修复包内 manifest。
合成图是棋盘纹理，没有客户数据；生成/解码/解压验证都仅在内存，不运行应用服务。

## 2. 所有接口共同约定

`wire:<route_id>_request` 精确结构是
`{route_id,method,path,headers,query,body}`，用于网关归一化后的契约测试，不是要求客户端发送
这个外层 JSON。`path` 为已解析的各占位参数；真正 URL 见第 3 节。
`wire:<route_id>_response/error` 是 `{route_id,status,headers,body}`。
真实 HTTP 的 status/headers/body 分别映射，不能把外层测试容器当业务 body。

Fleet GET 的两个版本/关联头必填、无 JSON body，fixture 中 body=null。
POST/PUT 的 `contract_version/correlation_id` 在 JSON body；可同时给同值头，
不同值 422。应用级请求头在 schema 中封闭；Authorization、Cookie、Host 等由既有认证/
HTTP 栈处理，不成为业务字段，也不因此禁用标准网络头。POST/PUT 要求 application/json。
所有 JSON 成功响应都有版本和 UUID correlation_id，历史完整版本用 `{record:完整记录}`
包装，绝不把 correlation_id 加入不可变资料摘要。IME 保留专用无 correlation 协议。

时间是带时区 RFC3339，生成值使用 UTC `Z`，精度最多微秒；比较须先解析时区。
期限统一左闭右开，页证据最大年龄 900 秒和 IME 既有 1000/2000ms 上界仍按主文允许等号。
客户端不能提交作者、生成时间、状态、摘要结果、可信身份或校准通过。
未测值用明确 null，不使用空串、0、`unknown/default` 字符串或缺字段代替。
字符串大小写不隐式折叠，版本不允许 `"latest"`，严格整数不接受布尔值和 `1.0`。
资产数组/结果数组必须以 ID 或复合标识唯一；四页数组固定列表、收起、空、有字的顺序。
数组不是任意字典。超上限拒绝，不截断后保存。

资源下载成功为原始 PNG/JPEG/ZIP 字节，**不是 JSON/base64 API**；
fixture 中 body 是这些原字节的标准带 padding base64，校验器再检查真实字节。
响应 Content-Length 为真实字节数，ETag 为带双引号的字节 SHA-256，Cache-Control=no-store。
图片 Content-Disposition=inline；ZIP 为 `attachment; filename=layout-package.zip`。
无任意磁盘路径、远程 URL、重定向或 Range 支持。非法 Range 属请求无效，不静默截片。
JSON 请求中的资源上传确实采用标准 base64，无 Data URL 前缀和空白；解码前先限制 HTTP body
为 280MiB，解码后及资源/包展开后仍分别限制 200MiB。失败请求不留可选用半资源。

Fleet 错误严格为 `api:error`，协议 `layout-error.v1`：
422 request_invalid，403 permission_denied，404 resource_missing；
409 revision_conflict/request_conflict/binding_changed/device_busy/preview_expired/items_unverified；
503 source_unavailable/archive_invalid/storage_failed，均加 `layout_` 前缀。
必填 `correlation_id,operation_id:null|UUID,code,message,motion_started=false,retryable=false`。
message 必须有中文业务说明；无任意 details。无法解析关联 ID 时由服务器生成响应 ID。
IME 错误 `api:ime_error`：401 unauthenticated、403 forbidden、409 session_stale/
sequence_conflict/nonce_expired、422 invalid、503 unavailable，前缀 `ime_geometry_`。
`wire` 对每个路径绑定错误 schema，校验器进一步检查 code 与 HTTP status 对应。

既有查看权限可读共享模板；草稿/会话/预览/上传/原图还必须具有设备权限及原创建身份，
禁止用户自称 operator。所有写入、取证、资料核对及导出导入需要既有人工调试权限；
原图访问沿用证据隐私权限。导出必须对闭合集每一资源逐一授权，不能只检查根资源。
跨身份查询操作/预览返回403且不泄露其结果；缺身份 Fleet 按现有入口拒绝并映射403。

## 3. 完整路由映射

下表的请求/成功列为封闭业务字段 schema；公共字段/头/路径/query 全部已经展开在
`wire:<编号>_request/response`，错误为 `wire:<编号>_error`。
无 body 的请求以 `-` 表示；默认 query 为严格空对象，只有两个列表及 IME poll 例外。
全部成功 HTTP 200，IME poll 无请求为204且无 body/Content-Type。

| 编号 | method + 路径 | 请求业务字段 | 成功业务字段 |
| --- | --- | --- | --- |
| region_get | GET `/v1/devices/{device_id}/phone-search-region` | - | region:get_response |
| region_preview | POST `/v1/devices/{device_id}/phone-search-region/preview` | region:preview_request | region:preview_response |
| region_save | PUT `/v1/devices/{device_id}/phone-search-region` | region:save_request | region:save_response |
| region_version | GET `/v1/devices/{device_id}/phone-search-region/versions/{version}` | - | api:region_read |
| template_list | GET `/v1/layout-templates` | query api:pagination | api:template_list |
| template_version | GET `/v1/layout-templates/{template_id}/versions/{version}` | - | api:template_read |
| template_asset | GET `/v1/layout-templates/{template_id}/versions/{version}/assets/{asset_id}` | - | PNG/JPEG |
| template_publish | POST `/v1/layout-templates` | api:template_publish | api:template_published |
| draft_create | POST `/v1/layout-template-drafts` | api:draft_create | api:draft_created |
| draft_get | GET `/v1/layout-template-drafts/{draft_id}` | - | api:draft_read |
| draft_metadata | PUT `/v1/layout-template-drafts/{draft_id}/metadata` | api:draft_metadata | api:draft_updated |
| draft_page | PUT `/v1/layout-template-drafts/{draft_id}/pages/{state}` | api:draft_page_write | api:draft_page_written |
| draft_asset_upload | POST `/v1/layout-template-drafts/{draft_id}/assets` | api:asset_upload | api:asset_uploaded |
| draft_asset_get | GET `/v1/layout-template-drafts/{draft_id}/assets/{asset_id}` | - | PNG/JPEG |
| capture | POST `/v1/devices/{device_id}/layout-captures` | api:capture_request | api:capture_response |
| evidence_get | GET `/v1/devices/{device_id}/layout-evidence/{evidence_id}` | - | api:evidence_read |
| session_create | POST `/v1/devices/{device_id}/layout-adaptation/sessions` | api:session_create | api:session_created |
| session_get | GET `/v1/devices/{device_id}/layout-adaptation/sessions/{session_id}` | - | api:session_read |
| session_page | PUT `/v1/devices/{device_id}/layout-adaptation/sessions/{session_id}/pages/{state}` | api:session_page_write | api:session_page_written |
| adaptation_preview | POST `/v1/devices/{device_id}/layout-adaptation/preview` | api:adaptation_preview | api:adaptation_previewed |
| adaptation_publish | PUT `/v1/devices/{device_id}/layout-adaptation` | api:adaptation_publish | api:adaptation_published |
| adaptation_get | GET `/v1/devices/{device_id}/layout-adaptation` | - | api:adaptation_current |
| adaptation_version | GET `/v1/devices/{device_id}/layout-adaptation/versions/{version}` | - | api:adaptation_read |
| adaptation_asset | GET `/v1/devices/{device_id}/layout-adaptation/versions/{version}/assets/{asset_id}` | - | PNG/JPEG |
| issue_list | GET `/v1/devices/{device_id}/layout-review/issues` | query api:pagination | api:issue_list |
| review | POST `/v1/devices/{device_id}/layout-review` | api:review_request | api:review_response |
| operation_request | GET `/v1/layout-operations/by-request/{client_request_id}` | - | api:operation_read |
| operation_get | GET `/v1/layout-operations/{operation_id}` | - | api:operation_read |
| package_export | POST `/v1/layout-packages/export` | package:export_request | package:export_response |
| package_get | GET `/v1/layout-packages/{package_id}` | - | package:export_response |
| package_download | GET `/v1/layout-packages/{package_id}/download` | - | ZIP |
| package_import_preview | POST `/v1/layout-packages/import/preview` | package:import_preview_request | package:import_preview_response |
| package_import | POST `/v1/layout-packages/import` | package:import_request | package:import_response |
| rollback_preview | POST `/v1/devices/{device_id}/layout-rollback/preview` | api:rollback_preview | api:rollback_previewed |
| rollback | PUT `/v1/devices/{device_id}/layout-rollback` | api:rollback_commit | api:rollback_committed |
| ime_session | POST `/api/v1/devices/{ime_device_id}/geometry/sessions` | ime:handshake_request | ime:handshake_response |
| ime_report | POST `/api/v1/devices/{ime_device_id}/geometry` | ime:snapshot | ime:report_response |
| ime_poll | GET `/api/v1/devices/{ime_device_id}/geometry/requests/next` | query ime:poll_query | ime:poll_response / 204 |

版本精确值在 `routes.json`，不存在由调用方选择版本的分支：
粗框沿用原三组版本；模板列表 `ui-layout-list.v1`、历史读取 `ui-layout-template-read.v1`、
草稿/发布 `ui-layout-edit.v1`；适配编辑 `device-layout-edit.v1`、读取 `device-layout-read.v1`；
另有 `layout-capture.v1/layout-evidence.v1/layout-review.v1/layout-operation.v1/
layout-package.v1/layout-rollback.v1`。这些只是已有第 11 节报文的稳定版本编号。

父级的 **GET `/v1/devices/{id}/camera-source-frame`** 返回
`camera-source-frame.v2` 的 `{contract_version,evidence_id,jpeg_sha256,source_frame}`。
原JPEG只通过 **GET `/v1/devices/{id}/camera-source-frame/{evidence_id}/image`** 读取，
按指定ID与jpeg_sha256核对原字节，而不是另取“当前图”。有校准的四态采集复用
**GET `/v1/devices/{id}/screen-source-observation?source_evidence_id=...`**，
返回 `screen-source-observation.v2` 的 `{contract_version,source,coordinate_frame_hash,observation}`；
必须核对source是原指定证据，coordinate_frame_hash沿用WX当前坐标域的既定计算并绑定
该次观察，不能与calibration_geometry_hash混用；后者另从本台v3 bundle读取和核对。
这三条的request/response/error/schema由camera-binding附件§5.1唯一维护，
不重复列为布局API。本附件capture是会话归属、两帧组合和逐页归档入口，
evidence_get只提供布局归属/证明元数据，不提供另一条原图/摆正识别能力。

## 4. 分页不是任意参数

仅 template_list、issue_list 使用 `api:pagination`：
`limit` 必填、严格整数1..100；`cursor` 第一页在 HTTP query 中省略，归一化为null，
后续为服务端随机 UUID。其他 query 一律拒绝；不提供未指定的排序、搜索、offset 或过滤。
URL 数值只接受十进制数字，不接受正号、指数、浮点、重复参数或空串。

模板列表每行是一份不可变版本，排序 `(template_id ASCII升序,version数值降序)`。
问题列表包含本设备 open/resolved，排序 `(opened_at UTC升序,issue_id ASCII升序)`。
第一次读取建立授权范围内的不可变结果快照，期限900秒；
`records:cursor` 保存原身份、route_id、device_id、limit、下个offset、完整投影和原截止时间。
后续页不重新查询活表，不因新增模板或问题解决漏项/重复。
每页响应回传 `limit,snapshot_at,snapshot_expires_at,next_cursor`，最后一页 next_cursor=null，
空第一页 `items=[]` 且 next_cursor=null。issue_list 另回传快照时的设备 review revision。
服务重启可以恢复游标的持久快照；超过墙钟截止或墙钟回退到 snapshot_at 前直接过期。
权限撤销、不同身份/设备/route/limit 不允许续页；过期409 preview_expired，非法UUID422，
不存在404。重复获取同一游标返回同页及同一 next_cursor，不能推进服务端可变offset。
创建下一游标时固定offset和原期限并持久保存，不把分页当活动续租。

## 5. 模板、草稿及资产闭环

compatibility 的所有键和 sources 均必填。草稿可 `value=null,source=null`；
有值 source 只能 reported/operator，人工只能补可靠来源未报告的字段。
不能用 operator 覆盖与当前可靠报告冲突的值；不一致为409 binding_changed。
平台/方向固定 android/portrait，完整模板和可发布适配禁止任何未知兼容字段。
display_scale 等可移植标识使用规范字符串，`"1.0"` 不是数值1；
reported 值来自协议，operator 值为人工确认的原档位标识，不猜默认值。
来源差异不等于布局差异：兼容比较比较值，不要求两台的来源标注相同。

`reference_screen_descriptor` 是完整正向屏幕，无机械/设备身份；
本台 `screen_descriptor.calibration_geometry_hash` 引用 camera-binding 附件的 **v3 几何指纹**。
v3 bundle 含 `schemaVersion=3,cameraBindingId,sourceGeometryId`，hash 排除全局 bindingRevision。
缺 camera_binding_id 的旧范围、旧v2校准不补当前身份伪升级。普通重启保留已发布资料。
读取/导出先校验完整性，不要求 ready 机械校准；只有新的四态抓页要求本台有效v3校准。

草稿创建固定设备、原身份、绑定、屏幕描述和已知兼容性，revision=1。
有 base_template_ref 时复制定义/资源到独立草稿，四页历史通过结果清为未采集，
旧模板不变；无base时四个 `draft_page` 都存在，definition/page_check/updated_at=null，
source_evidence_ids=[]，missing_items 列出本态全部最低标识。草稿无自动过期/隐式删除。
`metadata` 修改兼容字段后 revision+1；若兼容值变化，四页保留定义、清复核证据/结果，
missing_items 恢复未核验项，原证据作为审计保留，不能继承旧“通过”。
仅改来源注释且值不变不刷新证据时间。

页面 PUT 是单页完整替换，不是整套模板替换；path.state 必须等于 definition.state。
最低项按主文：列表含 wechat_chats_tab、unread/content；聊天含 back_button/input_box、
title/input/content；展开页有 keyboard_boundary 锚点；有字页另有 send_button 和
send_button_feature 局部锚点。每页至少两个分离且 required=true 的稳定锚点。
title/input/content 互不重叠；foreground/background 可与所属采样区域相交，不据此当点击区。
锚点 ID 页内唯一，target/region ID 各自在本类型页内唯一；跨页同名不视为同一项。
所有相对框最终仍在完整屏幕内，search_region 必须包含 reference_rect。

图像域固定为本台 v3 摆正后的完整屏幕，不是相机原图、粗框裁图或浏览器缩略图。
局部锚点资产按 `reference_rect` 在本台完整屏幕上的实际像素跨度缩放到一种确定尺寸：
左上 floor，右下 ceil（右下排他），宽高取两端之差，不能分别 round 宽高。
只消除恰好落在整数像素边界时不超过 `1e-9` 像素的浮点表示误差。
空输入参考保存、颜色采样及运行时区域提取沿用同一像素规则，不允许各自取整。
再在该屏幕域的 `search_region` 内匹配；匹配结果加回局部搜索偏移，返回完整屏幕归一化框。
不得扩大搜索区、降低 0.90/0.08 阈值或试多个尺寸后只取最高分来宣称第二台兼容。
不同长宽比、字体、导航栏或版本布局仍须实页验证；尺寸换算通过不代表页面兼容。
非法、空、越界的框拒绝，不静默裁断后把剩余部分当完整证据；低纹理资产不能借紧裁剪获得满分。

上传顺序固定：创建草稿 -> 采集受控页 -> 在本地预览裁出无隐私稳定局部图 ->
POST drafts/{id}/assets -> 页面 PUT 引用返回的 asset.id -> 四页齐全后发布。
上传必填 client_request_id、expected_revision、完整asset、content_base64、
source_evidence_ids、privacy_confirmed=true。手工导入的无隐私局部图可source_evidence_ids=[]，
不是伪造来源证据；页面通过仍须本台服务端采集的两帧。
服务器检查字节摘要、真实PNG/JPEG、单帧、尺寸、最大8192边/2000万像素、
拒绝EXIF/ICC/文字备注等隐私元数据；不以文件扩展名当图像有效。
`privacy_confirmed` 是受认证人工对所见受控图的确认，服务端记录真实principal和时间，
不是客户端可填 reviewed_by。图像检测不可能独自证明没有客户正文；不得把标签当匿名化算法。
参考块或实际命中块标准差<8或平均亮度<20均拒绝作为锚点，不把近空白画面当通过。
0.90 得分、0.08 分差使用原始测量值判定；四舍五入后的展示值不得把未达标变成达标。
通过项必须具有真实 `matched_rect`，非法或缺失的搜索框不得默认扩大为全屏。

资源先临时写/校验/fsync，再原子提交草稿revision及asset_record。
重复asset ID且字节/metadata完全一致可复用；不同内容409 request_conflict，不原地替换。
修正图用新asset ID并更新引用；发布只复制实际被锚点引用的资产，其余留在草稿不导出。
每个版本的相对路径独立，同path不同字节在同版本中绝不允许。
发布 CAS 同时核对 expected_draft_revision 与 expected_parent_version，
父版本必须是该template_id当前最高已提交版本；首版为null。
不同模板ID派生允许base来自另一模板，expected_parent仍按目的template_id计算。
所有最低项/资源/兼容信息/隐私复核齐全，四页模板检查均通过，才发布不可变模板。
不需要四页仍在15分钟内：这里确认的是持久模板定义，不是本台适配执行资格。
模板发布不改任何设备current，不复制手机ID/机械参数/历史发送结果。

新模板默认 live_ime_v1；此模式的两个 IME 目标由服务端加入展开页逐项结果，
不要求在模板内手画候选行。选择 legacy_registered 必须模板里同时有该页两个已登记
tap目标；不在运行时自动切模式。local_overrides 不得修改 live 模式的两个 IME目标。
anchor 覆盖的anchor_id必须等于自身ID，relative_rect相对其模板原reference_rect，
不递归引用修订结果；target/region覆盖相对最终定位的同页锚点，不能改mode/purpose。

## 6. 来源、采集与逐页状态

`capture` 必填 `owner_kind,owner_id,expected_owner_revision,page_state`：
search_region时 owner_id/page_state=null、revision=0、采一帧；draft/session时owner UUID和
正revision必填、page_state为四态之一、自动采两帧。设备由path决定，owner必须属于它。
所有采集零机械/输入/发送动作；设备忙不禁止只读取帧，但必须足够清晰且满足来源校验。
source不可用503，校准不满足四态要求409 items_unverified；不把校准要求施加到粗框。
capture成功返回完整capture及source_evidence_ids可从evidence数组取得；evidence_id直接复用
父级source_frame_view.evidence_id，不再分配第二个原图ID；查看图使用父级image入口。
原始证据不随共享模板或包输出；服务器使用本台摆正图匹配，保留原图/来源证明不可覆盖。

首次草稿登记和后续适配必须使用同一摆正/原图核验链：检查设备、物理相机、绑定、
来源几何、实际解码尺寸及原字节摘要，并以**当前生效指针**选择粗框，不取最大历史版本。
登记、适配和运行均先按 v3 `screen.edgeModel.corners` 定位当前四边并执行既有严格
姿态拟合，再补偿到已校准相机域，最后使用 `cameraToScreen` 摆正到完整屏幕。
缺少可信边界、无法定位、超过既有补偿范围或非刚性形变均拒绝；不得让登记直接用
原图而运行先补偿。这里不调整 H01 自动校准采样的“超限仅提示”既定政策，
也不扩大普通视觉证据的姿态容忍。
完整屏幕反投影必须在原图及生效粗框内；投影穿过无穷远、原图外填充或裁断都拒绝。
粗框只限制可用来源范围，不取代 v3 四边/矩阵或授予动作资格。
两帧必须分别摆正并分别匹配，合并结果取通过项交集，不能只检测一帧却列出两个证据编号。
任一帧缺锚点、低分或歧义时阻断该锚点及依赖项；两帧定位不稳定时不生成通过事实。

对父级来源证明的映射固定：

| 布局字段 | 父级来源 |
| --- | --- |
| installation_binding.camera_binding_id | camera_binding_id，单通道持久UUID |
| source_geometry_id / source_size | source_geometry_id / 解码原图width,height |
| source_binding.source_session_id | source_generation_id原UUID文本，不用daemon会话 |
| source_binding.frame_seq | source_frame.source_frame_seq原值，不是source_capture_seq；不得用HTTP查询次数替代，序号0等待来源推进后才进入既有正整数范围记录 |
| source_captured_at / server_received_at | source_frame.source_captured_at / source_frame.source_received_at，不用浏览器/归档时刻 |
| timing_proof | 原source_clock_id、clock_registration_id、source_generation_id、source_frame_seq、source_capture_seq、采样ns、不确定度及固定provenance |
| captured_monotonic_ms | 可信source_captured_monotonic_ns整除1000000的派生展示值，不是新计时器 |
| received_monotonic_ms / server_instance_id | 本次归档服务的首次接收单调时刻 / Fleet启动UUID |
| image_sha256 / media_type | source_frame_view.jpeg_sha256 / 固定image/jpeg |

clock_registration_id由父级受控时钟登记关联得到，不允许上传方仅凭自称注册ID过门禁。
布局保存的是证明投影，不重新定义clock/session签发；父级证明无效即拒绝。
同页两帧物理绑定、geometry、generation、clock一致、source_frame_seq和source_capture_seq均严格推进；
最坏采样间隔 `Δns/1e6 - uncertainty1 - uncertainty2 >=200ms`。
每帧最坏年龄 `接收单调ms-采样ns/1e6+uncertainty <=1000ms`，并继续满足父级/现有gate更严的限制。
墙钟仅供展示，不证明新鲜度。真正用来匹配的图不得更换字节而保留旧image_sha256。
两张相同图像内容允许，两个相同来源采样号不允许。

capture最多120秒内第一次交给页面PUT；先在采集时通过来源门禁，再在PUT时验证归属、
owner revision及未消费状态。每个capture只绑定一次页面编辑请求；
同client_request_id重放返回原结果，不重新采图或刷新时间。
需变更页面定义/局部修订时发新的capture；不能换请求编号重复使用旧capture给页续期。
页面首次保存的accepted_at为该capture首次归档时间，不采用PUT/重放处理时间；
capture_id的首次accepted_at/单调值是不可变记录。草稿相同约束。

session创建：固定template_ref、当前installation_region_ref、绑定/手机/兼容/屏幕、
keyboard_geometry_mode与expected_pointer_revision，30分钟，revision=1，四页capture=null。
请求显式 `inherit_session_id:null|UUID,inherit_states:[]|指定态`；
只有同身份/同配置/同服务实例且仍在15分钟内的核验页可逐页原样继承，
包括capture_id、accepted_at、原证据和颜色样本，不刷新年龄。
会话过期不自动丢草稿；普通查询/重放不续期。服务重启未发布session/preview失效，
历史已发布适配不失效，不要求用户普通重启后重新四页确认。

页面PUT只更新指定页，session revision+1，其余页按字节内容保留。
`session_page.capture=null` 唯一表示未采集；实际失败页保留check和证据，不回退成null。
session_get另外按evaluated_at计算四个page_status：not_captured/passed/failed/unsupported/
expired/binding_changed，每页带原expires_at和闭集原因。到期页不修改历史check。
颜色在每页摆正后的foreground/background区域分别逐通道采中位RGB。
若任一目的区域缺失，固定取本页按ID升序最前两个已通过必需锚点的实际矩形像素并集，
用OpenCV `COLOR_RGB2GRAY` 后 `THRESH_BINARY|THRESH_OTSU` 分成暗/亮两类，
暗类作为foreground、亮类作为background；每类至少16像素且灰度中位数之差>=8。
不足时本页reason=low_texture，不猜RGB，也不去真实消息区扩大采样。
hidden/empty页另保存受控空白输入区域参考图；list/text的empty_input_asset_ref必须null，
不能拿有字图当空白参考。session.assets保存实际本机无隐私裁图及摘要/来源；
发布时复制到本台适配版本目录并保存records:adaptation_asset索引。
同字节可去重存储但每页color_sample/evidence_ids保留各自真实取样来源。

preview要求四页均已采集且每页accepted_at距当前<=900秒、会话未过期，配置不变，
再取一张当前新鲜原图验证当前手机/布局；不要求四个页面同时出现在当前画面。
有效期120秒，绑定身份、设备、完整请求摘要、session revision和配置摘要。
失败/unsupported页照实返回完整check与失败项；不是全组清空。
页面pass不能掩盖任意失败项。anchor结果候选为0时矩形/分数/误差均null；
一个候选runner_up=null，多个候选必须有第二名，passed要求>=0.90且分差>=0.08。

accepted_items按 `(page_state,item_kind,item_id)` 排序且必须恰好等于预览中全部实际通过项；
不允许省略某通过项制造另一配置，也不允许选入失败/跨页同名项。
一次确认发布四页记录，保留失败项并为其开问题：部分失败投影needs_review，
仅阻断依赖失败项的动作；全部通过才是verified。
“全部必需项通过再整体确认”中的通过条件应用到每个欲启用项目及其必需锚点，
不能用这句话让一个键盘失败项擦掉已通过的纯列表导航项目，仍遵守主文T09/T22/T30。
本台保存必须空闲、CAS expected_revision等于session固定指针revision且等于当前值。
同preview只能消费一次；不同请求编号再消费409 request_conflict。

tap的safe_rect使用主文公式，误差来自本台v3映射与所属锚点本次定位；
IME真实display bounds无额外视觉定位偏移时定位误差取0，仍加本台映射误差，
且必须通过主文当前IME/相机门禁。hover_only/region不产生safe_rect；
failed/unsupported不产生可执行safe_rect。导入历史safe_rect从不直接作为当次落点。

实施补充（2026-10-08 本地，未部署）：本台映射误差读取同一有效 v3 的
`xy.maxResidualPx + camera.cameraToScreen.maxErrorPx`，两个分量须有
`validated=true` 且为有限非负数；不把 RMS、缺失值或默认阈值充当实测上界。
定位误差不先舍入；两帧合并后按合并定位误差重新内缩，不继承第一帧安全框。
误差求和按 `Decimal(str(value))` 的精确数值语义计算，返回浮点时不得低于该上界，
不能让刚超过整数的和被舍入到整数后少内缩1px。两帧3px位移门只在恢复已知
离散像素坐标时吸收最多1e-9像素的表示噪声，不给安全误差减epsilon。
缺测为 `mapping_error_unknown`，内缩为空为 `safe_rect_empty`，只将该目标判失败，
其余逐项结果保留。元数据与运行读取还要检查目标结果唯一性和安全框与当前误差一致。
这些是视觉资料检查，不生成机械坐标、输入许可或真实点击回执。
已发布记录同时核验锚点结果覆盖/唯一性/依赖状态；不以同名后项覆盖前项误差。
region、failed、unsupported 项有安全框属于矛盾记录，不能显示为可用。

运行态 empty/text 可能共享相同标题/键盘锚点，不能只按四页总分判歧义或猜状态：
使用本次同一摆正帧的 input 用途区域与本台 empty 参考按原像素规则比对，
并读取该实际裁图的 OCR。视觉空白且 OCR 无字才选 empty；
视觉有变化且 OCR 有字、总体及文字框置信度均至少 0.90，且本态
`send_button_feature` 通过，才选 text。仅发生像素变化、低置信度、文字/像素冲突、
尺寸不支持或其他页面仍歧义时明确未知。识别后复用同一帧同一裁图的 OCR，
不重新取帧、不用 IME 候选/typing 状态替代微信输入框事实。
一旦存在可靠键盘/输入证据，但对应键盘态没有完整通过（例如有字却缺发送特征），
不得把被否定的 empty 候选丢掉后改选列表或收起页；其他页高分不消除该冲突。
此补充不证明真实布局样本已达到主文的现场放行数量。

## 7. 当前状态、逐项核对与内部存储

当前适配GET分别给 `status/page_checks/blocking_reasons` 与
`execution_blocked/execution_blocking_reasons`，不混为一个状态。
无current、无session为unregistered；只有草稿为draft；全项通过且当前绑定相符为verified；
局部失败/绑定变化为needs_review。损坏指针或其记录直接503 archive_invalid；
可读记录但关联资源被确认损坏时投影invalid，并保留selected_ref。
校准丢失只加execution_blocked，不把可读资料清成未登记。
机械/输入/发送未知不属于资料问题，资料复核接口无法解除这些门禁。

`issue_list.revision` 是本设备**资料问题集合revision**，不是适配/范围current revision；
新开/解决问题都使其加一，存 `records:review_revision`。
`review.expected_revision` CAS这个值。各issue另有自己的revision。
POST review验证issue_ids全部属于本设备、请求时仍open且版本仍相符，
新证据必须来自同设备/当前绑定，证明该项原因确实消失。
返回每项resolved/open及明确原因；仅解决有证据的项，其他保留，不整体放行。
没有一项可解决返回409 items_unverified；部分可解决则一笔事务更新成功项、集合revision+1、
逐issue revision+1，保存原issues/版本、请求hash、身份、证据和原响应到不可变review记录。
CAS冲突不做部分提交。search_region/item的复合标识不能遗漏，resolved必须有resolution，
未解决resolution=null。保留reason_code作为原问题，不因解决覆写成空。

`records:row` 对应主文layout_review_records行；json在schema中表示解码后的对象，
落库使用canonical JSON文本。kind与record_id必须匹配operation/issue/review中的真实ID。
operation内部字段完整且response严格为 `wire:write_response` 联合，不允许任意dict；
prepared无response/error；committed有原response/after_revision且无error；
aborted有error、无response/after_revision；unknown无成功response。
resource_kind扩展draft/capture/package以覆盖新增入口，template/package的device_id=null；
原region/template/adaptation/review含义不变。

Store还持久第7.1节列出的15种固定类型，全部落在同一张 `layout_typed_state` 表；
主键、列映射和CAS以该节及 `typed-state.sql` 为准，不再由实施者另行设计表。
preview不自动获得操作/输入授权，不允许客户端提供表名/磁盘路径。
draft/session/preview行在原scope内读取；原始evidence私有、长期图像证据权限不因包导出扩散。
所有被current/问题/在途任务/导出记录引用的不可变版本及依赖资源保留，首版不提供删除API。

持久资产位置相对同一data_root：
模板沿用主文 `layout_templates/{id}/versions/{v}/assets/...`；
本台颜色资源为 `devices/{id}/layout_adaptation/versions/{v}/assets/...`，
不更改同目录原 `{v}.json` 适配记录约定；
草稿资源 `layout_drafts/{draft_id}/assets/...`，session资源
`devices/{id}/layout_adaptation/sessions/{session_id}/assets/...`。
归档原图复用现有EvidenceStore，不在模板目录再存一份真实页面。
导出 `layout_exports/{package_id}/package.zip`及manifest，隔离暂存仅
`layout_staging/{operation_id或preview_id}/`。路径全部由服务端白名单组件拼接。

### 7.1 唯一新增状态表

[typed-state.sql](typed-state.sql) 是直接可执行的SQLite建表DDL，固定表名
`layout_typed_state`、主键 `(kind,state_key)`、归属索引
`layout_typed_state_owner(device_id,principal_id,kind)`，无额外动态分表。
`records:state_row` 是完整行的严格schema；以下 `J` 表示行内已解码的 `json` 对象。

| 固定列 | SQL类型/null | JSON及约束映射 |
| --- | --- | --- |
| kind | TEXT NOT NULL | `state_row.kind`，仅下表15值 |
| state_key | TEXT NOT NULL | `state_row.state_key`，64位小写sha256，算法如下 |
| device_id | TEXT，可null | 下表设备归属，不从客户端额外取值 |
| principal_id | TEXT，可null | 下表身份归属，必须来自已认证服务端身份/可信记录 |
| revision | INTEGER NOT NULL | `state_row.revision`，1..9007199254740991，按下文映射 |
| json | TEXT NOT NULL | `canonical(J).decode("utf-8")`，读回严格解析为对应封闭对象 |

`state_key = sha256(canonical([kind, ...身份分量])).hexdigest()`；
canonical沿用本附件格式工具，version保留整数，不串接字符串，不包括内容sha256。
完整行在入库前、读回后均通过 `records:state_row` 的结构及语义校验；
DDL只承担列/枚举/主键/数值底线，不依赖SQLite JSON扩展，不把SQL约束当JSON校验器。
下表归属中的字段均相对J；null表示共享资料/无单台归属，不表示匿名可读。

| kind | J的确切schema fragment | 身份分量（不含前缀kind） | device_id / principal_id | 可变性 |
| --- | --- | --- | --- | --- |
| draft | `layout:draft` | `[draft_id]` | `device_id / principal_id` | 业务CAS |
| session | `layout:session` | `[capture_session_id]` | `device_id / principal_id` | 业务CAS |
| capture | `layout:capture` | `[capture_id]` | `device_id / principal_id` | 一次消费 |
| evidence | `layout:evidence` | `[capture_id,evidence_id]` | `device_id / principal_id` | 不可变 |
| region_draft | `records:region_draft` | `[device_id,ref.version]` | `device_id / principal_id` | 不可变 |
| region_preview | `records:region_preview` | `[response.preview_id]` | `request.path.device_id / principal_id` | 一次消费 |
| adaptation_preview | `layout:preview_record` | `[preview_id]` | `device_id / principal_id` | 一次消费 |
| rollback_preview | `records:rollback_preview` | `[response.preview_id]` | `device_id / principal_id` | 一次消费 |
| package_import_preview | `package:preview_record` | `[preview_id]` | `null / principal_id` | 一次消费 |
| package_export | `package:export_record` | `[package_id]` | `null / principal_id` | 不可变 |
| cursor | `records:cursor` | `[cursor_id]` | `device_id / principal_id` | 不可变 |
| review_revision | `records:review_revision` | `[device_id]` | `device_id / null` | 集合CAS |
| adaptation_asset | `records:adaptation_asset` | `[owner_ref.id,owner_ref.version,record.asset.id]` | `owner_ref.id / null` | 不可变 |
| catalog | `records:catalog` | `[resource.kind,resource.ref.id,resource.ref.version]` | template为null，其余为`resource.ref.id` / null | 不可变 |
| intent | `records:intent` | `[operation_id]` | 同operation行的`json.device_id / json.principal_id` | 不可变 |

以上短名按第1节展开，例如 `layout:session` 精确等于
`layout.schema.json#/definitions/session`，不是HTTP session_view。
region_draft的ref.id必须等于device_id；adaptation_asset的owner_ref必须解析到同台适配。
intent与 `layout_review_records(kind=operation,record_id=J.operation_id)` 同事务创建，
两个归属列逐值相等；独立JSON校验不能替代这次跨行检查。
私有draft/session/capture/evidence/preview/cursor及intent须核对原身份和设备scope；
共享catalog/review_revision/adaptation_asset仍按既有资料读取权限检查。

evidence行只存布局capture与父级source evidence的关联/证明，同一capture内的
`capture.evidence[]` 与对应evidence行须完整一致且同事务写入；
不同capture可关联同一父级evidence_id，原JPEG仍只存原EvidenceStore，不复制图像。
package manifest内嵌在import preview/export记录中，成功导入结果仍存原operation和catalog；
rollback只用rollback_preview、原operation/review及current，不新增另一种包/回滚记录。
模板/适配/范围完整版本及图片仍在第7节固定路径，catalog只索引精确ref，不以本表取代原档。

**revision与CAS固定规则：**

- 新行的SQL revision一律1，插入的expected_revision一律0。draft/session的
  `J.revision` 从1开始且始终等于SQL revision；每次成功修改两者同加1。
- review_revision的 `J.revision` 从0开始，SQL revision恒为 `J.revision+1`。
  第一次使用设备问题集合时在Store事务内创建零值行；HTTP仍比较原业务值，
  持久mutation比较SQL值，即 `HTTP expected_revision+1`，不改变API协议。
  每次实际问题集合变化同事务更新issues与该行一次，两个revision同加1。
- 一次消费类创建时 `consumed_by=null,revision=1`；仅允许变为
  `consumed_by=原提交client_request_id,revision=2`，J的其他字段全部不变。
  不刷新created/expires/时钟/证据。第9节先查台账，合法重放不再执行这次UPDATE。
- 其余类revision恒1且禁止UPDATE。不可变行同键同完整内容复用原行；
  catalog按同资源身份+同ref.sha256复用原行并保留最初operation_id/created_at，
  导入reuse不重建其adaptation_asset索引。异摘要/异内容报409 request_conflict，
  不以改主键、覆盖、`INSERT OR REPLACE` 或增加revision掩盖冲突。

固定参数化SQL如下；执行前须通过 `records:state_row` 及
`records:state_cas_case` 等价检查（后者仅为离线插入/更新容器，不是HTTP接口）。
所有值均绑定参数，kind/state_key/两个归属列在更新前后不可改变。

```sql
-- Only when expected_revision = 0 and no existing row is being reused.
INSERT INTO layout_typed_state
    (kind, state_key, device_id, principal_id, revision, json)
VALUES (:kind, :state_key, :device_id, :principal_id, 1, :json);

-- :json already contains the validated next business revision, if any.
UPDATE layout_typed_state SET revision = revision + 1, json = :json
WHERE kind = :kind AND state_key = :state_key
  AND device_id IS :device_id AND principal_id IS :principal_id
  AND revision = :expected_revision;
```

UPDATE影响行数必须恰为1，0行或插入唯一键竞态导致整笔Store事务回滚，
返回409 revision_conflict；不得只提交同批其他行。超整数上界同样拒绝，不绕回0。
第9节prepared/committed及文件恢复规则不变，CAS失败后的恢复仍按原intent对账。
私有预览/游标在返回前持久；游标翻页创建新UUID/固定offset行，旧行不推进或续期。

`records:mutation.table` 只是已有**逻辑标签**，不得拼成SQL表名：
`drafts→draft`、`sessions→session`、`captures→capture`、`catalog→catalog`、
`exports→package_export`、`region_drafts→region_draft`、
`review_revisions→review_revision`、`adaptation_assets→adaptation_asset`，
全部映射本表，key必须等于上述state_key，value就是J；
`expected_revision` 是目标SQL行revision，缺行才为0。
`issues/reviews` 仍映射 `layout_review_records` 的 `issue/review`，
key分别为issue_id/review_id，CAS沿用原表规则。operation仍由原台账状态机写原表。
preview消费由intent.request中的preview_id定位相应固定kind；
capture消费由原capture_id定位，evidence由capture.evidence派生插入，
全部与第9节第5步在同一事务内完成，不另加mutation标签。
intent本身在第2步写入，预览/游标生成按各自入口写入；不产生额外公开写入口。
`layout_review_records.kind` **始终仅operation/issue/review**，不得放入本节15类。

## 8. 导出、导入和回滚

导出一次选择 `roots:[{kind,ref}]`，以固定版本确定**整个manifest闭合集**：
适配 -> 精确模板/安装范围 -> 模板父版本链；每个模板的全部引用资产、
每个适配空白参考资产均必须包含。parent_version是溯源关系，也纳入闭合；
缺父档报archive_invalid，不假造父摘要。拒绝附带不在闭合中的额外记录/资源。
源 evidence_ids/作者/时间是历史溯源标识，不是包内可执行依赖，不携带原客户截图、
当前指针、校准bundle、输入许可、task/IME文本、会话租约、密钥或真实发送成功记录。
源身份/时间导入后只是原作者陈述，不作为本地可信权限。

manifest `package:manifest` 固定roots、按path排序的entries、file_count、
total_bytes、创建身份/时间、privacy_scope和sha256。
每entry区分record/asset，带完整resource或owner+asset、路径、原字节大小和原字节摘要。
记录文件内容使用canonical JSON，记录自身sha256仍按去掉自身字段计算；
entry.sha256则是包括该字段的完整文件字节摘要，两者不能混用。
manifest.sha256去掉自身字段计算；不把ZIP摘要放进manifest制造自引用。
total_bytes为entries字节总数，不包括manifest；file_count包括manifest。
实际包总量仍须把manifest字节算入200MiB上限。最多5000文件，不允许目录条目。

ZIP只允许stored/deflate、常规文件、无加密；先查central directory再流式计数，
压缩体和展开体均<=200MiB。拒绝绝对路径、空组件、`.`/`..`、反斜杠、百分号逃逸、
NUL、大小写碰撞、重复条目、符号/硬链接和设备文件、未列文件、坏CRC/摘要、
错误图片格式/尺寸/元数据、非闭合引用。服务端生产解包用隔离目录及
openat/O_NOFOLLOW等价防竞态路径访问；本离线工具只在内存读取，不伪装生产解包器。
包中的asset.relative_path仍相对其owner目录，绝不改写原模板路径再“修”摘要。

export先建operation意图，验证读取权限/闭合/隐私来源，生成全部字节并重新校验，
再发布export记录，成功后GET package_id能读取manifest、GET download能取得完整ZIP。
export返回成功不等于文件已移到异盘；D08没有自动外部备份。
不要求相机在线或机械校准ready，坏档必须显式错误。

import/preview接完整ZIP+运输sha256+privacy_confirmed=true；
受认证人工在预览中确认无客户图/秘密，服务器完整检查解码、闭合和所有资源，
并与本机catalog逐(kind,id,version)比较：同sha为reuse，不存在new，不同sha为conflict。
同名冲突只报告，不选择“覆盖”。返回完整manifest、逐资源结果、can_import和120秒preview。
import提交带client_request_id、preview_id、archive_sha256，只使用隔离的原字节；
认领锁内再次比较catalog，全部通过才一次提交全部catalog/asset索引。
有任一冲突409 request_conflict，整包不导入；不是已导一半再报错。
临时文件可留下用于对账但未进入catalog前不可经API读取/选择。
成功结果pointers_changed恒false。不创建适配session、不切current、不解除问题、不打开输入。
跨设备适配按原device_id归档；不能给另一个机械臂改ID后直接启用。
旧v2/缺新字段档案只能留原备份，不假冒本包v1记录导入；另走主文显式迁移。

回滚不是复制旧权限：preview明确kind、target_ref、expected_revision。
范围回滚提交当前来源evidence_id，session字段为null；旧目标绑定/geometry不同即拒绝，
要重新框选/复核并生成新版本，不能用回滚绕过换绑。
适配回滚使用新的四态session，source_evidence_id=null，必须与目标template/
installation_region/手机/布局一致，目标项目全通过；新session证据另存review，不覆写旧记录。
preview同样120秒且取新鲜当前帧；PUT持相同expected_revision并要求空闲。
成功current revision+1，但selected_version可指回旧版本，旧版本内容/sha/时间不改，
新问题按新证据逐项处理，机械/许可/IME/发送状态不动。
共享模板无全局current，因此没有“全局模板回滚”暗开关；
读取旧模板用于新草稿派生或本台显式选择/适配，旧模板一直可查。

## 9. 幂等、锁与崩溃规则

每个带client_request_id的Fleet写入都进入同一个幂等台账，UUID全局唯一；
存原身份、scope、route/path及完整请求/响应快照。重复编号先查台账再检查当前CAS/期限，
合法已提交重放不能因现在preview过期或revision变化而失败，也不再采图/分配版本。
不同身份403；同身份不同route/path/device/body为409 request_conflict。
request_hash精确计算 `{route_id,method,path,query,body}`，body仅移除correlation_id/
client_request_id。作者权限scope另外持久保存并核对，不能从body信任。
关联ID不是业务意图；同意图换correlation仍重放最初保存的correlation与响应头，
只有响应body.replayed改true，原台账不改。operation查询始终返回原replayed=false快照。

prepared/unknown同编号再POST/PUT不执行第二次，409 request_conflict/503 storage_failed，
返回原operation_id，引导GET按operation或原request查询；所有错误retryable=false。
aborted重放原错误，用户修正/重新预览后新编号发起新意图。
404只表示当前权限查询没找到可返回记录，不证明在途写未提交；
网络未知时可使用原编号同摘要重试查重，不能换编号盲建版本。

锁获取顺序固定：按(kind,id)排序的共享资源短锁 -> 设备短锁 -> draft/session锁 ->
Store事务；同一请求不反向嵌套取锁。长时间抓帧/解码/匹配/ZIP处理在锁外，
结果提交时重新核对固定版本、绑定和CAS。模板使用template_id锁，导入对闭合集排序取锁。
版本分配只在锁内使用已持久预约及文件/已提交版本的最大值+1；
包含未发布范围草稿、prepared已预约号及孤立完整档，允许跳号，不复用号覆盖文件。
导入的version固定为包内值，不能分配新version把冲突洗掉。

操作顺序固定：

1. 鉴权/严格解析/查重；读取当前完整资料与CAS，准备全部资源、引用、结果和计划。
2. 同一Store事务持久 `operation.phase=prepared` 与不可变 `records:intent`：
   完整request/hash、planned_response、files字节清单、pointer_before/after、typed mutations。
   实际分配的version/UUID/时间/响应已在此确定，恢复不能重新生成。
3. 暂存文件写入、逐文件fsync、校验、原子重命名到不可变位置、fsync各父目录。
   不允许覆盖同名异摘要文件。所有文件完成后才能切一个设备current。
4. 需要current变更的操作核对before，再原子替换after并fsync目录。
   草稿保存before=after，模板/导入/导出等无设备指针用两个null。
5. 一笔Store事务应用intent的typed mutations、记录消费preview/capture的原请求，
   写committed、after_revision和原响应。提交后才返回成功。

恢复启动先对未终结operation逐项对账，在相关资源恢复完成前拒绝新写/执行；
只读GET允许完整旧资料，但异常指针不降为未登记。

| 持久事实 | 唯一恢复结果 |
| --- | --- |
| 旧指针、Store未提交，文件缺失或只有部分暂存 | aborted，保留旧指针；暂存不入catalog |
| 旧指针、Store未提交，所有新文件已完整 | aborted，保留旧指针；完整孤档保留并占用版本号，不补发发布 |
| 指针精确等于intent.after，全部文件/摘要完整，Store未最终提交 | 根据原intent补齐Store事务并committed；不再次换指针/采图/增加revision |
| Store已提交，指针与after一致且文件完整 | committed，返回原快照 |
| 新指针但文件缺失/坏摘要，或指针既非before也非after，或Store与文件矛盾 | unknown/明确阻断；保留现场与历史，503 archive_invalid/storage_failed，不猜成功、不重放 |
| 无指针操作（模板/导入/导出/草稿/采集）全部文件完整但Store未提交 | aborted；只有Store最终提交的catalog/资源才可见，导入不得半包可见 |

unknown需修复存储完整性并再次按原intent对账，不用资料review掩盖存储损坏。
不得以另一个设备改动的全局binding_revision触发本设备指针回退/重新采集。
prepared意图/响应、历史版本、已引用资产默认保留；磁盘满停止新增，不清空旧档。
本附件离线crash场景验证上述状态表，不等于已实现真实fsync/并发故障注入。

## 10. 从读取到交付的固定实施顺序

先按schema实现Store与catalog/intent，接38条布局/IME路由及统一错误包装；
读取路径不得调用ready校准gate。接父级camera-source-frame原子证据后实现capture、
草稿资源上传/读取、单页定义更新及模板发布。随后实现本台session/逐项匹配/颜色资源，
再接120秒preview、CAS一次确认、问题列表/部分核对和操作查询。
最后接闭合export/import/rollback。每步运行本目录同一schema/fixture，不另造格式工具。
所有接口仍零机械/候选下发/文本输入/发送调用；实际执行继续原任务状态机门禁。

## 11. 校验边界

本目录校验了所有已列公共接口的严格请求、成功和错误格式，并覆盖内部记录、引用关系、
四页保留/过期/继承、绑定/v3校准、幂等/崩溃状态、真实合成图片和完整合成ZIP。
图像像素是不是正确页面、阈值在真实机型上的识别率、真正磁盘掉电/线程锁/安全解包
属于主文L01-L07实施与验收，不是“文档仍缺关键设计”。
校验器不实现图像匹配器/任务执行器，也不导入生产模块。
未安装相应功能或未跑真机测试不计作本文设计缺口。
本轮最终离线结果：11份Draft07 schema、38条路由、623个正例、328个反例、
13项解析/路径资源自检；497处封闭对象schema，引用全部本地解析。
另有26项纯内存SQLite DDL/CAS检查，覆盖15种state行、列/主键、读取往返、
非法插入及并发旧revision拒绝，不连接任何生产数据库。
`--show-case state-row-session` 可展开完整状态行，
`--show-case state-cas-consume-adaptation_preview` 可展开一次消费场景。

## 12. 主线程应同步的精确正文变更

不编辑主文，由主线程在同一第11节同步以下内容：

1. **§11.11.2**：新增指向本目录两个完整v1记录schema和本附件的链接；
   compatibility未知明确为值/source均null，仅草稿允许；完整发布禁未知。
   screen_descriptor.calibration_geometry_hash明确为camera-binding v3几何指纹，
   排除global bindingRevision，旧v2不伪升级；installation_binding现有字段不变。
2. **§11.11.3接口表**：用本附件第3节38条映射及wire.schema.json补齐原表。
   新增metadata、资产上传/读取、capture、证据读取、session读取、issue列表、
   按operation_id查询、export/get/download、import预览/提交、rollback预览/提交。
   原始相机入口只引用父级camera-source-frame，不另定义相机路由。
3. **§11.11.3请求字段**：模板发布增加expected_draft_revision；
   session创建增加keyboard_geometry_mode、inherit_session_id、inherit_states；
   导入预览增加privacy_confirmed。所有历史版本GET以{contract_version,correlation_id,record}
   包装完整记录。新版本号/字段逐值以routes.json及wire schema为准。
4. **§11.11.3分页**：固定limit=必填1..100、首cursor省略归一null、后续UUID，
   模板ID升序/版本降序，问题opened_at升序，900秒授权快照、last next_cursor=null。
5. **§11.11.3采集会话**：pages逐态capture可null；保存session.assets及明确原证据来源证明；
   capture一次消费，accepted_at固定首次归档时刻，页15分钟不因PUT/继承/重放续期；
   未发布session/preview重启失效，已发布资料不失效。粗框取帧不依赖校准，四态抓页要求本台v3。
6. **§11.11.5逐项/问题记录**：issue增加item:null|复合标识；
   明确review.expected_revision是设备资料问题集合CAS；operation.resource_kind增加
   draft/capture/package，response为完整wire快照；增加typed intent、review_revision、
   adaptation_asset等严格内部记录引用，不再留“自由json/dict”解释空间。
7. **§11.12.2/11.12.3**：accepted_items恰为所有实际通过三元组；
   四页保留失败结果，部分发布needs_review并只阻断相关依赖，整体通过不能覆盖失败；
   展开页固定keyboard_boundary、有字页send_button_feature，live IME目标由服务端产生；
   本台hidden/empty参考图有资源，list/text引用为null。
8. **§11.14.1**：补本附件第8/9节manifest闭合集、同ID/version同摘要复用、
   异摘要整包冲突、无指针导入事务、按原intent崩溃矩阵；显式回滚只CAS前进revision。
   补本台颜色资源、草稿/session暂存和export的唯一相对位置；不新增NAS/外部备份。
9. **§11.11.3幂等**：request_hash排除关联ID和client_request_id但包括完整route/path/query/body；
   不同身份403，同身份异内容409；重放保留原correlation/时间，仅replayed=true。
   完整operation快照持久，404不作为未提交证明。
10. **§13 G04/G06技术缺口行**：可将“完整模板/适配/资源/资料包schema待补”改为已交付本附件
    和离线校验；运行实现及主文既有T类验收保持未验收，不能写成功能完成。

完成这些主文引用同步后，本交付范围没有仍需实施者自行决定的产品选项或关键接口字段。
父级camera-binding负责的真实clock/session签发和camera-source-frame契约仍归父级，
本文已经明确消费边界，不复制为第二套实现设计。

本次仅存储限定补丁，主文§11.11.5只需追加以下引用（上述10项无需重复同步）：
“其他附加状态统一落库 `layout_typed_state`；DDL、15类记录的schema/主键/归属及CAS
见 `specs/layout-reuse/contract-completion.md` §7.1、`specs/layout-reuse/typed-state.sql`
和 `specs/layout-reuse/records.schema.json#/definitions/state_row`；
`layout_review_records` 仍仅承载operation/issue/review。”
