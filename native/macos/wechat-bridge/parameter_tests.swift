import AppKit
import Foundation
import CryptoKit
import ImageIO

@main struct ParameterTests {
    static func main() {
        emptyComposerChecks()
        for label in ["Send", "发送"] {
            precondition(VisualParameters.uniqueSendLabel([label], atButton: true))
            precondition(VisualParameters.uniqueSendLabel(["Toolbar", label], atButton: false))
            precondition(!VisualParameters.uniqueSendLabel(["Toolbar", label], atButton: true))
            precondition(!VisualParameters.uniqueSendLabel([label, label], atButton: false))
        }
        precondition(!VisualParameters.uniqueSendLabel(["Send", "发送"], atButton: false))
        precondition(!VisualParameters.uniqueSendLabel([], atButton: true))
        precondition(!VisualParameters.uniqueSendLabel(["发送文件"], atButton: true))
        precondition(!VisualParameters.uniqueSendLabel(["Send now"], atButton: true))
        precondition(VisualParameters.acceptsText("中文，标点 😊\n第二行", allowEmpty: false))
        precondition(VisualParameters.acceptsText("", allowEmpty: true))
        precondition(!VisualParameters.acceptsText("", allowEmpty: false))
        precondition(!VisualParameters.acceptsText(" \n ", allowEmpty: false))
        precondition(VisualParameters.acceptsText(String(repeating: "字", count: 2_000), allowEmpty: false))
        precondition(!VisualParameters.acceptsText(String(repeating: "字", count: 2_001), allowEmpty: false))
        precondition(!VisualParameters.acceptsText("a\0b", allowEmpty: false))
        precondition(!VisualParameters.acceptsText("a\u{001B}b", allowEmpty: false))
        precondition(VisualParameters.acceptsGroup("合成测试群"))
        precondition(!VisualParameters.acceptsGroup("群\n二"))
        precondition(!VisualParameters.acceptsGroup("同名 (3)"))
        precondition(VisualParameters.canonicalNewlines("甲\r\n乙\r丙") == "甲\n乙\n丙")
        precondition(VisualParameters.withinFocusSearchBudget(0))
        precondition(VisualParameters.withinFocusSearchBudget(5.999))
        precondition(!VisualParameters.withinFocusSearchBudget(6))
        precondition(!VisualParameters.withinFocusSearchBudget(-0.001))
        precondition(!VisualParameters.withinFocusSearchBudget(.infinity))
        let shortPopup = CGRect(x: 229, y: 306, width: 368, height: 194)
        let loadedPopup = CGRect(x: 229, y: 306, width: 368, height: 500)
        for beforeClick in [false, true] {
            precondition(VisualParameters.searchPopupDisposition(capturedID: 7, capturedFrame: loadedPopup,
                currentID: 7, currentFrame: loadedPopup, beforeClick: beforeClick) == .stable)
            precondition(VisualParameters.searchPopupDisposition(capturedID: 7, capturedFrame: loadedPopup,
                currentID: 7, currentFrame: loadedPopup.offsetBy(dx: 3, dy: -3),
                beforeClick: beforeClick) == .stable)
            let expected: SearchPopupDisposition = beforeClick ? .refuse : .pollAgain
            precondition(VisualParameters.searchPopupDisposition(capturedID: 7, capturedFrame: shortPopup,
                currentID: 7, currentFrame: loadedPopup, beforeClick: beforeClick) == expected)
            precondition(VisualParameters.searchPopupDisposition(capturedID: 7, capturedFrame: loadedPopup,
                currentID: 8, currentFrame: loadedPopup, beforeClick: beforeClick) == expected)
            precondition(VisualParameters.searchPopupDisposition(capturedID: 7, capturedFrame: loadedPopup,
                currentID: nil, currentFrame: nil, beforeClick: beforeClick) == expected)
            precondition(VisualParameters.searchPopupDisposition(capturedID: 7, capturedFrame: loadedPopup,
                currentID: 7, currentFrame: loadedPopup.offsetBy(dx: 4, dy: 0),
                beforeClick: beforeClick) == expected)
        }
        let poll = FocusPollDiagnostic(elapsedMs: 451, windowCount: 1,
            windows: [FocusWindowDiagnostic(id: 7, x: 10, y: 20, width: 880, height: 640)],
            popupPresent: false, popupSelected: false)
        let pollJSON = String(data: try! JSONEncoder().encode([poll]), encoding: .utf8)!
        precondition(pollJSON.contains("\"elapsedMs\":451"))
        precondition(pollJSON.contains("\"windowCount\":1"))
        precondition(pollJSON.contains("\"popupPresent\":false"))
        precondition(!pollJSON.contains("target") && !pollJSON.contains("title") &&
                     !pollJSON.contains("text"))
        precondition(VisualParameters.sameTransactionText(text: "你好", draft: "wxx", group: "合成测试群",
                                                          otherText: "你好", otherDraft: "wxx", otherGroup: "合成测试群"))
        precondition(!VisualParameters.sameTransactionText(text: "你好", draft: "wxx", group: "合成测试群",
                                                           otherText: "你好", otherDraft: "wx", otherGroup: "合成测试群"))
        precondition(!VisualParameters.sameTransactionText(text: "你好", draft: "wxx", group: "合成测试群",
                                                           otherText: "你好", otherDraft: "wxx", otherGroup: "其他群"))
        precondition(VisualParameters.exactGroupTitle("  合成测试群（１２） ", target: "合成测试群"))
        precondition(VisualParameters.exactGroupTitle("合成测试群 (12)", target: "合成测试群"))
        precondition(!VisualParameters.exactGroupTitle("合成测试群二", target: "合成测试群"))
        let oneRow = GroupOCRCandidate(text: "合成测试群", x: 180, y: 100)
        let anotherRow = GroupOCRCandidate(text: "其他群", x: 180, y: 150)
        precondition(VisualParameters.uniqueExactGroup([oneRow, anotherRow], target: "合成测试群", minimumY: 55) == oneRow)
        precondition(VisualParameters.uniqueExactGroup([anotherRow], target: "合成测试群", minimumY: 55) == nil)
        precondition(VisualParameters.uniqueExactGroup([oneRow, oneRow], target: "合成测试群", minimumY: 55) == nil)
        let searchBox = GroupOCRCandidate(text: "合成测试群", x: 150, y: 28)
        precondition(VisualParameters.uniqueExactGroup([searchBox, oneRow], target: "合成测试群", minimumY: 55) == oneRow)
        precondition(VisualParameters.uniqueExactGroup([searchBox], target: "合成测试群", minimumY: 55) == nil)
        let list = CGRect(x: 60, y: 52, width: 240, height: 580)
        let names = VisualParameters.conversationNameRect(list)!
        precondition(names.minX >= 108 && names.maxX <= 265)
        precondition(names.contains(CGPoint(x: 180, y: 145)))
        precondition(!names.contains(CGPoint(x: 270, y: 145)))
        let main = CGRect(x: 100, y: 100, width: 880, height: 640)
        let search = CGPoint(x: 100, y: 28)
        precondition(VisualParameters.plausibleSearchPopup(
            CGRect(x: 160, y: 145, width: 320, height: 300), main: main, search: search))
        precondition(VisualParameters.plausibleSearchPopup(
            CGRect(x: 229, y: 306, width: 368, height: 330),
            main: CGRect(x: 181, y: 271, width: 880, height: 640), search: search))
        precondition(VisualParameters.plausibleSearchPopup(
            CGRect(x: 229, y: 306, width: 368, height: 194),
            main: CGRect(x: 181, y: 271, width: 880, height: 640), search: search))
        precondition(!VisualParameters.plausibleSearchPopup(
            CGRect(x: 229, y: 306, width: 368, height: 159),
            main: CGRect(x: 181, y: 271, width: 880, height: 640), search: search))
        precondition(VisualParameters.plausibleSearchPopup(
            CGRect(x: 229, y: 306, width: 368, height: 614),
            main: CGRect(x: 181, y: 271, width: 880, height: 640), search: search))
        precondition(!VisualParameters.plausibleSearchPopup(
            CGRect(x: 229, y: 306, width: 368, height: 622),
            main: CGRect(x: 181, y: 271, width: 880, height: 640), search: search))
        precondition(!VisualParameters.plausibleSearchPopup(
            CGRect(x: 550, y: 145, width: 320, height: 300), main: main, search: search))
        precondition(!VisualParameters.plausibleSearchPopup(
            CGRect(x: 160, y: 60, width: 320, height: 300), main: main, search: search))
        let contactHeading = SearchOCRCandidate(text: "Contacts",
            bounds: CGRect(x: 15, y: 55, width: 90, height: 18))
        let groupHeading = SearchOCRCandidate(text: "群聊",
            bounds: CGRect(x: 15, y: 130, width: 60, height: 18))
        let webHeading = SearchOCRCandidate(text: "Search the Web",
            bounds: CGRect(x: 15, y: 210, width: 130, height: 18))
        let contactRow = SearchOCRCandidate(text: "Synthetic Person",
            bounds: CGRect(x: 65, y: 83, width: 155, height: 18))
        let groupRow = SearchOCRCandidate(text: "合成测试群",
            bounds: CGRect(x: 65, y: 163, width: 120, height: 18))
        let webRow = SearchOCRCandidate(text: "合成测试群",
            bounds: CGRect(x: 65, y: 245, width: 120, height: 18))
        let popupSize = CGSize(width: 320, height: 300)
        precondition(VisualParameters.uniqueSearchResult(
            [contactHeading, contactRow, groupHeading, groupRow, webHeading, webRow],
            target: "合成测试群", kind: .group, popupSize: popupSize) ==
            SearchSelection(row: groupRow, category: .groupChats))
        precondition(VisualParameters.uniqueSearchResult(
            [contactHeading, contactRow], target: "Synthetic Person", kind: .private, popupSize: popupSize) ==
            SearchSelection(row: contactRow, category: .contacts))
        precondition(VisualParameters.uniqueSearchResult(
            [webHeading, webRow], target: "合成测试群", kind: .group, popupSize: popupSize) == nil)
        precondition(VisualParameters.uniqueSearchResult(
            [groupRow], target: "合成测试群", kind: .group, popupSize: popupSize) == nil)
        precondition(VisualParameters.uniqueSearchResult(
            [groupHeading, groupRow, groupRow], target: "合成测试群", kind: .group, popupSize: popupSize) == nil)
        let sameContact = SearchOCRCandidate(text: "合成测试群",
            bounds: CGRect(x: 65, y: 83, width: 120, height: 18))
        precondition(VisualParameters.uniqueSearchResult(
            [contactHeading, sameContact, groupHeading, groupRow],
            target: "合成测试群", kind: .group, popupSize: popupSize) == nil)
        precondition(VisualParameters.uniqueSearchResult(
            [contactHeading, sameContact, groupHeading, groupRow],
            target: "合成测试群", kind: .private, popupSize: popupSize) == nil)
        let mostUsedHeading = SearchOCRCandidate(text: "Most used",
            bounds: CGRect(x: 15, y: 55, width: 90, height: 18))
        let internetHeading = SearchOCRCandidate(text: "Internet search results",
            bounds: CGRect(x: 15, y: 130, width: 190, height: 18))
        let recentRow = SearchOCRCandidate(text: "合成测试群",
            bounds: CGRect(x: 65, y: 83, width: 120, height: 18))
        precondition(VisualParameters.uniqueSearchResult(
            [mostUsedHeading, recentRow, internetHeading, webRow],
            target: "合成测试群", kind: .group, popupSize: popupSize) ==
            SearchSelection(row: recentRow, category: .mostUsed))
        precondition(VisualParameters.uniqueSearchResult(
            [mostUsedHeading, recentRow, internetHeading, webRow],
            target: "合成测试群", kind: .private, popupSize: popupSize) ==
            SearchSelection(row: recentRow, category: .mostUsed))
        precondition(VisualParameters.uniqueSearchResult(
            [mostUsedHeading, recentRow, groupHeading, groupRow],
            target: "合成测试群", kind: .group, popupSize: popupSize) == nil)
        let distantRow = SearchOCRCandidate(text: "合成测试群",
            bounds: CGRect(x: 65, y: 245, width: 120, height: 18))
        precondition(VisualParameters.uniqueSearchResult(
            [groupHeading, distantRow], target: "合成测试群", kind: .group, popupSize: popupSize) == nil)
        precondition(VisualParameters.uniqueSearchResult(
            [groupHeading, groupRow, distantRow], target: "合成测试群", kind: .group,
            popupSize: popupSize) == nil)
        // An unrecognized section heading closes the previous category. It
        // cannot authorize a result, including when OCR misreads a history heading.
        let unknownHeading = SearchOCRCandidate(text: "Unrecognized section",
            bounds: CGRect(x: 15, y: 210, width: 150, height: 18))
        for kind in [TargetKind.group, .private] {
            precondition(VisualParameters.uniqueSearchResult(
                [mostUsedHeading, recentRow, unknownHeading, distantRow],
                target: "合成测试群", kind: kind, popupSize: popupSize) ==
                SearchSelection(row: recentRow, category: .mostUsed))
            precondition(VisualParameters.uniqueSearchResult(
                [mostUsedHeading, unknownHeading, distantRow],
                target: "合成测试群", kind: kind, popupSize: popupSize) == nil)
        }
        let indentedText = SearchOCRCandidate(text: "Unrecognized section",
            bounds: CGRect(x: 65, y: 210, width: 150, height: 18))
        precondition(VisualParameters.uniqueSearchResult(
            [mostUsedHeading, recentRow, indentedText, distantRow],
            target: "合成测试群", kind: .private, popupSize: popupSize) == nil)
        precondition(VisualParameters.uniqueSearchResult(
            [unknownHeading, distantRow], target: "合成测试群", kind: .private,
            popupSize: popupSize) == nil)
        let nearbyUnknownHeading = SearchOCRCandidate(text: "Unrecognized section",
            bounds: CGRect(x: 15, y: 92, width: 150, height: 18))
        let nearbyUnknownRow = SearchOCRCandidate(text: "合成测试群",
            bounds: CGRect(x: 65, y: 120, width: 120, height: 18))
        precondition(VisualParameters.uniqueSearchResult(
            [mostUsedHeading, nearbyUnknownHeading, nearbyUnknownRow],
            target: "合成测试群", kind: .private, popupSize: popupSize) == nil)
        let title = HeaderOCRCandidate(text: "测试群 (3)", bounds: CGRect(x: 315, y: 19, width: 120, height: 16))
        let sender = HeaderOCRCandidate(text: "Synthetic Peer", bounds: CGRect(x: 320, y: 68, width: 90, height: 15))
        let privateTitle = HeaderOCRCandidate(text: "Synthetic Peer", bounds: CGRect(x: 315, y: 19, width: 100, height: 16))
        precondition(VisualParameters.headerTitleRect(imageWidth: 880, imageHeight: 640) ==
                     CGRect(x: 300, y: 8, width: 420, height: 42))
        precondition(VisualParameters.headerOCRRect(imageWidth: 880, imageHeight: 640) ==
                     CGRect(x: 300, y: 0, width: 420, height: 90))
        precondition(VisualParameters.headerTitleRect(imageWidth: 879, imageHeight: 640) == nil)
        precondition(VisualParameters.uniqueHeaderTitle([title, sender], target: "测试群", kind: .group,
                                                        imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.exactHeaderTitle("测试群", target: "测试群", kind: .group))
        precondition(!VisualParameters.exactHeaderTitle("测试群 (3)", target: "测试群", kind: .private))
        precondition(!VisualParameters.uniqueHeaderTitle([title, sender], target: "Synthetic Peer", kind: .private,
                                                         imageWidth: 880, imageHeight: 640))
        precondition(VisualParameters.uniqueHeaderTitle([privateTitle, sender], target: "Synthetic Peer", kind: .private,
                                                        imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([privateTitle, privateTitle], target: "Synthetic Peer", kind: .private,
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([privateTitle, title], target: "Synthetic Peer", kind: .private,
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([sender], target: "Synthetic Peer", kind: .private,
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([privateTitle], target: "Synthetic Peer", kind: .private,
                                                         imageWidth: 880, imageHeight: 639))
        let crossingBoundary = HeaderOCRCandidate(text: "Synthetic Peer",
            bounds: CGRect(x: 315, y: 43, width: 100, height: 14))
        let clippedLeftEdge = HeaderOCRCandidate(text: "Synthetic Peer",
            bounds: CGRect(x: 300, y: 19, width: 100, height: 16))
        let clippedTopEdge = HeaderOCRCandidate(text: "Synthetic Peer",
            bounds: CGRect(x: 315, y: 8, width: 100, height: 16))
        precondition(!VisualParameters.uniqueHeaderTitle([crossingBoundary], target: "Synthetic Peer", kind: .private,
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([clippedLeftEdge], target: "Synthetic Peer", kind: .private,
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([clippedTopEdge], target: "Synthetic Peer", kind: .private,
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([privateTitle, crossingBoundary], target: "Synthetic Peer", kind: .private,
                                                         imageWidth: 880, imageHeight: 640))
        let token = "private-sentinel"
        precondition(VisualParameters.copyIsExpected(expected: "a b\n😊", sentinel: token,
                                                     beforeCount: 10, afterCount: 11,
                                                     clipboardText: "a b\r\n😊", visualBlank: false))
        precondition(!VisualParameters.copyIsExpected(expected: "a b", sentinel: token,
                                                      beforeCount: 10, afterCount: 11,
                                                      clipboardText: "ab", visualBlank: false))
        precondition(VisualParameters.copyIsExpected(expected: "", sentinel: token,
                                                     beforeCount: 10, afterCount: 10,
                                                     clipboardText: token, visualBlank: true))
        precondition(!VisualParameters.copyIsExpected(expected: "", sentinel: token,
                                                      beforeCount: 10, afterCount: 10,
                                                      clipboardText: token, visualBlank: false))
        for copied in ["Hold mouse to input by voice", "user draft", token] {
            precondition(!VisualParameters.copyIsExpected(expected: "", sentinel: token,
                beforeCount: 10, afterCount: 11, clipboardText: copied,
                visualBlank: true))
        }
        precondition(!VisualParameters.copyIsExpected(expected: "user draft", sentinel: token,
            beforeCount: 10, afterCount: 10, clipboardText: token, visualBlank: true))
        precondition(!VisualParameters.copyIsExpected(expected: "", sentinel: token,
            beforeCount: 10, afterCount: 12, clipboardText: token, visualBlank: true))
        precondition(VisualParameters.mayRestoreClipboard(ownedCount: 12, currentCount: 12))
        precondition(!VisualParameters.mayRestoreClipboard(ownedCount: 12, currentCount: 13))
        let board = NSPasteboard(name: NSPasteboard.Name("codex-bridge-tests-" + UUID().uuidString))
        board.clearContents()
        let original = NSPasteboardItem()
        precondition(original.setString("original 😊", forType: .string))
        precondition(original.setData(Data([1, 2, 3]), forType: NSPasteboard.PasteboardType("io.codex.test-bytes")))
        precondition(board.writeObjects([original]))
        let snapshot = try! ClipboardSnapshot.capture(board: board)
        let owned = try! setClipboardString("temporary", snapshot: snapshot)
        precondition(board.string(forType: .string) == "temporary")
        try! snapshot.restore(ifCurrentCount: owned)
        precondition(board.string(forType: .string) == "original 😊")
        precondition(board.data(forType: NSPasteboard.PasteboardType("io.codex.test-bytes")) == Data([1, 2, 3]))
        let second = try! ClipboardSnapshot.capture(board: board)
        let secondOwned = try! setClipboardString("helper", snapshot: second)
        board.clearContents()
        precondition(board.setString("external", forType: .string))
        precondition(board.changeCount != secondOwned)
        do {
            try second.restore(ifCurrentCount: secondOwned)
            preconditionFailure("external clipboard change must block restore")
        } catch {}
        precondition(board.string(forType: .string) == "external")
        print("Parameter checks and named-pasteboard restoration checks passed")
    }

    static func emptyComposerChecks() {
        func image(_ changed: Bool = false) -> CGImage {
            var bytes = [UInt8](repeating: 255, count: 4 * 3 * 4)
            if changed { bytes[0] = 254 }
            let provider = CGDataProvider(data: Data(bytes) as CFData)!
            return CGImage(width: 4, height: 3, bitsPerComponent: 8, bitsPerPixel: 32,
                bytesPerRow: 16, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)!
        }
        let original = image(), rect = CGRect(x: 0, y: 0, width: 4, height: 3)
        precondition(VisualParameters.matchesEmptyComposer(original, rect: rect, reference: original))
        precondition(!VisualParameters.matchesEmptyComposer(image(true), rect: rect, reference: original))
        precondition(!VisualParameters.matchesEmptyComposer(original, rect: rect.offsetBy(dx: 1, dy: 0), reference: original))
        precondition(!VisualParameters.matchesEmptyComposer(original, rect: CGRect(x: 0, y: 0, width: 3, height: 3), reference: original))
        precondition(!VisualParameters.matchesEmptyComposer(original, rect: rect.offsetBy(dx: 0.1, dy: 0), reference: original))
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try! FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let path = directory.appendingPathComponent("empty.png")
        let data = NSMutableData()
        let destination = CGImageDestinationCreateWithData(data, "public.png" as CFString, 1, nil)!
        CGImageDestinationAddImage(destination, original, nil)
        precondition(CGImageDestinationFinalize(destination))
        try! (data as Data).write(to: path)
        try! FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        let hash = SHA256.hash(data: data as Data).map { String(format: "%02x", $0) }.joined()
        let reference = EmptyComposerReference(path: path.path, sha256: hash)
        let decoded = try! reference.load(in: directory, rect: rect)
        precondition(VisualParameters.matchesEmptyComposer(original, rect: rect, reference: decoded))
        func refused(_ candidate: EmptyComposerReference, _ crop: CGRect = rect) {
            do { _ = try candidate.load(in: directory, rect: crop); preconditionFailure("unsafe reference accepted") }
            catch {}
        }
        refused(EmptyComposerReference(path: path.path, sha256: String(repeating: "0", count: 64)))
        refused(reference, CGRect(x: 0, y: 0, width: 3, height: 3))
        refused(EmptyComposerReference(path: directory.appendingPathComponent("../empty.png").path, sha256: hash))
        try! FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: path.path)
        refused(reference)
        try! FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        let link = directory.appendingPathComponent("link.png")
        try! FileManager.default.createSymbolicLink(at: link, withDestinationURL: path)
        refused(EmptyComposerReference(path: link.path, sha256: hash))
        try! FileManager.default.removeItem(at: link)
        try! FileManager.default.linkItem(at: path, to: link)
        refused(reference)
        try! FileManager.default.removeItem(at: link)
        try! Data([137, 80, 78, 71, 13, 10, 26, 10]).write(to: path)
        refused(EmptyComposerReference(path: path.path,
            sha256: SHA256.hash(data: try! Data(contentsOf: path)).map { String(format: "%02x", $0) }.joined()))
    }
}
