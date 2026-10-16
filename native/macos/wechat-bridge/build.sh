#!/bin/zsh
set -euo pipefail
script_dir="${0:A:h}"
repo_root="${script_dir:h:h:h}"
cd "$script_dir"
build_root="${WX_BRIDGE_BUILD_DIR:-$repo_root/.local/bridge-build}"
app="$build_root/WeChat Test Bridge.app"
mkdir -p "$app/Contents/MacOS"
cp Info.plist "$app/Contents/Info.plist"
xcrun swiftc -parse-as-library -swift-version 5 -target arm64-apple-macos14.0 -O \
  -framework AppKit -framework ApplicationServices -framework CoreGraphics \
  -framework ScreenCaptureKit -framework Vision \
  main.swift VisualParameters.swift ClipboardSupport.swift -o "$app/Contents/MacOS/WeChatTestBridge"
codesign --force --sign - --identifier io.codex.wechat-test-bridge "$app"
codesign --verify --strict --verbose=1 "$app"
