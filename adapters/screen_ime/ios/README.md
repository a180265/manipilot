# Fleet 输入法 iOS / macOS 客户端（FleetIME）

K40 任务输入法的 Apple 平台移植：跨平台协议客户端（**iOS + macOS**）+ iOS 键盘扩展源码，
对接 Arm Fleet 屏幕输入法服务（`/api/v1/*`，与 Android K40 输入法同一协议）。

## 结构

```
Package.swift
Sources/
  FleetIMEKit/          跨平台协议库（iOS 15+ / macOS 12+，仅 Foundation）
    Models.swift        协议模型（camelCase，与服务端 JSON 一一对应）
    FleetIMEClient.swift URLSession 客户端 + ImeAPIServing 协议
    KeyboardTaskEngine.swift  任务引擎：长轮询→显示→点击→逐字→回报（取消/密码框/断线边界）
    HeartbeatCoordinator.swift 心跳（15 秒周期；keyboardActive 上报）
    TypingSpeedPolicy.swift    输入节奏（慢 150–280 / 正常 80–160 / 快 35–80ms；标点停顿），
                       与 K40 Kotlin 版逐项一致
    GraphemeSplitter.swift     图元分割（typedCharacterCount 与 Android 口径一致）
  FleetIMEKeyboard/     iOS 键盘扩展 UI（UIKit；macOS 上编译为空壳）
    KeyboardViewController.swift   候选栏 + 26 键；候选栏即机械臂点击目标
    CalibrationViewController.swift 全屏校准页（回报 open/closed 与触点像素）
  FleetIMEDemo/         macOS 调试 CLI（仿真输入法手机）
  FleetIMESelfCheck/    纯 Swift 自检（无需 XCTest，macOS 直接运行）
Tests/FleetIMEKitTests/ XCTest 单元测试（需完整 Xcode 环境）
```

## macOS 兼容性验证（需可用 Swift 工具链）

FleetIMEKit 与调试工具不依赖 UIKit，macOS 上一等公民：

```bash
cd arm_fleet/adapters/screen_ime/ios
swift build                    # FleetIMEKit + FleetIMEKeyboard(空壳) + 两个 CLI
swift run fleet-ime-selfcheck  # 20 项核心断言（节奏/图元/引擎全流程/密码框/断线）
```

端到端联调（Mac 上同时起服务端和仿真输入法）：

```bash
# 终端 1：Arm Fleet 服务（输入法协议默认随服务挂载）
cd arm_fleet && python -m agent

# 终端 2：仿真输入法手机
cd arm_fleet/adapters/screen_ime/ios
.build/debug/fleet-ime-demo --base-url http://127.0.0.1:8788 --device-id mac-demo

# 终端 3：下发任务
curl -X POST http://127.0.0.1:8788/api/v1/tasks -H 'Content-Type: application/json' \
  -d '{"taskId":"e2e-1","deviceId":"mac-demo","targetUser":"端到端","content":"你好","typingSpeed":"fast"}'

# 校准模式
.build/debug/fleet-ime-demo --device-id mac-cal --calibrate
```

## 装到 iPhone 上试（宿主 App 工程）

`app/` 目录提供双 target 工程规格（FleetIMEApp + 键盘扩展），仍需验证原生构建与安装：

- `app/project.yml`（XcodeGen 规格）+ `app/make-xcodeproj.sh` 一键生成 `FleetIME.xcodeproj`；
- 或按下文“iOS 键盘扩展接入”在 Xcode 里手工建 target；安装流程见
  [iOS 安装指南](app/INSTALL.md)；
- 在 Xcode 中为两个 target 选择可用 Team；签名资格、有效期和安装要求以工具及设备提示为准；
- 键盘内置「⚙ 设置」配置服务器地址与设备编号（键盘自身沙盒，无需 App Group）；
- App 内置：测试连接、校准标靶页、键盘安装步骤说明。

.ipa 的构建、签名与安装需在具备相应工具链和签名配置的环境中确认。
2026-08-30 的历史检查记录为本开发机缺少完整 Xcode/iOS SDK，且未发现签名身份；
这不代表当前环境状态，本次文档修订未重新查询工具链或系统签名身份。

## iOS 键盘扩展接入（需完整 Xcode）

Swift Package 同时声明 iOS 平台，键盘扩展源码在 `Sources/FleetIMEKeyboard`
（`#if canImport(UIKit)` 门控，macOS 上为空）。在装有完整 Xcode 的 Mac 上：

1. 在安装机器的 Xcode 中确认 iOS SDK 与目标设备支持；下述步骤需要完整 Xcode。
2. Xcode 单元测试：`swift test`（需 Xcode 工具链提供 XCTest；`FleetIMEKitTests` 覆盖
   协议客户端、输入节奏、图元分割、任务引擎取消/断线/密码框边界）。
3. 建 App 工程：
   - 新建 iOS App（如 `FleetIMEApp`），添加本包为 local package；
   - File → New → Target → Keyboard Extension（如 `FleetIMEKeyboardExtension`）；
   - 扩展的 `KeyboardViewController` 继承改为 `TaskKeyboardViewController`（来自
     `FleetIMEKeyboard` 库），删除模板 UI；
   - App 内放置 `CalibrationViewController` 入口（校准页）。
4. 键盘扩展需要"设置 → 键盘 → 添加第三方键盘"，并开启"允许完全访问"才能联网；
   宿主 App 中填写设置不替代键盘的联网授权。
5. 在键盘「⚙ 设置」中配置（键盘沙盒的 `UserDefaults.standard`，不使用 App Group）：
   - `fleet_ime_base_url` = `http://<Mac 局域网 IP>:8788`
   - `fleet_ime_device_id` = 稳定设备编号（同一台手机保持不变）

   宿主 App 用自己的 `@AppStorage` 保存配置，不自动同步；若用 App 校准同一设备，
   两边需手动填写相同服务器地址和设备编号（默认生成的编号不同）。
6. 对照 `app/` 中两个 target 各自的 Info.plist：当前扩展文件在顶层声明
   `RequestsOpenAccess = YES`，宿主 App 声明 `NSAllowsLocalNetworking = YES`，
   扩展文件没有对应 ATS 配置。不能把宿主设置视为扩展已配置完成；
   开放访问声明的位置及扩展 HTTP 联网行为仍需在 Xcode/iOS 环境核验。

### iOS 端行为边界（与 K40 输入法一致）

- 密码输入框（`isSecureTextEntry`）拒绝任务输入并回报 `typing_failed`。
- 候选栏显示任务文本后回报 `displayed`；机械臂点击候选栏后才逐字输入；
  每字回报 `typing` 进度，写完回报 `inserted`；不点击发送/回车。
- 服务端取消经控制长轮询送达：立即停止、确认取消、清空候选。
- 断线：已输入内容保持原样；状态补报重试 3 次（指数退避）后放弃，不自动重试任务。
- 校准页只回报触点像素与窗口元数据，不保存机械坐标。

## 已知限制

- 2026-08-30 的历史记录：当时仅有 Command Line Tools，无完整 Xcode/iOS SDK，
  使用 `fleet-ime-selfcheck`（纯 Swift，20 项）验证核心逻辑，未验证 UIKit 目标与 XCTest。
  本次仅核对源码与文档，未重新运行这些检查；iOS 编译、签名及键盘扩展联调仍需单独验证。
- 键盘扩展 UI 为功能实现（候选栏 + 26 键），视觉与 K40 布局细节可后续按需调整。
