// 协议模型：与 Arm Fleet 屏幕输入法服务（/api/v1/*）的 JSON 字段一一对应。
// 字段名保持 camelCase 协议兼容；Android K40 输入法与 Python 服务共用同一契约。
// 状态机：pending → displayed → typing → inserted；失败 typing_failed；
// 取消 pending → cancelled、displayed/typing → cancel_requested → cancelled。

public enum TaskStatus: String, Codable, Sendable, CaseIterable {
    case pending
    case displayed
    case typing
    case inserted
    case typingFailed = "typing_failed"
    case cancelRequested = "cancel_requested"
    case cancelled

    public var isTerminal: Bool {
        switch self {
        case .inserted, .typingFailed, .cancelled: return true
        default: return false
        }
    }
}

public struct ImeTask: Codable, Sendable, Equatable {
    public var taskId: String
    public var deviceId: String
    public var targetUser: String
    public var content: String
    public var typingSpeed: String
    public var resolvedTypingSpeed: String
    public var status: String
    public var typedCharacterCount: Int
    public var failureReason: String?
    public var source: String?
    public var createdAt: String
    public var updatedAt: String

    public var statusValue: TaskStatus? { TaskStatus(rawValue: status) }

    public init(taskId: String, deviceId: String, targetUser: String, content: String,
                typingSpeed: String, resolvedTypingSpeed: String, status: String,
                typedCharacterCount: Int, failureReason: String?, source: String?,
                createdAt: String, updatedAt: String) {
        self.taskId = taskId; self.deviceId = deviceId; self.targetUser = targetUser
        self.content = content; self.typingSpeed = typingSpeed
        self.resolvedTypingSpeed = resolvedTypingSpeed; self.status = status
        self.typedCharacterCount = typedCharacterCount; self.failureReason = failureReason
        self.source = source; self.createdAt = createdAt; self.updatedAt = updatedAt
    }
}

public struct ImeDevice: Codable, Sendable, Equatable {
    public var deviceId: String
    public var appVersion: String
    public var keyboardActive: Bool
    public var currentTaskId: String?
    public var lastSeenAt: String
    public var online: Bool

    public init(deviceId: String, appVersion: String, keyboardActive: Bool,
                currentTaskId: String?, lastSeenAt: String, online: Bool) {
        self.deviceId = deviceId; self.appVersion = appVersion
        self.keyboardActive = keyboardActive; self.currentTaskId = currentTaskId
        self.lastSeenAt = lastSeenAt; self.online = online
    }
}

public struct ControlCommand: Codable, Sendable, Equatable {
    public var commandId: Int
    public var taskId: String
    public var command: String
    public var createdAt: String

    public init(commandId: Int, taskId: String, command: String, createdAt: String) {
        self.commandId = commandId; self.taskId = taskId
        self.command = command; self.createdAt = createdAt
    }
}

// ---- 屏幕校准 ----

public struct CalibrationTarget: Codable, Sendable, Equatable {
    public var u: Double
    public var v: Double
    public var widthU: Double
    public var heightV: Double
}

public struct CalibrationSample: Codable, Sendable, Equatable {
    public var sampleId: String
    public var sequence: Int
    public var screenX: Double
    public var screenY: Double
    public var screenWidth: Int
    public var screenHeight: Int
    public var orientation: String
    public var eventTime: Int
    public var receivedAt: String
    public var target: CalibrationTarget?
}


public struct ArmedSample: Codable, Sendable, Equatable {
    public var sampleId: String
    public var sequence: Int
    public var target: CalibrationTarget?
}

public struct CalibrationSession: Codable, Sendable, Equatable {
    public var sessionId: String
    public var deviceId: String
    public var status: String
    public var clientState: String
    public var createdAt: String
    public var samples: [CalibrationSample]
    public var armed: ArmedSample?
}

/// 服务端错误（{"error": <code>, "message": <message>}）。
public struct ImeAPIFailure: Error, Codable, Sendable {
    public var error: String
    public var message: String

    public init(error: String, message: String) {
        self.error = error
        self.message = message
    }
}
