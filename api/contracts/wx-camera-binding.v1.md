# wx-camera-binding.v1 — WX 授权失效向 Fleet 查看流的传播

版本：`wx-camera-binding.v1`（R4-12-D/E；身份附件；冻结于《2026-10-06-v1.10最终软件收尾执行与验收》§5.S2）。

## 边界与信任模型

- 只做本地/受控 LAN；不接公网；不跨读对方数据库；无云/消息平台依赖。
- 可信代理（nginx 软件验收模板）是唯一身份边界：客户端自报的
  `requested_by/role` 永不作为授权依据；客户端伪造的 `X-WX-*` 头在代理
  处被 `proxy_set_header` 受控值覆盖（空值即不发送），永不到达 Fleet。
- WX 登录会话令牌明文不出 WX；跨服务只传 SHA-256 会话摘要（不可逆推）。
- 原始凭据（密码/令牌）不写日志、不入撤销通知。

## 代理边界（nginx，部署输入）

1. `location = /_wx_camera_identity`（internal）：auth_request 子请求目标，
   转发调用者 Cookie 到 WX `GET/POST /internal/wx-camera/v1/identity`。
2. `/arm-api/v1/camera/view-sessions*` 四个 location 全部挂
   `auth_request /_wx_camera_identity`；401 = 未登录/会话失效，请求不达 Fleet。
3. `auth_request_set` 捕获 WX identity 响应头并以 `proxy_set_header` 重建：
   - `X-WX-Operator`：WX 用户名（绑定与审计归属）。
   - `X-WX-Operator-Role`：`admin|staff`。
   - `X-WX-Session-Id`：WX 登录会话摘要（绑定键）。
   - `X-WX-Credential-Revision`：凭据代际（禁用/重置/改密递增）。
4. CSRF：会话 Cookie `SameSite=Strict`（跨站 POST 不携带 Cookie → auth_request
   401）+ WX 边界 Origin 校验；/arm-api 写方法不引入独立 CSRF 令牌。

## Fleet 侧（arm_fleet）

- 创建（`POST /v1/camera/view-sessions`）：四元组齐备且 `X-WX-Operator` 可信
  → 会话入库绑定 `wx_bound/wx_username/wx_session_id/wx_credential_revision`
  （契约视图 `camera-view.v1` 不外泄绑定字段）；只有 Operator 无 Session-Id
  → 可信身份但未绑定（不参与撤销传播——部署代理应始终成对发送）。
- 内部撤销端点 `POST /v1/internal/wx-revocations`：
  - 信任：`X-WX-Internal-Token` 与 `wx_camera_token_file` 常量时间比对；
    令牌文件未配置 = 端点整体拒绝（403 `internal_unauthorized`，无无令牌模式）。
  - 请求：`{"kind":"sessions","username","session_ids":[..1..100],"reason"}`；
    作用域精确到 `(username, session_id)`——迟到/重试幂等（终态跳过），
    其他用户与同用户新登录代际不误伤。
  - 响应：`{"contract_version","revoked_view_sessions"}`。
- 周期授权复核（自然到期与推送失败的兜底）：流式循环（preview 与回退两
  分支）在状态检查点调用 WX introspect，节流 `wx_camera_recheck_seconds`
  （默认 2s）；续期端点先 force 复核——失效绑定不得靠续期复活。
  - introspect 判 `valid=false` → 立即 close（客户端 ≤5s 收到 EOF）。
  - WX 不可达：连续失败超过 `wx_camera_recheck_grace`（默认 1）→
    fail-closed close（不允许复核失联仍无限供流）；单次失败保留宽限防抖。
- 配置：`wx_camera_introspect_url`（空=复核关闭，仅推送生效）、
  `wx_camera_token_file`、`wx_camera_recheck_seconds`、`wx_camera_recheck_grace`
  （环境变量 `ARM_FLEET_WX_INTROSPECT_URL`/`ARM_FLEET_WX_CAMERA_TOKEN_FILE`）。

## WX 侧（sales_bot）

- `GET/POST /internal/wx-camera/v1/identity`：只反映调用者自身 Cookie 会话；
  200 携带四元组响应头；无有效会话 401。不改任何状态。
- `POST /internal/wx-camera/v1/introspect`：`X-WX-Internal-Token` 校验后按
  会话摘要判定 `{valid,reason}`；reason ∈
  `active|revoked|expired|user_disabled|credential_superseded|session_unknown`
  （与 resolve_session 同一失效语义：撤销/绝对到期/禁用/代际递增）。
- 撤销推送时机（提交后、非阻断）：退出（本会话）、管理员禁用/启用、管理员
  重置密码、本人改密（目标用户全部被撤销会话）。
- 推送失败不重试不放行：记录失败，Fleet 周期复核兜底停流。
- 配置：`WX_ARM_INTERNAL_TOKEN_FILE`（与 Fleet 同文件）、
  `WX_FLEET_INTERNAL_BASE`（空=推送关闭，记录一次告警）。
- 自然到期不依赖浏览器下一请求：WX 无需主动通知——Fleet 周期复核在
  ≤复核周期+宽限内读到 `expired` 并停流。

## 时间预算（验收口径）

以 WX 持久失效/自然到期为起点，旧流客户端 EOF ≤5.0s：
推送路径 ~即时（同步推送 + 流循环 ≤1s 检查点）；复核路径 ≤
`recheck_seconds(2s) + 检查点间隔(≤1s)`；WX 完全不可达 ≤
`grace(1)×2s + 1s`。开发直连（motor debug 台）无绑定零复核，行为不变。

## 验收断言（R4-12-D/E 归属）

- D：创建/续期/删除经正式代理需有效 WX 会话（匿名 401）；身份不可自报；
  客户端伪造 X-WX-* 头被边界剥离。
- E：退出/禁用/重置/自然到期 → 已建流 ≤5s EOF；他用户流持续可续期不受
  误伤；同用户新登录新会话可正常建流；旧会话不得借续期复活。
