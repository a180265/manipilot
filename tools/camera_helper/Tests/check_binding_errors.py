#!/usr/bin/env python3
"""Exercise production HTTP response handling offline; no camera or service writes."""
from pathlib import Path
import subprocess
import tempfile

base = Path(__file__).resolve().parents[1]
source = (base / 'Sources/CameraHelper/main.swift').read_text()
start = source.index('    private func send(_ request: URLRequest,')
brace = source.index('{', start)
depth, end = 1, brace + 1
while depth:
    depth += (source[end] == '{') - (source[end] == '}')
    end += 1
method = source[start:end].replace('private func send', 'func send', 1)
fixture = r'''
import Foundation
struct V2Error: Error { let statusCode: Int; let code: String?; let message: String }
struct Task { let callback: () -> Void; func resume() { callback() } }
final class Session {
    let status: Int, body: Data
    init(_ status: Int, _ body: String) { self.status = status; self.body = Data(body.utf8) }
    func dataTask(with request: URLRequest,
                  completionHandler: @escaping (Data?, URLResponse?, Error?) -> Void) -> Task {
        Task { completionHandler(self.body, HTTPURLResponse(url: request.url!,
            statusCode: self.status, httpVersion: nil, headerFields: nil), nil) }
    }
}
final class Client {
    let session: Session
    init(_ status: Int, _ body: String) { session = Session(status, body) }
    __SEND__
}
let cases: [(Int, String, String, String?)] = [
    (409, #"{"error":{"code":"device_safety_unresolved","message":"机械臂安全状态未知"}}"#,
     "机械臂安全状态未知", "device_safety_unresolved"),
    (409, #"{"error":{"code":"camera_unique_id_conflict","message":"物理相机已绑定通道 cam-02"}}"#,
     "物理相机已绑定通道 cam-02", "camera_unique_id_conflict"),
    (503, "<html>unavailable</html>", "HTTP 503", nil),
    (403, #"{"error":{"code":"camera_permission_denied","message":""}}"#,
     "HTTP 403", "camera_permission_denied")
]
for (status, body, expected, code) in cases {
    Client(status, body).send(URLRequest(url: URL(string: "http://localhost/offline")!)) { result in
        guard case .failure(let error) = result else { fatalError("错误响应被误判成功") }
        guard error.message.contains(expected), error.statusCode == status, error.code == code else {
            print("FAIL: expected \(expected), got \(error.message)")
            exit(1)
        }
    }
}
Client(200, "{}").send(URLRequest(url: URL(string: "http://localhost/offline")!)) { result in
    guard case .success = result else { fatalError("成功响应被误判失败") }
}
print("PASS: 5 production HTTP response checks; no camera/network")
'''.replace('__SEND__', method)
with tempfile.TemporaryDirectory(prefix='camera-binding-errors-') as temporary:
    path = Path(temporary)
    (path / 'check.swift').write_text(fixture)
    subprocess.run(['swiftc', str(path / 'check.swift'), '-o', str(path / 'check')], check=True)
    subprocess.run([str(path / 'check')], check=True, timeout=10)
