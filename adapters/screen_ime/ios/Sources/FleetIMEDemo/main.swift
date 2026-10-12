// fleet-ime-demo：macOS 调试客户端（仿真手机输入法）。
// 用途：在 Mac 上直接联调 Arm Fleet 屏幕输入法服务，验证完整任务流：
//   心跳 → 长轮询领取 → 回报已显示 → （模拟机械臂点击候选）→ 按人工节奏逐字"输入"到终端 → 回报已输入。
// 校准模式：回报 open 后按预设像素序列回报触点（服务端只信布防配对）。
//
// 用法：
//   fleet-ime-demo --base-url http://127.0.0.1:8788 --device-id mac-demo [--calibrate]
//   fleet-ime-demo --help

import Foundation
import FleetIMEKit

struct DemoOptions {
    var baseURL = URL(string: "http://127.0.0.1:8788")!
    var deviceId = "mac-demo"
    var calibrate = false
}

func parseArguments() -> DemoOptions? {
    var options = DemoOptions()
    var iterator = CommandLine.arguments.dropFirst().makeIterator()
    while let argument = iterator.next() {
        switch argument {
        case "--base-url":
            guard let value = iterator.next(), let url = URL(string: value) else { return nil }
            options.baseURL = url
        case "--device-id":
            guard let value = iterator.next() else { return nil }
            options.deviceId = value
        case "--calibrate": options.calibrate = true
        case "--help", "-h": return nil
        default: return nil
        }
    }
    return options
}

func printUsage() {
    print("""
    Fleet 输入法 macOS 演示客户端（仿真手机输入法）
      --base-url <url>   服务地址（默认 http://127.0.0.1:8788）
      --device-id <id>   输入法设备编号（默认 mac-demo）
      --calibrate        校准页模式：回报 open 后按 3 点序列回报触点
    """)
}

func runTaskLoop(client: FleetIMEClient, deviceId: String) async {
    print("[demo] 心跳上线：\(deviceId)")
    while !Task.isCancelled {
        _ = try? await client.heartbeat(deviceId: deviceId,
                                        appVersion: "fleet-ime-mac-demo",
                                        keyboardActive: true, currentTaskId: nil)
        guard let task = try? await client.fetchNextTask(deviceId: deviceId,
                                                         waitSeconds: 20) else { continue }
        print("[demo] 收到任务 \(task.taskId)：\(task.content)")
        _ = try? await client.reportStatus(taskId: task.taskId, deviceId: deviceId,
                                          status: .displayed)
        print("[demo] 候选栏已显示（模拟机械臂点击候选）")
        try? await Task.sleep(nanoseconds: 300_000_000)
        let graphemes = GraphemeSplitter.split(task.content)
        for (index, unit) in graphemes.enumerated() {
            try? await Task.sleep(nanoseconds: 120_000_000)
            let count = index + 1
            let status: TaskStatus = count == graphemes.count ? .inserted : .typing
            _ = try? await client.reportStatus(taskId: task.taskId, deviceId: deviceId,
                                              status: status, typedCharacterCount: count)
            print("[demo] 已输入：\(unit)（\(count)/\(graphemes.count)）")
        }
        print("[demo] 任务完成：\(task.taskId)")
    }
}

func runCalibration(client: FleetIMEClient, deviceId: String) async {
    print("[demo] 校准页模式：等待会话并回报 open")
    let points: [(Double, Double)] = [(540, 1200), (570, 1200), (540, 1230)]
    while !Task.isCancelled {
        guard let session = try? await client.fetchCurrentCalibration(deviceId: deviceId) else {
            try? await Task.sleep(nanoseconds: 500_000_000)
            continue
        }
        if session.clientState != "open" {
            _ = try? await client.reportCalibrationState(sessionId: session.sessionId,
                                                         deviceId: deviceId, state: "open")
        }
        guard let armed = session.armed else {
            try? await Task.sleep(nanoseconds: 200_000_000)
            continue
        }
        let index = min(armed.sequence - 1, points.count - 1)
        let (x, y) = points[max(0, index)]
        print("[demo] 回报触点 \(armed.sampleId)：(\(x), \(y))")
        _ = try? await client.reportCalibrationTouch(
            sessionId: session.sessionId, deviceId: deviceId,
            sampleId: armed.sampleId, sequence: armed.sequence,
            screenX: x, screenY: y, screenWidth: 1080, screenHeight: 2400,
            orientation: "portrait", eventTime: Int(Date().timeIntervalSince1970 * 1000))
        try? await Task.sleep(nanoseconds: 300_000_000)
    }
}

@main
struct FleetIMEDemo {
    static func main() async {
        guard let options = parseArguments() else {
            printUsage()
            return
        }
        let client = FleetIMEClient(baseURL: options.baseURL)
        if options.calibrate {
            await runCalibration(client: client, deviceId: options.deviceId)
        } else {
            await runTaskLoop(client: client, deviceId: options.deviceId)
        }
    }
}
