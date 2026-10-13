import AppKit
@preconcurrency import AVFoundation
import CoreImage
import CoreMedia
import CryptoKit

private let serverBaseURL = URL(string: ProcessInfo.processInfo
    .environment["CAMERA_FRAME_SERVER_BASE_URL"]
    ?? "http://127.0.0.1:8790")!
private let previewBaseURL = URL(string: ProcessInfo.processInfo
    .environment["CAMERA_PREVIEW_SERVER_BASE_URL"]
    ?? "http://127.0.0.1:8793")!
private let cameraIDs = (1...10).map { String(format: "cam-%02d", $0) }
// §5/§6（camera-binding 附件）：来源时序证明固定标记与上限。
private let timingProvenance = "camera_pts_host_clock.v1"
private let maxCaptureUncertaintyMs = 20

// MARK: - R3-08：helper 机器凭据与受控本机证明（与 WX/手机凭据独立）

/// §4：机器凭据只从受限本机启动配置读取（CAMERA_CONTROL_TOKEN_FILE 指向的
/// 0600 文件，64 位小写 hex）。不进 URL、不进日志、不与 phone/IME 凭据混用；
/// 缺失/损坏时 v2 握手明确不可用，不退回 legacy 冒充 v2。
private struct MachineCredential {
    let token: String

    static func load() -> MachineCredential? {
        guard let raw = ProcessInfo.processInfo
            .environment["CAMERA_CONTROL_TOKEN_FILE"]?
            .trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else {
            return nil
        }
        guard let data = FileManager.default.contents(atPath: raw),
              let text = String(data: data, encoding: .utf8) else {
            return nil
        }
        let token = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard token.count == 64,
              token.allSatisfy({ ($0.isHexDigit && !$0.isUppercase) }) else {
            return nil
        }
        return MachineCredential(token: token)
    }
}

private func sha256Hex(_ string: String) -> String {
    let digest = SHA256.hash(data: Data(string.utf8))
    return digest.map { String(format: "%02x", $0) }.joined()
}

/// §6：宿主代际指纹输入 = {host_installation_id, boot_seconds, boot_microseconds}
///（本机 OS 读取；与 Fleet 侧 tools/verify_camera_clock.py 同一算法/键序）。
private func sourceClockID() -> String? {
    guard let raw = ProcessInfo.processInfo
        .environment["CAMERA_HOST_INSTALLATION_ID_FILE"]?
        .trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else {
        return nil
    }
    guard let fileText = try? String(contentsOfFile: raw, encoding: .utf8) else {
        return nil
    }
    let text = fileText.trimmingCharacters(in: .whitespacesAndNewlines)
    guard text.count == 36, text == text.lowercased() else { return nil }
    var size: Int = 0
    guard sysctlbyname("kern.boottime", nil, &size, nil, 0) == 0, size >= 12 else {
        return nil
    }
    var buffer = [UInt8](repeating: 0, count: size)
    guard sysctlbyname("kern.boottime", &buffer, &size, nil, 0) == 0 else {
        return nil
    }
    // struct timeval { tv_sec: Int64; tv_usec: Int32 }（arm64 小端布局）
    func littleEndian(_ offset: Int, _ count: Int) -> UInt64 {
        var value: UInt64 = 0
        for index in 0..<count {
            value |= UInt64(buffer[offset + index]) << (8 * UInt64(index))
        }
        return value
    }
    let bootSeconds = Int(littleEndian(0, 8))
    let bootMicroseconds = Int(littleEndian(8, 4))
    // 规范 JSON：键排序、紧凑分隔、整数值不加引号
    let canonical = "{\"boot_microseconds\":\(bootMicroseconds)," +
        "\"boot_seconds\":\(bootSeconds)," +
        "\"host_installation_id\":\"\(text)\"}"
    return "mach-absolute:" + sha256Hex(canonical)
}

/// 受控安装二进制摘要（自证身份；服务端与受控路径摘要比对后才登记会话）。
private func helperBinaryDigest() -> String? {
    let url = Bundle.main.executableURL
        ?? URL(fileURLWithPath: CommandLine.arguments.first ?? "")
    guard let data = try? Data(contentsOf: url) else { return nil }
    return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

// MARK: - §3 来源几何（实际管线事实；摘要=规范化 JSON SHA-256）

private func canonicalGeometryJSON(inputWidth: Int, inputHeight: Int,
                                   outputWidth: Int, outputHeight: Int,
                                   formatID: String) -> String {
    // 键排序、紧凑分隔、整数不加引号（与 Fleet v2.canonical_digest 一致）
    return "{\"contract_version\":\"source-geometry.v1\"," +
        "\"crop\":{\"bottom\":\(inputHeight),\"left\":0," +
        "\"right\":\(inputWidth),\"top\":0}," +
        "\"format_id\":\"\(formatID)\"," +
        "\"input_size\":{\"height\":\(inputHeight),\"width\":\(inputWidth)}," +
        "\"mirror_x\":false,\"mirror_y\":false," +
        "\"output_size\":{\"height\":\(outputHeight),\"width\":\(outputWidth)}," +
        "\"resampler\":\"none\",\"rotation_degrees\":0," +
        "\"stabilization\":\"off\",\"zoom_milli\":1000}"
}

/// §3：format_id = fourcc:<8位小写hex媒体子类型>;size:<宽>x<高>;pixel-aspect:<n>/<d>
///（从实际采集格式读取；缺失即不具新模式资格）。
private func formatIDFromDescription(_ format: CMFormatDescription) -> String? {
    let fourcc = CMFormatDescriptionGetMediaSubType(format)
    let dims = CMVideoFormatDescriptionGetDimensions(format)
    return "fourcc:\(String(format: "%08x", fourcc));" +
        "size:\(dims.width)x\(dims.height);pixel-aspect:1/1"
}

// MARK: - §6 同机时钟：CMSyncConvertTime → host clock → mach 纳秒

private func machTimebaseNanos(_ units: UInt64) -> Int64 {
    var timebase = mach_timebase_info_data_t()
    mach_timebase_info(&timebase)
    // 整数换算；舍入误差 ≤1ns，计入采样不确定度声明
    return Int64(units * UInt64(timebase.numer) / UInt64(timebase.denom))
}

/// 宿主 host clock（CMClockGetHostTimeClock）当前 tick 的纳秒值。
/// Fleet 侧 tools/verify_camera_clock.py 以本机前后 monotonic_ns 包住该值核对。
private func hostTickNanos() -> Int64 {
    let hostTime = CMClockGetTime(CMClockGetHostTimeClock())
    return machTimebaseNanos(CMClockConvertHostTimeToSystemUnits(hostTime))
}

/// 本机 mach 单调时钟纳秒值（与 Fleet time.monotonic_ns 同域：mach_absolute_time）。
private func monotonicNanos() -> Int64 {
    return machTimebaseNanos(mach_absolute_time())
}

// MARK: - §5 不可变来源帧包与会话事实

/// 采集回调一次生成的不可变来源帧包；编码/上传只消费该包，
/// 不得在上传时读取“当前绑定”补标，也不得用上传时刻 Date() 冒充采样时间。
private struct SourceFramePacket {
    let jpeg: Data
    let sourceSequence: UInt64
    let ptsHostNanos: Int64         // 本帧 PTS 经 CMSyncConvertTime 归一 host clock 的纳秒值
    let capturedMonotonicNanos: Int64
    let capturedAtISO: String       // PTS 采样时刻的 UTC 投影（回调现场投影，非上传时刻）
    let captureUncertaintyMs: Int   // 按本帧 PTS tick 上界 + 换算舍入计算（非固定常量）
    let width: Int
    let height: Int
    let formatFacts: String         // 采集格式事实（PTS 来源证明随帧留存）
}

/// 经 binding-v2 / capture-sessions 握手取得的可信来源会话事实。
/// R3-08：正式客户端只携带经服务端登记的会话上传；握手失败/认证失败/
/// 会话失效时明确不可用，不再退回兼容上传冒充 v2。
private struct SourceSessionInfo {
    let cameraBindingId: String
    let sourceGeometryId: String
    let sourceGenerationId: String
    let sourceClockId: String
    let formatId: String
    let helperSha256: String
}

/// v2 协议错误：状态码 + 服务端错误 code（用于区分认证失败/会话过期/换绑）。
private struct V2Error: Error {
    let statusCode: Int
    let code: String?
    let message: String
}

/// 握手用受控本机证明事实（采集启动时计算一次；只读跨队列传递）。
private struct HandshakeFacts: @unchecked Sendable {
    let geometry: [String: Any]
    let geometryID: String
    let formatID: String
    let clockID: String
    let helperSha256: String
}

/// capture-sessions/current 查询结果（JSON 字典只读跨队列传递）。
private struct CurrentSessionView: @unchecked Sendable {
    let serverInstanceID: String
    let session: [String: Any]?
}

private final class FrameUploader: @unchecked Sendable {
    private let session = URLSession(configuration: .ephemeral)
    /// §4：机器凭据（受限本机配置载入；缺失时 v2 明确不可用）
    let credential: MachineCredential?

    init(credential: MachineCredential? = MachineCredential.load()) {
        self.credential = credential
    }

    private func bearerRequest(_ url: URL, method: String,
                               jsonBody: [String: Any]?,
                               credential: MachineCredential?) -> URLRequest {
        var request = URLRequest(url: url)
        request.httpMethod = method
        if let jsonBody {
            request.httpBody = try? JSONSerialization.data(withJSONObject: jsonBody)
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        // Bearer 只进 Authorization 头；不入 URL、不写日志
        if let credential {
            request.setValue("Bearer \(credential.token)",
                             forHTTPHeaderField: "Authorization")
        }
        return request
    }

    private func send(_ request: URLRequest,
                      completion: @escaping @Sendable (Result<Data, V2Error>) -> Void) {
        session.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(V2Error(statusCode: 0, code: nil,
                                            message: error.localizedDescription)))
                return
            }
            guard let http = response as? HTTPURLResponse else {
                completion(.failure(V2Error(statusCode: 0, code: nil,
                                            message: "非 HTTP 响应")))
                return
            }
            let payload = data ?? Data()
            if http.statusCode == 200 {
                completion(.success(payload))
                return
            }
            var code: String?
            if let object = try? JSONSerialization.jsonObject(with: payload)
                    as? [String: Any],
               let errorBody = object["error"] as? [String: Any] {
                code = errorBody["code"] as? String
            }
            completion(.failure(V2Error(statusCode: http.statusCode, code: code,
                                        message: "HTTP \(http.statusCode)")))
        }.resume()
    }

    /// §4：冻结 v2 绑定（PUT /cameras/{id}/binding-v2，Bearer）。
    /// 不再调用旧 PUT /binding；cameraBindingId 由服务端分配（幂等 requestId）。
    func bindV2(
        cameraID: String,
        device: AVCaptureDevice,
        credential: MachineCredential,
        completion: @escaping @Sendable (Result<String, V2Error>) -> Void
    ) {
        bindV2(cameraID: cameraID, uniqueID: device.uniqueID,
               localizedName: device.localizedName,
               credential: credential, completion: completion)
    }

    /// 绑定实现（AVFoundation 与合成采集源共用同一生产路径）。
    func bindV2(
        cameraID: String,
        uniqueID: String,
        localizedName: String,
        credential: MachineCredential,
        completion: @escaping @Sendable (Result<String, V2Error>) -> Void
    ) {
        let url = serverBaseURL
            .appendingPathComponent("cameras")
            .appendingPathComponent(cameraID)
            .appendingPathComponent("binding-v2")
        let request = bearerRequest(url, method: "PUT",
                                    jsonBody: ["requestId": UUID().uuidString,
                                               "deviceUniqueId": uniqueID,
                                               "displayName": localizedName],
                                    credential: credential)
        send(request) { result in
            switch result {
            case .failure(let failure):
                completion(.failure(failure))
            case .success(let data):
                if let object = try? JSONSerialization.jsonObject(with: data)
                        as? [String: Any],
                   let bindingID = object["cameraBindingId"] as? String,
                   !bindingID.isEmpty {
                    completion(.success(bindingID))
                } else {
                    completion(.failure(V2Error(statusCode: 200, code: nil,
                                                message: "绑定响应缺少 cameraBindingId")))
                }
            }
        }
    }

    /// §4：读取当前绑定实例（GET /cameras/{id}/binding-v2，Bearer）。
    func fetchBindingV2(
        cameraID: String,
        credential: MachineCredential,
        completion: @escaping @Sendable (Result<(bindingID: String,
                                                 deviceUniqueID: String), V2Error>) -> Void
    ) {
        let url = serverBaseURL
            .appendingPathComponent("cameras")
            .appendingPathComponent(cameraID)
            .appendingPathComponent("binding-v2")
        let request = bearerRequest(url, method: "GET", jsonBody: nil,
                                    credential: credential)
        send(request) { result in
            switch result {
            case .failure(let failure):
                completion(.failure(failure))
            case .success(let data):
                if let object = try? JSONSerialization.jsonObject(with: data)
                        as? [String: Any],
                   let entry = object["binding"] as? [String: Any],
                   let bindingID = entry["cameraBindingId"] as? String,
                   let uniqueID = entry["deviceUniqueId"] as? String,
                   !bindingID.isEmpty {
                    completion(.success((bindingID, uniqueID)))
                } else {
                    completion(.failure(V2Error(statusCode: 200, code: nil,
                                                message: "binding-v2 响应不完整")))
                }
            }
        }
    }

    /// §4：读取当前采集会话视图（GET capture-sessions/current，Bearer；
    /// 无认证读法已废除——403 即 v2 不可用，不退回兼容）。
    func fetchCurrentSession(
        cameraID: String,
        credential: MachineCredential,
        completion: @escaping @Sendable (Result<CurrentSessionView, V2Error>) -> Void
    ) {
        let url = serverBaseURL
            .appendingPathComponent("cameras")
            .appendingPathComponent(cameraID)
            .appendingPathComponent("capture-sessions")
            .appendingPathComponent("current")
        let request = bearerRequest(url, method: "GET", jsonBody: nil,
                                    credential: credential)
        send(request) { result in
            switch result {
            case .failure(let failure):
                completion(.failure(failure))
            case .success(let data):
                if let object = try? JSONSerialization.jsonObject(with: data)
                        as? [String: Any],
                   let serverInstanceID = object["server_instance_id"] as? String,
                   !serverInstanceID.isEmpty {
                    completion(.success(CurrentSessionView(
                        serverInstanceID: serverInstanceID,
                        session: object["session"] as? [String: Any])))
                } else {
                    completion(.failure(V2Error(statusCode: 200, code: nil,
                                                message: "current 响应缺少 server_instance_id")))
                }
            }
        }
    }

    /// §4/§5：创建采集会话（POST capture-sessions，Bearer）。携带实际管线
    /// geometry、当前真实格式、时钟证明、host 代际、helper 二进制摘要与
    /// 误差界限；服务端受控核验通过后返回 source_generation_id。
    func createCaptureSession(
        cameraID: String,
        credential: MachineCredential,
        serverInstanceID: String,
        clientInstanceID: String,
        bindingID: String,
        geometry: [String: Any],
        clockID: String,
        formatID: String,
        helperSha256: String,
        captureUncertaintyMs: Int,
        replacesGenerationID: String?,
        completion: @escaping @Sendable (Result<SourceSessionInfo, V2Error>) -> Void
    ) {
        var body: [String: Any] = [
            "serverInstanceId": serverInstanceID,
            "clientInstanceId": clientInstanceID,
            "cameraBindingId": bindingID,
            "geometry": geometry,
            "sourceClockId": clockID,
            "timingProvenance": timingProvenance,
            "captureUncertaintyMs": captureUncertaintyMs,
            "formatId": formatID,
            "helperSha256": helperSha256,
        ]
        if let replacesGenerationID {
            body["replacesGenerationId"] = replacesGenerationID
        }
        let url = serverBaseURL
            .appendingPathComponent("cameras")
            .appendingPathComponent(cameraID)
            .appendingPathComponent("capture-sessions")
        let request = bearerRequest(url, method: "POST", jsonBody: body,
                                    credential: credential)
        send(request) { result in
            switch result {
            case .failure(let failure):
                completion(.failure(failure))
            case .success(let data):
                if let object = try? JSONSerialization.jsonObject(with: data)
                        as? [String: Any],
                   let generation = object["source_generation_id"] as? String,
                   let geometryID = object["source_geometry_id"] as? String,
                   let clock = object["source_clock_id"] as? String,
                   !generation.isEmpty, !geometryID.isEmpty, !clock.isEmpty {
                    completion(.success(SourceSessionInfo(
                        cameraBindingId: bindingID,
                        sourceGeometryId: geometryID,
                        sourceGenerationId: generation,
                        sourceClockId: clock,
                        formatId: formatID,
                        helperSha256: helperSha256)))
                } else {
                    completion(.failure(V2Error(statusCode: 200, code: nil,
                                                message: "会话响应身份字段不完整")))
                }
            }
        }
    }

    func fetchBindings(
        completion: @escaping @Sendable (Result<[String: String], Error>) -> Void
    ) {
        let request = URLRequest(url: serverBaseURL.appendingPathComponent("cameras"))
        session.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(error))
                return
            }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                  let data,
                  let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let cameras = payload["cameras"] as? [[String: Any]] else {
                completion(.failure(NSError(
                    domain: "CameraHelper", code: 0,
                    userInfo: [NSLocalizedDescriptionKey: "读取摄像头绑定失败"]
                )))
                return
            }
            var result: [String: String] = [:]
            for item in cameras {
                let cameraID = (item["camera_id"] ?? item["cameraId"]) as? String
                let uniqueID = (item["device_unique_id"] ?? item["deviceUniqueId"]) as? String
                if let cameraID, let uniqueID, !uniqueID.isEmpty {
                    result[cameraID] = uniqueID
                }
            }
            completion(.success(result))
        }.resume()
    }

    /// §5：上传只消费采集回调冻结的不可变帧包；X-Captured-At 取包内采样时刻，
    /// 禁止在上传现场用 Date() 重打时间。正式客户端必须携带已登记来源会话
    /// （v2 身份头：绑定实例/几何/代际/时钟/单调采样时间/PTS 证明/不确定度）；
    /// 无会话时调用方不得上传（明确不可用，不退回 legacy 冒充 v2）。
    func upload(
        cameraID: String,
        deviceUniqueID: String,
        packet: SourceFramePacket,
        sourceSession: SourceSessionInfo,
        completion: @escaping @Sendable (Result<Void, V2Error>) -> Void
    ) {
        var request = URLRequest(url: serverBaseURL
            .appendingPathComponent("cameras")
            .appendingPathComponent(cameraID)
            .appendingPathComponent("frame"))
        request.httpMethod = "POST"
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.setValue(cameraID, forHTTPHeaderField: "X-Camera-Id")
        request.setValue(deviceUniqueID, forHTTPHeaderField: "X-Camera-Unique-Id")
        // 真实采样时刻（采集回调现场投影），非上传时刻
        request.setValue(packet.capturedAtISO, forHTTPHeaderField: "X-Captured-At")
        request.setValue(String(packet.sourceSequence), forHTTPHeaderField: "X-Source-Frame-Seq")
        let info = sourceSession
        request.setValue("camera-binding.v2", forHTTPHeaderField: "X-Camera-Binding-Contract")
        request.setValue(info.cameraBindingId, forHTTPHeaderField: "X-Camera-Binding-Id")
        request.setValue(info.sourceGeometryId, forHTTPHeaderField: "X-Source-Geometry-Id")
        request.setValue(info.sourceGenerationId, forHTTPHeaderField: "X-Source-Generation-Id")
        request.setValue(info.sourceClockId, forHTTPHeaderField: "X-Source-Clock-Id")
        request.setValue(String(packet.capturedMonotonicNanos),
                         forHTTPHeaderField: "X-Source-Captured-Monotonic-Ns")
        request.setValue(timingProvenance, forHTTPHeaderField: "X-Source-Timing-Provenance")
        request.setValue(String(packet.captureUncertaintyMs),
                         forHTTPHeaderField: "X-Source-Capture-Uncertainty-Ms")
        // 正式采集来源标记：与 Chrome 兼容降级桥（chrome_compat）区分，禁止互相冒充。
        request.setValue("camera_helper", forHTTPHeaderField: "X-Frame-Source")
        request.httpBody = packet.jpeg
        session.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(V2Error(statusCode: 0, code: nil,
                                            message: error.localizedDescription)))
                return
            }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                var code: String?
                if let payload = data,
                   let object = try? JSONSerialization.jsonObject(with: payload)
                        as? [String: Any],
                   let errorBody = object["error"] as? [String: Any] {
                    code = errorBody["code"] as? String
                }
                let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                completion(.failure(V2Error(statusCode: status, code: code,
                                            message: "上传失败 HTTP \(status)")))
                return
            }
            completion(.success(()))
        }.resume()
    }

    /// R3-08：旧的无认证 binding-v2/current 读取已废除——会话事实一律经
    /// fetchBindingV2/fetchCurrentSession/createCaptureSession（Bearer）取得；
    /// 认证失败/握手失效按明确不可用处理，不退回兼容上传冒充 v2。

    func previewViewerCount(
        cameraID: String,
        completion: @escaping @Sendable (Result<Int, Error>) -> Void
    ) {
        var request = URLRequest(url: previewBaseURL
            .appendingPathComponent("cameras")
            .appendingPathComponent(cameraID)
            .appendingPathComponent("preview/status"))
        request.timeoutInterval = 1.0
        session.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(error))
                return
            }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                  let data,
                  let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let count = payload["viewer_count"] as? Int else {
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                completion(.failure(NSError(
                    domain: "CameraHelper", code: code,
                    userInfo: [NSLocalizedDescriptionKey: "读取预览观看状态失败 HTTP \(code)"]
                )))
                return
            }
            completion(.success(max(0, count)))
        }.resume()
    }

    func uploadPreview(
        cameraID: String,
        deviceUniqueID: String,
        sourceSequence: UInt64,
        jpeg: Data,
        width: Int,
        height: Int,
        completion: @escaping @Sendable (Result<Void, Error>) -> Void
    ) {
        var request = URLRequest(url: previewBaseURL
            .appendingPathComponent("cameras")
            .appendingPathComponent(cameraID)
            .appendingPathComponent("preview/frame"))
        request.httpMethod = "POST"
        request.timeoutInterval = 1.0
        request.setValue("image/jpeg", forHTTPHeaderField: "Content-Type")
        request.setValue(cameraID, forHTTPHeaderField: "X-Camera-Id")
        request.setValue(deviceUniqueID, forHTTPHeaderField: "X-Camera-Unique-Id")
        request.setValue(String(sourceSequence), forHTTPHeaderField: "X-Source-Frame-Seq")
        request.setValue(String(width), forHTTPHeaderField: "X-Frame-Width")
        request.setValue(String(height), forHTTPHeaderField: "X-Frame-Height")
        request.setValue(ISO8601DateFormatter().string(from: Date()), forHTTPHeaderField: "X-Captured-At")
        request.httpBody = jpeg
        session.dataTask(with: request) { data, response, error in
            if let error {
                completion(.failure(error))
                return
            }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                let body = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                completion(.failure(NSError(
                    domain: "CameraHelper", code: code,
                    userInfo: [NSLocalizedDescriptionKey: "预览上传失败 HTTP \(code) \(body)"]
                )))
                return
            }
            completion(.success(()))
        }.resume()
    }
}

// MARK: R4-07-D 合成采集源（唯一替换点：物理相机采集回调）

/// 受控测试缝：`--selftest-stream` 用定时器生成的 CMSampleBuffer 走与
/// AVFoundation 完全相同的 captureOutput 生产路径（帧包冻结/PTS 归一/
/// v2 身份头/上传全部不替换）。仅物理采集回调被替换；握手协议逻辑
/// （binding-v2 → current → capture-sessions → frame）保持生产实现。
struct SyntheticSourceDescriptor {
    let uniqueID: String
    let localizedName: String
    let width: Int
    let height: Int
    let fps: Double

    static let environment = SyntheticSourceDescriptor(
        uniqueID: ProcessInfo.processInfo
            .environment["CAMERA_SELFTEST_DEVICE_UNIQUE_ID"]
            ?? "uvc-swift-selftest-01",
        localizedName: "Swift Selftest Synthetic Camera",
        // 与 AVFoundation 路径的归一输出（1280x720）一致：上传帧不再缩放，
        // 声明的来源几何与实际帧尺寸一一对应。
        width: 1280, height: 720, fps: 4.0)
}

private final class CameraChannel: NSObject, AVCaptureVideoDataOutputSampleBufferDelegate, @unchecked Sendable {
    let cameraID: String
    private let uploader: FrameUploader
    let captureQueue: DispatchQueue
    private let encodeContext = CIContext(options: [.cacheIntermediates: false])

    private(set) var device: AVCaptureDevice?
    /// R4-07-D：合成采集源（非 nil 时 start() 走合成帧路径）。
    var syntheticSource: SyntheticSourceDescriptor?
    private var syntheticTimer: DispatchSourceTimer?
    private var syntheticFrameIndex: UInt64 = 0
    private var session: AVCaptureSession?
    private var lastFrameAt: CFTimeInterval = 0
    private var sourceSequence: UInt64 = 0
    private var previewSequence: UInt64 = 0
    private var uploadInFlight = false
    private var previewUploadInFlight = false
    private var pendingPreview: (jpeg: Data, sequence: UInt64)?
    private var previewViewerCount = 0
    private var previewPollTimer: DispatchSourceTimer?
    private var running = false
    var boundUniqueID: String?
    /// 每通道推送帧率上限（GUI 每行可调，默认 8fps；夹取 1.0-30.0）。
    /// 采集端经 videoMin/MaxFrameDuration 定节拍，上传端再按 1/fps 间隔闸门。
    var pushFPS: Double = 8.0
    private var frameWindowStart: CFTimeInterval = 0
    private var frameWindowCount = 0
    private var measuredFPS = 0.0
    // §4/§5：经 binding-v2/capture-sessions 握手登记的可信来源会话。
    // R3-08：正式客户端只在持有效会话时上传；握手失败/认证失败/会话失效
    // 明确不可用，不退回兼容上传冒充 v2。
    private var sourceSession: SourceSessionInfo?
    private var v2UnavailableReason: String?
    private var lastUnavailableNoticeAt: TimeInterval = 0
    // 当前捕获格式事实（PTS 来源证明随帧留存）
    private var formatFacts = "unknown"
    // 握手用的受控本机证明事实（启动采集时一次性计算）
    private var handshakeFacts: HandshakeFacts?

    var onStatus: (@MainActor @Sendable (String) -> Void)?
    var onPreview: (@MainActor @Sendable (Data) -> Void)?

    init(cameraID: String, uploader: FrameUploader) {
        self.cameraID = cameraID
        self.uploader = uploader
        self.captureQueue = DispatchQueue(label: "camera-helper.\(cameraID)", qos: .userInitiated)
        super.init()
    }

    /// 设置推送帧率；非法值返回 false（GUI 侧提示），成功夹取到 1.0-30.0。
    @discardableResult
    func setPushFPS(_ value: Double) -> Bool {
        guard value.isFinite, value >= 1.0, value <= 30.0 else { return false }
        pushFPS = value
        return true
    }

    func select(device: AVCaptureDevice?) {
        guard !running else { return }
        self.device = device
    }

    func restoreBinding(uniqueID: String?) {
        guard !running else { return }
        boundUniqueID = uniqueID
    }

    func saveBinding(completion: (@Sendable (Bool) -> Void)? = nil) {
        guard !running else {
            publish("请先停止该通道再保存绑定")
            completion?(false)
            return
        }
        guard let device else {
            publish("未选择摄像头")
            completion?(false)
            return
        }
        guard let credential = uploader.credential else {
            // §4：机器凭据独立于 WX/手机凭据；未配置=能力明确不可用
            publish("机器凭据未配置（CAMERA_CONTROL_TOKEN_FILE）：v2 绑定不可用")
            completion?(false)
            return
        }
        publish("保存绑定（binding-v2）…")
        uploader.bindV2(cameraID: cameraID, device: device,
                        credential: credential) { [weak self] result in
            guard let self else { return }
            switch result {
            case .failure(let error):
                self.publish("绑定失败：\(error.message)")
                completion?(false)
            case .success:
                self.boundUniqueID = device.uniqueID
                self.publish("绑定已保存，等待启动")
                completion?(true)
            }
        }
    }

    func hasBinding() -> Bool {
        boundUniqueID != nil
    }

    func isBound(to device: AVCaptureDevice?) -> Bool {
        guard let device else { return false }
        return boundUniqueID == device.uniqueID
    }

    func start(completion: (@Sendable (Bool) -> Void)? = nil) -> Bool {
        guard !running else { return true }
        if let synthetic = syntheticSource {
            // R4-07-D 合成采集路径：与物理相机同一身份/绑定/握手门。
            guard boundUniqueID == synthetic.uniqueID else {
                publish("请先保存该物理摄像头绑定")
                completion?(false)
                return false
            }
            captureQueue.async {
                self.startSyntheticCapture(source: synthetic, completion: completion)
            }
            return true
        }
        guard let device else {
            publish("未选择摄像头")
            completion?(false)
            return false
        }
        guard boundUniqueID == device.uniqueID else {
            publish("请先保存该物理摄像头绑定")
            completion?(false)
            return false
        }
        captureQueue.async { self.startCapture(device: device, completion: completion) }
        return true
    }

    func stop() {
        captureQueue.async {
            self.previewPollTimer?.cancel()
            self.previewPollTimer = nil
            self.syntheticTimer?.cancel()
            self.syntheticTimer = nil
            self.session?.stopRunning()
            self.session = nil
            self.running = false
            self.uploadInFlight = false
            self.previewUploadInFlight = false
            self.pendingPreview = nil
            self.previewViewerCount = 0
            self.sourceSession = nil
            self.v2UnavailableReason = nil
            self.handshakeFacts = nil
            self.publish("已停止")
        }
    }

    /// R4-07-D：合成采集启动。握手事实（format/geometry/clock/binary digest）
    /// 与 AVFoundation 路径同一构造逻辑；帧经同一 captureOutput 生产路径
    /// （冻结帧包/PTS→host clock 归一/v2 头/上传/失败重握手全部生产实现）。
    private func startSyntheticCapture(
        source: SyntheticSourceDescriptor,
        completion: (@Sendable (Bool) -> Void)? = nil
    ) {
        running = true
        lastFrameAt = 0
        sourceSequence = 0
        frameWindowStart = CACurrentMediaTime()
        frameWindowCount = 0
        measuredFPS = 0
        previewSequence = 0
        previewUploadInFlight = false
        pendingPreview = nil
        previewViewerCount = 0
        sourceSession = nil
        // 合成格式事实：与实际生成的像素缓冲一致（BGRA/W×H）。
        let formatID = "fourcc:\(String(format: "%08x", kCVPixelFormatType_32BGRA));"
            + "size:\(source.width)x\(source.height);pixel-aspect:1/1"
        formatFacts = formatID
        let canonical = canonicalGeometryJSON(
            inputWidth: source.width, inputHeight: source.height,
            outputWidth: min(source.width, 1280),
            outputHeight: min(source.height, 720),
            formatID: formatID)
        let geometryObject: [String: Any] = [
            "contract_version": "source-geometry.v1",
            "input_size": ["width": source.width, "height": source.height],
            "format_id": formatID,
            "crop": ["left": 0, "top": 0, "right": source.width,
                     "bottom": source.height],
            "rotation_degrees": 0,
            "mirror_x": false, "mirror_y": false,
            "output_size": ["width": source.width, "height": source.height],
            "resampler": "none", "zoom_milli": 1000, "stabilization": "off",
        ]
        let clockID = sourceClockID()
        let helperSha256 = helperBinaryDigest()
        if let clockID, let helperSha256 {
            handshakeFacts = HandshakeFacts(
                geometry: geometryObject,
                geometryID: sha256Hex(canonical),
                formatID: formatID,
                clockID: clockID,
                helperSha256: helperSha256)
            v2UnavailableReason = nil
        } else {
            handshakeFacts = nil
            v2UnavailableReason = "受控本机证明缺失：v2 来源不可用"
        }
        performHandshake(attempt: 0)
        startPreviewPolling()
        publish("推流中（合成采集源）")
        // 合成采集回调：定时生成 CMSampleBuffer → 同一 captureOutput。
        let timer = DispatchSource.makeTimerSource(queue: captureQueue)
        timer.schedule(deadline: .now() + 0.2,
                       repeating: .milliseconds(Int(1000.0 / source.fps)))
        timer.setEventHandler { [weak self] in
            self?.emitSyntheticFrame(source: source)
        }
        syntheticTimer = timer
        timer.resume()
        completion?(true)
    }

    private func emitSyntheticFrame(source: SyntheticSourceDescriptor) {
        guard running else { return }
        var pixelBuffer: CVPixelBuffer?
        let attrs: [CFString: Any] = [kCVPixelBufferCGImageCompatibilityKey: true]
        guard CVPixelBufferCreate(kCFAllocatorDefault, source.width, source.height,
                                  kCVPixelFormatType_32BGRA, attrs as CFDictionary,
                                  &pixelBuffer) == kCVReturnSuccess,
              let buffer = pixelBuffer else {
            publish("合成帧像素缓冲创建失败")
            return
        }
        syntheticFrameIndex += 1
        let index = syntheticFrameIndex
        CVPixelBufferLockBaseAddress(buffer, [])
        defer { CVPixelBufferUnlockBaseAddress(buffer, []) }
        if let base = CVPixelBufferGetBaseAddress(buffer) {
            let bytesPerRow = CVPixelBufferGetBytesPerRow(buffer)
            let height = CVPixelBufferGetHeight(buffer)
            let pattern = UInt8(truncatingIfNeeded: index &* 37)
            memset(base, Int32(pattern), bytesPerRow * height)
        }
        var formatDescription: CMFormatDescription?
        guard CMVideoFormatDescriptionCreateForImageBuffer(
            allocator: kCFAllocatorDefault, imageBuffer: buffer,
            formatDescriptionOut: &formatDescription) == noErr,
            let format = formatDescription else {
            publish("合成帧格式描述创建失败")
            return
        }
        // PTS 直接取 host clock 当前时刻（同机时钟域；与真实 PTS 归一目标一致）
        let hostClock = CMClockGetHostTimeClock()
        let now = CMClockGetTime(hostClock)
        var timing = CMSampleTimingInfo(
            duration: CMTime(value: 1, timescale: CMTimeScale(source.fps),
                             flags: [], epoch: 0),
            presentationTimeStamp: now,
            decodeTimeStamp: .invalid)
        var sampleBuffer: CMSampleBuffer?
        guard CMSampleBufferCreateForImageBuffer(
            allocator: kCFAllocatorDefault, imageBuffer: buffer,
            dataReady: true, makeDataReadyCallback: nil, refcon: nil,
            formatDescription: format, sampleTiming: &timing,
            sampleBufferOut: &sampleBuffer) == noErr,
            let synthesized = sampleBuffer else {
            publish("合成帧 CMSampleBuffer 创建失败")
            return
        }
        captureOutput(nil, didOutput: synthesized, from: nil)
    }

    private func startCapture(
        device: AVCaptureDevice,
        completion: (@Sendable (Bool) -> Void)? = nil
    ) {
        do {
            let captureSession = AVCaptureSession()
            captureSession.beginConfiguration()
            captureSession.sessionPreset = .hd1280x720
            let input = try AVCaptureDeviceInput(device: device)
            guard captureSession.canAddInput(input) else {
                throw NSError(domain: "CameraHelper", code: 1,
                              userInfo: [NSLocalizedDescriptionKey: "无法添加摄像头输入"])
            }
            captureSession.addInput(input)

            let output = AVCaptureVideoDataOutput()
            output.alwaysDiscardsLateVideoFrames = true
            output.videoSettings = [
                kCVPixelBufferPixelFormatTypeKey as String:
                    Int(kCVPixelFormatType_32BGRA)
            ]
            output.setSampleBufferDelegate(self, queue: captureQueue)
            guard captureSession.canAddOutput(output) else {
                throw NSError(domain: "CameraHelper", code: 2,
                              userInfo: [NSLocalizedDescriptionKey: "无法添加视频输出"])
            }
            captureSession.addOutput(output)
            // 推送帧率控制（GUI 每行可调，默认 8）在**上传端闸门**实现
            // （captureOutput 按 1/pushFPS 间隔放行）：本机 UVC 实测对
            // videoMinFrameDuration 任何设置都可能停止出帧（2026-10-13），
            // 采集层保持相机原生节奏，只控"推送"节奏。
            captureSession.commitConfiguration()

            session = captureSession
            running = true
            lastFrameAt = 0
            sourceSequence = 0
            frameWindowStart = CACurrentMediaTime()
            frameWindowCount = 0
            measuredFPS = 0
            previewSequence = 0
            previewUploadInFlight = false
            pendingPreview = nil
            previewViewerCount = 0
            sourceSession = nil
            captureSession.startRunning()
            // R3-08：普通采集重启=新来源代际+重握手（不动安装 UUID）。
            // 计算受控本机证明事实：实际采集格式/来源几何/宿主代际/二进制摘要。
            if let format = captureSession.inputs.first?.ports.first?
                .formatDescription {
                let dims = CMVideoFormatDescriptionGetDimensions(format)
                let outputWidth = Int(dims.width) > 1280 ? 1280 : Int(dims.width)
                let outputHeight = Int(dims.height) > 720 ? 720 : Int(dims.height)
                if let formatID = formatIDFromDescription(format) {
                    formatFacts = formatID
                    let canonical = canonicalGeometryJSON(
                        inputWidth: Int(dims.width), inputHeight: Int(dims.height),
                        outputWidth: outputWidth, outputHeight: outputHeight,
                        formatID: formatID)
                    let geometryObject: [String: Any] = [
                        "contract_version": "source-geometry.v1",
                        "input_size": ["width": Int(dims.width),
                                       "height": Int(dims.height)],
                        "format_id": formatID,
                        "crop": ["left": 0, "top": 0,
                                 "right": Int(dims.width), "bottom": Int(dims.height)],
                        "rotation_degrees": 0,
                        "mirror_x": false, "mirror_y": false,
                        "output_size": ["width": outputWidth,
                                        "height": outputHeight],
                        "resampler": "none", "zoom_milli": 1000,
                        "stabilization": "off",
                    ]
                    let clockID = sourceClockID()
                    let helperSha256 = helperBinaryDigest()
                    if let clockID, let helperSha256 {
                        // 会话登记的误差界限：按当前格式 PTS tick 上界 + 换算舍入
                        // 的已证上界（≤20ms）；逐帧值必须落在登记界限内。
                        handshakeFacts = HandshakeFacts(
                            geometry: geometryObject,
                            geometryID: sha256Hex(canonical),
                            formatID: formatID,
                            clockID: clockID,
                            helperSha256: helperSha256)
                    } else {
                        handshakeFacts = nil
                        v2UnavailableReason =
                            "受控本机证明缺失（host_installation_id/kern.boottime/"
                            + "二进制摘要）：v2 来源不可用"
                    }
                } else {
                    formatFacts = "unknown"
                    handshakeFacts = nil
                    v2UnavailableReason = "无法读取实际采集格式：v2 来源不可用"
                }
            } else {
                formatFacts = "unknown"
                handshakeFacts = nil
                v2UnavailableReason = "无法读取采集格式描述：v2 来源不可用"
            }
            // 登记来源会话事实（绑定实例/几何/代际/时钟/证明）；失败明确不可用。
            sourceSession = nil
            v2UnavailableReason = nil
            performHandshake(attempt: 0)
            startPreviewPolling()
            publish("推流中")
            completion?(true)
        } catch {
            running = false
            publish("启动失败：\(error.localizedDescription)")
            completion?(false)
        }
    }

    // MARK: R3-08 正式 v2 握手：binding-v2 → current → capture-sessions（Bearer）

    private func performHandshake(attempt: Int) {
        guard let credential = uploader.credential else {
            v2Unavailable("机器凭据未配置（CAMERA_CONTROL_TOKEN_FILE）："
                + "v2 来源不可用，不退回 legacy")
            return
        }
        guard let facts = handshakeFacts else {
            v2Unavailable(v2UnavailableReason ?? "受控证明事实缺失：v2 不可用")
            return
        }
        uploader.fetchBindingV2(cameraID: cameraID,
                                credential: credential) { [weak self] result in
            guard let self else { return }
            self.captureQueue.async {
                guard self.running else { return }
                switch result {
                case .failure(let failure) where failure.statusCode == 403:
                    self.v2Unavailable("认证失败（\(failure.code ?? "403")）："
                        + "v2 不可用，不退回 legacy")
                case .failure(let failure):
                    self.v2Unavailable("读取绑定实例失败：\(failure.message)")
                case .success(let binding):
                    guard binding.deviceUniqueID == self.boundUniqueID else {
                        self.v2Unavailable("帧桥绑定实例与本机物理摄像头不一致："
                            + "请在 Helper 重新保存绑定（旧绑定不冒充 v2）")
                        return
                    }
                    self.fetchCurrentThenCreateSession(
                        credential: credential, bindingID: binding.bindingID,
                        facts: facts, attempt: attempt)
                }
            }
        }
    }

    private func fetchCurrentThenCreateSession(
        credential: MachineCredential,
        bindingID: String,
        facts: HandshakeFacts,
        attempt: Int
    ) {
        uploader.fetchCurrentSession(cameraID: cameraID,
                                     credential: credential) { [weak self] result in
            guard let self else { return }
            self.captureQueue.async {
                guard self.running else { return }
                switch result {
                case .failure(let failure):
                    if failure.statusCode == 403 {
                        self.v2Unavailable("认证失败（\(failure.code ?? "403")）："
                            + "v2 不可用，不退回 legacy")
                    } else {
                        self.v2Unavailable("来源会话查询失败：\(failure.message)")
                    }
                case .success(let current):
                    // 普通重启：携带查询到的旧代际作为 replaces（无旧会话为 null）
                    let replaces = current.session?["source_generation_id"] as? String
                    self.performSessionCreation(
                        credential: credential,
                        serverInstanceID: current.serverInstanceID,
                        bindingID: bindingID,
                        replacesGenerationID: replaces,
                        facts: facts,
                        attempt: attempt)
                }
            }
        }
    }

    private func performSessionCreation(
        credential: MachineCredential,
        serverInstanceID: String,
        bindingID: String,
        replacesGenerationID: String?,
        facts: HandshakeFacts,
        attempt: Int
    ) {
        // 每次启动生成新 client_instance_id（§4）；重握手不换安装 UUID
        let clientInstanceID = "helper-" + UUID().uuidString
        var geometryBody = facts.geometry
        geometryBody["source_geometry_id"] = facts.geometryID
        uploader.createCaptureSession(
            cameraID: cameraID,
            credential: credential,
            serverInstanceID: serverInstanceID,
            clientInstanceID: clientInstanceID,
            bindingID: bindingID,
            geometry: geometryBody,
            clockID: facts.clockID,
            formatID: facts.formatID,
            helperSha256: facts.helperSha256,
            captureUncertaintyMs: maxCaptureUncertaintyMs,
            replacesGenerationID: replacesGenerationID) { [weak self] result in
            guard let self else { return }
            self.captureQueue.async {
                guard self.running else { return }
                switch result {
                case .success(let session):
                    self.sourceSession = session
                    self.v2UnavailableReason = nil
                    self.publish("v2 来源已握手：代际 "
                        + session.sourceGenerationId.prefix(8) + "…")
                case .failure(let failure)
                    where failure.statusCode == 409
                        && failure.code == "source_session_expired":
                    // 服务重启：重新查询 current 并以新 client_instance_id 重握手
                    if attempt < 3 {
                        self.performHandshake(attempt: attempt + 1)
                    } else {
                        self.v2Unavailable("会话创建持续冲突：v2 不可用")
                    }
                case .failure(let failure) where failure.statusCode == 403:
                    self.v2Unavailable("认证失败（\(failure.code ?? "403")）："
                        + "v2 不可用，不退回 legacy")
                case .failure(let failure) where failure.statusCode == 409:
                    self.v2Unavailable("会话冲突（\(failure.code ?? "409")）："
                        + "先停止旧采集或重新绑定后重试")
                case .failure(let failure):
                    self.v2Unavailable("会话创建失败：\(failure.message)")
                }
            }
        }
    }

    /// R3-08⑤：明确不可用状态——不携带 v2 会话的帧一律不上传（不冒充 v2）。
    private func v2Unavailable(_ reason: String) {
        sourceSession = nil
        v2UnavailableReason = reason
        let now = CACurrentMediaTime()
        if now - lastUnavailableNoticeAt >= 2.0 {
            lastUnavailableNoticeAt = now
            publish("v2 不可用：\(reason)")
        }
    }

    func captureOutput(
        _ output: AVCaptureOutput?,
        didOutput sampleBuffer: CMSampleBuffer,
        from connection: AVCaptureConnection?
    ) {
        guard running else { return }
        let now = CACurrentMediaTime()
        let shouldUploadOCR = !uploadInFlight && now - lastFrameAt >= 1.0 / max(pushFPS, 0.1)
        let shouldUploadPreview = previewViewerCount > 0
        guard shouldUploadOCR || shouldUploadPreview else { return }
        guard let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        // 设备身份：AVFoundation 物理相机或合成采集源（R4-07-D 同一生产路径）
        guard let deviceUniqueID = device?.uniqueID ?? syntheticSource?.uniqueID
        else { return }

        let image = CIImage(cvPixelBuffer: pixelBuffer)
        // Some UVC cameras ignore the AVCapture session preset. Normalize the
        // uploaded frame so it matches the calibrated 1280x720 coordinate space.
        let targetWidth: CGFloat = 1280
        let targetHeight: CGFloat = 720
        let sourceWidth = image.extent.width
        let sourceHeight = image.extent.height
        let normalizedImage: CIImage
        if sourceWidth > 0, sourceHeight > 0,
           sourceWidth != targetWidth || sourceHeight != targetHeight {
            normalizedImage = image.transformed(by: CGAffineTransform(
                scaleX: targetWidth / sourceWidth,
                y: targetHeight / sourceHeight
            ))
        } else {
            normalizedImage = image
        }
        // Keep OCR samples at the existing quality. Preview-only frames use a
        // lighter JPEG so an operator gets a fluid local view without adding
        // the same encoding cost to every sample.
        let compressionQuality: CGFloat = shouldUploadOCR ? 0.85 : 0.70
        let options: [CIImageRepresentationOption: Any] = [
            kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: compressionQuality
        ]
        guard let jpeg = encodeContext.jpegRepresentation(
            of: normalizedImage,
            colorSpace: CGColorSpaceCreateDeviceRGB(),
            options: options
        ), jpeg.count <= 8 * 1024 * 1024 else {
            publish("JPEG 编码失败或帧过大")
            return
        }

        if shouldUploadPreview {
            previewSequence += 1
            enqueuePreview(jpeg, sequence: previewSequence, deviceUniqueID: deviceUniqueID)
        }
        let frameWidth = Int(targetWidth)
        let frameHeight = Int(targetHeight)
        guard shouldUploadOCR else { return }
        // R3-08⑤：正式客户端必须持有效 v2 会话上传；无会话（未配置/认证失败/
        // 握手失效）= 明确不可用——丢弃帧，绝不退回 legacy 冒充 v2。
        guard let sessionInfo = sourceSession else {
            if let reason = v2UnavailableReason {
                v2Unavailable(reason)  // 节流后重发状态
            } else {
                v2Unavailable("来源会话未登记：v2 不可用（等待重握手）")
            }
            return
        }

        lastFrameAt = now
        sourceSequence += 1
        frameWindowCount += 1
        let elapsed = now - frameWindowStart
        if elapsed >= 1.0 {
            measuredFPS = Double(frameWindowCount) / elapsed
            frameWindowStart = now
            frameWindowCount = 0
        }
        uploadInFlight = true
        let sequence = sourceSequence
        // §5/§6（R2-08）：在采集回调现场冻结不可变来源帧包。采样时刻只从
        // 本帧 PTS 经 CMSyncConvertTime 归一 host clock 产生（host clock 与
        // mach_absolute_time 同域，可与 Fleet time.monotonic_ns 直接比较）；
        // 回调到达时刻/编码/上传时间只作诊断，不得冒充采样时间。
        let pts = CMSampleBufferGetPresentationTimeStamp(sampleBuffer)
        let hostClock = CMClockGetHostTimeClock()
        let hostTime = CMSyncConvertTime(pts, from: hostClock, to: hostClock)
        let sampledMonotonicNanos = machTimebaseNanos(
            CMClockConvertHostTimeToSystemUnits(hostTime))
        // 采样时刻的 UTC 投影：用同一回调内取得的 (墙钟, 单调钟) 配对，把
        // PTS 采样时刻投影到 UTC；上传现场不再另取 Date()。
        let arrivalMonotonicNanos = monotonicNanos()
        let sampledAtDate = Date().addingTimeInterval(
            TimeInterval(sampledMonotonicNanos - arrivalMonotonicNanos) / 1_000_000_000.0)
        // 采样不确定度按本帧实测换算链计算（非固定常量）：
        // PTS tick 上界（ceil(1e9/pts.timescale) 纳秒，来源=当前捕获格式时间戳）
        // + host→mach 整数换算舍入（≤1ns），向上取整到毫秒。
        let ptsTimescale = Int64(max(1, pts.timescale))
        let ptsTickBoundNanos = (1_000_000_000 + ptsTimescale - 1) / ptsTimescale
        let captureUncertaintyMs = Int(
            (ptsTickBoundNanos + 1 + 999_999) / 1_000_000)
        let frozenPacket = SourceFramePacket(
            jpeg: jpeg,
            sourceSequence: sequence,
            ptsHostNanos: sampledMonotonicNanos,
            capturedMonotonicNanos: sampledMonotonicNanos,
            capturedAtISO: ISO8601DateFormatter().string(from: sampledAtDate),
            // 证明标记 camera_pts_host_clock.v1 随头 X-Source-Timing-Provenance
            // 上传；PTS 证明与 capture_registration 的格式误差登记见
            // verify_camera_clock.py（clock_registration）。
            captureUncertaintyMs: captureUncertaintyMs,
            width: frameWidth,
            height: frameHeight,
            formatFacts: formatFacts)
        uploader.upload(
            cameraID: cameraID,
            deviceUniqueID: deviceUniqueID,
            packet: frozenPacket,
            sourceSession: sessionInfo
        ) { [weak self] result in
            guard let self else { return }
            self.captureQueue.async {
                self.uploadInFlight = false
                switch result {
                case .success:
                    if sequence % 10 == 0 {
                        let uploadedAt = frozenPacket.capturedAtISO.suffix(9)
                        self.publish(String(
                            format: "推流中 %dx%d · %.1ffps · %@",
                            frozenPacket.width, frozenPacket.height, self.measuredFPS, String(uploadedAt)
                        ))
                        self.publishPreview(frozenPacket.jpeg)
                    }
                case .failure(let error):
                    // R3-08④：换绑/代际被撤 → 会话失效并触发重握手；认证失败
                    // → 明确不可用（不退回 legacy 冒充 v2）。
                    if error.statusCode == 403 {
                        self.v2Unavailable("认证失败（\(error.code ?? "403")）："
                            + "v2 不可用，不退回 legacy")
                    } else if error.statusCode == 409
                                && (error.code == "source_generation_rejected"
                                    || error.code == "source_binding_changed"
                                    || error.code == "source_geometry_changed") {
                        self.sourceSession = nil
                        self.v2Unavailable("来源身份失效（\(error.code ?? "409")）："
                            + "触发重握手")
                        self.performHandshake(attempt: 0)
                    } else if error.statusCode == 409
                                && error.code == "source_session_conflict" {
                        self.sourceSession = nil
                        self.performHandshake(attempt: 0)
                    } else {
                        self.publish("上传失败：\(error.message)")
                    }
                }
            }
        }
    }

    private func startPreviewPolling() {
        previewPollTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: captureQueue)
        timer.schedule(deadline: .now(), repeating: .seconds(1))
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            self.uploader.previewViewerCount(cameraID: self.cameraID) { result in
                self.captureQueue.async {
                    switch result {
                    case .success(let count):
                        self.previewViewerCount = count
                    case .failure:
                        self.previewViewerCount = 0
                    }
                }
            }
        }
        previewPollTimer = timer
        timer.resume()
    }

    private func enqueuePreview(_ jpeg: Data, sequence: UInt64, deviceUniqueID: String) {
        guard previewViewerCount > 0 else { return }
        if previewUploadInFlight {
            pendingPreview = (jpeg, sequence)
            return
        }
        previewUploadInFlight = true
        sendPreview(jpeg, sequence: sequence, deviceUniqueID: deviceUniqueID)
    }

    private func sendPreview(_ jpeg: Data, sequence: UInt64, deviceUniqueID: String) {
        uploader.uploadPreview(
            cameraID: cameraID,
            deviceUniqueID: deviceUniqueID,
            sourceSequence: sequence,
            jpeg: jpeg,
            width: 1280,
            height: 720
        ) { [weak self] result in
            guard let self else { return }
            self.captureQueue.async {
                self.previewUploadInFlight = false
                if case .failure(let error) = result {
                    self.publish("高帧率预览上传失败：\(error.localizedDescription)")
                }
                guard self.previewViewerCount > 0, let pending = self.pendingPreview else {
                    self.pendingPreview = nil
                    return
                }
                self.pendingPreview = nil
                self.previewUploadInFlight = true
                self.sendPreview(pending.jpeg, sequence: pending.sequence, deviceUniqueID: deviceUniqueID)
            }
        }
    }

    private func publish(_ text: String) {
        let handler = onStatus
        DispatchQueue.main.async {
            MainActor.assumeIsolated {
                handler?(text)
            }
        }
    }

    private func publishPreview(_ jpeg: Data) {
        let handler = onPreview
        DispatchQueue.main.async {
            MainActor.assumeIsolated {
                handler?(jpeg)
            }
        }
    }
}

@MainActor
private final class ChannelRow {
    let cameraID: String
    let channel: CameraChannel
    let devicePopup = NSPopUpButton()
    let statusLabel = NSTextField(labelWithString: "未选择")
    let previewView = NSImageView()
    let saveButton = NSButton(title: "保存绑定", target: nil, action: nil)
    let toggleButton = NSButton(title: "启动", target: nil, action: nil)
    let fpsLabel = NSTextField(labelWithString: "帧率")
    let fpsField = NSTextField(string: "8")
    let view = NSStackView()

    private var devices: [AVCaptureDevice] = []
    private var running = false

    init(cameraID: String, channel: CameraChannel) {
        self.cameraID = cameraID
        self.channel = channel

        let idLabel = NSTextField(labelWithString: cameraID)
        idLabel.font = .monospacedSystemFont(ofSize: 13, weight: .semibold)
        idLabel.widthAnchor.constraint(equalToConstant: 72).isActive = true

        devicePopup.widthAnchor.constraint(equalToConstant: 270).isActive = true
        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.widthAnchor.constraint(equalToConstant: 300).isActive = true
        previewView.imageScaling = .scaleProportionallyUpOrDown
        previewView.wantsLayer = true
        previewView.layer?.backgroundColor = NSColor.black.cgColor
        previewView.widthAnchor.constraint(equalToConstant: 112).isActive = true
        previewView.heightAnchor.constraint(equalToConstant: 63).isActive = true
        saveButton.bezelStyle = .rounded
        saveButton.target = self
        saveButton.action = #selector(saveBinding)
        toggleButton.bezelStyle = .rounded
        toggleButton.target = self
        toggleButton.action = #selector(toggle)
        fpsLabel.font = .systemFont(ofSize: 12)
        fpsField.font = .monospacedDigitSystemFont(ofSize: 13, weight: .regular)
        fpsField.toolTip = "推送帧率（fps，1-30，默认 8）；启动时生效"
        fpsField.widthAnchor.constraint(equalToConstant: 48).isActive = true

        view.orientation = .horizontal
        view.alignment = .centerY
        view.spacing = 10
        view.addArrangedSubview(idLabel)
        view.addArrangedSubview(devicePopup)
        view.addArrangedSubview(previewView)
        view.addArrangedSubview(statusLabel)
        view.addArrangedSubview(fpsLabel)
        view.addArrangedSubview(fpsField)
        view.addArrangedSubview(saveButton)
        view.addArrangedSubview(toggleButton)

        devicePopup.target = self
        devicePopup.action = #selector(selectionChanged)
        channel.onStatus = { [weak self] text in self?.statusLabel.stringValue = text }
        channel.onPreview = { [weak self] data in self?.previewView.image = NSImage(data: data) }
    }

    func updateDevices(_ devices: [AVCaptureDevice], boundUniqueID: String?) {
        guard !running else { return }
        let previousID = boundUniqueID ?? selectedDevice()?.uniqueID
        self.devices = devices
        devicePopup.removeAllItems()
        devicePopup.addItem(withTitle: "未选择")
        devices.forEach {
            devicePopup.addItem(withTitle: "\($0.localizedName) · \($0.uniqueID.suffix(8))")
        }
        if let previousID, let index = devices.firstIndex(where: { $0.uniqueID == previousID }) {
            devicePopup.selectItem(at: index + 1)
        }
        channel.restoreBinding(uniqueID: boundUniqueID)
        selectionChanged()
        syncControls()
    }

    func selectedDevice() -> AVCaptureDevice? {
        let index = devicePopup.indexOfSelectedItem - 1
        return devices.indices.contains(index) ? devices[index] : nil
    }

    func start() {
        guard !running else { return }
        guard let fps = Double(fpsField.stringValue.trimmingCharacters(in: .whitespaces)),
              channel.setPushFPS(fps) else {
            statusLabel.stringValue = "帧率无效：请输入 1-30 的数字（默认 8）"
            return
        }
        channel.select(device: selectedDevice())
        guard channel.start(completion: { [weak self] started in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.running = started
                    self.devicePopup.isEnabled = !started
                    self.saveButton.isEnabled = !started
                    self.fpsField.isEnabled = !started
                    self.toggleButton.title = started ? "停止" : "启动"
                    self.syncControls()
                }
            }
        }) else { return }
        toggleButton.isEnabled = false
        statusLabel.stringValue = "正在启动…"
    }

    func stop() {
        guard running else { return }
        running = false
        channel.stop()
        devicePopup.isEnabled = true
        saveButton.isEnabled = true
        fpsField.isEnabled = true
        toggleButton.title = "启动"
        syncControls()
    }

    /// 外部事件（如摄像头拔出）写入状态行的中文说明。
    func note(_ text: String) {
        statusLabel.stringValue = text
    }

    @objc private func toggle() {
        running ? stop() : start()
    }

    @objc private func saveBinding() {
        channel.saveBinding { [weak self] saved in
            guard saved else { return }
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    self?.syncControls()
                }
            }
        }
    }

    @objc private func selectionChanged() {
        statusLabel.toolTip = selectedDevice()?.uniqueID
        let device = selectedDevice()
        channel.select(device: device)
        syncControls()
    }

    private func syncControls() {
        guard !running else { return }
        let device = selectedDevice()
        toggleButton.isEnabled = channel.isBound(to: device)
        if device == nil {
            statusLabel.stringValue = channel.hasBinding()
                ? "已绑定的物理摄像头当前不可用"
                : "未选择摄像头"
        } else if channel.isBound(to: device) {
            statusLabel.stringValue = "绑定已恢复，等待启动"
        } else {
            statusLabel.stringValue = "已选择，尚未保存绑定"
        }
    }
}

@MainActor
private final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private let uploader = FrameUploader()
    private var rows: [ChannelRow] = []
    private let authorizationLabel = NSTextField(labelWithString: "正在检查摄像头权限…")
    private let rowsStack = NSStackView()

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildWindow()
        requestAuthorization()
        observeDeviceDisconnection()
    }

    /// 摄像头拔出监听：只影响对应通道，其余通道继续采集；
    /// 拔出后该通道停止并给出中文处理建议，重新插拔后需人工重新启动。
    private func observeDeviceDisconnection() {
        NotificationCenter.default.addObserver(
            forName: .AVCaptureDeviceWasDisconnected,
            object: nil,
            queue: .main
        ) { [weak self] notification in
            guard let self,
                  let device = notification.object as? AVCaptureDevice else { return }
            MainActor.assumeIsolated {
                for row in self.rows {
                    guard row.channel.isBound(to: device)
                            || row.channel.device?.uniqueID == device.uniqueID else { continue }
                    row.stop()
                    row.note("已绑定的物理摄像头已拔出：该通道已停止，请重新插拔后刷新并重新启动；其他通道不受影响")
                }
            }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        rows.forEach { $0.stop() }
    }

    private func buildWindow() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1160, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Arm Fleet CameraHelper"
        window.center()

        let root = NSStackView()
        root.orientation = .vertical
        root.alignment = .leading
        root.spacing = 12
        root.edgeInsets = NSEdgeInsets(top: 18, left: 18, bottom: 18, right: 18)
        root.translatesAutoresizingMaskIntoConstraints = false

        let title = NSTextField(labelWithString: "Arm Fleet 多摄像头辅助程序")
        title.font = .systemFont(ofSize: 20, weight: .semibold)
        root.addArrangedSubview(title)
        root.addArrangedSubview(authorizationLabel)

        let toolbar = NSStackView()
        toolbar.orientation = .horizontal
        toolbar.spacing = 8
        let refresh = NSButton(title: "刷新摄像头", target: self, action: #selector(refreshDevices))
        let startAll = NSButton(title: "启动全部已绑定通道", target: self, action: #selector(startAll))
        let stopAll = NSButton(title: "停止全部", target: self, action: #selector(stopAll))
        toolbar.addArrangedSubview(refresh)
        toolbar.addArrangedSubview(startAll)
        toolbar.addArrangedSubview(stopAll)
        root.addArrangedSubview(toolbar)

        rowsStack.orientation = .vertical
        rowsStack.alignment = .leading
        rowsStack.spacing = 8
        for cameraID in cameraIDs {
            let channel = CameraChannel(cameraID: cameraID, uploader: uploader)
            let row = ChannelRow(cameraID: cameraID, channel: channel)
            rows.append(row)
            rowsStack.addArrangedSubview(row.view)
        }

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.borderType = .bezelBorder
        scroll.documentView = rowsStack
        scroll.translatesAutoresizingMaskIntoConstraints = false
        rowsStack.translatesAutoresizingMaskIntoConstraints = false
        rowsStack.widthAnchor.constraint(greaterThanOrEqualToConstant: 990).isActive = true
        root.addArrangedSubview(scroll)
        scroll.widthAnchor.constraint(equalTo: root.widthAnchor).isActive = true
        scroll.heightAnchor.constraint(greaterThanOrEqualToConstant: 480).isActive = true

        let content = NSView()
        content.addSubview(root)
        NSLayoutConstraint.activate([
            root.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            root.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            root.topAnchor.constraint(equalTo: content.topAnchor),
            root.bottomAnchor.constraint(equalTo: content.bottomAnchor)
        ])
        window.contentView = content
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func requestAuthorization() {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            authorizationLabel.stringValue = "摄像头权限：已授权"
            refreshDevices()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                DispatchQueue.main.async {
                    self?.authorizationLabel.stringValue =
                        granted ? "摄像头权限：已授权" : "摄像头权限：已拒绝，请在系统设置中授权"
                    if granted { self?.refreshDevices() }
                }
            }
        case .denied, .restricted:
            authorizationLabel.stringValue = "摄像头权限：未授权，请在系统设置的隐私与安全性中允许 CameraHelper"
        @unknown default:
            authorizationLabel.stringValue = "摄像头权限：未知状态"
        }
    }

    @objc private func refreshDevices() {
        authorizationLabel.stringValue = "正在读取摄像头绑定…"
        uploader.fetchBindings { [weak self] result in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    switch result {
                    case .success(let bindings):
                        self.applyDeviceRefresh(bindings: bindings)
                    case .failure(let error):
                        self.authorizationLabel.stringValue =
                            "帧服务不可用：\(error.localizedDescription)"
                        // Keep the last known bindings during a transient
                        // service failure; clearing them makes every row look
                        // unbound and disables the start action.
                    }
                }
            }
        }
    }

    private func applyDeviceRefresh(bindings: [String: String]) {
        let discovery = AVCaptureDevice.DiscoverySession(
            deviceTypes: [.externalUnknown, .builtInWideAngleCamera],
            mediaType: .video,
            position: .unspecified
        )
        let devices = discovery.devices.sorted {
            if $0.localizedName == $1.localizedName { return $0.uniqueID < $1.uniqueID }
            return $0.localizedName < $1.localizedName
        }
        authorizationLabel.stringValue = devices.isEmpty
            ? "摄像头权限：已授权，但未发现视频设备"
            : "摄像头权限：已授权 · 发现 \(devices.count) 台设备"
        for row in rows {
            row.updateDevices(devices, boundUniqueID: bindings[row.cameraID])
        }
    }

    @objc private func startAll() {
        var used = Set<String>()
        for row in rows {
            guard let device = row.selectedDevice() else { continue }
            if used.contains(device.uniqueID) {
                row.statusLabel.stringValue = "该物理摄像头已被其他通道选择"
                continue
            }
            used.insert(device.uniqueID)
            row.start()
        }
    }

    @objc private func stopAll() {
        rows.forEach { $0.stop() }
    }
}

// --clock-proof（§6 受控核验入口）：Fleet 的 tools/verify_camera_clock.py 在本机
// 启动该子命令，以 Fleet 前后 monotonic_ns 包住此处输出的 host tick ns 核对
// （连续 20 次全部位于包络内且往返 ≤20ms）。只输出一行 JSON，不启动 GUI/相机。
if CommandLine.arguments.contains("--clock-proof") {
    let payload: [String: Any] = [
        "host_tick_ns": hostTickNanos(),
        "monotonic_ns": monotonicNanos(),
    ]
    if let data = try? JSONSerialization.data(withJSONObject: payload),
       let line = String(data: data, encoding: .utf8) {
        print(line)
    }
    exit(0)
}

// --selftest-stream <cameraID> [--selftest-seconds N]（R4-07-D 受控测试缝）：
// 独立客户端进程运行真实生产握手逻辑（binding-v2 → current → capture-sessions
// → 帧上传冻结包/v2 身份头/失败重握手），仅以合成采集源替换物理相机回调
// （不启动 GUI/AVFoundation；硬件时钟与真实相机效果仍属现场待验）。
if let selftestIndex = CommandLine.arguments.firstIndex(of: "--selftest-stream"),
   selftestIndex + 1 < CommandLine.arguments.count {
    let cameraID = CommandLine.arguments[selftestIndex + 1]
    let seconds: Double
    if let secondsIndex = CommandLine.arguments.firstIndex(of: "--selftest-seconds"),
       secondsIndex + 1 < CommandLine.arguments.count,
       let parsed = Double(CommandLine.arguments[secondsIndex + 1]) {
        seconds = max(1.0, parsed)
    } else {
        seconds = 8.0
    }
    guard let credential = MachineCredential.load() else {
        FileHandle.standardError.write(("机器凭据未配置（CAMERA_CONTROL_TOKEN_FILE）："
            + "selftest 不可运行\n").data(using: .utf8)!)
        exit(2)
    }
    let source = SyntheticSourceDescriptor.environment
    let uploader = FrameUploader(credential: credential)
    let channel = CameraChannel(cameraID: cameraID, uploader: uploader)
    channel.syntheticSource = source
    channel.restoreBinding(uniqueID: source.uniqueID)
    // selftest 可诊断性：状态流写 stderr（生产 GUI 路径不受影响）
    channel.onStatus = { text in
        FileHandle.standardError.write(("selftest: " + text + "\n")
            .data(using: .utf8)!)
    }
    uploader.bindV2(cameraID: cameraID,
                    uniqueID: source.uniqueID,
                    localizedName: source.localizedName,
                    credential: credential) { result in
        switch result {
        case .failure(let error):
            FileHandle.standardError.write("selftest 绑定失败：\(error.message)\n"
                .data(using: .utf8)!)
            exit(3)
        case .success(let bindingID):
            channel.captureQueue.async {
                channel.boundUniqueID = source.uniqueID
                _ = channel.start { started in
                    guard started else {
                        FileHandle.standardError.write(
                            "selftest 采集启动失败\n".data(using: .utf8)!)
                        exit(4)
                    }
                    print("selftest-bound:\(bindingID)")
                    fflush(stdout)
                    // 采集队列不得被等待阻塞（握手/推流回调都在该队列上）：
                    // 在独立线程等待 selftest 时限后停止并退出。
                    Thread.detachNewThread {
                        Thread.sleep(forTimeInterval: seconds)
                        channel.stop()
                        Thread.sleep(forTimeInterval: 0.8)
                        exit(0)
                    }
                }
            }
        }
    }
    dispatchMain()
}

MainActor.assumeIsolated {
    let application = NSApplication.shared
    let delegate = AppDelegate()
    application.delegate = delegate
    application.setActivationPolicy(.regular)
    application.run()
}
