# ime-pairing.v1 — 手机输入法一次性配对与凭据撤销契约

规格来源：《2026-10-05-v1.10身份与局域网实施附件》P/N 部分（D02/D03）。版本 1.0。
错误外壳 `ime-pairing-error.v1`；所有响应顶层含 `contract_version` 与 `request_id`。

## 身份模型

- 每台手机一个不可变 `ime_device_id`；同一 `server_id` 内 `phone_instance_id`
  恒映射同一 `ime_device_id`；重装/新实例生成新 `ime_device_id`，不自动抢旧绑定。
- 凭据：32 字节随机 `credential_token`（服务端只存 SHA-256 摘要）；
  每个 `ime_device_id` 至多一个 active 凭据；`credential_generation` 随重配/撤销单调递增。
- 配对不改变 input_binding、输入开关、校准资格或 unknown 状态。

## 状态机

- invitation：`offered → consumed | expired(5min) | revoked`
- pairing：`pending → active | expired(activation_deadline 10min) | revoked`
- credential：`pending → active | revoked`（pending 不能领取任务/报几何）

## Fleet 本机管理路由（调试台后端机器身份转发；手机不可调用）

| 方法/路径 | 请求 | 成功 |
| --- | --- | --- |
| POST `/v1/ime-pairing/invitations` | `request_id, invite_secret(43 字符 base64url), arm_id?` | 201 `invitation_id, server_id, expires_at, trust{https_origin, ca_cert_b64, spki_sha256}`；同号同摘要重放原邀请（200 + `replayed=true`，掉包同号恢复可辨） |
| GET `/v1/ime-pairing/invitations/{id}` | — | 200 `status, expires_at, phone摘要|null`（不回传秘密） |
| POST `/v1/ime-pairing/invitations/{id}/revoke` | `request_id` | 200 状态（offered→revoked；consumed 不隐式撤销手机） |
| GET `/v1/ime-phones` | — | 200 `items[{ime_device_id, phone_instance_id, state, revision, credential_generation, pairing摘要}]`（不含 token/hash） |
| POST `/v1/ime-phones/{ime_device_id}/revoke` | `request_id, expected_revision, reason` | 200 新 `revision, credential_generation`；CAS 对账，同号掉包重放原结果 |

## 手机 LAN 路由（受控 LAN HTTPS；N1 拓扑）

| 方法/路径 | 认证 | 说明 |
| --- | --- | --- |
| POST `/api/v1/pairings/confirm` | 邀请秘密（body） | `request_id, invitation_id, invite_secret, phone_instance_id, phone_label, credential_id, credential_token` → 201 `pairing_id, ime_device_id, state=pending, revision, activation_deadline`；同原请求 200 原结果 |
| GET `/api/v1/pairings/{pairing_id}` | `x-ime-phone-token` | 200 pending/active 非秘密字段 |
| POST `/api/v1/pairings/{pairing_id}/activate` | `x-ime-phone-token` | `request_id, expected_revision` → 200 `active, ime_device_id, credential_id, generation, revision` |

## 错误闭集（ime-pairing-error.v1）

`invalid_request`(400)、`authentication_required`(401)、`phone_scope_denied`(403)、
`pairing_not_found`(404)、`method_not_allowed`(405)、
`invitation_consumed / phone_already_paired / pairing_pending / revision_conflict / idempotency_conflict`(409)、
`invitation_expired / pairing_expired`(410)、`rate_limited`(429)、
`persistence_unavailable / clock_untrusted`(503)。
无效秘密统一 401（不泄漏他人配对资料）。确认失败限每可信来源每分钟 10 次（持久窗口，
重启不清）；重试同一有效请求不消耗新名额。

## 时钟与撤销

- 邀请有效期 5 分钟，服务重启不延长；服务器时钟回退（相对持久水位）→ `clock_untrusted` 拒绝确认。
- 撤销：一个事务推进 `phone.revision`/`credential_generation`，active+pending 全 revoked，
  几何 session/nonce 失效；迟到 activate/旧 confirm 重放不得恢复身份；失效 token 统一 401。
- 任务 owner/交付事实记录原 `credential_id` 与 `generation`；同 `ime_device_id` 的新配对不得确认旧代际输入。

## 传输（N2）

受控 LAN HTTPS；首次信任经配对码中的公开 CA 证书与服务器 SPKI SHA-256 传递；
校验指纹/链/有效期/主机名后才发送秘密。TLS≥1.2；无 trust-all、无 HTTP 降级。
