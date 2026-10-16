import AppKit
import ApplicationServices
import CoreGraphics
import Foundation
import ScreenCaptureKit
import Vision

// Deliberately restricted to one app and explicit, single-window visual transactions.
private let weChatBundle = "com.tencent.xinWeChat"
private let outputDirectory = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("wechat-wx-cli-test/ui", isDirectory: true)

private struct Command: Decodable {
    let id: String
    let action: String
    let text: String?
    let body: String?
    let targetGroup: String?
    let targetKind: TargetKind?
    let focusEvidenceID: String?
    let expectedDraft: String?
    let expectedPID: Int32?
    let expectedWindowID: UInt32?
    let expectedFrame: RectInput?
    let composerRect: RectInput?
    let buttonPoint: PointInput?
    let conversationListRect: RectInput?
    let searchPoint: PointInput?
}

private struct RectInput: Codable, Equatable {
    let x: Double, y: Double, width: Double, height: Double
    var rect: CGRect { CGRect(x: x, y: y, width: width, height: height) }
}

private struct PointInput: Codable, Equatable {
    let x: Double, y: Double
    var point: CGPoint { CGPoint(x: x, y: y) }
}

private struct Failure: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) { description = message }
}

private func safeID(_ value: String) -> Bool {
    !value.isEmpty && value.count <= 80 && value.range(of: "^[A-Za-z0-9_-]+$", options: .regularExpression) != nil
}


private func preparePrivateOutputDirectory() throws {
    let parent = outputDirectory.deletingLastPathComponent()
    for directory in [parent, outputDirectory] {
        var statBuffer = stat()
        if lstat(directory.path, &statBuffer) != 0 {
            guard mkdir(directory.path, 0o700) == 0 else { throw Failure("Cannot create private output directory") }
            guard lstat(directory.path, &statBuffer) == 0 else { throw Failure("Cannot verify output directory") }
        }
        guard (statBuffer.st_mode & S_IFMT) == S_IFDIR,
              statBuffer.st_uid == geteuid() else { throw Failure("Output directory must be an owned, non-symlink directory") }
        guard chmod(directory.path, 0o700) == 0 else { throw Failure("Cannot make output directory private") }
    }
}

private func save(_ object: [String: Any], to url: URL) {
    guard let data = try? JSONSerialization.data(withJSONObject: object, options: [.prettyPrinted, .sortedKeys]) else { return }
    try? data.write(to: url, options: .atomic)
}

private func readCommand() throws -> Command {
    guard CommandLine.arguments.count == 3, CommandLine.arguments[1] == "--input" else {
        throw Failure("Usage: open -g -n APP --args --input ~/wechat-wx-cli-test/ui/COMMAND.json")
    }
    let path = (CommandLine.arguments[2] as NSString).expandingTildeInPath
    let url = URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath()
    let base = outputDirectory.standardizedFileURL.resolvingSymlinksInPath().path + "/"
    guard url.path.hasPrefix(base), url.pathExtension == "json" else {
        throw Failure("Input JSON must be directly or recursively inside ~/wechat-wx-cli-test/ui/")
    }
    let command = try JSONDecoder().decode(Command.self, from: Data(contentsOf: url))
    guard safeID(command.id), ["request-permissions", "inspect", "capture", "focus-group", "prepare-visual", "submit-visual"].contains(command.action) else {
        throw Failure("Invalid id or action")
    }
    return command
}

private func axString(_ element: AXUIElement, _ attribute: String) -> String? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else { return nil }
    return value as? String
}

private func axBool(_ element: AXUIElement, _ attribute: String) -> Bool? {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else { return nil }
    return value as? Bool
}

private func axChildren(_ element: AXUIElement) -> [AXUIElement] {
    var value: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value) == .success,
          let children = value as? [AXUIElement] else { return [] }
    return Array(children.prefix(80))
}

private func axFrame(_ element: AXUIElement) -> CGRect? {
    var positionRef: CFTypeRef?
    var sizeRef: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &positionRef) == .success,
          AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &sizeRef) == .success,
          let positionRef, let sizeRef,
          CFGetTypeID(positionRef) == AXValueGetTypeID(),
          CFGetTypeID(sizeRef) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(positionRef as! AXValue, .cgPoint, &point),
          AXValueGetValue(sizeRef as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: point, size: size)
}

private struct AXNode {
    let element: AXUIElement
    let role: String
    let title: String
    let ownerWindow: AXUIElement?
}

private func traverse(_ root: AXUIElement, maxNodes: Int = 300) -> [AXNode] {
    var queue: [(AXUIElement, AXUIElement?)] = [(root, nil)]
    var nodes: [AXNode] = []
    while !queue.isEmpty && nodes.count < maxNodes {
        let (element, inheritedWindow) = queue.removeFirst()
        let role = axString(element, kAXRoleAttribute) ?? ""
        let ownerWindow = role == kAXWindowRole as String ? element : inheritedWindow
        let title = [kAXWindowRole as String, kAXButtonRole as String].contains(role)
            ? (axString(element, kAXTitleAttribute) ?? "") : ""
        let node = AXNode(element: element, role: role, title: title, ownerWindow: ownerWindow)
        nodes.append(node)
        if nodes.count < maxNodes {
            queue.append(contentsOf: axChildren(element).map { ($0, ownerWindow) })
        }
    }
    return nodes
}

private func processAndWindows() async throws -> (NSRunningApplication, [SCWindow]) {
    let apps = NSRunningApplication.runningApplications(withBundleIdentifier: weChatBundle)
    guard apps.count == 1, let app = apps.first else { throw Failure("Expected exactly one running WeChat process") }
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
    let windows = content.windows.filter {
        $0.owningApplication?.processID == app.processIdentifier &&
        // Include short search panels so the sole-window send guard sees them too.
        $0.isOnScreen && $0.frame.width >= 300 && $0.frame.height >= 160
    }
    return (app, windows)
}

private func permissionSummary() -> [String: Any] {
    ["accessibility": AXIsProcessTrusted(),
     "screenRecording": CGPreflightScreenCaptureAccess(),
     "postEvents": CGPreflightPostEventAccess()]
}

private func windowSummary(_ window: SCWindow) -> [String: Any] {
    ["id": window.windowID,
     "title": window.title ?? "",
     "frame": ["x": window.frame.origin.x, "y": window.frame.origin.y,
               "width": window.frame.width, "height": window.frame.height]]
}

private func capture(_ window: SCWindow, destination: URL,
                     minimumHeight: Int = 250) async throws -> CGImage {
    let filter = SCContentFilter(desktopIndependentWindow: window)
    let configuration = SCStreamConfiguration()
    configuration.width = max(1, Int(window.frame.width))
    configuration.height = max(1, Int(window.frame.height))
    configuration.showsCursor = false
    let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: configuration)
    guard image.width >= 300, image.height >= minimumHeight else {
        throw Failure("Window screenshot too small")
    }
    let bitmap = NSBitmapImageRep(cgImage: image)
    guard let png = bitmap.representation(using: .png, properties: [:]) else { throw Failure("Cannot encode window screenshot") }
    // A uniform/black image cannot establish the selected conversation.
    var samples = Set<UInt32>()
    for row in stride(from: 10, to: image.height - 10, by: max(1, image.height / 18)) {
        for col in stride(from: 10, to: image.width - 10, by: max(1, image.width / 18)) {
            guard let color = bitmap.colorAt(x: col, y: row)?.usingColorSpace(.deviceRGB) else { continue }
            let r = UInt32((color.redComponent * 15).rounded())
            let g = UInt32((color.greenComponent * 15).rounded())
            let b = UInt32((color.blueComponent * 15).rounded())
            samples.insert((r << 8) | (g << 4) | b)
        }
    }
    guard samples.count >= 4 else { throw Failure("Blank or uniform window screenshot") }
    try png.write(to: destination, options: .atomic)
    return image
}

private func headerContainsTarget(_ image: CGImage, target: String,
                                  kind: TargetKind) async throws -> Bool {
    guard let crop = VisualParameters.headerOCRRect(imageWidth: image.width,
            imageHeight: image.height) else {
        throw Failure("Chat header geometry is not calibrated for this window")
    }
    guard let header = image.cropping(to: crop) else { throw Failure("Cannot crop chat header") }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: header).perform([request])
    let candidates = (request.results ?? []).compactMap { result -> HeaderOCRCandidate? in
        guard let text = result.topCandidates(1).first?.string else { return nil }
        let bounds = result.boundingBox
        return HeaderOCRCandidate(text: text, bounds: CGRect(
            x: crop.minX + bounds.minX * crop.width,
            y: crop.minY + (1 - bounds.maxY) * crop.height,
            width: bounds.width * crop.width,
            height: bounds.height * crop.height))
    }
    return VisualParameters.uniqueHeaderTitle(candidates, target: target, kind: kind,
        imageWidth: image.width, imageHeight: image.height)
}

private func sessionSummary() -> [String: Any] {
    let session = CGSessionCopyCurrentDictionary() as? [String: Any]
    let locked = session?["CGSSessionScreenIsLocked"] as? Bool
    let onConsole = session?["kCGSSessionOnConsoleKey"] as? Bool
    return ["lockFlagPresent": locked != nil, "locked": locked as Any? ?? NSNull(),
            "consoleFlagPresent": onConsole != nil, "onConsole": onConsole as Any? ?? NSNull()]
}

private func inspectAX(_ app: NSRunningApplication) -> [[String: Any]] {
    let root = AXUIElementCreateApplication(app.processIdentifier)
    return traverse(root).prefix(300).map {
        ["role": $0.role, "title": ($0.role == kAXWindowRole as String || ["发送", "Send"].contains($0.title)) ? $0.title : ""]
    }
}

private struct VisualStage: Codable {
    let id: String
    var stage: String
    let text: String
    let targetGroup: String
    let targetKind: TargetKind
    let focusEvidenceID: String
    let expectedDraft: String
    let expectedPID: Int32
    let expectedWindowID: UInt32
    let expectedFrame: RectInput
    let composerRect: RectInput
    let buttonPoint: PointInput
}

private struct FocusEvidence: Codable {
    let id: String
    let targetGroup: String
    let targetKind: TargetKind
    let expectedPID: Int32
    let expectedWindowID: UInt32
    let expectedFrame: RectInput
    let source: String
    let category: SearchCategory
    let selectedAt: Date
}

private func focusEvidenceURL(_ id: String) -> URL {
    outputDirectory.appendingPathComponent("\(id).focus-evidence.json")
}

private func requireFocusEvidence(_ state: VisualStage) throws {
    let evidence = try JSONDecoder().decode(FocusEvidence.self,
        from: Data(contentsOf: focusEvidenceURL(state.focusEvidenceID)))
    let age = Date().timeIntervalSince(evidence.selectedAt)
    guard evidence.id == state.focusEvidenceID, evidence.source == "search",
          evidence.category.accepts(state.targetKind),
          evidence.targetGroup == state.targetGroup, evidence.targetKind == state.targetKind,
          evidence.expectedPID == state.expectedPID,
          evidence.expectedWindowID == state.expectedWindowID,
          evidence.expectedFrame == state.expectedFrame,
          age >= 0, age <= 120 else {
        throw Failure("Recent category-verified focus evidence does not match visual target")
    }
}

private func visualStageURL(_ id: String) -> URL {
    outputDirectory.appendingPathComponent("\(id).visual-state.json")
}

private func writeStage(_ state: VisualStage) throws {
    try JSONEncoder().encode(state).write(to: visualStageURL(state.id), options: .atomic)
}

private func exclusiveMarker(_ url: URL) throws {
    let fd = open(url.path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
    guard fd >= 0 else { throw Failure("Transaction step already attempted; refusing repeat") }
    close(fd)
}

private func visualInputs(_ command: Command) throws -> VisualStage {
    guard let text = command.text ?? command.body,
          command.text == nil || command.body == nil || command.text == command.body,
          let targetGroup = command.targetGroup,
          let targetKind = command.targetKind,
          let focusEvidenceID = command.focusEvidenceID, safeID(focusEvidenceID),
          let expectedDraft = command.expectedDraft,
          VisualParameters.acceptsText(text, allowEmpty: false),
          VisualParameters.acceptsText(expectedDraft, allowEmpty: true),
          VisualParameters.acceptsGroup(targetGroup),
          let pid = command.expectedPID,
          let windowID = command.expectedWindowID,
          let frame = command.expectedFrame,
          let composer = command.composerRect,
          let button = command.buttonPoint else {
        throw Failure("Visual action requires valid text, targetGroup, targetKind, focusEvidenceID, expectedDraft and calibrated window coordinates")
    }
    guard pid > 0, windowID > 0,
          frame.width >= 300, frame.height >= 250,
          composer.x >= frame.width * 0.32,
          composer.y >= frame.height * 0.68,
          composer.width >= 100, composer.height >= 35,
          composer.rect.maxX <= frame.width - 6,
          composer.rect.maxY <= frame.height * 0.94,
          button.x >= frame.width * 0.70,
          button.y >= frame.height * 0.88,
          button.x < frame.width - 6,
          button.y < frame.height - 6,
          button.y > composer.rect.maxY else {
        throw Failure("Visual coordinates must identify the lower-right composer and Send button inside the expected window")
    }
    return VisualStage(id: command.id, stage: "",
                       text: VisualParameters.canonicalNewlines(text), targetGroup: targetGroup,
                       targetKind: targetKind, focusEvidenceID: focusEvidenceID,
                       expectedDraft: VisualParameters.canonicalNewlines(expectedDraft),
                       expectedPID: pid, expectedWindowID: windowID,
                       expectedFrame: frame, composerRect: composer, buttonPoint: button)
}

private func sameVisualInputs(_ a: VisualStage, _ b: VisualStage) -> Bool {
    a.id == b.id &&
    VisualParameters.sameTransactionText(text: a.text, draft: a.expectedDraft, group: a.targetGroup,
                                         otherText: b.text, otherDraft: b.expectedDraft, otherGroup: b.targetGroup) &&
    a.expectedPID == b.expectedPID &&
    a.targetKind == b.targetKind && a.focusEvidenceID == b.focusEvidenceID &&
    a.expectedWindowID == b.expectedWindowID && a.expectedFrame == b.expectedFrame &&
    a.composerRect == b.composerRect && a.buttonPoint == b.buttonPoint
}

private func screenshotText(_ image: CGImage, rect: CGRect) throws -> [String] {
    guard rect.minX >= 0, rect.minY >= 0,
          rect.maxX <= CGFloat(image.width), rect.maxY <= CGFloat(image.height),
          let cropped = image.cropping(to: rect.integral) else { throw Failure("OCR crop is outside captured window") }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["en-US"]
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: cropped).perform([request])
    return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }
}

private func normalizedDraft(_ text: String) -> String {
    String(text.filter { !$0.isWhitespace })
}

private func requireDraft(_ image: CGImage, rect: CGRect, expected: String) throws {
    let firstLine = CGRect(x: rect.minX, y: rect.minY, width: rect.width, height: min(52, rect.height))
    let remainder = CGRect(x: rect.minX, y: firstLine.maxY, width: rect.width, height: rect.maxY - firstLine.maxY)
    var lines = try screenshotText(image, rect: firstLine)
        .map(normalizedDraft).filter { !$0.isEmpty }
    if remainder.height >= 10 {
        lines += try screenshotText(image, rect: remainder)
            .map(normalizedDraft).filter { !$0.isEmpty }
    }
    if expected.isEmpty {
        guard lines.isEmpty else { throw Failure("Composer is not visually empty") }
    } else {
        guard lines.count == 1, lines[0] == expected else {
            throw Failure("Composer OCR does not exactly match expected draft")
        }
    }
}

private func requireSendButton(_ image: CGImage, point: CGPoint) throws {
    let lowerPane = CGRect(x: CGFloat(image.width) * 0.30,
                           y: CGFloat(image.height) * 0.84,
                           width: CGFloat(image.width) * 0.70,
                           height: CGFloat(image.height) * 0.16).integral
    let allLabels = try screenshotText(image, rect: lowerPane).map(normalizedDraft)
    guard allLabels.filter({ $0 == "Send" }).count == 1 else {
        throw Failure("Expected exactly one visible Send label in lower conversation pane")
    }
    let crop = CGRect(x: point.x - 48, y: point.y - 23, width: 96, height: 46)
    let words = try screenshotText(image, rect: crop).map(normalizedDraft)
    guard words.count == 1, words[0] == "Send" else { throw Failure("Unique Send label not confirmed at button point") }
}

private func consoleAndFocus(_ app: NSRunningApplication) throws -> String {
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier else {
        throw Failure("WeChat is not frontmost")
    }
    let session = sessionSummary()
    guard session["onConsole"] as? Bool == true else { throw Failure("Session is not on the console") }
    guard session["locked"] as? Bool != true else { throw Failure("Console is explicitly locked") }
    return (session["lockFlagPresent"] as? Bool == true) ? "reported-unlocked" : "unknown"
}

private func freshVisual(_ state: VisualStage, step: String, draft: String?) async throws -> (SCWindow, CGImage, String) {
    try requireFocusEvidence(state)
    guard AXIsProcessTrusted(), CGPreflightScreenCaptureAccess(), CGPreflightPostEventAccess() else {
        throw Failure("Accessibility, Screen Recording and Post Events access required")
    }
    let (app, windows) = try await processAndWindows()
    guard app.processIdentifier == state.expectedPID,
          windows.count == 1, let window = windows.first,
          window.windowID == state.expectedWindowID else { throw Failure("WeChat PID or sole window changed") }
    let a = window.frame, b = state.expectedFrame.rect
    guard abs(a.minX - b.minX) <= 3, abs(a.minY - b.minY) <= 3,
          abs(a.width - b.width) <= 3, abs(a.height - b.height) <= 3 else {
        throw Failure("WeChat window frame changed")
    }
    let lockStatus = try consoleAndFocus(app)
    let image = try await capture(window, destination: outputDirectory.appendingPathComponent("\(state.id).\(step).png"))
    guard try await headerContainsTarget(image, target: state.targetGroup,
                                        kind: state.targetKind) else {
        throw Failure("Fresh chat header is not exact target and kind")
    }
    guard image.width == Int(b.width), image.height == Int(b.height) else {
        throw Failure("Screenshot pixel size differs from expected geometry")
    }
    if let draft { try requireDraft(image, rect: state.composerRect.rect, expected: draft) }
    _ = try consoleAndFocus(app)
    return (window, image, lockStatus)
}

private func globalPoint(_ point: CGPoint, window: SCWindow) -> CGPoint {
    CGPoint(x: window.frame.minX + point.x, y: window.frame.minY + point.y)
}

private func mouseClick(_ point: CGPoint) throws {
    guard let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
                             mouseCursorPosition: point, mouseButton: .left),
          let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                           mouseCursorPosition: point, mouseButton: .left) else {
        throw Failure("Could not construct mouse events")
    }
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
}

private func key(_ code: CGKeyCode, command: Bool = false) throws {
    guard let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true),
          let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false) else {
        throw Failure("Could not construct key events")
    }
    down.flags = command ? .maskCommand : []
    up.flags = command ? .maskCommand : []
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
}

private func readSelectedDraft(_ state: VisualStage, step: String, expected: String) async throws -> String {
    let (window, _, _) = try await freshVisual(state, step: "\(step)-before-click", draft: nil)
    let clickPoint = CGPoint(x: state.composerRect.x + 25, y: state.composerRect.y + 20)
    try mouseClick(globalPoint(clickPoint, window: window))
    try await Task.sleep(nanoseconds: 200_000_000)
    _ = try await freshVisual(state, step: "\(step)-before-select", draft: nil)
    try key(0, command: true) // Select only the clicked composer.
    try await Task.sleep(nanoseconds: 200_000_000)
    _ = try await freshVisual(state, step: "\(step)-before-copy", draft: nil)

    let snapshot = try ClipboardSnapshot.capture()
    let sentinel = "CODEX-COPY-\(UUID().uuidString)"
    let ownedCount = try setClipboardString(sentinel, snapshot: snapshot)
    var restorableCount = ownedCount
    do {
        _ = try await freshVisual(state, step: "\(step)-copy-ready", draft: nil)
        guard NSPasteboard.general.changeCount == ownedCount else { throw Failure("Clipboard changed before Copy") }
        try key(8, command: true) // Command-C.
        try await Task.sleep(nanoseconds: 200_000_000)
        let board = NSPasteboard.general
        let afterCount = board.changeCount
        let copied = board.string(forType: .string)
        var visuallyBlank = false
        if expected.isEmpty && afterCount == ownedCount {
            let (_, image, _) = try await freshVisual(state, step: "\(step)-empty-check", draft: nil)
            visuallyBlank = (try? requireDraft(image, rect: state.composerRect.rect, expected: "")) != nil
        }
        guard VisualParameters.copyIsExpected(expected: expected, sentinel: sentinel,
                                              beforeCount: ownedCount, afterCount: afterCount,
                                              clipboardText: copied, visualBlank: visuallyBlank) else {
            throw Failure("Copied composer text does not exactly match expected draft")
        }
        restorableCount = afterCount
        try snapshot.restore(ifCurrentCount: restorableCount)
        return VisualParameters.canonicalNewlines(expected)
    } catch {
        if NSPasteboard.general.changeCount == restorableCount {
            try? snapshot.restore(ifCurrentCount: restorableCount)
        }
        throw error
    }
}

private func pastePlainText(_ state: VisualStage, step: String) async throws {
    let snapshot = try ClipboardSnapshot.capture()
    let ownedCount = try setClipboardString(state.text, snapshot: snapshot)
    do {
        _ = try await freshVisual(state, step: "\(step)-before-paste", draft: nil)
        guard NSPasteboard.general.changeCount == ownedCount,
              NSPasteboard.general.string(forType: .string) == state.text else {
            throw Failure("Temporary clipboard changed before Paste")
        }
        try key(9, command: true) // Command-V.
        try await Task.sleep(nanoseconds: 200_000_000)
        try snapshot.restore(ifCurrentCount: ownedCount)
    } catch {
        if NSPasteboard.general.changeCount == ownedCount,
           NSPasteboard.general.string(forType: .string) == state.text {
            try? snapshot.restore(ifCurrentCount: ownedCount)
        }
        throw error
    }
}

private struct FocusInputs {
    let id: String
    let targetGroup: String
    let targetKind: TargetKind
    let expectedPID: Int32
    let expectedWindowID: UInt32
    let expectedFrame: RectInput
    let listRect: RectInput
    let searchPoint: PointInput
}

private func focusInputs(_ command: Command) throws -> FocusInputs {
    guard let group = command.targetGroup, VisualParameters.acceptsGroup(group),
          let kind = command.targetKind,
          let pid = command.expectedPID, pid > 0,
          let windowID = command.expectedWindowID, windowID > 0,
          let frame = command.expectedFrame,
          let list = command.conversationListRect,
          let search = command.searchPoint else {
        throw Failure("Focus requires targetGroup, targetKind, PID, window ID, frame, list rect and search point")
    }
    guard frame.width >= 300, frame.height >= 250,
          list.x >= 0, list.y >= 50,
          list.width >= 100, list.height >= 100,
          list.rect.maxX <= frame.width * 0.36,
          list.rect.maxY <= frame.height - 5,
          search.x >= list.x, search.x < list.rect.maxX,
          search.y >= 5, search.y < list.y - 5 else {
        throw Failure("Focus coordinates must identify the left conversation list and top search input")
    }
    return FocusInputs(id: command.id, targetGroup: group, targetKind: kind, expectedPID: pid,
                       expectedWindowID: windowID, expectedFrame: frame,
                       listRect: list, searchPoint: search)
}

private func freshFocus(_ input: FocusInputs, step: String,
                        allowSearchPopup: Bool = false) async throws -> (SCWindow, CGImage, SCWindow?) {
    guard AXIsProcessTrusted(), CGPreflightScreenCaptureAccess(), CGPreflightPostEventAccess() else {
        throw Failure("Accessibility, Screen Recording and Post Events access required")
    }
    let (app, windows) = try await processAndWindows()
    guard app.processIdentifier == input.expectedPID,
          let window = windows.first(where: { $0.windowID == input.expectedWindowID }),
          windows.count <= (allowSearchPopup ? 2 : 1) else {
        throw Failure("WeChat PID, expected window, or navigation window count changed")
    }
    let actual = window.frame, expected = input.expectedFrame.rect
    guard abs(actual.minX - expected.minX) <= 3,
          abs(actual.minY - expected.minY) <= 3,
          abs(actual.width - expected.width) <= 3,
          abs(actual.height - expected.height) <= 3 else {
        throw Failure("WeChat window frame changed")
    }
    let extras = windows.filter { $0.windowID != input.expectedWindowID }
    guard extras.count <= 1 else { throw Failure("Ambiguous WeChat navigation windows") }
    if let popup = extras.first {
        guard allowSearchPopup,
              VisualParameters.plausibleSearchPopup(popup.frame, main: expected,
                                                    search: input.searchPoint.point) else {
            throw Failure("Unexpected WeChat window during navigation")
        }
    }
    _ = try consoleAndFocus(app)
    let image = try await capture(window, destination: outputDirectory.appendingPathComponent("\(input.id).\(step).png"))
    guard image.width == Int(expected.width), image.height == Int(expected.height) else {
        throw Failure("Screenshot pixel size differs from expected geometry")
    }
    _ = try consoleAndFocus(app)
    return (window, image, extras.first)
}

private func groupCandidates(_ image: CGImage, rect: CGRect) throws -> [GroupOCRCandidate] {
    guard rect.minX >= 0, rect.minY >= 0,
          rect.maxX <= CGFloat(image.width), rect.maxY <= CGFloat(image.height),
          let crop = image.cropping(to: rect.integral) else {
        throw Failure("Conversation list crop is outside captured window")
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: crop).perform([request])
    return (request.results ?? []).compactMap { result in
        guard let text = result.topCandidates(1).first?.string else { return nil }
        let bounds = result.boundingBox
        return GroupOCRCandidate(text: text,
            x: Double(rect.minX + bounds.midX * rect.width),
            y: Double(rect.minY + (1 - bounds.midY) * rect.height))
    }
}

private func exactVisibleRow(_ image: CGImage, input: FocusInputs) throws -> GroupOCRCandidate? {
    guard let nameRect = VisualParameters.conversationNameRect(input.listRect.rect) else {
        throw Failure("Conversation name column is not calibrated")
    }
    let candidates = try groupCandidates(image, rect: nameRect)
    let matches = candidates.filter { $0.y >= input.listRect.y &&
        VisualParameters.exactGroupTitle($0.text, target: input.targetGroup) }
    guard matches.count <= 1 else { throw Failure("Ambiguous exact group rows in left conversation list") }
    return VisualParameters.uniqueExactGroup(candidates, target: input.targetGroup,
                                             minimumY: input.listRect.y)
}

private func clickGroupRow(_ input: FocusInputs, row: GroupOCRCandidate, step: String) async throws {
    let (window, image, _) = try await freshFocus(input, step: step)
    guard let current = try exactVisibleRow(image, input: input),
          current.text == row.text,
          abs(current.x - row.x) <= 10, abs(current.y - row.y) <= 10 else {
        throw Failure("Group row changed before click")
    }
    try mouseClick(globalPoint(CGPoint(x: row.x, y: row.y), window: window))
    try await Task.sleep(nanoseconds: 300_000_000)
    let (_, after, _) = try await freshFocus(input, step: "\(step)-after-click")
    guard try await headerContainsTarget(after, target: input.targetGroup,
                                         kind: input.targetKind) else {
        throw Failure("Selected chat header is not exact target and kind")
    }
}

private func pasteSearch(_ input: FocusInputs) async throws {
    let (window, _, _) = try await freshFocus(input, step: "before-search-click")
    try mouseClick(globalPoint(input.searchPoint.point, window: window))
    try await Task.sleep(nanoseconds: 150_000_000)
    _ = try await freshFocus(input, step: "before-search-select", allowSearchPopup: true)
    try key(0, command: true) // Select only calibrated search input.
    let snapshot = try ClipboardSnapshot.capture()
    let owned = try setClipboardString(input.targetGroup, snapshot: snapshot)
    do {
        _ = try await freshFocus(input, step: "before-search-paste", allowSearchPopup: true)
        guard NSPasteboard.general.changeCount == owned,
              NSPasteboard.general.string(forType: .string) == input.targetGroup else {
            throw Failure("Temporary clipboard changed before Search Paste")
        }
        try key(9, command: true) // Paste exact target group into search input.
        try await Task.sleep(nanoseconds: 200_000_000)
        try snapshot.restore(ifCurrentCount: owned)
    } catch {
        if NSPasteboard.general.changeCount == owned,
           NSPasteboard.general.string(forType: .string) == input.targetGroup {
            try? snapshot.restore(ifCurrentCount: owned)
        }
        throw error
    }
    // Copy back from the calibrated search input, never from the message composer.
    _ = try await freshFocus(input, step: "before-search-copy", allowSearchPopup: true)
    try key(0, command: true)
    let copiedSnapshot = try ClipboardSnapshot.capture()
    let sentinel = "CODEX-SEARCH-\(UUID().uuidString)"
    let copiedOwned = try setClipboardString(sentinel, snapshot: copiedSnapshot)
    var restorable = copiedOwned
    do {
        _ = try await freshFocus(input, step: "search-copy-ready", allowSearchPopup: true)
        guard NSPasteboard.general.changeCount == copiedOwned else { throw Failure("Clipboard changed before Search Copy") }
        try key(8, command: true)
        try await Task.sleep(nanoseconds: 150_000_000)
        let board = NSPasteboard.general
        restorable = board.changeCount
        guard VisualParameters.copyIsExpected(expected: input.targetGroup, sentinel: sentinel,
              beforeCount: copiedOwned, afterCount: restorable,
              clipboardText: board.string(forType: .string), visualBlank: false) else {
            throw Failure("Search input does not contain exact target group")
        }
        try copiedSnapshot.restore(ifCurrentCount: restorable)
    } catch {
        if NSPasteboard.general.changeCount == restorable {
            try? copiedSnapshot.restore(ifCurrentCount: restorable)
        }
        throw error
    }
}

private func popupCandidates(_ image: CGImage) throws -> [SearchOCRCandidate] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]
    request.usesLanguageCorrection = false
    try VNImageRequestHandler(cgImage: image).perform([request])
    return (request.results ?? []).compactMap { result in
        guard let text = result.topCandidates(1).first?.string else { return nil }
        let box = result.boundingBox
        return SearchOCRCandidate(text: text, bounds: CGRect(
            x: box.minX * CGFloat(image.width),
            y: (1 - box.maxY) * CGFloat(image.height),
            width: box.width * CGFloat(image.width),
            height: box.height * CGFloat(image.height)))
    }
}

private struct SearchProbe {
    let windows: [SCWindow]
    let selection: (SCWindow, SearchSelection)?
}

private func searchPopupResult(_ input: FocusInputs, step: String,
                               expectedPopupID: UInt32? = nil,
                               expectedPopupFrame: CGRect? = nil)
    async throws -> SearchProbe {
    let (main, _, popup) = try await freshFocus(input, step: "\(step)-main", allowSearchPopup: true)
    guard let popup else { return SearchProbe(windows: [main], selection: nil) }
    if let expectedPopupID, popup.windowID != expectedPopupID {
        throw Failure("Search popup ID changed before click")
    }
    if let frame = expectedPopupFrame {
        guard abs(popup.frame.minX - frame.minX) <= 3,
              abs(popup.frame.minY - frame.minY) <= 3,
              abs(popup.frame.width - frame.width) <= 3,
              abs(popup.frame.height - frame.height) <= 3 else {
            throw Failure("Search popup frame changed before click")
        }
    }
    let image = try await capture(popup,
        destination: outputDirectory.appendingPathComponent("\(input.id).\(step)-popup.png"),
        minimumHeight: 160)
    guard image.width == Int(popup.frame.width), image.height == Int(popup.frame.height) else {
        throw Failure("Search popup screenshot geometry changed")
    }
    let (_, _, currentPopup) = try await freshFocus(input, step: "\(step)-confirm", allowSearchPopup: true)
    guard let currentPopup, currentPopup.windowID == popup.windowID,
          abs(currentPopup.frame.minX - popup.frame.minX) <= 3,
          abs(currentPopup.frame.minY - popup.frame.minY) <= 3,
          abs(currentPopup.frame.width - popup.frame.width) <= 3,
          abs(currentPopup.frame.height - popup.frame.height) <= 3 else {
        throw Failure("Search popup changed during capture")
    }
    let candidates = try popupCandidates(image)
    let selection = VisualParameters.uniqueSearchResult(candidates, target: input.targetGroup,
        kind: input.targetKind,
        popupSize: CGSize(width: image.width, height: image.height)).map { (popup, $0) }
    return SearchProbe(windows: [main, popup], selection: selection)
}

private func writeFocusPolls(_ polls: [FocusPollDiagnostic], id: String) throws {
    try JSONEncoder().encode(polls).write(
        to: outputDirectory.appendingPathComponent("\(id).focus-polls.json"), options: .atomic)
}

private func focusWindowDiagnostic(_ window: SCWindow) -> FocusWindowDiagnostic {
    let frame = window.frame
    return FocusWindowDiagnostic(id: window.windowID,
        x: Double(frame.minX), y: Double(frame.minY),
        width: Double(frame.width), height: Double(frame.height))
}

private func runFocus(_ command: Command) async throws -> [String: Any] {
    let input = try focusInputs(command)
    _ = try await freshFocus(input, step: "focus-initial")
    try exclusiveMarker(outputDirectory.appendingPathComponent("\(input.id).focus-attempted.json"))
    try await pasteSearch(input)
    let searchStarted = Date()
    var polls: [FocusPollDiagnostic] = []
    while VisualParameters.withinFocusSearchBudget(Date().timeIntervalSince(searchStarted)) {
        let probe = try await searchPopupResult(input, step: "focus-search-result")
        polls.append(FocusPollDiagnostic(
            elapsedMs: Int(Date().timeIntervalSince(searchStarted) * 1_000),
            windowCount: probe.windows.count,
            windows: probe.windows.map(focusWindowDiagnostic),
            popupPresent: probe.windows.count == 2,
            popupSelected: probe.selection != nil))
        try writeFocusPolls(polls, id: input.id)
        if let (popup, selected) = probe.selection {
            let confirmed = try await searchPopupResult(input,
                step: "focus-search-before-click", expectedPopupID: popup.windowID,
                expectedPopupFrame: popup.frame)
            guard let (currentPopup, current) = confirmed.selection,
                current.category == selected.category,
                current.row.text == selected.row.text,
                abs(current.row.bounds.midX - selected.row.bounds.midX) <= 10,
                abs(current.row.bounds.midY - selected.row.bounds.midY) <= 10 else {
                throw Failure("Search popup row changed before click")
            }
            try mouseClick(globalPoint(CGPoint(x: current.row.bounds.midX, y: current.row.bounds.midY),
                                       window: currentPopup))
            try await Task.sleep(nanoseconds: 300_000_000)
            let (_, after, _) = try await freshFocus(input, step: "focus-search-after-click")
            guard try await headerContainsTarget(after, target: input.targetGroup,
                                                 kind: input.targetKind) else {
                throw Failure("Selected chat header is not exact target and kind")
            }
            let evidence = FocusEvidence(id: input.id, targetGroup: input.targetGroup,
                targetKind: input.targetKind, expectedPID: input.expectedPID,
                expectedWindowID: input.expectedWindowID, expectedFrame: input.expectedFrame,
                source: "search", category: current.category, selectedAt: Date())
            try JSONEncoder().encode(evidence).write(to: focusEvidenceURL(input.id), options: .atomic)
            return ["focused": true, "alreadySelected": false, "source": "search",
                    "category": current.category.rawValue,
                    "targetKind": input.targetKind.rawValue, "focusEvidenceID": input.id]
        }
        try await Task.sleep(nanoseconds: 200_000_000)
    }
    if polls.allSatisfy({ !$0.popupPresent }) {
        throw Failure("Search popup not enumerated by ScreenCaptureKit within six seconds")
    }
    throw Failure("Search popup had no unique category-eligible exact target within six seconds")
}

private func runVisual(_ command: Command) async throws -> [String: Any] {
    let input = try visualInputs(command)
    if command.action == "prepare-visual" {
        // Marker is durable before the first click; any partial attempt blocks repeat.
        try exclusiveMarker(visualStageURL(input.id))
        var state = input
        state.stage = "prepare-attempted"
        try writeStage(state)
        let (_, _, lockStatus) = try await freshVisual(state, step: "prepare-start", draft: nil)
        _ = try await readSelectedDraft(state, step: "old-draft", expected: state.expectedDraft)
        try await pastePlainText(state, step: "replace-draft")
        _ = try await readSelectedDraft(state, step: "new-draft", expected: state.text)
        state.stage = "prepared"
        try writeStage(state)
        return ["prepared": true, "lockStatus": lockStatus,
                "message": "Exact copied draft verified; no Send action performed"]
    }
    let state = try JSONDecoder().decode(VisualStage.self, from: Data(contentsOf: visualStageURL(input.id)))
    guard state.stage == "prepared", sameVisualInputs(state, input) else {
        throw Failure("Submit requires matching prepared transaction")
    }
    _ = try await readSelectedDraft(state, step: "submit-draft", expected: state.text)
    let (window, image, lockStatus) = try await freshVisual(state, step: "before-submit", draft: nil)
    try requireSendButton(image, point: state.buttonPoint.point)
    try exclusiveMarker(outputDirectory.appendingPathComponent("\(state.id).submit-attempted.json"))
    var attempted = state
    attempted.stage = "submit-attempted"
    try writeStage(attempted)
    let (currentWindow, currentImage, _) = try await freshVisual(state, step: "submit-immediate", draft: nil)
    guard currentWindow.windowID == window.windowID else { throw Failure("Window changed before final click") }
    try requireSendButton(currentImage, point: state.buttonPoint.point)
    try mouseClick(globalPoint(state.buttonPoint.point, window: currentWindow))
    attempted.stage = "submitted-unverified"
    try writeStage(attempted)
    return ["submitted": true, "lockStatus": lockStatus,
            "message": "One visual Send click posted; delivery needs independent verification"]
}

@main struct Main {
    static func main() async {
        umask(0o077)
        NSApplication.shared.setActivationPolicy(.accessory)
        var command: Command?
        do {
            try preparePrivateOutputDirectory()
            command = try readCommand()
            let cmd = command!
            var result: [String: Any] = ["id": cmd.id, "action": cmd.action, "permissions": permissionSummary()]
            switch cmd.action {
            case "request-permissions":
                _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
                _ = CGRequestScreenCaptureAccess()
                result["permissionsAfterRequest"] = permissionSummary()
            case "focus-group":
                result.merge(try await runFocus(cmd)) { _, new in new }
            case "prepare-visual", "submit-visual":
                result.merge(try await runVisual(cmd)) { _, new in new }
            case "inspect", "capture":
                let (app, windows) = try await processAndWindows()
                result["weChatPID"] = app.processIdentifier
                result["windows"] = windows.map(windowSummary)
                result["frontmost"] = NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier
                result["session"] = sessionSummary()
                if AXIsProcessTrusted() { result["axMetadata"] = inspectAX(app) }
                if cmd.action != "inspect" {
                    guard CGPreflightScreenCaptureAccess() else { throw Failure("Screen Recording permission required") }
                    guard windows.count == 1, let window = windows.first else { throw Failure("Expected exactly one usable on-screen WeChat window") }
                    let imageURL = outputDirectory.appendingPathComponent("\(cmd.id).wechat-window.png")
                    let image = try await capture(window, destination: imageURL)
                    result["capture"] = imageURL.path
                    _ = image
                }
            default: break
            }
            result["ok"] = true
            let resultName = cmd.action.hasSuffix("-visual") ? "\(cmd.id).\(cmd.action).result.json" : "\(cmd.id).result.json"
            save(result, to: outputDirectory.appendingPathComponent(resultName))
        } catch {
            let id = command?.id ?? "invalid-command"
            let resultName = command?.action.hasSuffix("-visual") == true ? "\(id).\(command!.action).result.json" : "\(id).result.json"
            save(["id": id, "ok": false, "error": String(describing: error), "permissions": permissionSummary()],
                 to: outputDirectory.appendingPathComponent(resultName))
        }
    }
}
