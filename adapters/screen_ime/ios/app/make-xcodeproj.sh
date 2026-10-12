#!/bin/zsh
# 生成 FleetIME.xcodeproj（需要 XcodeGen：brew install xcodegen）
# 生成后：open FleetIME.xcodeproj → 两个 target 各选 Team → Run 到手机
set -e
cd "$(dirname "$0")"
if ! command -v xcodegen >/dev/null; then
  echo "未安装 xcodegen。安装：brew install xcodegen"
  echo "没有 brew 的机器请按 app/INSTALL.md 的手工步骤在 Xcode 里建工程（约 10 分钟）。"
  exit 1
fi
xcodegen generate
echo "已生成 FleetIME.xcodeproj，打开后："
echo "  1. 选中 FleetIMEApp target → Signing & Capabilities → Team 选你的 Apple ID"
echo "  2. 同样给 FleetIMEKeyboardExtension 选 Team"
echo "  3. iPhone 连接（首次需在手机上信任开发者：设置→通用→VPN与设备管理）"
echo "  4. Run"
