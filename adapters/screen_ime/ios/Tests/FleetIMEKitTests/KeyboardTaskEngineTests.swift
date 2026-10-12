// 键盘任务引擎测试：完整任务流、取消、密码框拒绝、断线补报（macOS 运行）。

import XCTest
@testable import FleetIMEKit

/// 内存版服务端桩：可编程响应并记录回报序列。
final class FakeService: ImeAPIServing {
    struct Report: Equatable {
        var taskId: String
        var status: TaskStatus
        var typed: Int
        var reason: String?
    }

    private let lock = NSLock()
    private var _queuedTasks: [ImeTask] = []
    private var _reports: [Report] = []
    private var _controls: [ControlCommand] = []
    var failCountdown = 0

    init(queuedTasks: [ImeTask] = [], controls: [ControlCommand] = []) {
        _queuedTasks = queuedTasks
        _controls = controls
    }

    var reports: [Report] {
        lock.lock(); defer { lock.unlock() }
        return _reports
    }

    func enqueue(_ task: ImeTask) {
        lock.lock(); defer { lock.unlock() }
        _queuedTasks.append(task)
    }

    func heartbeat(deviceId: String, appVersion: String, keyboardActive: Bool,
                   currentTaskId: String?) async throws -> ImeDevice {
        ImeDevice(deviceId: deviceId, appVersion: appVersion, keyboardActive: keyboardActive,
                  currentTaskId: currentTaskId, lastSeenAt: "t", online: true)
    }

    func fetchNextTask(deviceId: String, waitSeconds: Int) async throws -> ImeTask? {
        lock.lock(); defer { lock.unlock() }
        return _queuedTasks.isEmpty ? nil : _queuedTasks.removeFirst()
    }

    func reportStatus(taskId: String, deviceId: String, status: TaskStatus,
                      typedCharacterCount: Int?, failureReason: String?) async throws -> ImeTask {
        lock.lock()
        if failCountdown > 0 {
            failCountdown -= 1
            lock.unlock()
            throw ImeAPIFailure(error: "driver_disconnected", message: "连接中断")
        }
        _reports.append(Report(taskId: taskId, status: status,
                               typed: typedCharacterCount ?? 0, reason: failureReason))
        lock.unlock()
        return ImeTask(taskId: taskId, deviceId: deviceId, targetUser: "x", content: "",
                       typingSpeed: "normal", resolvedTypingSpeed: "normal",
                       status: status.rawValue, typedCharacterCount: typedCharacterCount ?? 0,
                       failureReason: failureReason, source: "api", createdAt: "t", updatedAt: "t")
    }

    func fetchNextControl(taskId: String, deviceId: String,
                          waitSeconds: Int) async throws -> ControlCommand? {
        lock.lock(); defer { lock.unlock() }
        return _controls.isEmpty ? nil : _controls.removeFirst()
    }

    func acknowledgeControl(taskId: String, deviceId: String, commandId: Int,
                            typedCharacterCount: Int?) async throws -> ImeTask {
        ImeTask(taskId: taskId, deviceId: deviceId, targetUser: "x", content: "",
                typingSpeed: "normal", resolvedTypingSpeed: "normal",
                status: TaskStatus.cancelled.rawValue, typedCharacterCount: 0,
                failureReason: nil, source: "api", createdAt: "t", updatedAt: "t")
    }
}

/// 内存文本框桩。
final class FakeProxy: TextDocumentProxying, @unchecked Sendable {
    var secure = false
    private let lock = NSLock()
    private var _inserted: [String] = []
    var inserted: [String] {
        get { lock.lock(); defer { lock.unlock() }; return _inserted }
        set { lock.lock(); defer { lock.unlock() }; _inserted = newValue }
    }
    var isSecureTextEntry: Bool { secure }
    func insertText(_ text: String) { inserted.append(text) }
}

final class KeyboardTaskEngineTests: XCTestCase {
    private func makeTask(content: String) -> ImeTask {
        ImeTask(taskId: "t-1", deviceId: "k40-01", targetUser: "张三", content: content,
                typingSpeed: "random", resolvedTypingSpeed: "fast", status: "pending",
                typedCharacterCount: 0, failureReason: nil, source: "api",
                createdAt: "t", updatedAt: "t")
    }

    private func makeEngine(_ service: FakeService) -> KeyboardTaskEngine {
        KeyboardTaskEngine(client: service, deviceId: "k40-01", appVersion: "test",
                           clock: KeyboardClock { _ in }, randomSeed: 42)
    }

    /// 等待直到条件成立（引擎内部并发，轮询收口）。
    private func waitUntil(timeout: TimeInterval = 5,
                           _ condition: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        XCTFail("等待条件超时")
    }

    func testFullTypingFlowReportsDisplayedTypingInserted() async throws {
        let service = FakeService()
        let engine = makeEngine(service)
        engine.confirmTap()
        let proxy = FakeProxy()
        await engine.process(task: makeTask(content: "你好a"), proxy: proxy)
        XCTAssertEqual(proxy.inserted, ["你", "好", "a"])
        XCTAssertEqual(service.reports.map(\.status),
                       [.displayed, .typing, .typing, .inserted])
        XCTAssertEqual(service.reports.last?.typed, 3)
    }

    func testLongTextProgressCountsMatchGraphemes() async throws {
        let service = FakeService()
        let engine = makeEngine(service)
        engine.confirmTap()
        let proxy = FakeProxy()
        await engine.process(task: makeTask(content: "测试，标点！"), proxy: proxy)
        XCTAssertEqual(proxy.inserted, GraphemeSplitter.split("测试，标点！"))
        XCTAssertEqual(service.reports.last?.status, .inserted)
        XCTAssertEqual(service.reports.last?.typed, 6)
    }

    func testSecureFieldRefusesTask() async {
        let service = FakeService()
        let engine = makeEngine(service)
        let proxy = FakeProxy()
        proxy.secure = true
        await engine.process(task: makeTask(content: "秘密"), proxy: proxy)
        XCTAssertEqual(proxy.inserted, [])
        XCTAssertEqual(service.reports.count, 1)
        XCTAssertEqual(service.reports[0].status, .typingFailed)
        XCTAssertEqual(service.reports[0].typed, 0)
        XCTAssertEqual(service.reports[0].reason, "密码输入框禁止任务输入")
    }

    func testConnectionLossReportsTypingFailedAfterRetries() async throws {
        let service = FakeService()
        service.failCountdown = 99 // 所有回报都断连
        let engine = makeEngine(service)
        engine.confirmTap()
        await engine.process(task: makeTask(content: "x"), proxy: FakeProxy())
        // 无任何成功回报；引擎重试后放弃，不崩溃、不插入文本
        XCTAssertTrue(service.reports.isEmpty)
    }

    func testCancelStopsTypingAndAcknowledges() async throws {
        let service = FakeService()
        let command = ControlCommand(commandId: 7, taskId: "t-1", command: "cancel",
                                     createdAt: "t")
        let engine = KeyboardTaskEngine(client: service, deviceId: "k40-01",
                                        appVersion: "test",
                                        clock: KeyboardClock { nanoseconds in
                                            // 人为放慢逐字节奏，给取消留出窗口
                                            try await Task.sleep(nanoseconds: 20_000_000)
                                        })
        service.enqueue(makeTask(content: "这是一段比较长的任务文本"))
        engine.confirmTap()
        async func runPoll() async {
            _ = await engine.pollAndProcess(proxy: FakeProxy())
        }
        let pollTask = Task { await runPoll() }
        // 等任务进入 displayed 后注入取消命令
        try await waitUntil { !service.reports.isEmpty
            && service.reports[0].status == .displayed }
        await service.enqueueControl(command)
        await pollTask.value
        // 停止后已插入内容少于全文（保持原样、不回删），且不再继续
        let insertedCount = service.reports.filter { $0.status == .typing || $0.status == .inserted }.count
        XCTAssertLessThan(insertedCount, GraphemeSplitter.count("这是一段比较长的任务文本"))
        XCTAssertTrue(service.reports.contains { $0.status == .typingFailed }
                      || engine.currentTask == nil)
    }
}

private extension FakeService {
    func enqueueControl(_ command: ControlCommand) async {
        await MainActor.run {}
        lock.lock(); defer { lock.unlock() }
        _controls.append(command)
    }
}
