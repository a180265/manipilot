# 屏幕输入法适配（screen_ime）

把 AI_demo 工作区的 K40 输入法体系（`K40InputMethod` + `K40TaskServer`）移植进 Arm Fleet，
用于**向手机屏幕下发文字输入**和**校准机械臂对屏幕的点击坐标**。

- 手机端（Android K40 输入法 APK、iOS 键盘扩展）通过 `/api/v1/*` 设备侧协议与本适配通信，
  旧输入/校准任务沿用 K40TaskServer 协议；新增 `gesture-capture.v1` 路由是独立协议，
  客户端须显式支持，不能由旧任务兼容性推断新采样能力。
- 机械臂仍然只通过平台运行时的能力调用（`arm.click` 等）执行物理动作；
  真机运动受平台硬件安全门禁统一管控。代码及历史仿真记录不证明当前设备已连接、
  校准就绪或新手势真机效果已验收。

## 模块

| 模块 | 职责 |
|---|---|
| `service.py` | 任务服务核心：任务/心跳/取消控制/长轮询，SQLite 持久化（`data/screen_ime_tasks.db`） |
| `calibration.py` | 屏幕校准会话协调器（内存态；服务重启失效，未完成会话转人工） |
| `geometry.py` | 仿射拟合 `screen = A·robot + b`、残差校验、按 deviceId 持久化映射（`data/screen_ime_mappings.json`） |
| `keyboard_layout.py` | 系统键盘布局模板：字符→键位（归一化）、大写/空格/回车动作序列、内置 26 键 QWERTY 示例 |
| `camera_calibration.py` | 固定机位摄像头定位：色标检测、摄像头→屏幕单应、探头尖识别 |
| `camera_source.py` | 摄像头帧源（OpenCV 懒打开；测试注入静态帧） |
| `routes.py` | `/api/v1/*` FastAPI 路由（与 K40TaskServer 路径字段一致） |
| `gesture_capture.py` / `gesture_routes.py` | `gesture-capture.v1` 手机触摸采样协调器及独立设备侧路由；仅启用输入法适配时挂载 |
| [screen-ime-adapter](../../examples/plugins/screen-ime-adapter/manifest.json) | 屏幕文字输入任务 + 屏幕校准采样任务的插件清单与实现 |
| `ios/` | FleetIME Swift 包：跨平台协议客户端（macOS 可运行验证）+ iOS 键盘扩展源码 |

## 固定机位摄像头定位（校准页已适配）

摄像头与手机都固定时，"摄像头画面 ↔ 手机屏幕"是固定透视映射，两段式标定：

1. **屏幕段（摄像头→屏幕）**：手机打开校准页 `?base=...&device=...` → "摄像头标定"标签 →
   进入标定画面（默认四角 ArUco ID 1–4，页面回报屏幕像素）→ 点"回报标定点"。
   服务端优先按 ID 检出 4 个标码，只有请求提供颜色时才回退到 HSV 色点检测，
   拟合单应矩阵并按设备存档（通常为 `data/screen_ime_camera.json`，带版本号）。
   残差须结合当前帧与现场标定结果核对，不能预设小于 1px。
2. **机械臂段（机械→屏幕）**：机械臂探头贴品红标记，移到 3 个不共线位置，每个位置调用
   `POST /api/v1/screen-ime/camera/robot-point {deviceId, robotX, robotY}` → 返回换算后的
   屏幕像素；凑齐 3 组后调 `POST /api/v1/screen-ime/camera/robot-complete {deviceId, pairs}`
   → 仿射拟合（残差阈值 3px）→ 存入正式映射表。**下游（打字/图标点击）与手机触点回报法
   完全同格式，零改动。**

接口：`GET /api/v1/screen-ime/camera/status`（相机与标定档案）。
依赖 OpenCV（`pip install opencv-python-headless numpy`，已含在 pyproject `real` extra）；
正式服务按 `device_id` 解析设备配置中的 `camera_id` 和 8790 分路帧源；
未配置时拒绝猜测全局 `/latest.jpg`，旧单路模式须显式开启。直连 OpenCV
帧源仅供明确的单路兼容路径，懒打开；服务启动不会因此自动占用 UVC 摄像头。

边界：色标检测依赖光线/曝光（固定机位建议锁定曝光）；单应按 deviceId 存档。
仅摄像头机位变化时，须重做第 1 步并核验既有机械↔屏幕映射及平台校准资格；
手机或夹具移动后，机械↔屏幕关系也已改变，须重做第 1、2 步并重新通过现场门禁，
不能只重拍标靶。机械移动仍走平台安全门禁，摄像头只做定位测量。
手机网页版（`/screen-ime/phone`）已内置标定画面与回报按钮；触点回报法（原校准流程）仍然可用，两者产出的映射同格式。

## 三种输入方式（按可靠性与成本选择）

| 方式 | 任务类型 | 机械臂动作 | 适用 |
|---|---|---|---|
| 输入法候选栏（推荐） | `screen_input` | **1 次**点击候选栏 | 长文本、最高可靠性（K40 原设计） |
| 系统键盘逐字打字 | `screen_type` | 每字符 1 次坐标点击 | 不想装输入法时的零安装方案 |
| 校准采样 | `screen_calibration` | 3 次标靶点击 | 生成机械↔屏幕映射（前两者的前提之一） |

`screen_type`（"让机械臂直接在系统键盘上打字"）的链路：
`校准映射（机械↔屏幕像素） + 布局模板（字符→键位归一化坐标） → 逆变换 → 机械臂绝对坐标逐键点击`。
布局是数据文件：24 键、26 键、九宫格等任何布局用同一格式（`region` + 每行 `(键名, 宽度权重)`），
内置英文 26 键 QWERTY 仅为起点，需按机型截图微调。注意：

- 布局必须与手机实际键盘一致（机型/方向/输入法类型/候选栏状态），变化后需重新确认；
- 每字符一次点击，节奏默认 350ms/键（`pace_ms` 可调 100–3000），文本上限 60 字符，
  长文本请用 `screen_input`；
- 大写字母自动插入 SHIFT 点击；空格/回车映射到 SPACE/RETURN 功能键；
- 坐标点击需当前真机驱动、有效校准、绑定及安全门禁均满足；仿真结果不能替代现场验证；
- 未经人工授权依旧不点击发送/回车确认（RETURN 只在文本里包含换行时才会点击）。

## 边界（继承 AI_demo 原设计）

**输入侧**
- 文本原样下发、原样回报，服务端与插件都不改写；插件核对服务端返回 `content`
  与原文一致后才允许机械臂点击候选栏（防乱码，如 PowerShell 中文被替换成 `?` 的历史问题）。
- 状态机：`pending → displayed → typing → inserted`；失败 `typing_failed`（回报失败原因
  与已输入字符数，内容保持原样、不回删）；取消经控制长轮询确认。
- 心跳门禁：设备最近 45 秒内有心跳且 `keyboardActive=true` 才允许创建任务。
- 幂等：同 `taskId` 同内容重放返回原任务；不同内容 409。
- 任务之间完全独立：不自动重试、不自动清理、不点击发送/回车（发送按钮必须人工授权）。

**校准侧**
- 手机校准页只回报屏幕像素与窗口元数据；机械坐标由 Agent/插件配对，手机端不保存机械坐标。
- 没有匹配布防点（sampleId+sequence）的触点回报被拒绝；页面未回报 `open` 前布防被拒绝。
- 至少 3 个不共线样本；仿射拟合残差超阈值（默认 3px，可配）不写入正式映射。
- 映射按 deviceId 保存并带版本号；机械臂控制器会话、手机夹具或摄像头改变后必须重新校准。
- `MPos` 只是控制器计算坐标，不能单独证明电机真实移动；正式校准需摄像头或现场确认。

**平台侧**
- 本适配只承载"输入法相关"能力；机械臂驱动、真机门禁、任务租约、证据、审计全部沿用平台机制。
- `/api/v1/*` 是设备侧协议，与平台 `/v1/*` 契约、WX 适配层互相独立。
- 无认证，仅限可信局域网部署；不要暴露到公网。

## macOS / Linux 部署（mac 兼容）

服务端为纯 Python（asyncio + FastAPI + SQLite），macOS / Linux / Windows 通用。

```bash
cd arm_fleet
pip install -e ".[server]"
python -m agent          # 默认监听 127.0.0.1:8788
curl http://127.0.0.1:8788/api/v1/health
```

- 手机要访问时，在 `arm_fleet` 目录用
  `ARM_FLEET_HOST=0.0.0.0 ARM_FLEET_ALLOW_LAN=1 ARM_FLEET_PORT=8788 python -m agent` 启动，
  手机端服务器地址填 `http://<Mac 局域网 IP>:8788`，仅用于可信局域网。
  入口通过 `runtime/config.py` 加载配置，环境变量覆盖 `configs/platform.json`；
  只改 `listen_host` 而未开启 `allow_lan` 时，非回环监听会回落到 `127.0.0.1`。
- 环境变量 `ARM_FLEET_SCREEN_IME=0` 可关闭输入法协议路由；`ARM_FLEET_SCREEN_IME=1` 开启（默认开启）。
- 中文经 HTTP JSON 传输全程 UTF-8；macOS 上用 `curl --data-binary` 或从 UTF-8 文件读取正文，
  避免历史 Windows PowerShell 内联中文被静默替换的问题。
- 不需要 JDK/Gradle/Android Studio 常驻：原 K40TaskServer 的"电脑重启后重启服务即可"语义保留，
  且省去 Java 运行时。

### 调试客户端（不需要手机）

`fleet-ime-demo`（Swift，macOS 可直接运行）模拟一部输入法手机：心跳、长轮询、显示、
逐字输入回报；`--calibrate` 模式回报校准触点。详见 `ios/README.md`。

## 配置

| 配置项 | 默认 | 说明 |
|---|---|---|
| `screen_ime_enabled` | `true` | 是否挂载 `/api/v1/*` 输入法协议路由 |
| `screen_ime_db_path` | `data/screen_ime_tasks.db` | 任务/心跳/控制命令 SQLite |
| `screen_ime_mappings_path` | `data/screen_ime_mappings.json` | 校准映射注册表 |

## 手机端

- **零安装网页版（先试这个）**：`http://<服务IP>:8788/screen-ime/phone`，iPhone/Android
  浏览器直接打开；心跳、长轮询、候选栏点击、逐字回报、校准标靶全部走真实协议。
  iOS 系统键盘只允许键盘扩展实现，网页版用于先验证链路。
- **iOS**：`ios/` 提供 FleetIMEKit（协议客户端）+ 键盘扩展 + 宿主 App 工程（`ios/app/`）。
  安装到 iPhone 的完整步骤与 .ipa 的现实约束见[iOS 安装指南](ios/app/INSTALL.md)
  （2026-08-30 的历史检查记录为无完整 Xcode/iOS SDK/签名身份，不代表当前环境；
  可用签名方式、有效期及安装要求以当前 Xcode 和设备提示为准）。
  键盘内置「⚙ 设置」入口，配置保存在键盘自身沙盒，**无需 App Group 权限**。
  宿主 App 的配置不会自动同步到键盘；使用 App 校准同一设备时，两边需填写相同设备编号。
- **Android（K40）**：沿用 AI_demo 工作区已构建的 K40 输入法 APK，设置服务器地址为
  Arm Fleet 服务地址可复用旧输入/校准任务；新触摸轨迹采样需客户端另行支持
  `gesture-capture.v1`，不能仅凭 APK 存在推断可用。

## 测试

```bash
# 协议兼容 / 状态机 / 校准 / 仿射 / 插件端到端；数量以当前输出为准
python -m pytest tests/test_screen_ime.py -q
# 平台全量
python -m pytest tests/ -q
# Swift 自检（macOS；20 项）
cd adapters/screen_ime/ios && swift run fleet-ime-selfcheck
```

## 与原实现的差异清单

| 项 | K40TaskServer（Java） | 本移植 |
|---|---|---|
| 运行时 | JDK 17 + Jetty 风格 HttpServer | Python asyncio + FastAPI（与平台同进程） |
| 持久化 | SQLite（jdbc） | SQLite（标准库），表结构与状态语义一致 |
| 长轮询 | ReentrantLock/Condition | asyncio.Condition，语义一致 |
| 仿射拟合 | 由 WechatAgent 侧实现 | 内置于 `geometry.py`（纯 Python，无 numpy） |
| 校准映射 | 未持久化 | 按 deviceId 持久化 + 版本号（新增强化） |
| Web 调试控制台 | 内置 HTML 页面 | 设备侧提供 `/screen-ime/phone` 协议演示页，平台控制台另行部署 |
| 清库脚本 | clear-database.cmd | 未移植（macOS 用 sqlite3 删除记录即可，危险操作由平台权限管控） |
