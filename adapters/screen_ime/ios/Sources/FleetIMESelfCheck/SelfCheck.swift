// fleet-ime-selfcheck：无 Xcode 环境下的纯 Swift 自检（macOS 可直接运行）。
// 覆盖与 FleetIMEKitTests 相同的核心断言：输入节奏、图元分割、档位决议、
// 键盘引擎全流程（显示→逐字→已输入）、密码框拒绝、断线补报。
// 运行：swift run fleet-ime-selfcheck；全部通过打印 PASS 并退出 0。

import Foundation
import FleetIMEKit

var failures = 0
var checks = 0

func check(_ condition: Bool, _ name: String) {
    checks += 1
    if condition {
        print("  ✔ \(name)")
    } else {
        failures += 1
        print("  ✘ \(name)")
    }
}

// ---- FakeService / FakeProxy（与测试桩一致的内存实现） ----

final class FakeService: ImeAPIServing {
    struct Report { var status: TaskStatus; var typed: Int; var reason: String? }
    private let lock = NSLock()
    private var queue: [ImeTask] = []
    private(set) var reports: [Report] = []
    var failAllReports = false

    init(task: ImeTask?) {
        if let task { queue.append(task) }
    }

    func heartbeat(deviceId: String, appVersion: String,
                   keyboardActive: Bool, currentTaskId: String?) async throws -> ImeDevice {
        ImeDevice(deviceId: deviceId, appVersion: appVersion, keyboardActive: keyboardActive,
                  currentTaskId: currentTaskId, lastSeenAt: "t", online: true)
    }

    func fetchNextTask(deviceId: String, waitSeconds: Int) async throws -> ImeTask? {
        lock.lock(); defer { lock.unlock() }
        return queue.isEmpty ? nil : queue.removeFirst()
    }

    func reportStatus(taskId: String, deviceId: String, status: TaskStatus,
                      typedCharacterCount: Int?, failureReason: String?) async throws -> ImeTask {
        lock.lock()
        if failAllReports {
            lock.unlock()
            throw ImeAPIFailure(error: "driver_disconnected", message: "连接中断")
        }
        reports.append(Report(status: status, typed: typedCharacterCount ?? 0,
                              reason: failureReason))
        lock.unlock()
        return ImeTask(taskId: taskId, deviceId: deviceId, targetUser: "x", content: "",
                       typingSpeed: "normal", resolvedTypingSpeed: "normal",
                       status: status.rawValue, typedCharacterCount: typedCharacterCount ?? 0,
                       failureReason: failureReason, source: "api", createdAt: "t", updatedAt: "t")
    }

    func fetchNextControl(taskId: String, deviceId: String,
                          waitSeconds: Int) async throws -> ControlCommand? { nil }

    func acknowledgeControl(taskId: String, deviceId: String, commandId: Int,
                            typedCharacterCount: Int?) async throws -> ImeTask {
        ImeTask(taskId: taskId, deviceId: deviceId, targetUser: "x", content: "",
                typingSpeed: "normal", resolvedTypingSpeed: "normal",
                status: "cancelled", typedCharacterCount: 0, failureReason: nil,
                source: "api", createdAt: "t", updatedAt: "t")
    }
}

final class FakeProxy: TextDocumentProxying {
    var secure = false
    private(set) var inserted: [String] = []
    var isSecureTextEntry: Bool { secure }
    func insertText(_ text: String) { inserted.append(text) }
}

func makeTask(_ content: String) -> ImeTask {
    ImeTask(taskId: "t-1", deviceId: "k40-01", targetUser: "张三", content: content,
            typingSpeed: "random", resolvedTypingSpeed: "fast", status: "pending",
            typedCharacterCount: 0, failureReason: nil, source: "api",
            createdAt: "t", updatedAt: "t")
}

@main
struct SelfCheck {
    static func main() async {
        setbuf(stdout, nil)
        print("Fleet 输入法自检（macOS）")

        print("[1] 输入节奏策略（与 K40 Kotlin 版逐项一致）")
        let policy = TypingSpeedPolicy()
        check(policy.delayMillis(for: "a", speed: .slow, randomUnit: 0, punctuationUnit: 0) == 150,
              "慢档下界 150ms")
        check(policy.delayMillis(for: "a", speed: .slow, randomUnit: 1, punctuationUnit: 0) == 280,
              "慢档上界 280ms")
        check(policy.delayMillis(for: "a", speed: .fast, randomUnit: 0, punctuationUnit: 0) == 35,
              "快档下界 35ms")
        check(policy.delayMillis(for: "a", speed: .fast, randomUnit: 1, punctuationUnit: 0) == 80,
              "快档上界 80ms")
        check(policy.delayMillis(for: "a", speed: .normal, randomUnit: 0.5, punctuationUnit: 0) == 120,
              "正常档中点 120ms")
        check(policy.delayMillis(for: "，", speed: .normal, randomUnit: 0, punctuationUnit: 0) == 200,
              "逗号额外停顿下界 120ms")
        check(policy.delayMillis(for: "。", speed: .normal, randomUnit: 0, punctuationUnit: 1) == 580,
              "句号额外停顿上界 500ms")
        check(policy.delayMillis(for: "x", speed: .normal, randomUnit: 0, punctuationUnit: 1) == 80,
              "普通字符不加标点停顿")

        print("[2] 档位决议：服务端 resolved 优先，其次 requested，最后随机档")
        check(policy.resolve(requested: "fast", resolvedFromServer: "slow", randomIndex: 0) == .slow,
              "服务端 resolved 优先")
        check(policy.resolve(requested: "fast", resolvedFromServer: "random", randomIndex: 0) == .fast,
              "其次任务 requested")
        check(policy.resolve(requested: nil, resolvedFromServer: "random", randomIndex: 0) == .slow,
              "random 按序取具体档位")

        print("[3] 图元分割（与 Android typedCharacterCount 口径一致）")
        check(GraphemeSplitter.split("ab你好") == ["a", "b", "你", "好"], "中英文逐字")
        check(GraphemeSplitter.count("👍🏽ok") == 3, "肤色修饰符并入前字")
        let family = "👨\u{200D}👩\u{200D}👧"
        check(GraphemeSplitter.count(family) == 1, "ZWJ 家庭 emoji 计 1")

        print("[4] 键盘引擎全流程：显示 → 逐字 → 已输入")
        let fake = FakeService(task: makeTask("你好a"))
        let engine = KeyboardTaskEngine(client: fake, deviceId: "k40-01", appVersion: "selfcheck",
                                        clock: KeyboardClock { _ in }, randomSeed: 42)
        let proxy = FakeProxy()
        // 真实时序：长轮询领取 → 候选栏显示 → 机械臂点击候选栏 → 逐字输入
        let pollTask = Task { await engine.pollAndProcess(proxy: proxy) }
        while fake.reports.isEmpty { await Task.yield() }
        engine.confirmTap()
        await pollTask.value
        check(proxy.inserted == ["你", "好", "a"], "逐字插入顺序与内容")
        check(fake.reports.map(\.status) == [.displayed, .typing, .typing, .inserted],
              "回报序列 displayed→typing→inserted")
        check(fake.reports.last?.typed == 3, "已输入字符数 = 3")

        print("[5] 密码输入框拒绝任务")
        let secureFake = FakeService(task: makeTask("秘密"))
        let secureEngine = KeyboardTaskEngine(client: secureFake, deviceId: "k40-01",
                                              appVersion: "selfcheck",
                                              clock: KeyboardClock { _ in })
        let secureProxy = FakeProxy()
        secureProxy.secure = true
        await secureEngine.pollAndProcess(proxy: secureProxy)
        check(secureProxy.inserted.isEmpty, "密码框不插入任何字符")
        check(secureFake.reports.count == 1
              && secureFake.reports[0].status == .typingFailed
              && secureFake.reports[0].typed == 0,
              "回报 typing_failed 且已输入 0")

        print("[6] 断线：已输入内容保持原样，补报失败后放弃")
        let brokenFake = FakeService(task: makeTask("x"))
        brokenFake.failAllReports = true
        let brokenEngine = KeyboardTaskEngine(client: brokenFake, deviceId: "k40-01",
                                              appVersion: "selfcheck",
                                              clock: KeyboardClock { _ in })
        let brokenProxy = FakeProxy()
        // displayed 回报即断连：引擎在等待点击之前就放弃，无需确认点击
        await brokenEngine.pollAndProcess(proxy: brokenProxy)
        check(brokenFake.reports.isEmpty && brokenProxy.inserted.isEmpty,
              "回报全失败时不误报成功")

        print("")
        if failures == 0 {
            print("PASS：\(checks) 项检查全部通过")
        } else {
            print("FAIL：\(failures)/\(checks) 项失败")
            exit(1)
        }
    }
}
