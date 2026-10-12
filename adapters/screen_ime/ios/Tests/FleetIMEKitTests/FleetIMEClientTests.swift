// 协议客户端测试：URLProtocol 桩验证路径、方法、请求体与响应解码（macOS 运行）。

import Foundation
import XCTest
@testable import FleetIMEKit

/// 可编程的 URL 桩：按路径返回录制响应。
final class StubURLProtocol: URLProtocol {
    struct Stub {
        var statusCode: Int
        var body: Data
    }

    nonisolated(unsafe) static var routes: [String: Stub] = [:]
    nonisolated(unsafe) static var recorded: [(method: String, path: String, body: Data?)] = []
    private static let lock = NSLock()

    static func reset(routes: [String: Stub]) {
        lock.lock()
        defer { lock.unlock() }
        self.routes = routes
        self.recorded = []
    }

    static var requests: [(String, String, Data?)] {
        lock.lock()
        defer { lock.unlock() }
        return recorded
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let path = request.url?.path ?? ""
        Self.lock.lock()
        Self.recorded.append((request.httpMethod ?? "GET", path, request.httpBody))
        let stub = Self.routes[path]
        Self.lock.unlock()

        let response = HTTPURLResponse(
            url: request.url!, statusCode: stub?.statusCode ?? 404,
            httpVersion: "HTTP/1.1",
            headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        if let body = stub?.body {
            client?.urlProtocol(self, didLoad: body)
        }
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}

final class FleetIMEClientTests: XCTestCase {
    var client: FleetIMEClient!

    override func setUp() {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        client = FleetIMEClient(baseURL: URL(string: "http://stub.local")!,
                                session: URLSession(configuration: configuration))
    }

    private func json(_ value: Any) -> Data {
        try! JSONSerialization.data(withJSONObject: value)
    }

    func testHeartbeatEncodesAndDecodes() async throws {
        StubURLProtocol.reset(routes: [
            "/api/v1/devices/k40-01/heartbeat": .init(statusCode: 200, body: json([
                "deviceId": "k40-01", "appVersion": "test", "keyboardActive": true,
                "currentTaskId": NSNull(), "lastSeenAt": "2026-08-30T00:00:00.000Z",
                "online": true])),
        ])
        let device = try await client.heartbeat(deviceId: "k40-01", appVersion: "test",
                                                keyboardActive: true, currentTaskId: nil)
        XCTAssertTrue(device.online)
        let (method, path, body) = StubURLProtocol.requests[0]
        XCTAssertEqual(method, "POST")
        XCTAssertEqual(path, "/api/v1/devices/k40-01/heartbeat")
        let payload = try JSONSerialization.jsonObject(with: body!) as! [String: Any]
        XCTAssertEqual(payload["keyboardActive"] as? Bool, true)
        XCTAssertNil(payload["currentTaskId"])
    }

    func testFetchNextTaskDecodesAnd204MeansNil() async throws {
        let taskJSON = [
            "taskId": "t-1", "deviceId": "k40-01", "targetUser": "张三",
            "content": "你好", "typingSpeed": "random", "resolvedTypingSpeed": "normal",
            "status": "pending", "typedCharacterCount": 0, "failureReason": NSNull(),
            "source": "api", "createdAt": "2026-08-30T00:00:00.000Z",
            "updatedAt": "2026-08-30T00:00:00.000Z",
        ] as [String: Any]
        StubURLProtocol.reset(routes: [
            "/api/v1/devices/k40-01/tasks/next": .init(statusCode: 200, body: json(taskJSON)),
            "/api/v1/devices/none/tasks/next": .init(statusCode: 204, body: Data()),
        ])
        let task = try await client.fetchNextTask(deviceId: "k40-01", waitSeconds: 0)
        XCTAssertEqual(task?.content, "你好")
        XCTAssertEqual(task?.statusValue, .pending)
        let empty = try await client.fetchNextTask(deviceId: "none", waitSeconds: 0)
        XCTAssertNil(empty)
    }

    func testServerFailureDecodedAsImeAPIFailure() async {
        StubURLProtocol.reset(routes: [
            "/api/v1/tasks/t-1/status": .init(statusCode: 409, body: json([
                "error": "invalid_transition",
                "message": "Cannot transition from pending to inserted"])),
        ])
        do {
            _ = try await client.reportStatus(taskId: "t-1", deviceId: "k40-01",
                                              status: .inserted, typedCharacterCount: 1)
            XCTFail("应当抛出服务端错误")
        } catch let failure as ImeAPIFailure {
            XCTAssertEqual(failure.error, "invalid_transition")
        } catch {
            XCTFail("错误类型不符：\(error)")
        }
    }

    func testCalibrationTouchReport() async throws {
        StubURLProtocol.reset(routes: [
            "/api/v1/calibrations/cal-1/touch": .init(statusCode: 200, body: json([
                "sessionId": "cal-1", "deviceId": "k40-01", "status": "active",
                "clientState": "open", "createdAt": "2026-08-30T00:00:00.000Z",
                "samples": [], "armed": NSNull()])),
        ])
        let session = try await client.reportCalibrationTouch(
            sessionId: "cal-1", deviceId: "k40-01", sampleId: "s1", sequence: 1,
            screenX: 540, screenY: 1200, screenWidth: 1080, screenHeight: 2400,
            orientation: "portrait", eventTime: 1)
        XCTAssertEqual(session.status, "active")
        let payload = try JSONSerialization.jsonObject(
            with: StubURLProtocol.requests[0].body!) as! [String: Any]
        XCTAssertEqual(payload["sampleId"] as? String, "s1")
        XCTAssertEqual(payload["screenX"] as? Double, 540)
    }
}
