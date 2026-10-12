// iOS 键盘扩展 UI：任务候选栏 + 基础 26 键 + 校准页入口 + ⚙ 设置。
// 仅 iOS 可用（UIKit/UIInputViewController）；macOS 上本文件编译为空，
// 协议逻辑全部在跨平台的 FleetIMEKit 中，macOS 用 swift build/test/selfcheck 验证。
//
// 配置边界（免 App Group 权限，兼容免费 Apple ID 签名）：
// 键盘读写自己沙盒的 UserDefaults.standard（⚙ 设置入口维护）：
//   - fleet_ime_base_url   服务地址，例如 http://192.168.1.10:8788
//   - fleet_ime_device_id  设备编号（同一台手机保持稳定）
// 编译期兜底：Info.plist 的 fleet_ime_default_server_url。

#if canImport(UIKit)
import UIKit
import FleetIMEKit

public final class TaskKeyboardViewController: UIInputViewController {
    private var engine: KeyboardTaskEngine?
    private var heartbeat: HeartbeatCoordinator?
    private var pollTask: Task<Void, Never>?
    private var currentBaseURL: URL?

    private let candidateButton = UIButton(type: .system)

    public override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = UIColor(white: 0.92, alpha: 1)
        setupCandidateBar()
        setupKeyboardRows()
    }

    public override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        startNetworking()
    }

    public override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        heartbeat?.stop()
        pollTask?.cancel()
    }

    // MARK: - 配置

    private func loadBaseURL() -> URL? {
        let defaults = UserDefaults.standard
        if let raw = defaults.string(forKey: "fleet_ime_base_url"), let url = URL(string: raw) {
            return url
        }
        // 编译期兜底：扩展 Info.plist 的 fleet_ime_default_server_url
        if let raw = Bundle.main.object(forInfoDictionaryKey: "FleetIMEDefaultServerURL") as? String,
           let url = URL(string: raw) {
            return url
        }
        return nil
    }

    private func loadDeviceID() -> String {
        let defaults = UserDefaults.standard
        if let id = defaults.string(forKey: "fleet_ime_device_id"), !id.isEmpty {
            return id
        }
        let generated = "ios-" + (UIDevice.current.identifierForVendor?.uuidString.prefix(8) ?? "unknown")
        defaults.set(generated, forKey: "fleet_ime_device_id")
        return generated
    }

    /// ⚙ 设置：服务器地址与设备编号（键盘自己沙盒内保存，无需 App Group）。
    private func showSettings() {
        let defaults = UserDefaults.standard
        let alert = UIAlertController(title: "输入法设置",
                                      message: "服务器地址例如 http://192.168.1.10:8788",
                                      preferredStyle: .alert)
        alert.addTextField { field in
            field.placeholder = "服务器地址"
            field.text = defaults.string(forKey: "fleet_ime_base_url")
            field.keyboardType = .URL
            field.autocorrectionType = .no
            field.autocapitalizationType = .none
        }
        alert.addTextField { field in
            field.placeholder = "设备编号（留空自动生成）"
            field.text = defaults.string(forKey: "fleet_ime_device_id")
            field.autocorrectionType = .no
            field.autocapitalizationType = .none
        }
        alert.addAction(UIAlertAction(title: "保存并重连", style: .default) { [weak self] _ in
            guard let self else { return }
            let fields = alert.textFields ?? []
            let url = fields.first?.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let device = fields.last?.text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if !url.isEmpty { defaults.set(url, forKey: "fleet_ime_base_url") }
            if !device.isEmpty { defaults.set(device, forKey: "fleet_ime_device_id") }
            self.restartNetworking()
        })
        alert.addAction(UIAlertAction(title: "取消", style: .cancel))
        present(alert, animated: true)
    }

    // MARK: - 网络

    private func stopNetworking() {
        heartbeat?.stop()
        pollTask?.cancel()
        pollTask = nil
        engine = nil
        heartbeat = nil
    }

    private func restartNetworking() {
        stopNetworking()
        startNetworking()
    }

    private func startNetworking() {
        guard engine == nil else { return }
        guard let baseURL = loadBaseURL() else {
            candidateButton.setTitle("点击下方 ⚙ 设置服务器地址后开始接收任务", for: .normal)
            return
        }
        currentBaseURL = baseURL
        let deviceId = loadDeviceID()
        let client = FleetIMEClient(baseURL: baseURL)
        let engine = KeyboardTaskEngine(client: client, deviceId: deviceId,
                                        appVersion: "fleet-ime-ios-1.0.0")
        let heartbeat = HeartbeatCoordinator(client: client, deviceId: deviceId,
                                             appVersion: "fleet-ime-ios-1.0.0")
        self.engine = engine
        self.heartbeat = heartbeat
        heartbeat.start()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await engine.pollAndProcess(proxy: self.textDocumentProxy)
                await MainActor.run { self.refreshCandidate() }
            }
        }
        refreshCandidate()
    }

    private func refreshCandidate() {
        let text = engine?.candidateText()
        let deviceId = loadDeviceID()
        candidateButton.setTitle(text ?? "等待任务（\(deviceId)）", for: .normal)
        candidateButton.isEnabled = text != nil
    }

    // MARK: - 候选栏（机械臂点击目标）

    private func setupCandidateBar() {
        candidateButton.setTitle("任务输入法（等待任务）", for: .normal)
        candidateButton.titleLabel?.font = .systemFont(ofSize: 15)
        candidateButton.backgroundColor = .white
        candidateButton.layer.cornerRadius = 6
        candidateButton.addAction(UIAction { [weak self] _ in
            // 机械臂点击候选栏：放行逐字输入
            self?.engine?.confirmTap()
            self?.candidateButton.isEnabled = false
        }, for: .touchUpInside)
        view.addSubview(candidateButton)
        candidateButton.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            candidateButton.topAnchor.constraint(equalTo: view.safeAreaLayoutGuide.topAnchor, constant: 4),
            candidateButton.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 4),
            candidateButton.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -4),
            candidateButton.heightAnchor.constraint(equalToConstant: 38),
        ])
    }

    // MARK: - 基础键盘（含 ⚙ 设置）

    private func makeKey(_ title: String, widthMultiplier: CGFloat = 1) -> UIButton {
        let button = UIButton(type: .system)
        button.setTitle(title, for: .normal)
        button.backgroundColor = title == "⚙ 设置" ? UIColor(white: 0.85, alpha: 1) : .white
        button.layer.cornerRadius = 5
        button.titleLabel?.font = .systemFont(ofSize: 16)
        return button
    }

    private func setupKeyboardRows() {
        var rowsStack: [UIStackView] = []

        func letterRow(_ letters: String) -> UIStackView {
            let stack = UIStackView(arrangedSubviews: letters.map { letter in
                let button = makeKey(String(letter))
                button.addAction(UIAction { [weak self] _ in
                    self?.textDocumentProxy.insertText(String(letter))
                }, for: .touchUpInside)
                return button
            })
            stack.axis = .horizontal
            stack.distribution = .fillEqually
            stack.spacing = 5
            return stack
        }

        rowsStack.append(letterRow("qwertyuiop"))
        rowsStack.append(letterRow("asdfghjkl"))

        // 第三行：删除 + zxcvbnm
        let deleteButton = makeKey("⌫")
        deleteButton.addAction(UIAction { [weak self] _ in
            self?.textDocumentProxy.deleteBackward()
        }, for: .touchUpInside)
        let thirdRow = UIStackView(arrangedSubviews: [deleteButton] + "zxcvbnm".map { letter -> UIButton in
            let button = makeKey(String(letter))
            button.addAction(UIAction { [weak self] _ in
                self?.textDocumentProxy.insertText(String(letter))
            }, for: .touchUpInside)
            return button
        })
        thirdRow.axis = .horizontal
        thirdRow.distribution = .fillEqually
        thirdRow.spacing = 5
        rowsStack.append(thirdRow)

        // 第四行：⚙ 设置 / 空格 / 换行
        let settingsButton = makeKey("⚙ 设置")
        settingsButton.addAction(UIAction { [weak self] _ in
            self?.showSettings()
        }, for: .touchUpInside)
        let spaceButton = makeKey("空格")
        spaceButton.addAction(UIAction { [weak self] _ in
            self?.textDocumentProxy.insertText(" ")
        }, for: .touchUpInside)
        let returnButton = makeKey("换行")
        returnButton.addAction(UIAction { [weak self] _ in
            self?.textDocumentProxy.insertText("\n")
        }, for: .touchUpInside)
        let bottomRow = UIStackView(arrangedSubviews: [settingsButton, spaceButton, returnButton])
        bottomRow.axis = .horizontal
        bottomRow.distribution = .fillProportionally
        bottomRow.spacing = 5
        rowsStack.append(bottomRow)

        let container = UIStackView(arrangedSubviews: rowsStack)
        container.axis = .vertical
        container.distribution = .fillEqually
        container.spacing = 7
        view.addSubview(container)
        container.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            container.topAnchor.constraint(equalTo: candidateButton.bottomAnchor, constant: 7),
            container.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 4),
            container.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -4),
            container.bottomAnchor.constraint(equalTo: view.safeAreaLayoutGuide.bottomAnchor, constant: -4),
        ])
    }
}

/// 全屏校准页（宿主 App 内打开）：轮询布防点，把 ACTION_UP 的屏幕像素回报服务端。
/// 页面只回传屏幕像素与窗口元数据，不保存任何机械坐标（机械坐标由 Agent 配对）。
public final class CalibrationViewController: UIViewController {
    private let client: FleetIMEClient
    private let deviceId: String
    private let label = UILabel()
    private var loopTask: Task<Void, Never>?
    private var sessionId: String?
    private var armed: ArmedSample?

    public init(client: FleetIMEClient, deviceId: String) {
        self.client = client
        self.deviceId = deviceId
        super.init(nibName: nil, bundle: nil)
    }

    required init?(coder: NSCoder) { return nil }

    public override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .black
        label.text = "屏幕校准：请保持本页打开"
        label.textColor = .white
        label.textAlignment = .center
        view.addSubview(label)
        label.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: view.centerXAnchor),
            label.centerYAnchor.constraint(equalTo: view.centerYAnchor),
        ])
    }

    public override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        loopTask = Task { await run() }
    }

    public override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        loopTask?.cancel()
        if let sessionId {
            Task { _ = try? await client.reportCalibrationState(
                sessionId: sessionId, deviceId: deviceId, state: "closed") }
        }
    }

    private func run() async {
        while !Task.isCancelled {
            if let session = try? await client.fetchCurrentCalibration(deviceId: deviceId),
               let session {
                sessionId = session.sessionId
                if session.clientState != "open" {
                    _ = try? await client.reportCalibrationState(
                        sessionId: session.sessionId, deviceId: deviceId, state: "open")
                }
                if let target = session.armed {
                    armed = target
                    await MainActor.run {
                        label.text = "标靶 \(target.sequence)：等待机械臂短按屏幕任意位置"
                    }
                }
            }
            try? await Task.sleep(nanoseconds: 300_000_000)
        }
    }

    /// ACTION_UP：把触点换算成设备像素并回报（必须先有匹配布防点，否则服务端拒绝）。
    public override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        super.touchesEnded(touches, with: event)
        guard let touch = touches.first, let sessionId, let armed else { return }
        let scale = view.contentScaleFactor
        let location = touch.location(in: view)
        Task {
            _ = try? await client.reportCalibrationTouch(
                sessionId: sessionId, deviceId: deviceId,
                sampleId: armed.sampleId, sequence: armed.sequence,
                screenX: Double(location.x * scale),
                screenY: Double(location.y * scale),
                screenWidth: Int(view.bounds.width * scale),
                screenHeight: Int(view.bounds.height * scale),
                orientation: view.bounds.width > view.bounds.height ? "landscape" : "portrait",
                eventTime: Int(Date().timeIntervalSince1970 * 1000))
            await MainActor.run { label.text = "已回报触点，等待下一个标靶" }
        }
    }
}
#endif
