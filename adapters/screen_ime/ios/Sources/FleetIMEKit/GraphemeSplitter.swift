// 图元分割：移植自 K40InputMethod GraphemeSplitter.kt。
// Swift 的 String 原生按用户感知图元（Grapheme Cluster）迭代，与 Kotlin 版
// 手写的合并规则（ZWJ 序列、区域指示符、肤色修饰、变体选择符）语义等价，
// 因此 typedCharacterCount 在 Android 与 iOS 客户端之间保持一致。

import Foundation

public enum GraphemeSplitter {
    /// 把文本拆成"每次输入一个单位"的图元数组（如 👨‍👩‍👧 计为 1）。
    public static func split(_ text: String) -> [String] {
        text.map { String($0) }
    }

    /// 图元数量（服务端 typedCharacterCount 的客户端口径）。
    public static func count(_ text: String) -> Int {
        text.count
    }
}
