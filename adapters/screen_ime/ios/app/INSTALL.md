# FleetIME iOS 安装指南（安装到 iPhone 上试）

## 先说清楚 .ipa 的事

**历史检查（2026-08-30）**：当时记录了以下两个缺失，不能据此判断当前机器状态：

1. 当时仅有 Command Line Tools，没有完整 Xcode 和 iPhoneOS SDK，未能编译 iOS 目标；
2. 当时的 `security find-identity` 检查记录为 0 个签名身份。

本次仅核对源码和文档，没有重新查询工具链或系统签名身份，也没有构建、签名或安装验证。
签名资格、有效期、设备要求和分发范围以当前 Xcode、账号及设备提示为准，不保证固定天数。
路径 A 是原生键盘的安装流程；路径 B 仅用于浏览器协议演示，不能替代原生键盘安装验证。

## 路径 A：Xcode 工程安装原生键盘

工程规格在本目录的 `project.yml`（App + 键盘扩展双 target），可生成 Xcode 工程；
这不代表已通过 iOS 构建或真机验证。

1. 准备具备完整 Xcode、iOS SDK 及目标设备支持的 Mac；本包声明 Swift 工具版本 5.9。
2. 生成工程（二选一）：
   - 有 [XcodeGen](https://github.com/yonaskolb/XcodeGen)：`brew install xcodegen`
     然后在本目录执行 `./make-xcodeproj.sh`，得到 `FleetIME.xcodeproj`；
   - 没有 XcodeGen：参考 [iOS README 的手工接入步骤](../README.md#ios-键盘扩展接入需完整-xcode)
     在 Xcode 中建两个 target，并核对包依赖、扩展入口及各 target 的配置。
3. 打开工程 → 两个 target（FleetIMEApp、FleetIMEKeyboardExtension）→
   Signing & Capabilities → Team 选择账号可用的团队，按 Xcode 提示处理签名配置。
4. iPhone 用数据线连接，Xcode 顶部选 FleetIMEApp + 你的手机，按 ▶ Run。
   首次安装的开发者信任、开发者模式等要求，以 Xcode 和手机实际提示为准。
5. 若签名到期或安装被拒绝，按开发工具提示处理，不预设有效期或保证重新 Run 即可解决。

## 装好之后怎么用

1. 打开 FleetIME App → 填服务器地址（Mac 的局域网 IP，如 `http://192.168.1.10:8788`）→ 测试连接。
   Mac 端在 `arm_fleet` 目录启动服务（仅限可信局域网）：
   ```bash
   ARM_FLEET_HOST=0.0.0.0 ARM_FLEET_ALLOW_LAN=1 ARM_FLEET_PORT=8788 python -m agent
   ```
   `agent/__main__.py` 使用 `runtime/config.py` 加载配置，环境变量覆盖 JSON 配置；
   只改 `listen_host` 而未开启 `allow_lan` 时，会回落到回环地址，手机无法访问。
2. iPhone 设置 → 通用 → 键盘 → 键盘 → 添加新键盘 → **FleetIME**；
   点开它，开启 **"允许完全访问"**（键盘联网需要）。
3. 在任意文本框切到 FleetIME 键盘 → 点键盘上的 **「⚙ 设置」** → 填同样的服务器地址
   和设备编号。键盘使用自身沙盒的 `UserDefaults.standard`，宿主 App 使用独立的
   `@AppStorage`，无需 App Group，但配置不会自动同步；用 App 校准同一设备时，
   两边需手动填写相同设备编号，下发任务也使用该编号。
4. 电脑端下发任务（Web 控制台或 `POST /api/v1/tasks`）→ 键盘候选栏出现任务文本 →
   机械臂点击候选栏（或手动点）→ 输入法按人工节奏逐字写入输入框。
5. 屏幕校准：App 内"打开校准标靶页"，配合电脑端校准会话完成三点采样。

## 路径 B（无需安装原生 App）：手机网页版协议演示

服务端内置了一个走真实协议的手机演示页（心跳、长轮询、候选栏、逐字回报、校准标靶全都有）：

1. Mac 上按“装好之后怎么用”第 1 步的命令启动服务，显式开启局域网监听。
2. iPhone Safari 打开 `http://<Mac 局域网 IP>:8788/screen-ime/phone?base=http://<Mac IP>:8788&device=phone-demo`
3. "输入演示"：开始接收任务 → 电脑端 `POST /api/v1/tasks` 下发 → 候选栏出现 →
   点候选栏 → 文本逐字"输入"并回报 inserted。
4. "屏幕校准"：进入全屏标靶页 → 电脑端创建会话/布防 → 点击屏幕回报触点。

网页版不是系统级输入法：`phone.html` 只把文本追加到页面的 `fake-input` 元素，
其 `inserted` 回报不代表已写入其他 App 的输入框。它可验证部分设备侧协议和校准回报链路，
不覆盖原生键盘的系统输入、权限、生命周期、密码框或断线处理，也不保证输入节奏和字符计数一致；
这些仍需按路径 A 单独验证。

## 已知限制

- 2026-08-30 的历史记录称 `../Sources/FleetIMEKeyboard` 的 UIKit 代码只做过语法级校验
  （`swift-frontend -parse`），未在 iOS SDK 下编译；本次未重新执行检查，不能据此保证当前构建可用。
- 当前扩展 Info.plist 在顶层声明 `RequestsOpenAccess`，局域网 ATS 设置仅见于宿主 App 的
  Info.plist。扩展的开放访问声明位置与 HTTP 联网行为仍待 Xcode/iOS 环境核验，
  不能把宿主 App 测试连接成功视为键盘联网成功；本次未改动这些配置。
- iOS 15.0+；键盘联网依赖"允许完全访问"。
