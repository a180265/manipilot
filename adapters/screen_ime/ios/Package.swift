// swift-tools-version:5.9
import PackageDescription

// Fleet 输入法客户端（移植自 AI_demo K40InputMethod 的协议实现）。
// - FleetIMEKit：服务端协议客户端（心跳/长轮询/状态回报/校准回报/输入节奏），
//   同时支持 iOS 与 macOS（macOS 用于单元测试与调试 CLI，验证 mac 兼容性）。
// - FleetIMEKeyboard：iOS 键盘扩展 UI（候选栏 + 26 键 + 校准页），仅 iOS。
//   在 macOS 上编译为空壳（canImport 门控），不影响 swift test。
let package = Package(
    name: "FleetIME",
    platforms: [.iOS(.v15), .macOS(.v12)],
    products: [
        .library(name: "FleetIMEKit", targets: ["FleetIMEKit"]),
        .library(name: "FleetIMEKeyboard", targets: ["FleetIMEKeyboard"]),
        .executable(name: "fleet-ime-demo", targets: ["FleetIMEDemo"]),
        .executable(name: "fleet-ime-selfcheck", targets: ["FleetIMESelfCheck"]),
    ],
    targets: [
        .target(name: "FleetIMEKit"),
        .target(name: "FleetIMEKeyboard", dependencies: ["FleetIMEKit"]),
        .executableTarget(name: "FleetIMEDemo", dependencies: ["FleetIMEKit"]),
        .executableTarget(name: "FleetIMESelfCheck", dependencies: ["FleetIMEKit"]),
        .testTarget(name: "FleetIMEKitTests", dependencies: ["FleetIMEKit"]),
    ]
)
