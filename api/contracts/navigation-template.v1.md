# navigation-template.v1 — 桌面/顶部模板登记

版本：`navigation-template.v1`（规范第 22.15 节）。

- `POST /v1/devices/{device_id}/navigation-template-previews`：body `{kind}`，
  kind ∈ {launcher, top}；只采 3 张新帧和候选，无机械动作；preview 60 秒有效。
- `PUT /v1/devices/{device_id}/navigation-templates/{template_id}`：
  `{expected_version, preview_id, confirmed=true, anchor_regions, restore_target}`；
  仅显式确认登记，top 确认语义是"当前列表在最顶部"。
- anchor_regions 为校准域像素矩形；launcher 需 3-6 个非重叠区域，top 需 1-3 个。
- restore_target 仅 launcher 可非 null：`kind=ocr_text / visible_text /
  region(归一化) / expected_page_id=gesture-parent`。
- 模板记录含 `confirmation_source=debug_explicit_confirmation`、服务端 UTC
  `confirmed_at`；不接受客户端文件路径、URL 或 base64 图像。
- expected_version 首次 0；过期 preview 或 binding 变化返回 409。

## 服务端事实与登记边界

上述 body 为业务字段，写请求另携带平台通用操作上下文
`operator_id/role/reason/correlation_id/client_request_id/confirmation_token`。
预览沿用 `task_submit` 权限，登记沿用 `navigation_template_register` 权限。
不接受业务字段之外的图像、`binding`、`frame_evidence`、证据编号、路径、URL、
base64 或客户端 `kind` 覆盖。`template_id` 仅允许 `top`、`launcher`，
必须与服务端预览类型一致；其他编号不能成为文件路径。

预览通过设备共享 camera broker 连续采集三次新帧，交由现有 EvidenceStore
截图接口归档。每帧包含真实截图 SHA-256、证据编号、采集时间、摄像头和当前
完整校准指纹；源帧有序号时保留并检查严格递增，无序号来源保留 null，不造号。
真实相机桥的 `source_frame_seq/source_captured_at` 优先于本地编码时间；
来源设备、摄像头、物理唯一标识、绑定修订必须与当前绑定一致，拒绝旧兼容来源。
采集时间必须带时区、非未来且不超过 5 秒，三帧时间递增；图像须可解码且
尺寸等于当前校准相机尺寸。静止画面的相同像素不等于重复帧。

完整 binding 包含 `device_id/daemon_session/camera_id/camera_device_unique_id/`
`binding_revision/calibration_fingerprint/frame_width/frame_height/coordinate_frame_hash/`
`gesture_profile_version/recipe_version`，取自当前校准、坐标桥、自动手势档案和
规则版本，并复核设备当前绑定。采集前、帧间、采集后及登记落盘前重新核对；
变化则拒绝，不能静默重绑。没有有效校准/坐标桥不能生成可登记预览。

平台将预览事实原子保存于设备模板目录的 `previews/{preview_id}.json`，
不从客户端响应重建。60 秒有效期从完成采集计算；登记只读取该设备下已保存的
同编号、同类型预览。候选只是校准屏幕范围内的确定性建议框，不证明页面语义。
`confirmed=true` 是本次人工确认，不代表设备动作成功或页面匹配已通过。

登记必须提供非负整数 `expected_version`；首次为 0，更新必须等于现存版本。
同设备线程锁与文件锁覆盖读取版本、校验、裁剪、原子替换，只有一个相同版本的
并发写入可成功。损坏/身份不符的旧档案拒绝覆盖；GET 不修复、迁移、重绑、
采帧或写入模板。版本及像素矩形要求 JSON 整数，不接受布尔、文本或浮点代替。
区域不可重叠，且 `left_px+width_px`、`top_px+height_px` 不得越过校准屏幕边界。
launcher 的恢复目标必须符合 schema，归一化区域的右/下边界也不得超过 1；
top 的 `restore_target` 必须为 null。

## 裁剪证据与消费一致性

三张原始相机截图归档于平台 `navtpl-{kind}/{evidence_id}.png` 证据路径，
用于核验来源，不在匹配时重新裁剪为模板。登记逐一从平台证据库
解析、检查设备/任务/摄像头/时间/文件归属，重新计算文件哈希并解码，不能将
其他任务证据或任意客户端文件当作预览来源。

登记每个锚点时，灰度解码原始相机帧，应用校准 homography
到 `(screenWidth, screenHeight)` 屏幕平面，再按校准像素矩形裁剪。
三帧均校正并核验可读；第一帧的真实裁剪 PNG 通过 EvidenceStore 保存，
`evidence_id` 引用该裁剪、`crop_sha256` 为该 PNG 字节的真实 SHA-256。
`frame_evidence` 保存预览三张原始截图引用，结合锚点 region 和完整 binding
可重现三帧裁剪；不是把裁剪编号伪装成原始帧编号，也不是填假哈希过 schema。
落盘前校验 `templateRecord`，版本仅在成功原子替换时递增。

资格计算、动作解析和模板消费共用只读完整性检查。必须显式传入运行时配置的
证据根，不从数据目录猜测路径。检查三份不同的原图引用及保存裁剪的 PNG、
摘要、尺寸、绑定、区域和安全路径；平台入口另通过证据库核对设备、任务、
文件类型、路径与来源元数据。缺失、篡改、跨设备引用或链接越界均失败关闭。
匹配仅加载登记裁剪，禁止用被替换的原图重新生成“匹配成功”的模板。
提交、出队及真正动作前复核；匹配取到新帧后再次通过运行时核验资格。
损坏证据使动作不可用（`navigation_template_invalid`），顶部证明可用标记为假，
不得进入业务驱动。检查不重采、不补写、不自动修复或缓存授权。

参数/注入/区域不合法返回 400 `invalid_request`；预览缺失、过期、归属错误、
证据损坏或 CAS 冲突返回 409 `idempotency_conflict`；绑定变化返回 409
`gesture_binding_changed`，校准/相机不可用沿用对应 409 错误，损坏模板返回
409 `gesture_profile_invalid`。GET 未登记返回 404。失败不产生机械动作，
不清除设备隔离，不改变任何既有 UNKNOWN/结果未知记录，不重放任务。

模板登记只补足 `ag.navigation_view` 的模板资格依赖；仍需动作证书、开关、
规则和布局版本等门禁全部有效。资格可用不是当前页面已匹配，更不是现场验收。
过期预览及未引用证据按平台证据保留策略处理，本接口不会自动删除历史证据。
