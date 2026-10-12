// 心跳协调器：键盘激活期间周期上报（移植自 K40InputMethod HeartbeatCoordinator）。
// 服务端以"最近 45 秒内有心跳且 keyboardActive=true"判定设备可接收任务。

import Foundation

public final class HeartbeatCoordinator: @unchecked Sendable {
    public let client: any ImeAPIServing
    public let deviceId: String
    public let appVersion: String
    private var heartbeatTask: Task<Void, Never>?
    private let lock = NSLock()
    private var _currentTaskId: String?

    public init(client: any ImeAPIServing, deviceId: String, appVersion: String) {
        self.client = client
        self.deviceId = deviceId
        self.appVersion = appVersion
    }

    /// 当前正在输入的任务（随心跳上报，控制台可见）。
    public var currentTaskId: String? {
        get { lock.withLock { _currentTaskId } }
        set { lock.withLock { _currentTaskId = newValue } }
    }

    /// 键盘激活时调用；内部按 15 秒周期上报（远小于 45 秒在线窗口）。
    public func start(intervalSeconds: UInt64 = 15) {
        stop()
        let task = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                _ = try? await self.client.heartbeat(
                    deviceId: self.deviceId, appVersion: self.appVersion,
                    keyboardActive: true, currentTaskId: self.currentTaskId)
                try? await Task.sleep(nanoseconds: intervalSeconds * 1_000_000_000)
            }
        }
        lock.withLock { heartbeatTask = task }
    }

    /// 键盘收起时调用：keyboardActive=false 让服务端立即知道键盘已关闭。
    public func stop() {
        lock.withLock { heartbeatTask?.cancel() }
        lock.withLock { heartbeatTask = nil }
        Task { [client, deviceId, appVersion, currentTaskId] in
            _ = try? await client.heartbeat(deviceId: deviceId, appVersion: appVersion,
                                            keyboardActive: false,
                                            currentTaskId: currentTaskId)
        }
    }
}
