import Foundation

@main
struct CaptureFormatPinsTests {
    static func rejects(_ action: () throws -> Void) {
        do { try action(); fatalError("预期拒绝却成功") } catch {}
    }

    static func require(_ condition: @autoclosure () throws -> Bool) rethrows {
        let passed = try condition()
        assert(passed)
    }

    static func main() throws {
        let yuvs = "fourcc:79757673;size:1920x1080;pixel-aspect:1/1"
        let nv12 = "fourcc:34323076;size:1920x1080;pixel-aspect:1/1"
        let suite = "CameraHelper.format-tests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = CaptureFormatPins(defaults: defaults)
        try require(try store.requestedFormat(uniqueID: "camera-A") == nil)
        try store.remember(formatID: yuvs, uniqueID: "camera-A")
        try store.remember(formatID: nv12, uniqueID: "camera-B")
        let restarted = CaptureFormatPins(defaults: defaults)
        try require(try restarted.requestedFormat(uniqueID: "camera-A") == yuvs)
        try require(try restarted.requestedFormat(uniqueID: "camera-B") == nv12)
        try require(try restarted.requestedFormat(uniqueID: "camera-C") == nil)
        rejects { try store.remember(formatID: nv12, uniqueID: "camera-A") }
        try require(try store.requestedFormat(uniqueID: "camera-A") == yuvs)
        try require(try CaptureFormatPins.formatIndex(requested: yuvs, available: [nv12, yuvs]) == 1)
        rejects { _ = try CaptureFormatPins.formatIndex(requested: yuvs, available: [nv12]) }
        try CaptureFormatPins.verifyActual(requested: yuvs, device: yuvs, input: yuvs)
        rejects { try CaptureFormatPins.verifyActual(requested: yuvs, device: nv12, input: yuvs) }
        rejects { try CaptureFormatPins.verifyActual(requested: yuvs, device: yuvs, input: nv12) }
        rejects { try CaptureFormatPins.verifyActual(requested: yuvs, device: yuvs, input: nil) }
        for bad in ["", "fourcc:79757673;size:1920x1080", "fourcc:79757673;size:0x1080;pixel-aspect:1/1", yuvs + "extra"] {
            rejects { try store.remember(formatID: bad, uniqueID: "camera-C") }
        }
        let badPreferences: [Any] = ["bad", ["camera-A": 42], ["camera-A": "yuvs"], ["": yuvs]]
        for bad in badPreferences {
            defaults.set(bad, forKey: CaptureFormatPins.preferenceKey)
            rejects { _ = try store.requestedFormat(uniqueID: "camera-A") }
        }
        defaults.set(["camera-A": yuvs], forKey: CaptureFormatPins.preferenceKey)
        try require(try store.requestedFormat(uniqueID: "camera-A") == yuvs)
        // 生产 App 的偏好域等于 BundleID：必须使用 standard，不能请求同名 suite。
        var suiteCalled = false
        let appStore = try CaptureFormatPins.productionStore(
            bundleIdentifier: CaptureFormatPins.preferenceDomain,
            standard: defaults,
            suite: { _ in suiteCalled = true; return nil })
        require(!suiteCalled)
        try require(try appStore.requestedFormat(uniqueID: "camera-A") == yuvs)
        // CLI/异域必须读取固定 suite；失败不能变成空配置或使用 standard。
        rejects {
            _ = try CaptureFormatPins.productionStore(
                bundleIdentifier: "different.cli.domain", standard: defaults,
                suite: { _ in nil })
        }
        rejects {
            _ = try CaptureFormatPins.productionStore(
                bundleIdentifier: nil, standard: defaults,
                suite: { _ in nil })
        }
        var selectedDomain: String?
        let cliStore = try CaptureFormatPins.productionStore(
            bundleIdentifier: nil, standard: .standard,
            suite: { domain in selectedDomain = domain; return defaults })
        require(selectedDomain == CaptureFormatPins.preferenceDomain)
        try require(try cliStore.requestedFormat(uniqueID: "camera-A") == yuvs)
        print("CaptureFormatPins: 27 checks passed (no camera opened)")
    }
}
