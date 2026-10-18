#!/usr/bin/env python3
"""Compile actual ChannelRow methods with an offline channel; no AppKit/camera/UI."""
from pathlib import Path
import subprocess
import tempfile

base = Path(__file__).resolve().parents[1]
source = (base / 'Sources/CameraHelper/main.swift').read_text()
row_source = source[source.index('private final class ChannelRow {'):]

def method(signature):
    start = row_source.index(signature)
    brace = row_source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (row_source[end] == '{') - (row_source[end] == '}')
        end += 1
    return row_source[start:end]

fixture = r'''
import Foundation
@MainActor final class Control {
    var stringValue = "8"
    var title = "启动"
    var isEnabled = true
}
final class OfflineChannel: @unchecked Sendable {
    let outcome: Bool
    let message: String
    var onStatus: (@MainActor @Sendable (String) -> Void)?
    init(outcome: Bool, message: String) { self.outcome = outcome; self.message = message }
    func setPushFPS(_ fps: Double) -> Bool { (1...30).contains(fps) }
    func select(device: Int?) {}
    func isBound(to device: Int?) -> Bool { device == 1 }
    func hasBinding() -> Bool { true }
    func start(completion: (@Sendable (Bool) -> Void)? = nil) -> Bool {
        DispatchQueue.global().async {
            let handler = self.onStatus
            DispatchQueue.main.async {
                MainActor.assumeIsolated { handler?(self.message) }
            }
            completion?(self.outcome)
        }
        return true
    }
}
@MainActor final class RowHarness {
    let channel: OfflineChannel
    let statusLabel = Control(), fpsField = Control(), devicePopup = Control()
    let saveButton = Control(), toggleButton = Control()
    private var running = false
    init(outcome: Bool, message: String) {
        channel = OfflineChannel(outcome: outcome, message: message)
        channel.onStatus = { [weak self] text in self?.statusLabel.stringValue = text }
    }
    func selectedDevice() -> Int? { 1 }
    // Insert production methods verbatim, rather than testing copied transition logic.
    __START__
    __SYNC__
}
@main struct Check {
    @MainActor static func main() {
        let failure = "摄像头启动后实际格式不符：要求 yuvs，输入 420v"
        let failed = RowHarness(outcome: false, message: failure)
        failed.start()
        let success = RowHarness(outcome: true, message: "推流中")
        success.start()
        let deadline = Date().addingTimeInterval(2)
        while (!failed.toggleButton.isEnabled || !success.toggleButton.isEnabled) && Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.01))
        }
        guard failed.statusLabel.stringValue == failure else {
            fatalError("启动错误被覆盖：\(failed.statusLabel.stringValue)")
        }
        assert(failed.toggleButton.title == "启动" && failed.fpsField.isEnabled)
        assert(success.statusLabel.stringValue == "推流中")
        assert(success.toggleButton.title == "停止" && !success.fpsField.isEnabled)
        print("ChannelRow: 4 asynchronous start presentation checks passed (no UI/camera)")
    }
}
'''
fixture = fixture.replace('__START__', method('    func start() {')).replace('__SYNC__', method('    private func syncControls() {'))
with tempfile.TemporaryDirectory(prefix='camera-row-status-') as temporary:
    path = Path(temporary)
    (path / 'check.swift').write_text(fixture)
    subprocess.run(['swiftc', '-parse-as-library', str(path / 'check.swift'), '-o', str(path / 'check')], check=True)
    subprocess.run([str(path / 'check')], check=True, timeout=10)
