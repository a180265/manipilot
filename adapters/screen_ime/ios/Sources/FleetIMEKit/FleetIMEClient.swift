// Fleet 输入法协议客户端：与 Arm Fleet 屏幕输入法服务（K40TaskServer 兼容协议）通信。
// 跨平台：iOS（键盘扩展）与 macOS（调试 CLI、单元测试）共用本实现，仅依赖 Foundation。

import Foundation

/// 键盘扩展依赖的服务端接口（生产用 FleetIMEClient，测试用内存桩）。
public protocol ImeAPIServing: Sendable {
    func heartbeat(deviceId: String, appVersion: String,
                   keyboardActive: Bool, currentTaskId: String?) async throws -> ImeDevice
    func fetchNextTask(deviceId: String, waitSeconds: Int) async throws -> ImeTask?
    @discardableResult
    func reportStatus(taskId: String, deviceId: String, status: TaskStatus,
                      typedCharacterCount: Int?, failureReason: String?) async throws -> ImeTask
    func fetchNextControl(taskId: String, deviceId: String,
                          waitSeconds: Int) async throws -> ControlCommand?
    @discardableResult
    func acknowledgeControl(taskId: String, deviceId: String, commandId: Int,
                            typedCharacterCount: Int?) async throws -> ImeTask
}

public struct FleetIMEClient: Sendable {
    public let baseURL: URL
    public let session: URLSession

    public init(baseURL: URL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    // MARK: - 底层请求

    struct TransportError: Error { var message: String }

    private func request(_ method: String, _ path: String,
                         query: [String: String] = [:],
                         body: Data? = nil,
                         contentType: String = "application/json") -> URLRequest {
        var components = URLComponents(url: baseURL.appendingPathComponent(path),
                                       resolvingAgainstBaseURL: false)!
        if !query.isEmpty {
            components.queryItems = query.map { URLQueryItem(name: $0.key, value: $0.value) }
        }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.httpBody = body
        if body != nil { request.setValue(contentType, forHTTPHeaderField: "Content-Type") }
        return request
    }

    private func send(_ request: URLRequest) async throws -> (Data?, HTTPURLResponse) {
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw TransportError(message: "非 HTTP 响应")
        }
        return (data, http)
    }

    /// 2xx 返回解码 JSON；204 返回 nil；其余按服务端错误格式抛出。
    private func sendJSON<T: Decodable>(_ request: URLRequest, as type: T.Type) async throws -> T? {
        let (data, http) = try await send(request)
        guard !(200...299).contains(http.statusCode) else {
            if data == nil || (data?.isEmpty ?? true) { return nil }
            return try JSONDecoder().decode(T.self, from: data!)
        }
        if http.statusCode == 204 { return nil }
        if let data, let failure = try? JSONDecoder().decode(ImeAPIFailure.self, from: data) {
            throw failure
        }
        throw TransportError(message: "服务端错误：HTTP \(http.statusCode)")
    }

    private func sendJSONBody<T: Decodable>(_ request: URLRequest, as type: T.Type) async throws -> T {
        guard let value = try await sendJSON(request, as: type) else {
            throw TransportError(message: "服务端返回空响应")
        }
        return value
    }

    private func encodeBody(_ payload: [String: Any?]) throws -> Data {
        let filtered = payload.filter { $0.value != nil }.mapValues { $0! }
        return try JSONSerialization.data(withJSONObject: filtered)
    }

    // MARK: - 心跳与任务

    public func heartbeat(deviceId: String, appVersion: String,
                          keyboardActive: Bool, currentTaskId: String?) async throws -> ImeDevice {
        let request = request("POST", "api/v1/devices/\(deviceId)/heartbeat",
                              body: try encodeBody([
                                "appVersion": appVersion,
                                "keyboardActive": keyboardActive,
                                "currentTaskId": currentTaskId]))
        return try await sendJSONBody(request, as: ImeDevice.self)
    }

    /// 长轮询领取任务；服务端 204（无任务）返回 nil。
    public func fetchNextTask(deviceId: String, waitSeconds: Int = 20) async throws -> ImeTask? {
        let request = request("GET", "api/v1/devices/\(deviceId)/tasks/next",
                              query: ["waitSeconds": String(waitSeconds)])
        return try await sendJSON(request, as: ImeTask.self)
    }

    @discardableResult
    public func reportStatus(taskId: String, deviceId: String, status: TaskStatus,
                             typedCharacterCount: Int? = nil,
                             failureReason: String? = nil) async throws -> ImeTask {
        let request = request("POST", "api/v1/tasks/\(taskId)/status",
                              body: try encodeBody([
                                "deviceId": deviceId,
                                "status": status.rawValue,
                                "typedCharacterCount": typedCharacterCount,
                                "failureReason": failureReason]))
        return try await sendJSONBody(request, as: ImeTask.self)
    }

    /// Agent 侧等待接口（调试 CLI 使用；键盘扩展用 control 长轮询感知取消）。
    public func waitForStatus(taskId: String, expected: [TaskStatus],
                              waitSeconds: Int = 20) async throws -> ImeTask {
        let request = request("GET", "api/v1/tasks/\(taskId)/wait",
                              query: ["expected": expected.map(\.rawValue).joined(separator: ","),
                                      "waitSeconds": String(waitSeconds)])
        return try await sendJSONBody(request, as: ImeTask.self)
    }

    public func cancel(taskId: String) async throws -> ImeTask {
        let request = request("POST", "api/v1/tasks/\(taskId)/cancel")
        return try await sendJSONBody(request, as: ImeTask.self)
    }

    /// 取消控制长轮询；无命令返回 nil。
    public func fetchNextControl(taskId: String, deviceId: String,
                                 waitSeconds: Int = 20) async throws -> ControlCommand? {
        let request = request("GET", "api/v1/tasks/\(taskId)/control/next",
                              query: ["deviceId": deviceId,
                                      "waitSeconds": String(waitSeconds)])
        return try await sendJSON(request, as: ControlCommand.self)
    }

    @discardableResult
    public func acknowledgeControl(taskId: String, deviceId: String, commandId: Int,
                                   typedCharacterCount: Int? = nil) async throws -> ImeTask {
        let request = request("POST", "api/v1/tasks/\(taskId)/control/ack",
                              body: try encodeBody([
                                "deviceId": deviceId,
                                "commandId": commandId,
                                "typedCharacterCount": typedCharacterCount]))
        return try await sendJSONBody(request, as: ImeTask.self)
    }

    // MARK: - 屏幕校准（校准页使用）

    public func fetchCurrentCalibration(deviceId: String) async throws -> CalibrationSession? {
        let request = request("GET", "api/v1/devices/\(deviceId)/calibration")
        return try await sendJSON(request, as: CalibrationSession.self)
    }

    @discardableResult
    public func reportCalibrationState(sessionId: String, deviceId: String,
                                       state: String) async throws -> CalibrationSession {
        let request = request("POST", "api/v1/calibrations/\(sessionId)/client",
                              body: try encodeBody(["deviceId": deviceId, "state": state]))
        return try await sendJSONBody(request, as: CalibrationSession.self)
    }

    // MARK: - 服务健康（宿主 App 的"测试连接"使用）

    /// GET /api/v1/health；返回 status 字段（ok / degraded），非 200 抛错。
    public func health() async throws -> String {
        let request = request("GET", "api/v1/health")
        let (data, http) = try await send(request)
        guard (200...299).contains(http.statusCode), let data else {
            throw TransportError(message: "服务不可达：HTTP \(http.statusCode)")
        }
        if let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let status = payload["status"] as? String {
            return status
        }
        throw TransportError(message: "健康检查响应格式不符")
    }

    /// 校准页 ACTION_UP 后回报触点（只回传屏幕像素与元数据，不保存机械坐标）。
    @discardableResult
    public func reportCalibrationTouch(sessionId: String, deviceId: String,
                                       sampleId: String, sequence: Int,
                                       screenX: Double, screenY: Double,
                                       screenWidth: Int, screenHeight: Int,
                                       orientation: String,
                                       eventTime: Int) async throws -> CalibrationSession {
        let request = request("POST", "api/v1/calibrations/\(sessionId)/touch",
                              body: try encodeBody([
                                "deviceId": deviceId, "sampleId": sampleId,
                                "sequence": sequence, "screenX": screenX,
                                "screenY": screenY, "screenWidth": screenWidth,
                                "screenHeight": screenHeight,
                                "orientation": orientation, "eventTime": eventTime]))
        return try await sendJSONBody(request, as: CalibrationSession.self)
    }
}

// MARK: - 协议符合（方法签名与 ImeAPIServing 一致，直接由既有实现满足）

extension FleetIMEClient: ImeAPIServing {}
