// 键盘任务引擎：输入法扩展的核心循环（移植自 K40InputMethodService 的任务流）。
//
// 流程（继承原设计边界）：
// 1. 长轮询领取任务；候选栏显示任务文本后回报 displayed。
// 2. 机械臂点击候选栏后（UI 调 confirmTap）按 TypingSpeedPolicy 逐字写入，
//    每字回报 typing 进度；标点额外停顿。
// 3. 全部写完回报 inserted（附带图元字符数）；未点击发送/回车。
// 4. 密码输入框（isSecureTextEntry）拒绝任务，回报 typing_failed。
// 5. 服务端取消经 control 长轮询送达：立即停止后续字符、确认取消、清空候选。
// 6. 焦点丢失或连接失效：立即停止，回报 typing_failed 与已输入字符数，不删除已输入内容。
// 7. 断线重试仅用于状态补报（有限次、指数退避）；任务本身不自动重试。

import Foundation

/// 键盘对文本框的抽象（UIInputViewController.textDocumentProxy 的最小接口）。
public protocol TextDocumentProxying: AnyObject {
    var isSecureTextEntry: Bool { get }
    func insertText(_ text: String)
}

/// 时钟抽象（测试注入；生产用连续时钟）。
public struct KeyboardClock: Sendable {
    public var sleepNanoseconds: @Sendable (UInt64) async throws -> Void

    public init(sleepNanoseconds: @escaping @Sendable (UInt64) async throws -> Void) {
        self.sleepNanoseconds = sleepNanoseconds
    }

    public static let real = KeyboardClock { nanoseconds in
        try await Task.sleep(nanoseconds: nanoseconds)
    }
}

public final class KeyboardTaskEngine: @unchecked Sendable {
    public let client: any ImeAPIServing
    public let deviceId: String
    public let appVersion: String
    private let clock: KeyboardClock
    private let randomSeed: Int?

    private let lock = NSLock()
    private var _currentTask: ImeTask?
    private var _tapConfirmed = false
    private var _cancelRequested = false
    private(set) public var currentTask: ImeTask? {
        get { lock.withLock { _currentTask } }
        set { lock.withLock { _currentTask = newValue } }
    }
    var tapConfirmed: Bool {
        get { lock.withLock { _tapConfirmed } }
        set { lock.withLock { _tapConfirmed = newValue } }
    }
    var cancelRequested: Bool {
        get { lock.withLock { _cancelRequested } }
        set { lock.withLock { _cancelRequested = newValue } }
    }

    public init(client: any ImeAPIServing, deviceId: String, appVersion: String,
                clock: KeyboardClock = .real, randomSeed: Int? = nil) {
        self.client = client
        self.deviceId = deviceId
        self.appVersion = appVersion
        self.clock = clock
        self.randomSeed = randomSeed
    }

    // MARK: - 对外入口

    /// 长轮询一次并处理一个任务（键盘扩展在后台任务中循环调用）。
    /// 返回 true 表示处理了一个任务，false 表示本轮无任务。
    @discardableResult
    public func pollAndProcess(proxy: TextDocumentProxying) async -> Bool {
        guard let task = try? await client.fetchNextTask(deviceId: deviceId, waitSeconds: 20) else {
            return false
        }

        await process(task: task, proxy: proxy)
        return true
    }

    // MARK: 调试探针（临时）
    /// UI 在机械臂点击候选栏后调用：放行逐字输入。
    public func confirmTap() {
        tapConfirmed = true
    }

    /// 候选栏当前应显示的文本（无任务为 nil）。
    public func candidateText() -> String? {
        currentTask.map { "✦ " + $0.content }
    }

    /// 任务被取消或完成后清空候选栏提示。
    public func clearCandidate() {
        currentTask = nil
        tapConfirmed = false
    }

    // MARK: - 单任务处理

    func process(task: ImeTask, proxy: TextDocumentProxying) async {
        currentTask = task
        tapConfirmed = false
        cancelRequested = false
        defer { clearCandidate() }

        // 密码输入框阻止任务输入（继承 K40 输入安全边界）
        if proxy.isSecureTextEntry {
            _ = try? await client.reportStatus(
                taskId: task.taskId, deviceId: deviceId, status: .typingFailed,
                typedCharacterCount: 0, failureReason: "密码输入框禁止任务输入")
            return
        }

        // 启动取消监听（displayed 之后服务端可取消）
        let cancelWatcher = Task { await self.watchCancel(taskId: task.taskId) }
        defer { cancelWatcher.cancel() }

        do {
            _ = try await client.reportStatus(taskId: task.taskId, deviceId: deviceId,
                                              status: .displayed, typedCharacterCount: nil,
                                              failureReason: nil)
        } catch {
            await reportFailure(taskId: task.taskId, typed: 0, reason: "回报已显示失败")
            return
        }

        // 等待机械臂点击候选栏（或服务端取消）
        while !tapConfirmed {
            if cancelRequested { return }
            if await waitTapTick() { return }
        }

        // 逐字输入（图元为单位；标点额外停顿；每字回报进度）
        let policy = TypingSpeedPolicy()
        let resolved = policy.resolve(requested: task.typingSpeed,
                                      resolvedFromServer: task.resolvedTypingSpeed,
                                      randomIndex: randomSeed ?? Int(Date().timeIntervalSince1970))
        let graphemes = GraphemeSplitter.split(task.content)
        var typed = 0
        for unit in graphemes {
            if cancelRequested { return }
            if !proxy.isSecureTextEntry {
                proxy.insertText(unit)
            }
            typed += 1
            let delay = policy.delayMillis(
                for: unit, speed: resolved,
                randomUnit: pseudoRandom(typed * 7),
                punctuationUnit: pseudoRandom(typed * 13))
            do {
                _ = try await client.reportStatus(
                    taskId: task.taskId, deviceId: deviceId,
                    status: typed == graphemes.count ? .inserted : .typing,
                    typedCharacterCount: typed, failureReason: nil)
            } catch {
                // 已输入内容保持原样；有限次补报后停止（不自动重试任务）
                await reportFailure(taskId: task.taskId, typed: typed,
                                    reason: "回报进度失败，已输入内容保持原样")
                return
            }
            try? await clock.sleepNanoseconds(UInt64(delay) * 1_000_000)
        }
    }

    private func waitTapTick() async -> Bool {
        // 真实挂起：等待机械臂点击期间不让出协作线程不能变成自旋
        try? await Task.sleep(nanoseconds: 50_000_000)
        return cancelRequested
    }

    /// 取消控制长轮询：收到取消立即置位；引擎在字符间检查后停止并确认。
    func watchCancel(taskId: String) async {
        while !Task.isCancelled {
            let command = try? await client.fetchNextControl(
                taskId: taskId, deviceId: deviceId, waitSeconds: 5)
            guard let command else {
                if cancelRequested { return }
                // 真实挂起的重试退避（不能用注入时钟：避免无挂起的自旋占用协作线程）
                try? await Task.sleep(nanoseconds: 100_000_000)
                continue
            }
            cancelRequested = true
            _ = try? await client.acknowledgeControl(
                taskId: taskId, deviceId: deviceId, commandId: command.commandId,
                typedCharacterCount: nil)
            return
        }
    }

    private func reportFailure(taskId: String, typed: Int, reason: String) async {
        // 状态补报：有限次指数退避；全部失败则放弃（服务端任务停留在 typing）
        var delayMs = 500
        for _ in 0..<3 {
            try? await clock.sleepNanoseconds(UInt64(delayMs) * 1_000_000)
            if (try? await client.reportStatus(
                taskId: taskId, deviceId: deviceId, status: .typingFailed,
                typedCharacterCount: typed, failureReason: reason)) != nil {
                return
            }
            delayMs *= 2
        }
    }

    private func pseudoRandom(_ salt: Int) -> Double {
        var seed = (salt ^ (randomSeed ?? 0x5DEECE66D)) & 0x7FFFFFFF
        seed = (seed &* 48271) % 0x7FFFFFFF
        return Double(abs(seed) % 1000) / 1000.0
    }
}

extension NSLock {
    fileprivate func withLock<T>(_ body: () -> T) -> T {
        lock()
        defer { unlock() }
        return body()
    }
}
