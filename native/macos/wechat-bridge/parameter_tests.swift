import AppKit
import Foundation
import CryptoKit
import ImageIO

@main struct ParameterTests {
    static func main() {
        headerBandCalibrationChecks()
        visualTitleWhitespaceChecks()
        emptyComposerChecks()
        imageMediaChecks()
        sendButtonContextChecks()
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
        // The calibrated click stays in the crop, while a nearby toolbar glyph
        // that was OCR text in a 96px crop stays outside it.
        let sendCrop = VisualParameters.sendButtonCrop(at: CGPoint(x: 829, y: 606),
                                                        imageWidth: 880, imageHeight: 640)
        precondition(sendCrop!.contains(CGPoint(x: 829, y: 606)))
        precondition(sendCrop!.contains(CGPoint(x: 817, y: 605)))
        precondition(!sendCrop!.intersects(CGRect(x: 781, y: 599, width: 10, height: 16)))
        precondition(sendCrop == CGRect(x: 795, y: 589, width: 68, height: 34))
        let centeredSend = HeaderOCRCandidate(text: "发送",
            bounds: CGRect(x: 819, y: 601, width: 26, height: 12))
        precondition(VisualParameters.sendLabelAtButton([centeredSend],
            point: CGPoint(x: 829, y: 606), crop: sendCrop!))
        precondition(!VisualParameters.sendLabelAtButton([
            HeaderOCRCandidate(text: "⑨）", bounds: CGRect(x: 781, y: 599, width: 10, height: 16)),
            centeredSend], point: CGPoint(x: 829, y: 606), crop: sendCrop!))
        precondition(!VisualParameters.sendLabelAtButton([centeredSend, centeredSend],
            point: CGPoint(x: 829, y: 606), crop: sendCrop!))
        precondition(!VisualParameters.sendLabelAtButton([
            HeaderOCRCandidate(text: "发送文件", bounds: centeredSend.bounds)],
            point: CGPoint(x: 829, y: 606), crop: sendCrop!))
        precondition(!VisualParameters.sendLabelAtButton([centeredSend],
            point: CGPoint(x: 803, y: 606), crop: sendCrop!))
        precondition(!VisualParameters.sendLabelAtButton([
            HeaderOCRCandidate(text: "发送", bounds: CGRect(x: 859, y: 601, width: 20, height: 12))],
            point: CGPoint(x: 829, y: 606), crop: sendCrop!))
        precondition(VisualParameters.sendButtonCrop(at: CGPoint(x: 10, y: 606),
                                                     imageWidth: 880, imageHeight: 640) == nil)
        precondition(VisualParameters.sendButtonCrop(at: CGPoint(x: 829, y: 632),
                                                     imageWidth: 880, imageHeight: 640) == nil)
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
        precondition(VisualParameters.acceptsFocusSource("current-header", category: .groupHeader,
            kind: .group, headerConfirmations: 2))
        precondition(!VisualParameters.shouldUseCurrentHeader(nil))
        precondition(!VisualParameters.shouldUseCurrentHeader(false))
        precondition(VisualParameters.shouldUseCurrentHeader(true))
        precondition(VisualParameters.acceptsFocusSource("current-header", category: .privateHeader,
            kind: .private, headerConfirmations: 2))
        precondition(!VisualParameters.acceptsFocusSource("current-header", category: .groupHeader,
            kind: .private, headerConfirmations: 2))
        precondition(!VisualParameters.acceptsFocusSource("current-header", category: .groupHeader,
            kind: .group, headerConfirmations: 1))
        precondition(!VisualParameters.acceptsFocusSource("current-header", category: .mostUsed,
            kind: .group, headerConfirmations: 2))
        precondition(!VisualParameters.acceptsFocusSource("search", category: .groupHeader,
            kind: .group, headerConfirmations: 2))
        precondition(!VisualParameters.acceptsFocusSource("search", category: .groupChats,
            kind: .group, headerConfirmations: 2))
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
                     CGRect(x: 300, y: 8, width: 420, height: 36))
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
            bounds: CGRect(x: 315, y: 37, width: 100, height: 14))
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
        // Vision normalizes pixel boundaries; conversion back to pixels can
        // turn exact edge contact into a fraction-of-a-millionth overlap.
        let lowerEdgeContact = HeaderOCRCandidate(text: "Unrelated line",
            bounds: CGRect(x: 570, y: 43.9999998, width: 36, height: 12))
        precondition(VisualParameters.uniqueHeaderTitle([privateTitle, lowerEdgeContact],
            target: "Synthetic Peer", kind: .private, imageWidth: 880, imageHeight: 640))
        precondition(VisualParameters.uniqueHeaderTitle([title, lowerEdgeContact],
            target: "测试群", kind: .group, imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([lowerEdgeContact],
            target: "Unrelated line", kind: .private, imageWidth: 880, imageHeight: 640))
        let realOverlap = HeaderOCRCandidate(text: "Unrelated line",
            bounds: CGRect(x: 570, y: 43.999, width: 36, height: 12))
        precondition(!VisualParameters.uniqueHeaderTitle([privateTitle, realOverlap],
            target: "Synthetic Peer", kind: .private, imageWidth: 880, imageHeight: 640))
        let sideEdgeContact = HeaderOCRCandidate(text: "Unrelated line",
            bounds: CGRect(x: 719.9999998, y: 20, width: 36, height: 12))
        precondition(VisualParameters.uniqueHeaderTitle([privateTitle, sideEdgeContact],
            target: "Synthetic Peer", kind: .private, imageWidth: 880, imageHeight: 640))
        let sideOverlap = HeaderOCRCandidate(text: "Unrelated line",
            bounds: CGRect(x: 719.999, y: 20, width: 36, height: 12))
        precondition(!VisualParameters.uniqueHeaderTitle([privateTitle, sideOverlap],
            target: "Synthetic Peer", kind: .private, imageWidth: 880, imageHeight: 640))
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

    static func headerBandCalibrationChecks() {
        // Saved p06 Vision geometry, with synthetic text in place of private identities.
        let exactTitle = HeaderOCRCandidate(text: "Synthetic Peer",
            bounds: CGRect(x: 314, y: 20, width: 156, height: 18))
        let bodyRow = HeaderOCRCandidate(text: "Unrelated body row",
            bounds: CGRect(x: 443.9355, y: 49.67034, width: 100, height: 14.6593))
        precondition(VisualParameters.uniqueHeaderTitle([exactTitle, bodyRow],
            target: "Synthetic Peer", kind: .private, imageWidth: 880, imageHeight: 640),
            "saved p06 title y20..38 plus body y49.67034 must be unambiguous")
        let crossingRow = HeaderOCRCandidate(text: "Unrelated crossing row",
            bounds: CGRect(x: 443.9355, y: 43.75, width: 100, height: 14.6593))
        precondition(!VisualParameters.uniqueHeaderTitle([exactTitle, crossingRow],
            target: "Synthetic Peer", kind: .private, imageWidth: 880, imageHeight: 640))
        let insetTitle = HeaderOCRCandidate(text: "Synthetic Peer",
            bounds: CGRect(x: 303, y: 11, width: 414, height: 30))
        precondition(VisualParameters.uniqueHeaderTitle([insetTitle],
            target: "Synthetic Peer", kind: .private, imageWidth: 880, imageHeight: 640))
        let clippedBottom = HeaderOCRCandidate(text: "Synthetic Peer",
            bounds: CGRect(x: 314, y: 24, width: 156, height: 18))
        precondition(!VisualParameters.uniqueHeaderTitle([clippedBottom],
            target: "Synthetic Peer", kind: .private, imageWidth: 880, imageHeight: 640))
        print("Header band regression: saved p06 body accepted; y43.75 crossing rejected")
    }

    static func emptyComposerChecks() {
        func image(_ changed: Bool = false, unexpectedDraft: Bool = false) -> CGImage {
            var bytes = [UInt8](repeating: 255, count: 4 * 3 * 4)
            if changed { bytes[0] = 254 }
            if unexpectedDraft { bytes[4] = 253 }
            let provider = CGDataProvider(data: Data(bytes) as CFData)!
            return CGImage(width: 4, height: 3, bitsPerComponent: 8, bitsPerPixel: 32,
                bytesPerRow: 16, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
                provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)!
        }
        let original = image(), rect = CGRect(x: 0, y: 0, width: 4, height: 3)
        precondition(VisualParameters.matchesEmptyComposer(original, rect: rect, reference: original))
        precondition(!VisualParameters.matchesEmptyComposer(image(true), rect: rect, reference: original))
        let indicator = image(true)
        precondition(VisualParameters.matchesEmptyComposer(indicator, rect: rect, references: [original, indicator]))
        precondition(!VisualParameters.matchesEmptyComposer(indicator, rect: rect, references: [original]))
        precondition(!VisualParameters.matchesEmptyComposer(image(true, unexpectedDraft: true),
            rect: rect, references: [original, indicator]))
        precondition(!VisualParameters.matchesEmptyComposer(original, rect: rect, references: []))
        precondition(!VisualParameters.matchesEmptyComposer(original, rect: rect,
            references: [original, indicator, original, indicator]))
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

    static func sendButtonContextChecks() {
        let point = CGPoint(x: 829, y: 606)
        let composer = CGRect(x: 308, y: 497, width: 559, height: 93)
        let button = HeaderOCRCandidate(text: "发送",
            bounds: CGRect(x: 819, y: 601, width: 26, height: 12))
        func accepts(_ wide: [HeaderOCRCandidate], _ narrow: [HeaderOCRCandidate]? = nil,
                     composerRect: CGRect? = nil) -> Bool {
            VisualParameters.sendButtonConfirmed(wideLabels: wide, buttonLabels: narrow ?? [button],
                point: point, composerRect: composerRect ?? composer, imageWidth: 880, imageHeight: 640)
        }
        precondition(accepts([])) // Wide OCR may miss or misread the real button.
        precondition(accepts([HeaderOCRCandidate(text: "Other", bounds: button.bounds)]))
        precondition(accepts([button]))
        let draft = HeaderOCRCandidate(text: "发送",
            bounds: CGRect(x: 330, y: 548, width: 26, height: 12))
        precondition(accepts([draft]))
        precondition(accepts([draft, button]))
        precondition(accepts([HeaderOCRCandidate(text: "Send", bounds: draft.bounds)]))
        precondition(!accepts([draft, button], composerRect:
            CGRect(x: 308, y: 497, width: 559, height: 130)))
        // A crop may overlap the composer while its click and label retain
        // distinct button roles. The real calibration overlaps by one pixel.
        precondition(VisualParameters.sendButtonCrop(at: point, imageWidth: 880, imageHeight: 640)!
            .intersects(composer))
        precondition(accepts([]))
        // Broad OCR missing the real button must not turn draft text into one.
        precondition(!VisualParameters.sendButtonConfirmed(wideLabels: [], buttonLabels: [draft],
            point: CGPoint(x: 343, y: 554), composerRect: composer, imageWidth: 880, imageHeight: 640))
        // Exercise each role fence independently, within the original center tolerances.
        precondition(!VisualParameters.sendButtonConfirmed(wideLabels: [], buttonLabels: [
            HeaderOCRCandidate(text: "发送", bounds: CGRect(x: 819, y: 591, width: 26, height: 8))],
            point: CGPoint(x: 829, y: 588), composerRect: composer, imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.sendButtonConfirmed(wideLabels: [], buttonLabels: [
            HeaderOCRCandidate(text: "发送", bounds: CGRect(x: 819, y: 580, width: 26, height: 8))],
            point: CGPoint(x: 829, y: 592), composerRect: composer, imageWidth: 880, imageHeight: 640))
        // Full boxes must fit a declared region, not just intersect its edge.
        for bounds in [CGRect(x: 700, y: 605, width: 26, height: 12),
                       CGRect(x: 330, y: 584, width: 26, height: 12),
                       CGRect(x: 786, y: 601, width: 26, height: 12),
                       CGRect(x: 330, y: 532, width: 26, height: 12),
                       CGRect(x: 870, y: 601, width: 26, height: 12),
                       CGRect(x: 330, y: 548, width: -26, height: 12),
                       CGRect(x: CGFloat.nan, y: 548, width: 26, height: 12)] {
            precondition(!accepts([HeaderOCRCandidate(text: "Send", bounds: bounds)]))
        }
        precondition(!accepts([], []))
        precondition(!accepts([], [button, button]))
        precondition(!accepts([], [button, HeaderOCRCandidate(text: "Send", bounds: button.bounds)]))
        precondition(!accepts([], [HeaderOCRCandidate(text: "发送文件", bounds: button.bounds)]))
        precondition(!accepts([], [HeaderOCRCandidate(text: "Send now", bounds: button.bounds)]))
        precondition(!accepts([], [HeaderOCRCandidate(text: "发送",
            bounds: CGRect(x: 859, y: 601, width: 20, height: 12))]))
        precondition(!accepts([], [HeaderOCRCandidate(text: "发送",
            bounds: CGRect(x: 795, y: 601, width: 12, height: 12))]))
        precondition(!accepts([], [HeaderOCRCandidate(text: "发送",
            bounds: CGRect(x: 819, y: 601, width: 0, height: 12))]))
        precondition(!VisualParameters.sendButtonConfirmed(wideLabels: [], buttonLabels: [button],
            point: CGPoint(x: 10, y: 606), composerRect: composer, imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.sendLabelAtButton([button], point: CGPoint(x: 780, y: 606),
            crop: VisualParameters.sendButtonCrop(at: point, imageWidth: 880, imageHeight: 640)!))
        precondition(!accepts([], composerRect: CGRect(x: 308, y: 497, width: 600, height: 93)))
    }

    static func imageMediaChecks() {
        let width = 4, height = 3
        var bytes = [UInt8](repeating: 255, count: width * height * 4)
        bytes[0] = 14
        bytes[4] = 67
        let provider = CGDataProvider(data: Data(bytes) as CFData)!
        let image = CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32,
            bytesPerRow: width * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!,
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)!
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try! FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
            attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        let path = directory.appendingPathComponent("source.png")
        let png = try! MediaSupport.pngData(image)
        try! png.write(to: path)
        try! FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        let input = ImageMedia(path: path.path, sha256: MediaSupport.digest(png),
                               mimeType: "image/png", sizeBytes: png.count)
        let loaded = try! MediaSupport.load(input)
        precondition(loaded.pixels.width == width && loaded.pixels.height == height)
        precondition(loaded.pixels == (try! MediaSupport.decode(png, allowedTypes: ["public.png"])).1)
        imageCopyReplacementChecks(loaded: loaded, png: png)
        let jpegBuffer = NSMutableData()
        let jpegDestination = CGImageDestinationCreateWithData(jpegBuffer, "public.jpeg" as CFString, 1, nil)!
        CGImageDestinationAddImage(jpegDestination, image, nil)
        precondition(CGImageDestinationFinalize(jpegDestination))
        let jpeg = jpegBuffer as Data
        let jpegPath = directory.appendingPathComponent("source.jpg")
        try! jpeg.write(to: jpegPath)
        try! FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: jpegPath.path)
        let jpegInput = ImageMedia(path: jpegPath.path, sha256: MediaSupport.digest(jpeg),
                                   mimeType: "image/jpeg", sizeBytes: jpeg.count)
        let loadedJPEG = try! MediaSupport.load(jpegInput)
        precondition(loadedJPEG.pixels.width == width && loadedJPEG.pixels.height == height)
        let rotatedBuffer = NSMutableData()
        let rotatedDestination = CGImageDestinationCreateWithData(rotatedBuffer, "public.jpeg" as CFString, 1, nil)!
        CGImageDestinationAddImage(rotatedDestination, image,
            [kCGImagePropertyOrientation: 6] as CFDictionary)
        precondition(CGImageDestinationFinalize(rotatedDestination))
        let rotatedJPEG = rotatedBuffer as Data
        let rotatedPath = directory.appendingPathComponent("rotated.jpg")
        try! rotatedJPEG.write(to: rotatedPath)
        try! FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: rotatedPath.path)
        let rotatedInput = ImageMedia(path: rotatedPath.path,
            sha256: MediaSupport.digest(rotatedJPEG), mimeType: "image/jpeg",
            sizeBytes: rotatedJPEG.count)
        let loadedRotated = try! MediaSupport.load(rotatedInput)
        precondition(loadedRotated.pixels.width == height && loadedRotated.pixels.height == width)
        let normalizedPNG = try! MediaSupport.pngData(loadedRotated.image)
        precondition((try! MediaSupport.decode(normalizedPNG, allowedTypes: ["public.png"])).1 ==
                     loadedRotated.pixels)
        func refused(_ candidate: ImageMedia) {
            do { _ = try MediaSupport.load(candidate); preconditionFailure("unsafe image source accepted") }
            catch {}
        }
        refused(ImageMedia(path: input.path, sha256: String(repeating: "0", count: 64),
                           mimeType: input.mimeType, sizeBytes: input.sizeBytes))
        refused(ImageMedia(path: input.path, sha256: input.sha256,
                           mimeType: "image/jpeg", sizeBytes: input.sizeBytes))
        refused(ImageMedia(path: input.path, sha256: input.sha256,
                           mimeType: input.mimeType, sizeBytes: input.sizeBytes + 1))
        try! FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: path.path)
        refused(input)
        try! FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        let fifo = directory.appendingPathComponent("blocked-source.png")
        precondition(mkfifo(fifo.path, 0o600) == 0)
        refused(ImageMedia(path: fifo.path, sha256: input.sha256,
                           mimeType: input.mimeType, sizeBytes: input.sizeBytes))
        let link = directory.appendingPathComponent("link.png")
        try! FileManager.default.createSymbolicLink(at: link, withDestinationURL: path)
        refused(ImageMedia(path: link.path, sha256: input.sha256,
                           mimeType: input.mimeType, sizeBytes: input.sizeBytes))
        let board = NSPasteboard(name: NSPasteboard.Name("codex-image-tests-" + UUID().uuidString))
        board.clearContents()
        precondition(board.setString("original clipboard", forType: .string))
        let snapshot = try! ClipboardSnapshot.capture(board: board)
        let owned = try! setClipboardImage(loaded, snapshot: snapshot)
        precondition(try! MediaSupport.copiedImage(from: board, expected: loaded.pixels) != "")
        let wrong = ImagePixels(width: width, height: height, sha256: String(repeating: "0", count: 64))
        do {
            _ = try MediaSupport.copiedImage(from: board, expected: wrong)
            preconditionFailure("wrong composer pixels accepted")
        } catch {}
        try! snapshot.restore(ifCurrentCount: owned)
        precondition(board.string(forType: .string) == "original clipboard")
        let jpegSnapshot = try! ClipboardSnapshot.capture(board: board)
        let jpegOwned = try! setClipboardImage(loadedJPEG, snapshot: jpegSnapshot)
        precondition(try! MediaSupport.copiedImage(from: board, expected: loadedJPEG.pixels) != "")
        try! jpegSnapshot.restore(ifCurrentCount: jpegOwned)
        precondition(board.string(forType: .string) == "original clipboard")
        let textOnly = NSPasteboard(name: NSPasteboard.Name("codex-image-text-" + UUID().uuidString))
        textOnly.clearContents()
        precondition(textOnly.setString("image label", forType: .string))
        do {
            _ = try MediaSupport.copiedImage(from: textOnly, expected: loaded.pixels)
            preconditionFailure("text copy accepted as image evidence")
        } catch {}
        func acceptWeChatMixed(_ type: NSPasteboard.PasteboardType, _ data: Data) {
            let mixed = NSPasteboard(name: NSPasteboard.Name("codex-image-mixed-" + UUID().uuidString))
            mixed.clearContents()
            let item = NSPasteboardItem()
            precondition(item.setData(png, forType: .png))
            precondition(item.setData(data, forType: type))
            precondition(mixed.writeObjects([item]))
            precondition(try! MediaSupport.copiedImage(from: mixed, expected: loaded.pixels) == "public.png")
        }
        acceptWeChatMixed(NSPasteboard.PasteboardType("public.file-url"), Data("file:///tmp/other".utf8))
        acceptWeChatMixed(.rtf, Data("{\\rtf1 extra}".utf8))
        let multipage = NSMutableData()
        let multipageDestination = CGImageDestinationCreateWithData(multipage, "public.tiff" as CFString, 2, nil)!
        CGImageDestinationAddImage(multipageDestination, image, nil)
        CGImageDestinationAddImage(multipageDestination, image, nil)
        precondition(CGImageDestinationFinalize(multipageDestination))
        do {
            _ = try MediaSupport.decode(multipage as Data, allowedTypes: ["public.tiff"])
            preconditionFailure("multi-frame image accepted")
        } catch {}
        try! Data([137, 80, 78, 71, 13, 10, 26, 10]).write(to: path)
        refused(ImageMedia(path: path.path,
                           sha256: MediaSupport.digest(try! Data(contentsOf: path)),
                           mimeType: "image/png", sizeBytes: 8))
    }

    static func visualTitleWhitespaceChecks() {
        let target = "咸蛋AI - AI落地企业"
        let observed = "咸蛋 AI- AI落地企业"
        let heading = SearchOCRCandidate(text: "群聊",
            bounds: CGRect(x: 15, y: 55, width: 60, height: 18))
        let row = SearchOCRCandidate(text: observed,
            bounds: CGRect(x: 65, y: 83, width: 230, height: 18))
        let size = CGSize(width: 320, height: 300)
        precondition(VisualParameters.exactGroupTitle(observed, target: target),
            "Visual titles must tolerate only whitespace insertion/deletion")
        precondition(VisualParameters.exactGroupTitle(target, target: observed))
        precondition(VisualParameters.uniqueSearchResult([heading, row], target: target,
            kind: .group, popupSize: size) == SearchSelection(row: row, category: .groupChats))
        let title = HeaderOCRCandidate(text: observed + " (12)",
            bounds: CGRect(x: 315, y: 19, width: 280, height: 16))
        precondition(VisualParameters.uniqueHeaderTitle([title], target: target, kind: .group,
            imageWidth: 880, imageHeight: 640))
        let duplicate = SearchOCRCandidate(text: target,
            bounds: row.bounds.offsetBy(dx: 0, dy: 35))
        precondition(VisualParameters.uniqueSearchResult([heading, row, duplicate], target: target,
            kind: .group, popupSize: size) == nil)
        precondition(!VisualParameters.uniqueHeaderTitle([title, title], target: target, kind: .group,
            imageWidth: 880, imageHeight: 640))
        let groupRows = [GroupOCRCandidate(text: observed, x: 180, y: 100),
                         GroupOCRCandidate(text: target, x: 180, y: 140)]
        precondition(VisualParameters.uniqueExactGroup(groupRows, target: target, minimumY: 55) == nil)
        precondition(VisualParameters.uniqueSearchResult([heading, row], target: target,
            kind: .private, popupSize: size) == nil)
        for section in ["Contacts", "Search the Web", "Chat history", "Unrecognized section"] {
            let otherHeading = SearchOCRCandidate(text: section, bounds: heading.bounds)
            precondition(VisualParameters.uniqueSearchResult([otherHeading, row], target: target,
                kind: .group, popupSize: size) == nil)
        }
        let contactHeading = SearchOCRCandidate(text: "Contacts", bounds: heading.bounds)
        let groupHeading = SearchOCRCandidate(text: "群聊", bounds: heading.bounds.offsetBy(dx: 0, dy: 75))
        let groupRow = SearchOCRCandidate(text: target, bounds: row.bounds.offsetBy(dx: 0, dy: 75))
        for kind in [TargetKind.group, .private] {
            precondition(VisualParameters.uniqueSearchResult([contactHeading, row, groupHeading, groupRow],
                target: target, kind: kind, popupSize: size) == nil)
        }
        // Cover Unicode White_Space, including nonbreaking and ideographic spaces.
        for code: UInt32 in [0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0x85, 0xA0, 0x1680,
                            0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006,
                            0x2007, 0x2008, 0x2009, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000] {
            let space = String(UnicodeScalar(code)!)
            let spaced = "咸蛋" + space + "AI-" + space + "AI落地企业"
            precondition(VisualParameters.exactGroupTitle(spaced, target: target))
            precondition(VisualParameters.exactGroupTitle(target, target: spaced))
            precondition(VisualParameters.exactHeaderTitle(spaced + "（１２）", target: target, kind: .group))
            precondition(VisualParameters.exactHeaderTitle(spaced, target: target, kind: .private))
        }
        for altered in ["咸蛋AI – AI落地企业", "咸蛋AI AI落地企业", "咸蛋AI - AI落地企亚",
                        "咸蛋ai - AI落地企业", "咸蛋AI - AI落地企业二", "前咸蛋AI - AI落地企业",
                        "咸蛋\u{200B}AI - AI落地企业", "咸蛋\u{FEFF}AI - AI落地企业"] {
            precondition(!VisualParameters.exactGroupTitle(altered, target: target))
            precondition(!VisualParameters.exactHeaderTitle(altered + " (12)", target: target, kind: .group))
            precondition(!VisualParameters.exactHeaderTitle(altered, target: target, kind: .private))
        }
        precondition(!VisualParameters.exactHeaderTitle(observed, target: target, kind: .group))
        precondition(VisualParameters.exactHeaderTitle(observed, target: target, kind: .private))
        for suffix in [" (12)", "（１２）"] {
            precondition(!VisualParameters.exactHeaderTitle(observed + suffix, target: target, kind: .private))
            precondition(VisualParameters.exactHeaderTitle(observed + suffix,
                target: target + suffix, kind: .private))
            precondition(!VisualParameters.exactHeaderTitle(observed + suffix,
                target: target + " (13)", kind: .private))
        }
        for suffix in [" ()", " (abc)", " [12]", " (12）", " (12) extra", " (12)(3)"] {
            precondition(!VisualParameters.exactGroupTitle(observed + suffix, target: target))
            precondition(!VisualParameters.exactHeaderTitle(observed + suffix, target: target, kind: .group))
        }
        precondition(!VisualParameters.sameTransactionText(text: "text", draft: "draft", group: target,
            otherText: "text", otherDraft: "draft", otherGroup: observed))
        print("Visual title whitespace and identity boundary checks passed")
    }

    static func imageCopyReplacementChecks(loaded: LoadedImage, png: Data) {
        let board = NSPasteboard(name: NSPasteboard.Name("codex-image-copy-proof-" + UUID().uuidString))
        board.clearContents()
        precondition(board.setString("original", forType: .string))
        let snapshot = try! ClipboardSnapshot.capture(board: board)
        let sentinel = "private-image-sentinel"
        let owned = try! setClipboardString(sentinel, snapshot: snapshot)

        func refused(_ ownedCount: Int, _ pixels: ImagePixels = loaded.pixels) {
            do {
                _ = try copiedImageReplacingSentinel(from: board, ownedCount: ownedCount,
                    sentinel: sentinel, expected: pixels)
                preconditionFailure("unsafe image Copy evidence accepted")
            } catch {}
        }
        func imageItem() -> NSPasteboardItem {
            let item = NSPasteboardItem()
            precondition(item.setData(png, forType: .png))
            return item
        }

        refused(owned) // unchanged sentinel
        board.clearContents()
        precondition(board.setString(sentinel, forType: .string))
        refused(owned) // count advanced, sentinel still present
        board.clearContents()
        precondition(board.writeObjects([imageItem()]))
        let copiedCount = board.changeCount
        precondition(copiedCount == owned + 2)
        let (type, provenCount) = try! copiedImageReplacingSentinel(
            from: board, ownedCount: owned, sentinel: sentinel, expected: loaded.pixels)
        precondition(type == "public.png" && provenCount == copiedCount)
        refused(copiedCount) // unchanged ownership
        refused(copiedCount + 1) // backwards ownership
        refused(owned, ImagePixels(width: loaded.pixels.width, height: loaded.pixels.height,
            sha256: String(repeating: "0", count: 64)))

        let sentinelImage = imageItem()
        precondition(sentinelImage.setString(sentinel, forType: .string))
        board.clearContents()
        precondition(board.writeObjects([sentinelImage]))
        refused(owned) // raster present, but sentinel is still advertised

        board.clearContents()
        precondition(board.writeObjects([imageItem(), imageItem()]))
        refused(owned) // multiple image items
        board.clearContents()
        precondition(board.setString("not raster", forType: .string))
        refused(owned) // no image bytes
        board.clearContents()
        precondition(board.setString("file:///tmp/image.png", forType: .fileURL))
        refused(owned) // a file URL is not decoded image evidence

        board.clearContents()
        precondition(board.writeObjects([imageItem()]))
        let stableCount = try! copiedImageReplacingSentinel(from: board, ownedCount: owned,
            sentinel: sentinel, expected: loaded.pixels).1
        board.clearContents()
        precondition(board.setString("external change", forType: .string))
        do {
            try snapshot.restore(ifCurrentCount: stableCount)
            preconditionFailure("external clipboard change was overwritten")
        } catch {}
        precondition(board.string(forType: .string) == "external change")
    }
}
