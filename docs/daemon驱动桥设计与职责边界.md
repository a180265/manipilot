# Daemon 驱动桥（DaemonArmDriver）设计与职责边界

版本：design-v1.3，更新时间：2026-09-04。

### 2026-09-11 连续手势协议补充

此补充为向后兼容的 daemon TCP 特性扩展，不变更既有 `TAP`、
视觉 OCR/输入法校准、映射矩阵、下压深度、悬停高度和进给参数。
本轮源码增加，尚未重启现场 daemon；部署状态不能由文件内容推定。

| 协议键 | 中文名称及含义 |
|---|---|
| `STATUS.motionFeatures` | 连续手势特性列表，新增 `tap2.v1`、`swipe.v1` |
| `TAP2 depth Ffeed [Pdwell] Iinterval` | 连续双击；沿用校准 Z 参数，第一次完全释放后由控制器停顿，再执行第二次点击 |
| `SWIPE dx dy depth Fxy Zz` | 连续滑动；已悬停到起点后，按下、拖动和抬起连续执行 |
| `gesture_point` | 视觉手势点；必须带当前画面动态 `target_ref`，不能当作未经登记的固定坐标 |
| `interval_semantics=release_pause` | `interval_ms` 是完全抬起后的停顿，不是两次触屏的实测间隔 |

双击停顿 50~500ms；滑动时长仍由驱动限制为 150~5000ms，
实际 XY 进给由映射后的距离计算。daemon 再校验接触深度、Z 进给、
滑动距离大于 0.05mm 且不超过 150mm、滑动进给不超过 F3000。
原有 F300 禁用规则不变，禁止通过手势界面覆盖校准 Z 参数。

驱动在取得运动租约和任何走位之前检查特性。旧 daemon 缺失特性时
明确拒绝，不回退为两次慢点击或在屏幕上长按后再拖动。
单击继续使用原 `TAP`。连续手势仍受同一租约、事务、会话和急停保护；
每次串口写入前检查取消/停止，会话变化、连接丢失或响应未知后禁止重放，
也不猜测剩余位移去补抬或补点。

悬停与到起点保留原有校验；接触阶段不插入摄像头或位置查询等待，
整组完成、释放后再做视觉检查。控制器 ACK、画面变化、手机确实唤醒/
滚动到目标是不同层次的结果。运动有画面证据不等于业务效果已经确认。
双击识别与滑动实际时长受已校准 Z 行程、加减速和手机设置影响，
仍需有人值守实测；不得为追求双击时间擅自改动既有校准。
状态：**已实施（2026-08-31）**。运动租约、daemon 级急停锁定、设备级档案、驱动桥本体、
调试台适配与假 daemon 单测（tests/test_daemon_arm.py 14 项）全部落地；全量 pytest 141 通过。
协议联调（租约全周期/急停锁定-复位/无租约拒绝）已在真机 daemon 上验证；
真机运动冒烟（连接 → 候选栏干跑 → 正式点击）待设备连接后按 §七 顺序执行。

实施记录（2026-08-31）：
- `drivers/daemon_protocol.py`：协议常量、安全限值、业务目标中文名与动作模式（单一事实源，daemon/桥/调试台共用）。
- `scripts/arm_daemon3.py`：新增 CLAIM/RENEW/RELEASE/BEGIN_MOTION/END_MOTION/ESTOP_LOCK/RESET_ESTOP；
  普通运动（XY/Z/TAP）必须持租约且在 cmd_lock 内原子校验；ESTOP 为 daemon 级锁定并立即下发 !；
  租约绑定连接，断线/超时自动失效。
- `drivers/target_archive.py`：设备级档案读取/目标登记（data/devices/{device_id}/ 下
  calibration.json、targets.json、arm_profile.json）；旧 motor_debug_calibration.json、
  screen_targets.json 已废弃，不做迁移。
- `drivers/daemon_arm.py`：DaemonArmDriver（ArmDriver 完整实现）；业务 click 只接受 target_id，
  hover_only 目标不执行 TAP；conversation_item 明确拒绝（待 chat-send.v1 校验链路）；
  go_home=保持当前位置；move_target=移动并停在悬停高度；motion_verifier 挂钩保留，
  未验证 outcome_hint=unknown。
- `runtime/devices.py` driver_mode="daemon" 装配分支；`runtime/capabilities.py` 与
  `runtime/runtime.py` 新增 arm.move_target 能力。
- `scripts/motor_debug_server.py`+页面：设备编号隔离（data/devices/<设备>/）、调试运动自动
  CLAIM debug 租约并后台续期、急停锁定/复位按钮、目标面板按 v1.2 协议键（含中文显示名）。
关联文档：《WX总设计-十机械臂在线聊单系统.md》（职责边界与 WX 边界）、`docs/通用硬件任务运行时规划.md`（运行时）、`scripts/arm_daemon3.py`（串口守护）、`scripts/motor_debug_server.py`（电机调试台）、`examples/plugins/screen-ime-adapter/plugin.py`（输入法任务插件）。

---

## 一、背景与要解决的问题

当前系统里存在三个需要"动机械臂"的消费方，但它们与串口的关系没有统一：

| 消费方 | 现状 | 问题 |
|---|---|---|
| 电机调试台（8792） | TCP 客户端，连 arm_daemon3（127.0.0.1:8795） | 正常，但它是私有实现，不产出可复用组件 |
| 运行时任务执行（screen-ime 插件等） | `runtime.py:347` 将 `arm.click {target_id}` 交给驱动桥 | 业务目标需要由设备级目标档案和摄像头→机械坐标标定解析；运行时不得直开串口 |
| 独立标定/方向验证流程 | 设备上线前的调试与验收 | 必须通过对应 daemon 的调试接口执行；不得在 daemon 持有串口时再次直连 |

底层物理约束：ESP32 在串口打开瞬间被 DTR 复位、MPos 重新计数，因此**串口必须有唯一持有者**；这就是 arm_daemon3 存在的理由。缺口是一个**以 daemon 为后端、实现平台 `ArmDriver` 接口的驱动**，把所有平台内消费方统一到"daemon 独占串口、人人连 daemon"的模型上。这个驱动就是 **daemon 驱动桥**。

## 二、一句话定位

> daemon 驱动桥 = `drivers/arm.py::ArmDriver` 接口的 daemon 实现。
> 它把“具名目标 + 当前设备标定”翻译成 daemon 指令序列，是外部任务运行时与物理机械臂之间的唯一通道；串口的唯一持有者永远是对应的 arm_daemon 实例。这里的“业务目标”只表示可执行目标键，不表示 WX 业务判断。

```
插件(screen-ime) ──arm.click{target_id}──▶ runtime 能力层 ──▶ DaemonArmDriver（驱动桥）
                                                              │  ├─ 目标档案解析（calibration_bundle.json；旧 targets.json 仅显式导入）
                                                              │  ├─ 标定换算（calibration_bundle.json / 兼容 calibration.json）
                                                              │  └─ 会话锚点校验
                                                              ▼
                                                    TCP 127.0.0.1:8795  arm_daemon3
                                                              ▼
                                                    串口（独占）→ ESP32/GRBL → 机械臂
```

**端口边界：** 8788 只提供 Arm Fleet 管理/API；8795（以及 8796 起）是每台机械臂独立 daemon 的串口控制端口。8788 可以监督 daemon，但不得把 daemon 的串口持有、租约、事务和急停锁定合并进 8788 进程。

## 三、功能与负责范围

### 负责（v1 全部落地）

1. **`ArmDriver` 接口的完整实现**：`connect / disconnect / read_state / go_home / move_preset / move_target / click / stop / estop` + `driver_version` + `supports`，错误统一抛 `DriverError`（复用现有错误类别，中文显示值随类别带出）。
2. **固定业务目标解析**：`click({"target_id": "ime_candidate_bar"})` → 查设备目标档案（画面位置 + 中文显示名）→ 按当前设备标定换算机械坐标 → 校验会话/定位状态 → 生成完整运动序列。业务插件不直接提交裸位移。
3. **标定换算的运行时消费**：设备级 `camera → robot` 仿射由调试台/真机验收流程产出；驱动桥是执行侧的**唯一读取方**。OCR 只回答“画面看到了什么”，自动校准只回答“摄像头坐标如何转换为机械坐标”，两者通过带证据的目标观测衔接，不互相替代。调试点动、标定采样和方向验证属于运维/调试接口，不属于普通插件能力。
4. **会话/定位失效语义**：daemon 会话 UUID ≠ 标定记录的会话，或设备尚未完成当前会话定位时，除 `stop/estop/read_state` 外一切运动拒绝执行（`driver_uncalibrated`），并给出"重新定位/重新标定"指引。
5. **安全运动编排**：先回悬停高度（Z 相对分段 ≤9.5mm）→ XY 单条直达（单轴 ≤300mm，总位移 >300mm 拒绝）→ 执行 TAP（下压+抬起，可选停留）→ 回悬停。每步走 daemon 指令，daemon 侧限值（Z 行程≤10 / XY≤300 / Z 进给 30-2000 禁 F300 / XY 100-7000）之外，桥侧再做一层同规则预检（双保险）。
6. **软停止/急停语义**：`stop` = daemon STOP（进给保持）；`estop` = daemon 全局 STOP + daemon 级锁定 + 平台本地锁定（`reset_estop(operator)` 人工复位，复位后强制重查状态）。STOP/ESTOP 具有高于普通运动的优先级。
7. **审计与动作日志**：保留与 `RealArmDriver` 相同的 `action_log` 结构（每条 daemon 指令、响应、解析结果、锚点、失败原因），供运行时审计与证据链使用。
8. **发送键边界**：业务发送只接受 WX 当前 SendCommand，经 Arm Fleet 校验设备、账号、会话和正文哈希后调用固定目标 `send_button`。桥不维护长期有效的独立发送授权文件；调试台的发送按钮测试属于单独的运维/调试权限。

### 不负责（边界外，禁止做）

1. **不做业务判断**：何时点击、点完怎么办，由插件/编排层决定。总设计"机械臂项目不得自行生成回复、修改 SendCommand、自行重试"对桥同样生效。
2. **不做 WX 业务理解**：驱动桥可以消费观测桥提供的目标坐标/目标键并执行验证，但不识别客户身份、不做 OCR 语义判断、不判断白名单或销售状态。感知侧是独立调用方，“点开会话”在桥内只是一次具名目标点击。
3. **不拥有标定数据**：标定由人工在调试台/真机验收流程完成；桥只读设备级标定和目标档案。业务桥不写入标定事实源。
4. **不绕过安全门禁**：`require_confirmed_hardware`（硬件确认表）对桥同样生效；未确认时拒绝实例化真机模式。
5. **不直连串口、不发原始 G-code**：桥只讲 daemon 协议。基础运动指令沿用
   `CONNECT/STATUS/MPOS/XY/Z/TAP/STOP/RESUME`；为保证调试台与业务运行的运动隔离，
   v1.2 增加带中文说明的运动租约和事务控制指令：
   `CLAIM（申请运动租约）/RENEW（续期）/RELEASE（释放）/BEGIN_MOTION（开始动作事务）/END_MOTION（结束动作事务）/ESTOP_LOCK（急停锁定）/RESET_ESTOP（人工复位急停）`。

## 四、接口设计

### 4.1 驱动方法 → daemon 指令映射

| ArmDriver 方法 | 实现 | 备注 |
|---|---|---|
| `connect()` | TCP 连对应 daemon → 读欢迎行 → 发 `STATUS` 校验 `connected=true` → 读取当前会话/定位状态 | 不自动打开串口；串口连接由调试台或设备运维动作触发。当前 MPOS 不能自动视为永久安全原点 |
| `disconnect()` | 关 TCP（不发 DISCONNECT——串口连接归用户所有） | |
| `read_state()` | `MPOS` → `{position, controller_state, session_origin_mpos, estop_latched}` | |
| `go_home()` | 停止并保持当前位置 | 设备没有限位器，v1 不执行自动回零或猜测归位；WX 聊天流程结束位置由插件显式调用“输入法删除键位置”目标 |
| `move_target(id)` | 移动到设备目标并停在悬停高度 | 用于聊天结束后的停靠目标；只移动，不执行 TAP |
| `click(target)` | 见 4.2 | 核心方法 |
| `move_preset(id)` | 业务运行时拒绝；调试/验收端通过 daemon 的调试运动接口执行 | 六方向验证、标定采样和保守运动模板属于设备上线准备，不得被普通业务插件调用 |
| `stop()` | `STOP` | |
| `estop()` | daemon 级 STOP/锁定 + 平台本地 latch；`reset_estop(operator)` 人工复位 | 复位后第一次运动前强制状态和定位自检 |
| `supports()` | `{"arm.home","arm.move_target","arm.click","arm.stop","arm.estop"}`；不含 `arm.move_preset` | 能力声明收窄，越权调用被运行时拦截 |

### 4.2 click(target) 执行序列（核心）

```
业务输入：
  {"target_id": "ime_candidate_bar"}                      ← 固定业务目标

调试输入（仅运维/调试接口，不经普通插件 arm.click）：
  {"dx","dy"[, "tap_depth_mm", "feed_xy"]}              ← 裸位移/点动

具名目标路径：
  1. 会话/定位校验：daemon session 与设备标定匹配且当前定位有效？否 → driver_uncalibrated（"请重新定位/标定"）
  2. 查设备目标档案 → {中文显示名, nx, ny, hoverZ, tapDepth, tapFeed}
     send_button 仅接受已校验的 WX SendCommand；调试台测试走独立运维权限
  3. 换算：nx/ny × 标定 frameW/H → 摄像头像素 → robot = A·camera+b → 目标机械 XY
  4. 读当前 MPOS → 求 delta；总位移 >300mm → driver_limit_exceeded（映射疑似异常）
  5. Z → 悬停高度（相对分段）
  6. XY 单条直达（dx/dy 同一条相对 XY 指令）
  7. TAP tapDepth F tapFeed（+可选 P 停留）
  8. 回悬停；返回 {ok, position, target, resolved_robot_xy, segments, motion_verified, outcome_hint}
```

每步前后保留 `motion_verifier` 挂钩（与 RealArmDriver 相同）：配置了 `CameraMotionVerifier` 时逐段验证；未验证时 `outcome_hint="unknown"`，**不得当成功**（沿用总设计"ok/MPos 只代表控制器接受"的现场规则）。

### 4.3 目标档案协议（运行时唯一事实源为统一校准档案）

```jsonc
// data/devices/{device_id}/calibration_bundle.json.targets —— 只存画面坐标，机械坐标永远现算
// data/devices/{device_id}/targets.json —— 旧档案，仅通过显式迁移导入，不参与运行时决策
{
  "ime_candidate_bar": {"name_display": "输入法候选栏", "target_mode": "tap", "nx": 0.51, "ny": 0.83, "hoverZ": 2.0, "tapDepth": 3.0, "tapFeed": 1500, "ts": "..."},
  "ime_delete_key":    {"name_display": "输入法删除键位置", "target_mode": "hover_only", "nx": 0.10, "ny": 0.83, "hoverZ": 2.0, "ts": "..."},
  "send_button":       {"name_display": "发送按钮", "nx": 0.51, "ny": 0.90, "hoverZ": 2.0, "tapDepth": 3.0, "tapFeed": 1500, "ts": "..."},
  "back_button":       {"name_display": "返回主界面", "nx": 0.05, "ny": 0.05, "hoverZ": 2.0, "tapDepth": 3.0, "tapFeed": 1500, "ts": "..."}
}
```

- **有效性规则**：目标本身无"过期"概念；有效 = 当前设备标定可用、会话/定位匹配、摄像头流属于绑定设备。摄像头或手机挪动后必须重新标记；无限位设备重启后的映射恢复策略另行确认。
- 目标登记入口 = 电机调试台「输入法目标」面板（已实现，2026-08-31）。
- WX 完整发送闭环结束时不使用抽象 `go_home`，而是调用 `move_target("ime_delete_key")`，移动到并停在设备级“输入法删除键位置”，不执行 TAP，以退出输入区域并避开新消息 OCR 区域。
- 业务发送授权不落在目标档案中，由 WX 当前 SendCommand 和 Arm Fleet 的账号/会话/正文哈希校验共同完成；调试台运动使用运维权限和审计。

### 4.4 错误类别映射（全部复用现有登记，不新增类别）

| 情形 | DriverError 类别 |
|---|---|
| daemon 不可达 / 串口未连接 | `driver_connect_failed` / `driver_disconnected` |
| 标定缺失或锚点不匹配 | `driver_uncalibrated` |
| 目标未登记 / 无授权 | `driver_policy_rejected` |
| 超行程 / 超限速 / 总位移超限 | `driver_limit_exceeded` |
| daemon 返回 ERROR（控制器拒绝、超时） | `driver_motion_failed` / `driver_timeout` |

## 五、与系统的匹配性（逐项核对）

| 系统组件 | 匹配结论 | 说明 |
|---|---|---|
| `runtime.py` 能力层 | ✅ 零改动 | `arm.click` 本来就透传 `driver.click(params)`；桥实现后 target_id 自然生效 |
| `runtime/devices.py` 装配 | ✅ 小改动 | `_create_drivers` 增加 `driver_mode="daemon"` 分支：构造 `DaemonArmDriver(device_id, daemon_addr=cfg.get("daemon_addr","127.0.0.1:8795"))`，并照常挂 `CameraMotionVerifier`；`require_confirmed_hardware` 门禁照走 |
| screen-ime 插件 | ✅ 零改动 | `tap_candidate` 步骤的 `arm.click {"target_id": "ime_candidate_bar"}` 直接被解析；后续 `tap_send` 步骤复用同一机制 |
| 电机调试台（8792） | ✅ 并存但运动隔离 | 调试台保留自己的 DaemonClient；与业务桥可以同时连接同一 daemon，但运动必须取得调试/业务租约。业务租约期间调试台默认只读；STOP/ESTOP 高优先级。 |
| 独立标定/方向验证流程 | ✅ 通过 daemon | 正式流程通过对应 daemon 的调试接口执行；禁止在 daemon 持有串口时再由脚本直开串口。 |
| `FakeArmDriver` 与既有测试 | ✅ 不受影响 | 桥是新增实现；桥自身用"内嵌假 daemon 服务器"做单元测试（模拟协议、锚点变化、限值拒绝） |
| WX 聊天机器人 | ✅ 完全隔离 | WX 通过 8788 版本化 HTTP/JSON 提交已批准任务并接收结果；WX 监听器消费观测桥事件完成业务判断。目标档案与标定文件是 arm_fleet 私有资产，WX 不读取坐标、不连接 daemon；驱动桥不承担 WX 身份、白名单或销售判断 |
| 感知侧执行器（未来：发现新消息/点开会话） | ✅ 预留 | "点开会话列表第 N 项"在桥的眼里也是一次目标点击；感知执行器只需产出目标（静态档案或未来动态坐标），复用同一运动编排 |
| 10 臂扩展 | 🟡 设计已定、待实现 | 每个串口一个 daemon；设备菜单维护 arm/camera/daemon 绑定。测试阶段 8790 按 `camera_id` 分流，正式软件优先直接采集摄像头。 |

## 六、安全设计（双保险模型）

1. **桥侧预检**（执行前，纯计算，不下发）：进给范围、单段行程、总位移上限、会话/定位、目标存在性、业务 SendCommand 校验，以及当前运动租约和事务 ID。
2. **daemon 侧强制**（执行时）：同样的限值再拦一遍（对齐 `drivers/grbl.py` 现场策略），F300 永久禁用。
3. **运动学保守化**：Z 优先回悬停再平移（悬停=设备级校准值）；XY 单条直达，单轴 ≤300mm；总位移 >300mm 判为映射异常直接拒绝。
4. **失效即停**：会话或定位不匹配时只放行 `stop/estop/read_state`；运动一律 `driver_uncalibrated`。
5. **运动租约**：业务桥和调试台均可连接 daemon，但同一串口同一时刻只能有一个运动租约；普通运动必须持有租约，点击序列从 `BEGIN_MOTION` 到 `END_MOTION` 持有同一事务锁；`STOP/ESTOP` 不受普通租约阻塞。租约申请、中文说明、续期、释放、超时和断线回收均由 daemon 强制执行。
6. **审计**：每条指令进出、解析出的目标坐标、会话/定位、运动租约、SendCommand 校验和失败原因都进 `action_log` 并可在运行时审计中追溯；管理动作必须带操作者与中文理由。

## 七、实现清单（确认后按此开工）

| # | 文件 | 内容 | 规模 |
|---|---|---|---|
| 1 | `drivers/daemon_protocol.py` | 协议常量、指令编解码、安全限值（调试台与桥共同引用） | ~80 行 |
| 2 | `drivers/daemon_arm.py` | `DaemonTCPTransport` + `DaemonArmDriver(ArmDriver)`，业务目标解析与运动租约 | ~350 行 |
| 3 | `drivers/target_archive.py` | 按 `device_id` 加载目标档案、标定换算、中文显示映射 | ~150 行 |
| 4 | `runtime/devices.py` | `driver_mode="daemon"` 装配分支 + `camera_frame_url`/`daemon_addr` | ~40 行 |
| 5 | `tests/test_daemon_arm.py` | 内嵌假 daemon：会话/定位拒绝、目标解析、分段/悬停、运动租约、STOP 优先级、F300 拦截、unknown 语义 | ~300 行 |
| 6 | `motor_debug_server.py`（可选小改） | 目标档案读取改引 `drivers/target_archive.py`，消除双实现 | ~10 行 |

实施顺序：1 → 3 → 2 → 5（先离线测试全绿）→ 4 → 真机冒烟（连接 → click 候选栏干跑【Z 抬高不触屏】→ 正式点击）。

## 八、验收标准

1. 8788 创建 screen_input 任务（文本）后，全链路可在不动调试台的情况下完成"下发 → displayed → 机械臂点候选栏 → typed"。
2. 业务桥和调试台同时连接时，业务运动租约期间调试台不能插入普通运动；STOP/ESTOP 可以及时打断。
3. 业务发送只能由已校验的 WX 当前 SendCommand 调用 `send_button`；调试台测试与业务发送审计隔离。
4. 桥的单元测试在无真机环境下全绿（假 daemon 覆盖上述故障注入）。
5. 旧 `RealArmDriver` 路径仅作为兼容/离线验收路径保留；正式标定、方向验证和业务运行统一通过对应 daemon，禁止与 daemon 并行直连同一串口。
6. WX 完整发送闭环必须覆盖：用户主页确认 → 新消息用户/会话确认 → 打开会话 → 原样输入 → 发送取证 → 退出会话 → 移动并停在“输入法删除键位置”（不点击）→ 回到适合 OCR 新消息的画面并确认。

## 九、实施约束（已确认）

1. 无限位机械臂的标定、方向验证、悬停高度和停靠目标均绑定当前 daemon 会话。机械臂不重启、串口不重连、摄像头与手机位置不变化时，业务可以持续使用已验收映射。
2. 机械臂重启、串口重连、daemon 重新建立物理会话，或设备绑定发生变化后，旧映射必须立即标记为不可用；业务点击全部拒绝，必须由调试台重新定位并重新校准后才能恢复。
3. 调试台负责处理和保存重要设备参数，包括摄像头→机械坐标映射、方向验证结果、悬停高度、点击深度、进给范围和停靠目标。业务运行时只读取这些已验收参数，不自行修改。
4. 每台设备的参数按 `device_id` 隔离保存；运行时以 `calibration_bundle.json` 为统一事实源，旧 `calibration.json`、`targets.json` 和 `arm_profile.json` 仅作为兼容输入或迁移产物。

---

## 十、v1.1 已确认决策（与前文冲突时以本节为准）

### 10.1 摄像头取流与设备绑定

1. 测试阶段 Mac mini 不依赖命令行进程直接获取摄像头权限，统一使用 Chrome 取流桥。
2. 当前单路测试固定使用 `http://127.0.0.1:8790`：
   - `GET /latest.jpg` 获取最新 JPEG；
   - `GET /status` 获取是否有画面和帧序号；
   - `X-Frame-Seq` 用于判断取流是否持续更新。
3. 多设备阶段必须提供设备菜单页，维护 `device_id / arm_slot / camera_id / daemon_addr / camera_frame_url` 的绑定关系。
4. 当前 8790 的单路全局帧只用于测试；十设备阶段必须按 `camera_id` 分流，禁止所有设备读取同一份 `latest.jpg`。

### 10.2 真机准备与业务运行

1. 标定、方向验证、保守运动验证属于真机上线前的调试/验收流程，不属于普通业务插件能力。
2. 正式标定和方向验证必须通过对应 daemon 的调试接口完成；daemon 持有串口时，禁止其他脚本再次直开该串口。
3. 调试完成后保存设备级标定、方向验证结果和安全姿态；WX/Arm Fleet 业务运行只调用固定业务目标或受控的动态会话目标。
4. v1 暂不支持 `kb:<key>` 逐键键盘点击；输入法路径只使用固定的候选栏目标。

### 10.3 daemon、客户端与运动隔离

1. 一个物理串口对应一个 daemon 实例；十台设备最终使用十个 daemon 实例或十个独立 daemon 端口。
2. daemon 的多客户端是电机调试台客户端与 Arm Fleet 运行时客户端；WX 只通过 Arm Fleet HTTP/JSON，不直接连接 daemon。
3. `XY / Z / TAP` 以及由它们组成的点击序列都属于普通运动，必须取得对应的运动租约。
4. 业务运动租约和调试运动租约互斥；业务任务持有租约时，调试台默认只读。
5. `STOP / ESTOP` 为高优先级控制，不得被普通运动锁阻塞；ESTOP 必须形成 daemon 级锁定，不能只在某个客户端本地锁定。

### 10.4 业务目标与中文映射

1. 业务固定目标统一使用稳定协议键，并提供中文显示映射：

   | 协议键 | 中文显示名 |
   |---|---|
   | `ime_candidate_bar` | 输入法候选栏 |
   | `ime_delete_key` | 输入法删除键位置 |
   | `send_button` | 发送按钮 |
   | `back_button` | 返回主界面 |
   | `conversation_item` | 微信会话入口（动态目标，必须带外部会话编号） |

2. 协议键可保留英文，控制台、日志、审计和人工队列必须显示中文名称。
3. 普通业务 `arm.click` 只接受 `target_id`；裸位移仅限调试/标定接口，不得由普通插件提交。
4. 调试层可以使用点动和其他受控低级运动，但必须经过运维权限、运动租约和审计。

### 10.5 标定事实源

1. 业务点击使用唯一的设备级 `camera -> robot` 标定。
2. 建议产物目录为：

   ```text
   data/devices/{device_id}/calibration.json
   data/devices/{device_id}/targets.json
   data/devices/{device_id}/arm_profile.json
   ```

3. `configs/screen_calibration.json` 和 screen-ime 自有 mapping 只能作为旧流程/插件内部数据，不能与设备级硬件标定并列作为业务点击事实源。
4. 设备菜单调整机械臂与摄像头关系后，原设备级标定必须标记为不可用，完成重新标定后才能恢复业务运动。

### 10.6 发送边界

1. 业务发送由 WX 当前 SendCommand 管控。
2. Arm Fleet 负责校验指定设备、微信账号、外部会话和正文哈希，然后调用固定目标 `send_button`。
3. daemon 驱动桥不维护长期有效的 `screen_targets_auth.json` 发送授权文件。
4. 调试台发送按钮测试属于独立运维操作，不得伪装成业务发送。

### 10.7 无限位设备的会话规则

机械臂没有限位器，不能宣称存在真正的硬件“回零”。标定结果属于当前 daemon 物理会话：会话不变且设备相对位置不变时可以持续使用；机械臂重启、串口重连或 daemon 新建物理会话后，旧 MPos 的物理意义视为失效，必须由调试台重新定位并重新校准。方向验证和安全参数可以作为设备验收记录保留，但不能替代新会话的坐标校准。

---

## 十一、v1.2 已确认决策（覆盖前文未明确处）

### 11.1 运动租约协议

daemon 协议允许多客户端连接，但普通运动必须先取得 daemon 内部运动租约。租约和事务指令必须同时提供稳定协议键与中文说明，控制台、日志和审计显示中文说明。

```text
CLAIM         申请运动租约
RENEW         续期运动租约
RELEASE       释放运动租约
BEGIN_MOTION  开始动作事务
END_MOTION    结束动作事务
ESTOP_LOCK    急停锁定
RESET_ESTOP   人工复位急停
```

租约至少包含 `lease_id / owner_type / owner_id / task_id / expires_at`。一次点击从开始到回悬停必须使用同一租约和事务 ID；断线、超时或 daemon 重启时自动失效。`STOP/ESTOP` 不受普通运动租约阻塞。

### 11.2 摄像头取流模式

1. 测试模式使用 Chrome 取流桥，按 `camera_id` 分流；建议接口为：

   ```text
   POST /cameras/{camera_id}/frame
   GET  /cameras/{camera_id}/latest.jpg
   GET  /cameras/{camera_id}/status
   ```

2. 现有 `http://127.0.0.1:8790/latest.jpg` 仅保留为单路兼容入口，不得作为十设备正式地址。
3. 十路测试取流属于临时兼容能力，帧率、分辨率和 JPEG 质量必须可配置；默认采用较低帧率，避免十路同时编码造成 Mac mini 不必要的 CPU 压力。
4. 正式多设备默认由已获 macOS 摄像头权限的 `CameraHelper.app` 采集并经 8790 分路上传；设备配置必须明确
   `camera_source_mode=camera_helper|direct|chrome_bridge`。`direct` 仅在明确配置时允许，`chrome_bridge` 仅为降级来源。

### 11.3 WX 完整发送闭环和动态会话目标

完整 `chat_send` 必须覆盖：用户主页确认、新消息用户/会话确认、打开会话、原样输入、发送取证、退出会话、移动并停在“输入法删除键位置”（不执行 TAP）、OCR 主界面确认。

动态会话不得拼接成任意目标字符串，统一使用稳定目标键加参数：

```json
{
  "target_id": "conversation_item",
  "target_ref": {"external_conversation_id": "..."}
}
```

Arm Fleet 只接受已校验的 `chat-send.v1`，并在点击前再次核对设备、微信账号、外部会话和正文哈希。目标档案只保存固定位置；动态会话位置由 WX/感知插件提供并由摄像头确认。

### 11.4 旧标定文件处理

旧的 `data/motor_debug_calibration.json`、`data/screen_targets.json` 和 screen-ime 自有 mapping 直接废弃，不做运行时自动迁移，不得与设备级档案并列读取。正式运行唯一读取：

```text
data/devices/{device_id}/calibration_bundle.json

旧 `calibration.json`、`targets.json`、`arm_profile.json` 仅在显式兼容导入或无统一档案的已部署旧设备回退路径中读取；一旦存在 `calibration_bundle.json`，不得再用旧文件覆盖其中的运行时事实。
```

### 11.5 归位和聊天结束位置

由于设备没有限位器，`arm.home` 在 v1 中表示“停止并保持当前位置”，不宣称硬件回零。WX 聊天任务完成后的清理动作由插件显式调用 `move_target("ime_delete_key")`，使机械臂移动到并停在输入法删除键位置，不执行 TAP，同时尽量保持摄像头对新消息 OCR 区域的无遮挡；随后必须执行主界面确认。当前位置未知、摄像头不可用或删除键停靠目标未标定时，任务不得伪造成功，设备进入人工处理路径。
