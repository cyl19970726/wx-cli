#!/bin/zsh
set -euo pipefail
cd "${0:A:h}"
repo_root="${0:A:h:h:h:h}"
build_root="${WX_BRIDGE_BUILD_DIR:-$repo_root/.local/bridge-build}"
mkdir -p "$build_root"
xcrun swiftc -parse-as-library -swift-version 5 -framework AppKit -framework CoreImage \
  VisualParameters.swift MediaSupport.swift ClipboardSupport.swift parameter_tests.swift -o "$build_root/parameter-tests"
"$build_root/parameter-tests"
