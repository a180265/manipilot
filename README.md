# Manipilot 通用机械臂执行平台

Manipilot 是一个与具体业务无关的机械臂 + 摄像头 + 屏幕执行平台（SDK）：设备驱动、通用界面动作、任务租约、执行证据、屏幕输入法、统一校准和调试台。业务应用（RPA、自动化流程等）作为外部插件经版本化 HTTP/JSON 契约接入，平台不内置任何具体业务流程。

> 本仓库完全自包含：平台核心与任何外部业务系统零代码耦合（有 AST 守卫测试固化）；通用服务签名等通道能力位于可选的 `extensions/` 扩展机制，本仓库不发行任何具体业务扩展，剔除后平台行为完整。

## 功能组成

- **运动控制**：GRBL/串口驱动、daemon 驱动桥（8795）、任务租约/设备锁/归位/急停/人工接管、`fake_arm` 仿真
- **摄像头链路**：帧桥（8790）+ 预览（8793）、`tools/camera_helper`（macOS 授权取流 CameraHelper.app）、帧保留策略
- **OCR 与屏幕感知**：Apple Vision 本地 OCR（`adapters/vision`）、文字定位点击、手机定位（`phone_localization`）、屏幕姿态/边缘/亮屏唤醒
- **统一校准栈**：校准向导 + bundle、屏幕校准、ui_targets 目标登记、探头运动、校准门禁、视觉验证、恢复/re-stamp
- **手势与滑动**：手势程序/配方/收藏手势、自动手势、校准直驱滑动（`calibrated_scroll`）、滑动视觉判定、layout 布局识别家族（`adapters/uinav` + `runtime/layout_*`）
- **屏幕输入法**：`adapters/screen_ime` 全套（候选栏下发文本 + iOS/macOS 客户端 + 自动配对 + LAN TLS 代理）
- **多设备任务运行时**：队列/租约/eligibility/能力注册/插件框架、证据链、回调 outbox、设备发现与注册
- **API + 契约 + 控制台**：8788 HTTP 服务、`api/contracts/` 版本化契约、TS/Vite 调试台（console/，8792）、通用服务签名原语（`runtime/service_signing.py`）

## 自包含声明

- **摄像头授权取流**：`tools/camera_helper/dist/CameraHelper.app` 构建产物随仓发行（macOS 摄像头权限由它持有，向 8790 帧桥推帧）；
- **OCR**：Apple Vision 引擎源码（`adapters/vision/vision_ocr.swift`）在仓，首次调用由 swiftc 现场编译到 `data/bin/`（部署缓存，不入 git）——无任何外部服务依赖；
- **全新部署自举**：绑定与安装指纹是本机部署态（不入 git）。初始化：`python -c` 调 `scripts.camera_binding_v2.generate_control_token(<data>/camera_helper/control.token)` 生成控制令牌、`save_binding_v2(...)` 播种摄像头绑定（deviceUniqueId 来自 helper 报告的物理摄像头标识）、`CAMERA_HOST_INSTALLATION_ID_FILE` 首启自动生成。

## 快速开始

```bash
# 环境（Python ≥3.11）
uv venv --python 3.11 .venv
uv pip install -e ".[server,real]" pytest

# 后端（默认 127.0.0.1:8788；硬件模式由配置与安全门禁决定，未确认时回退仿真）
.venv/bin/python -m agent

# 调试台（127.0.0.1:5174）
cd console && npm install && npm run dev

# 测试
.venv/bin/python -m pytest tests/ -q -n 4

# 真机只读检查（串口 ? + 摄像头采集，零运动；需先完成 configs/硬件输入确认表.json）
.venv/bin/python scripts/g_live_readonly.py
```

设备绑定：复制 `configs/devices.json`（模板）按现场填写；`configs/硬件输入确认表.json` 未全部确认时强制仿真，真机驱动拒绝启用。

### 摄像头链路（真机视觉，2026-10-12 实测顺序）

```bash
# 1) 帧桥（宿主安装指纹缺失时会自动生成到 data/camera_source/）
.venv/bin/python -B -m scripts.camera_frame_server --fleet-db data/armfleet.db --evidence-dir evidence/store
# 2) 预览面（helper 的预览平面）
.venv/bin/python -B -m scripts.camera_preview_server
# 3) 摄像头辅助程序（GUI）。必须先注入两个环境变量，缺一会在界面报"V2 认证失败/不可用"：
launchctl setenv CAMERA_CONTROL_TOKEN_FILE "$PWD/data/camera_helper/control.token"
launchctl setenv CAMERA_HOST_INSTALLATION_ID_FILE "$PWD/data/camera_source/host_installation_id"
open tools/camera_helper/dist/CameraHelper.app     # GUI 里：刷新摄像头 → 启动通道（每行可设推送帧率，默认 8，详见 tools/camera_helper/README.md）
# 4) 设备调试台（连接设备/点动/校准向导，8792）
IME_BASE_URL=http://127.0.0.1:8788 .venv/bin/python scripts/motor_debug_server.py
```

- 控制令牌与宿主安装指纹是**部署态**（`data/` 不入库）：令牌经 `scripts.camera_binding_v2.generate_control_token` 生成；安装指纹为 36 位小写 UUID（帧桥启动自举兜底生成）。摄像头绑定记录同理，可经 `camera_binding_v2.save_binding_v2` 播种或走 helper GUI 绑定（运行时换绑写受设备安全视图保护：平台侧配 `FLEET_SAFETY_KEYS_FILE`、帧桥侧配 `CAMERA_FLEET_SAFETY_*` 后闭环可用，未配置时按设计 fail-closed 拒绝。）
- `configs/devices.json` 以占位符示例入库；真机部署按现场修改后**按文件名精确 `git add`，不要整库 `-A`**（避免把本机硬件标识带进公开历史）。

## 开发边界（平台宪法，见 AGENTS.md）

- 平台只提供驱动抽象、能力注册、任务运行时、租约/锁、证据、安全和插件接口；不做客户画像、价格/档期事实、草稿生成、发送许可或自动重试，也不内置刷视频、拍照、录屏、桌面巡检等业务插件。
- 业务应用经 `api/contracts/` 的版本化 HTTP/JSON 契约接入；插件不 import 平台内部 Python 包、不读平台数据库。
- 动作后无法确认结果时必须报告"结果未知"，不能猜成"明确失败"或"已完成"。
- 控制台、日志摘要、报告使用中文业务关键词；英文只作协议键与稳定编号。

## 目录

`agent/` 设备常驻进程 · `drivers/` 机械臂/摄像头/输入驱动 · `adapters/screen_ime` 屏幕输入法 · `adapters/vision` OCR 与视觉 · `adapters/uinav` 列表 UI 导航规划 · `runtime/` 队列/租约/状态机/校准门禁 · `api/` HTTP 服务与契约 · `console/` 调试台前端 · `tasks/` 插件接口 · `examples/plugins/` 示例插件 · `fakes/` 仿真 · `tests/` 协议与故障注入测试 · `docs/` 平台文档

## 测试基线

基线（2026-10-12）：**4290 绿 / 30 红 / 240 跳过 / 0 错误**。30 红为平台继承的存量评审债（手势接线 7、串口重枚举 6、手机姿态终审 6、布局授权 4、裁剪匹配 2、线契约/能力预检/调试台屏证/摄像头代理各 1）加 1 例顺序敏感用例（隔离跑绿）；240 跳过 = 以业务示例插件为载具的端到端流量用例（本仓不发行业务示例插件，见各文件 skipif 说明），后续以通用示例载具重建。

## 设备绑定表（configs/devices.json）行为说明（2026-10-29）

- **写入是"按设备读改写"**：运行期绑定变更（相机/串口/daemon/微信号/IME、禁用/恢复）走
  `runtime.config.update_device_config`，在 `configs/.devices.lock` 的 flock 内重读磁盘、
  只合并该设备的字段并原子写。整表接口 `save_device_configs` 仅用于初始化/迁移/测试。
- **写盘投影白名单 `STATIC_KEYS`**：落盘只保留静态字段，`daemon_session`/心跳/质量等运行时键
  不落盘；**手写在 devices 条目里的自定义字段会在下一次绑定变更时被剥掉**（需要持久化的新字段
  必须先登记进 `STATIC_KEYS`）。非 JSON 值（函数/Path）一律不落盘。
- **失败语义**：绑定表写不进去时返回 `503 device_config_write_failed`（中文标题+建议），
  内存与数据库保持原样；锁被其它进程占用返回 `409 device_config_busy`；文件损坏返回
  `400 device_config_invalid` 且**原文件不动**。
- **未知动作门禁**：设备存在未核对的动作结果（`unknown_hold_task_id`/`unknown_hold_task_ids`）
  时，变更绑定返回 `409 device_unknown_hold`，须先完成核对/收尾。
- `.gitignore` 已忽略 `configs/.devices.lock` 与 `configs/.devices.json.*.tmp`（运行时生成）。
