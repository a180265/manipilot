// FleetIME 宿主 App：设置服务器地址与设备编号、测试连接、打开校准页、
// 展示键盘安装步骤。App 与键盘扩展各自沙盒保存配置（免 App Group，免费签名友好）。

import SwiftUI
import FleetIMEKit

@main
struct FleetIMEApp: App {
    var body: some Scene {
        WindowGroup {
            NavigationStack {
                SettingsView()
            }
        }
    }
}

struct SettingsView: View {
    @AppStorage("fleet_ime_base_url") private var baseURLText = ""
    @AppStorage("fleet_ime_device_id") private var deviceID = ""

    @State private var connectionMessage = ""
    @State private var checking = false

    var body: some View {
        Form {
            Section("服务端") {
                TextField("服务器地址，例如 http://192.168.1.10:8788",
                          text: $baseURLText)
                    .keyboardType(.URL)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
                TextField("设备编号（留空自动生成）", text: $deviceID)
                    .autocorrectionDisabled()
                    .textInputAutocapitalization(.never)
                Button {
                    checkConnection()
                } label: {
                    if checking { ProgressView() } else { Text("测试连接") }
                }
                if !connectionMessage.isEmpty {
                    Text(connectionMessage).font(.footnote).foregroundStyle(.secondary)
                }
            }

            Section("屏幕校准（机械臂标定）") {
                NavigationLink("打开校准标靶页") {
                    if let url = URL(string: baseURLText), let client = makeClient(url: url) {
                        CalibrationScreen(client: client, deviceId: effectiveDeviceID)
                    } else {
                        Text("请先填写服务器地址").padding()
                    }
                }
                Text("流程：先在电脑端创建校准会话，再打开本页，机械臂按提示依次短按屏幕三个标靶位置。页面只回报触点像素，不保存机械坐标。")
                    .font(.footnote).foregroundStyle(.secondary)
            }

            Section("键盘安装步骤") {
                Text("""
                1. 本页填好服务器地址并测试连接
                2. iPhone 设置 → 通用 → 键盘 → 键盘 → 添加新键盘 → FleetIME
                3. 点开该键盘，开启"允许完全访问"（联网需要）
                4. 在任意文本框切到 FleetIME 键盘，点键盘上的「⚙ 设置」填入同样的服务器地址
                5. 键盘收到任务后，候选栏显示文本；由机械臂点击候选栏，输入法按人工节奏逐字输入
                """).font(.footnote)
            }
        }
        .navigationTitle("Fleet 任务输入法")
    }

    private var effectiveDeviceID: String {
        if deviceID.isEmpty {
            let generated = "ios-app-" + UUID().uuidString.prefix(8)
            deviceID = generated
        }
        return deviceID
    }

    private func makeClient(url: URL) -> FleetIMEClient? {
        _ = effectiveDeviceID
        return FleetIMEClient(baseURL: url)
    }

    private func checkConnection() {
        guard let url = URL(string: baseURLText.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            connectionMessage = "地址格式不正确"
            return
        }
        checking = true
        connectionMessage = ""
        _ = effectiveDeviceID
        let client = FleetIMEClient(baseURL: url)
        Task {
            defer { checking = false }
            do {
                let status = try await client.health()
                await MainActor.run { connectionMessage = "连接成功：服务状态 \(status)" }
            } catch {
                await MainActor.run { connectionMessage = "连接失败：\(error)" }
            }
        }
    }
}

/// 校准标靶页包装（UIKit 页面桥接）。
struct CalibrationScreen: UIViewControllerRepresentable {
    let client: FleetIMEClient
    let deviceId: String

    func makeUIViewController(context: Context) -> CalibrationViewController {
        CalibrationViewController(client: client, deviceId: deviceId)
    }

    func updateUIViewController(_ controller: CalibrationViewController, context: Context) {}
}
