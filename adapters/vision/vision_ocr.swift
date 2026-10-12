// Apple Vision 本地 OCR 辅助程序（实施文档 5.2.1）。
//
// 约定：
//   用法：vision_ocr <region> <request_id>，PNG 字节从 stdin 读入；
//   输出：stdout 打印一条 JSON（词级坐标为像素、左上原点）；
//   失败：打印 {"ok":false,"error":...} 并以非 0 退出，绝不把失败伪装成"没有文字"。
//
// 由 adapters/wechat/ocr.py 负责编译、进程池、超时和结果校验。
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
let region = args.count > 1 ? args[1] : "unknown"
let requestId = args.count > 2 ? args[2] : ""

func fail(_ message: String, code: Int32 = 2) -> Never {
    let payload: [String: Any] = ["ok": false, "region": region, "request_id": requestId, "error": message]
    if let data = try? JSONSerialization.data(withJSONObject: payload),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    }
    exit(code)
}

let inputData = FileHandle.standardInput.readDataToEndOfFile()
if inputData.isEmpty { fail("stdin 中没有收到图片数据") }
guard let image = NSImage(data: inputData),
      let cgImage = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    fail("图片解码失败（输入必须是 PNG）")
}

let startedAt = Date()
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = true
// 微信界面以中文为主，同时保留英文/数字识别（价格、时间线索）。
request.recognitionLanguages = ["zh-Hans", "en-US"]

let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
do {
    try handler.perform([request])
} catch {
    fail("Vision 识别失败：\(error.localizedDescription)")
}

let width = cgImage.width
let height = cgImage.height
var words: [[String: Any]] = []
var texts: [String] = []
var lowestConfidence: Double = 1.0
for observation in request.results ?? [] {
    guard let candidate = observation.topCandidates(1).first else { continue }
    let text = candidate.string
    guard !text.trimmingCharacters(in: .whitespaces).isEmpty else { continue }
    // Vision 坐标为归一化、左下原点；这里转换为像素、左上原点。
    let box = observation.boundingBox
    let x = box.origin.x * Double(width)
    let y = (1.0 - box.origin.y - box.height) * Double(height)
    let confidence = Double(candidate.confidence)
    lowestConfidence = min(lowestConfidence, confidence)
    texts.append(text)
    words.append([
        "text": text,
        "x": x,
        "y": y,
        "width": box.width * Double(width),
        "height": box.height * Double(height),
        "confidence": confidence,
    ])
}

let output: [String: Any] = [
    "ok": true,
    "region": region,
    "request_id": requestId,
    "text": texts.joined(separator: "\n"),
    "words": words,
    "confidence": words.isEmpty ? 0.0 : lowestConfidence,
    "truncated": false,
    "engine": "Apple Vision",
    "engine_version": ProcessInfo.processInfo.operatingSystemVersionString,
    "elapsed_ms": Int(Date().timeIntervalSince(startedAt) * 1000),
    "width": width,
    "height": height,
]
guard let data = try? JSONSerialization.data(withJSONObject: output),
      let text = String(data: data, encoding: .utf8) else {
    fail("识别结果 JSON 序列化失败")
}
print(text)
