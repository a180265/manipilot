// 输入节奏策略：移植自 K40InputMethod TypingSpeedPolicy.kt，语义逐项一致。
// - 档位区间：慢 150–280ms/字、正常 80–160ms/字、快 35–80ms/字。
// - 服务端 resolvedTypingSpeed 优先；其次任务 requested；random 时整条任务固定一档。
// - 标点额外停顿：逗号/顿号/分号 120–250ms；句号/问号/感叹号/换行 250–500ms。

import Foundation

public enum TypingSpeedTier: String, Sendable, CaseIterable {
    case slow
    case normal
    case fast

    public var minimumMillis: Int {
        switch self {
        case .slow: return 150
        case .normal: return 80
        case .fast: return 35
        }
    }

    public var maximumMillis: Int {
        switch self {
        case .slow: return 280
        case .normal: return 160
        case .fast: return 80
        }
    }
}

public struct TypingSpeedPolicy: Sendable {
    public init() {}

    /// 解析：未知值回退 normal（与 Kotlin parse 一致）。
    public static func parse(_ value: String?) -> TypingSpeedTier? {
        switch value?.lowercased() {
        case "slow": return .slow
        case "fast": return .fast
        case "normal": return .normal
        default: return nil
        }
    }

    /// 与服务端一致的档位决议：服务端 resolved 优先，其次 requested，最后随机。
    public func resolve(requested: String?, resolvedFromServer: String?,
                        randomIndex: Int) -> TypingSpeedTier {
        if let server = TypingSpeedPolicy.parse(resolvedFromServer) { return server }
        if let requestedTier = TypingSpeedPolicy.parse(requested) { return requestedTier }
        let concrete: [TypingSpeedTier] = [.slow, .normal, .fast]
        return concrete[((randomIndex % concrete.count) + concrete.count) % concrete.count]
    }

    /// 单字延迟：基准档位区间按 unit 随机数取值；标点追加额外停顿。
    public func delayMillis(for unit: String, speed: TypingSpeedTier,
                            randomUnit: Double, punctuationUnit: Double) -> Int {
        let bounded = min(max(randomUnit, 0.0), 1.0)
        let base = speed.minimumMillis
            + Int((Double(speed.maximumMillis - speed.minimumMillis) * bounded).rounded(.down))
        let extraRange: ClosedRange<Int>?
        switch unit.last {
        case ",", "，", "、", ";", "；": extraRange = 120...250
        case ".", "。", "?", "？", "!", "！", "\n": extraRange = 250...500
        default: extraRange = nil
        }
        guard let range = extraRange else { return base }
        let punctuation = min(max(punctuationUnit, 0.0), 1.0)
        return base + range.lowerBound
            + Int((Double(range.upperBound - range.lowerBound) * punctuation).rounded(.down))
    }
}
