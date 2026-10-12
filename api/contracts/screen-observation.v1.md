# 屏幕观测证据契约 v1

状态：记录屏幕定位与等待状态的响应语义，不是现场验收证明。
Arm Fleet 和 WX 分别随包保存本文件及同名 Schema；运行时只使用本项目文件，
不得从另一项目目录导入代码或读取契约。跨项目副本兼容检查只在联合测试中进行。

## 接口与职责

`GET /v1/devices/{device_id}/screen-observation` 无请求体，成功返回 HTTP 200 JSON，
`contract_version` 固定为 `screen-observation.v1`，格式见同名 `.schema.json`。
它不创建聊天、输入或点击，不授予发送权限，也不代替只读视频查看会话。

Fleet 经设备 CameraBroker 取得原始帧，按当前校准矫正屏幕，再对矫正图运行 OCR。
WX `ScreenReader` 消费图像和 OCR，解释页面；WX `LiveObservationWorker` 负责
白名单、身份、基线、消息游标、撤回及业务入库。接口不返回生产客户消息或业务判断。

本契约不同于 `observation-envelope.v1`：后者是 WX `/v1/observation/*` 入站信封，
包含 `envelope/payload`；本接口直接返回屏幕证据对象，不能套用或混用该信封。
`screen-proof.v1` 则描述登记页面/区域的证据能力，不是本读屏响应的别名。

## 成功字段

以下字段在当前成功响应中全部出现；仅注明者允许空值：

| 字段 | 语义 |
|---|---|
| contract_version | 固定 `screen-observation.v1` |
| device_id、camera_id | 本次请求设备及其摄像头通道 |
| daemon_session | 当前机械臂控制器会话，须与校准一致 |
| wechat_account_id | 设备配置中的账号上下文，可为 `null` 或空字符串；不是画面识别的身份结论 |
| binding_revision | 帧源绑定版本，整数 |
| calibration_fingerprint | 当前 `cameraToScreen` 数据生成的 SHA-256 摘要，不代表全部校准条件已经永久有效 |
| camera_device_unique_id | 可选；原始帧的物理摄像头编号，经校准绑定一致性核对后返回 |
| motion_calibration_fingerprint | 可选；完整动作校准摘要，与手势/唤醒资格的 `binding.calibration_fingerprint` 使用同一算法，覆盖设备、会话、摄像头、绑定版本、帧尺寸、相机/屏幕/XY/Z 及安全停车位。不替代当前动作资格；自动唤醒消费方缺少此字段时只等待，不把旧投影摘要当动作摘要 |
| captured_at | 原始帧带时区的采集时间 |
| frame_sha256 | 原始摄像头帧的 SHA-256 |
| frame_seq | 持续推流的非负帧源序号；缺失序号时拒绝提供屏幕证据 |
| quality | `clear` 为已定位且可读取屏幕，`waiting` 为暗屏等待；等待帧不提供可用点击坐标 |
| frame_width、frame_height | 原始摄像头帧尺寸 |
| screen_width、screen_height | 返回矫正图尺寸，当前缩放上限为 720×1600，不放大原屏幕 |
| screen_to_camera | 3×3 齐次逆变换矩阵，从返回矫正图像素映射回原始摄像头像素，不是机械臂坐标；`waiting` 时仅为占位，不可用于动作 |
| screen_to_calibrated_camera | 可选；仅 `clear` 帧提供。从矫正图映射至校准时的相机坐标系，不含本帧手机位移；供 WX 将视觉目标以 `calibrated_camera.v1` 回传，由 Fleet 在接触前的新帧重新定位并补偿。缺失时旧消费者仍用 `screen_to_camera`，手机偏移超出原始坐标容差时拒绝动作 |
| image_base64 | 经过透视矫正、缩放及亮度改善的 PNG 字节，使用 Base64 编码 |
| image_sha256 | `image_base64` 解码后 PNG 字节的 SHA-256，与原始帧摘要分开 |
| ocr | 针对返回矫正图的 OCR 结果，结构如下 |

`ocr` 包含 `region`、`text`、`words`、`confidence`、`truncated`、`engine`、
`engine_version`、`request_id`、`elapsed_ms`、`width`、`height`。
每个 word 包含 `text/x/y/width/height/confidence`，坐标相对返回的矫正图，
不是原始摄像头或机械坐标。置信度为 0 到 1；`truncated=true` 表示不能当完整内容使用。
OCR 的尺寸/耗时可以为 0，字符串元数据可以为空；客户端仍以顶层图像尺寸为准。
画面先按校准四角定位手机并补偿有限偏移，再遮蔽屏外区域；`waiting` 返回黑色占位图和空 OCR，
消费方只等待恢复，不从占位图、OCR 或矩阵推导手势/点击目标。
任一定位失配、帧过期或补偿超限均不改变校准档案，也不自动执行手势。

Schema 允许附加字段以保持现有消费者的忽略未知字段行为，但这不授权改变既有字段语义。
时间字段的 `pattern` 明确要求日期、时间及 `Z` 或带冒号的时区偏移；
`date-time` 的完整校验还需要验证器实际提供格式检查支持，不能假设仅启用
`FormatChecker` 就已安装全部可选格式检查依赖。Base64、PNG 解码、哈希相等、
矩阵可用性、坐标边界、设备绑定和实时性需要额外语义核对，不能仅依赖 Schema。

## 错误与处理

错误不包含成功证据对象，使用现有 Fleet 错误外壳：

```json
{
  "error": {
    "key": "calibration_invalid",
    "title": "校准档案无效",
    "suggestion": "请在调试台完成本设备的摄像头、机械臂和输入法校准后重试。",
    "trace_id": "trace-example",
    "detail": "屏幕观测需要本设备当前会话的有效校准"
  }
}
```

`key/title/suggestion/trace_id` 必有，`detail` 仅有具体说明时出现。
错误结构对应同名 Schema 的 `#/$defs/apiError`，不能用成功响应 Schema 将错误当作帧验证。

| HTTP / key | 当前含义与操作 |
|---|---|
| 409 / calibration_invalid | 无有效 ready 档案，设备/摄像头/daemon 会话不匹配，矩阵/尺寸无效，或帧的物理摄像头/绑定/尺寸与档案不一致。完成对应设备校准或绑定核对后重新读取；不是“观测桥未实现” |
| 409 / camera_unavailable | 帧获取、矫正或 OCR 等证据处理失败。检查摄像头及处理服务，不使用旧画面兜底 |
| 404 / device_not_found | 设备未注册，核对设备编号 |

任何错误都不能解释为可以执行动作。错误建议是操作提示，不授权自动移动、重新校准、
修改绑定或重放原任务。

## WX 消费约束

WX 校验版本及请求设备匹配、采集时间带时区且在当前时间前 5 秒至后 2 秒范围内、
Base64/PNG 可解码、图像 SHA-256 匹配且解码尺寸与顶层尺寸一致。
设备、摄像头、账号、daemon 会话、绑定版本、校准摘要和变换属于观测缓存隔离范围，
不能跨设备或绑定复用。OCR 不可靠时按现有本地复核/受限视觉流程处理，不自行补造消息。

HTTP 409 是可解释门禁，不按瞬时网络错误重试策略处理；当前 WX HTTP 客户端只把
429、5xx 和网络异常标为可重试。后续观测轮次可以重新查询，但不能因此重复任何物理动作。
Schema 补录不改变现有 WX 消费校验逻辑，也不意味着运行时已经增加 Schema 验证。
