#!/usr/bin/env python3
"""Replay the actual format-selection block with a device that renegotiates on unlock."""
from pathlib import Path
import subprocess
import tempfile

base = Path(__file__).resolve().parents[1]
source = (base / 'Sources/CameraHelper/main.swift').read_text()

def block(text, signature):
    start = text.index(signature)
    brace = text.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (text[end] == '{') - (text[end] == '}')
        end += 1
    return text[start:end]

capture = source[source.index('    private func startCapture('):]
selection = block(capture, '            if let requestedFormat {')
input_creation = '            let input = try AVCaptureDeviceInput(device: device)'
# Replay both production operations in their actual order: input ports may snapshot
# the format on creation rather than refresh when activeFormat changes later.
configuration = '\n'.join(sorted([selection, input_creation], key=capture.index))
release_signature = '    private func releaseFormatLock() {'
release = block(source, release_signature)
fixture = r'''
import Foundation
struct Format { let formatDescription: String }
func formatIDFromDescription(_ description: String) -> String? { description }
final class Device {
    let yuvs = "fourcc:79757673;size:1920x1080;pixel-aspect:1/1"
    let nv12 = "fourcc:34323076;size:1920x1080;pixel-aspect:1/1"
    var formats: [Format] { [Format(formatDescription: nv12), Format(formatDescription: yuvs)] }
    var activeFormat = Format(formatDescription: "fourcc:34323076;size:1920x1080;pixel-aspect:1/1")
    var locked = false
    var unlockCount = 0
    var denyLock = false
    func lockForConfiguration() throws {
        if denyLock { throw NSError(domain: "offline-lock", code: 1) }
        locked = true
    }
    func unlockForConfiguration() { locked = false; unlockCount += 1 }
    func sessionNegotiates() {
        if !locked { activeFormat = Format(formatDescription: nv12) }
    }
}
final class AVCaptureDeviceInput {
    let formatID: String
    init(device: Device) throws { formatID = device.activeFormat.formatDescription }
}
final class Harness {
    private var formatLockedDevice: Device?
    @discardableResult
    func configure(_ device: Device, requestedFormat: String?) throws -> AVCaptureDeviceInput {
        __CONFIGURATION__
        return input
    }
    __RELEASE__
    func finish() { releaseFormatLock() }
}
@main struct Check {
    static func require(_ condition: @autoclosure () -> Bool, _ message: String) {
        guard condition() else {
            FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
            exit(1)
        }
    }
    static func main() throws {
        let device = Device()
        let harness = Harness()
        let input = try harness.configure(device, requestedFormat: device.yuvs)
        require(input.formatID == device.yuvs, "输入端口在格式固定前创建，保留了旧媒体格式")
        require(device.locked, "格式锁在session提交/启动前已释放，实际格式会漂移")
        device.sessionNegotiates()
        require(device.activeFormat.formatDescription == device.yuvs, "session覆盖了已固定格式")
        require(device.unlockCount == 0, "采集期间必须保留格式锁")
        harness.finish()
        require(!device.locked && device.unlockCount == 1, "停止后必须释放格式锁")
        harness.finish()
        require(device.unlockCount == 1, "格式锁只能释放一次")
        let automatic = Device()
        try harness.configure(automatic, requestedFormat: nil)
        require(!automatic.locked && automatic.unlockCount == 0, "未配置设备保留自动选择")
        let unsupported = Device()
        do {
            try harness.configure(unsupported, requestedFormat: "fourcc:00000000;size:1x1;pixel-aspect:1/1")
            require(false, "不支持的格式应拒绝")
        } catch {}
        require(!unsupported.locked, "格式不支持不能泄漏设备锁")
        let denied = Device()
        denied.denyLock = true
        do { try harness.configure(denied, requestedFormat: denied.yuvs); require(false, "设备锁失败应拒绝") } catch {}
        harness.finish()
        require(denied.unlockCount == 0, "未获取的设备锁不能释放")
        print("Format lock: 9 lifetime/negotiation/input-creation checks passed (no camera opened)")
    }
}
'''
fixture = fixture.replace('__CONFIGURATION__', configuration).replace('__RELEASE__', release)
with tempfile.TemporaryDirectory(prefix='camera-format-lock-') as temporary:
    path = Path(temporary)
    (path / 'check.swift').write_text(fixture)
    subprocess.run(['swiftc', '-parse-as-library', str(base / 'Sources/CameraHelper/CaptureFormatPins.swift'), str(path / 'check.swift'), '-o', str(path / 'check')], check=True)
    subprocess.run([str(path / 'check')], check=True, timeout=10)
# Cleanup must release only after the production session has stopped, including failure.
assert release_signature in source
stop = block(source[source.index('    func stop() {'):], '    func stop() {')
assert stop.index('self.session?.stopRunning()') < stop.index('self.releaseFormatLock()')
catch = capture[capture.index('        } catch {'):capture.index('    // MARK: R3-08 正式 v2 握手')]
assert catch.index('captureSession.stopRunning()') < catch.index('releaseFormatLock()')
