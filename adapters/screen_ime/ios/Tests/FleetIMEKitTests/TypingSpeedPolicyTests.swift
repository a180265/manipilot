// 输入节奏与图元分割测试（macOS 上运行，验证与 K40 Kotlin 版语义一致）。

import XCTest
@testable import FleetIMEKit

final class TypingSpeedPolicyTests: XCTestCase {
    let policy = TypingSpeedPolicy()

    func testDelayWithinTierRange() {
        let slow = policy.delayMillis(for: "a", speed: .slow, randomUnit: 0, punctuationUnit: 0)
        let slowMax = policy.delayMillis(for: "a", speed: .slow, randomUnit: 1, punctuationUnit: 0)
        XCTAssertEqual(slow, 150)
        XCTAssertEqual(slowMax, 280)

        let fastMin = policy.delayMillis(for: "a", speed: .fast, randomUnit: 0, punctuationUnit: 0)
        let fastMax = policy.delayMillis(for: "a", speed: .fast, randomUnit: 1, punctuationUnit: 0)
        XCTAssertEqual(fastMin, 35)
        XCTAssertEqual(fastMax, 80)

        let normal = policy.delayMillis(for: "a", speed: .normal, randomUnit: 0.5, punctuationUnit: 0)
        XCTAssertEqual(normal, 120)
    }

    func testPunctuationExtraPause() {
        let comma = policy.delayMillis(for: "，", speed: .normal, randomUnit: 0, punctuationUnit: 0)
        XCTAssertEqual(comma, 80 + 120)
        let period = policy.delayMillis(for: "。", speed: .normal, randomUnit: 0, punctuationUnit: 1)
        XCTAssertEqual(period, 80 + 500)
        let newline = policy.delayMillis(for: "\n", speed: .fast, randomUnit: 1, punctuationUnit: 0)
        XCTAssertEqual(newline, 80 + 250)
        let plain = policy.delayMillis(for: "x", speed: .normal, randomUnit: 0, punctuationUnit: 1)
        XCTAssertEqual(plain, 80)
    }

    func testResolvePrefersServerThenRequestedThenRandom() {
        XCTAssertEqual(policy.resolve(requested: "fast", resolvedFromServer: "slow",
                                      randomIndex: 0), .slow)
        XCTAssertEqual(policy.resolve(requested: "fast", resolvedFromServer: "random",
                                      randomIndex: 0), .fast)
        // random 落在具体档位
        let resolved = policy.resolve(requested: nil, resolvedFromServer: "random",
                                      randomIndex: 1)
        XCTAssertTrue([TypingSpeedTier.slow, .normal, .fast].contains(resolved))
        XCTAssertEqual(policy.resolve(requested: nil, resolvedFromServer: "random",
                                      randomIndex: 0), .slow)
    }
}

final class GraphemeSplitterTests: XCTestCase {
    func testAsciiAndChinese() {
        XCTAssertEqual(GraphemeSplitter.split("ab你好"), ["a", "b", "你", "好"])
        XCTAssertEqual(GraphemeSplitter.count("ab你好"), 4)
    }

    func testEmojiZWJSequenceCountsAsOne() {
        // 👨‍👩‍👧（三人家庭，ZWJ 连接）计 1 个图元
        let family = "👨\u{200D}👩\u{200D}👧"
        XCTAssertEqual(GraphemeSplitter.split(family), [family])
        XCTAssertEqual(GraphemeSplitter.count(family), 1)
    }

    func testSkinToneModifierCountsAsOne() {
        let thumb = "👍🏽"
        XCTAssertEqual(GraphemeSplitter.split(thumb), [thumb])
        XCTAssertEqual(GraphemeSplitter.count("👍🏽ok"), 3)
    }
}
