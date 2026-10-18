import Foundation

/// 固定物理设备的实际输入格式；不包含校准/来源 ID，不赋予旧校准可用性。
final class CaptureFormatPins: @unchecked Sendable {
    static let preferenceKey = "captureFormatPinsByUniqueID"
    static let preferenceDomain = "com.armfleet.camerahelper"
    // 初始化失败作为普通启动错误传播，不能强制解包或退回空配置。
    static let shared: Result<CaptureFormatPins, Error> = Result {
        try productionStore()
    }

    static func productionStore(
        bundleIdentifier: String? = Bundle.main.bundleIdentifier,
        standard: UserDefaults = .standard,
        suite: (String) -> UserDefaults? = { UserDefaults(suiteName: $0) }
    ) throws -> CaptureFormatPins {
        if bundleIdentifier == preferenceDomain {
            // 当前 App 域不可作为附加 suite 打开；standard 才是生产偏好域。
            return CaptureFormatPins(defaults: standard)
        }
        guard let defaults = suite(preferenceDomain) else {
            throw failure("无法打开摄像头固定格式偏好域：\(preferenceDomain)")
        }
        return CaptureFormatPins(defaults: defaults)
    }
    private let defaults: UserDefaults
    private let lock = NSLock()

    init(defaults: UserDefaults) { self.defaults = defaults }

    private static func failure(_ message: String) -> NSError {
        NSError(domain: "CameraHelper.CaptureFormat", code: 1,
                userInfo: [NSLocalizedDescriptionKey: message])
    }

    private static func isComplete(_ value: String) -> Bool {
        value.range(of: #"\Afourcc:[0-9a-f]{8};size:[1-9][0-9]*x[1-9][0-9]*;pixel-aspect:[1-9][0-9]*/[1-9][0-9]*\z"#,
                    options: .regularExpression) != nil
    }

    private func readPins() throws -> [String: String] {
        guard let object = defaults.object(forKey: Self.preferenceKey) else { return [:] }
        guard let pins = object as? [String: String],
              pins.allSatisfy({ !$0.key.isEmpty && Self.isComplete($0.value) }) else {
            throw Self.failure("摄像头固定格式配置损坏：请检查 captureFormatPinsByUniqueID")
        }
        return pins
    }

    func requestedFormat(uniqueID: String) throws -> String? {
        lock.lock()
        defer { lock.unlock() }
        return try readPins()[uniqueID]
    }

    func remember(formatID: String, uniqueID: String) throws {
        lock.lock()
        defer { lock.unlock() }
        guard !uniqueID.isEmpty, Self.isComplete(formatID) else {
            throw Self.failure("无法保存摄像头格式：需要物理设备标识和完整格式")
        }
        var pins = try readPins()
        if let existing = pins[uniqueID], existing != formatID {
            throw Self.failure("摄像头实际格式与已固定格式不同，拒绝覆盖配置")
        }
        pins[uniqueID] = formatID
        defaults.set(pins, forKey: Self.preferenceKey)
    }

    static func formatIndex(requested: String, available: [String]) throws -> Int {
        guard let index = available.firstIndex(of: requested) else {
            throw failure("摄像头不支持已固定的完整采集格式：\(requested)")
        }
        return index
    }

    static func verifyActual(requested: String, device: String?, input: String?) throws {
        guard device == requested, input == requested else {
            throw failure("摄像头启动后实际格式不符：要求 \(requested)，"
                + "设备 \(device ?? "缺失")，输入 \(input ?? "缺失")")
        }
    }
}
