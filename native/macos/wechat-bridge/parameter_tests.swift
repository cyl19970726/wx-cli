import AppKit
import Foundation

@main struct ParameterTests {
    static func main() {
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
        precondition(VisualParameters.canonicalNewlines("甲\r\n乙\r丙") == "甲\n乙\n丙")
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
        let title = HeaderOCRCandidate(text: "测试群 (3)", bounds: CGRect(x: 315, y: 19, width: 120, height: 16))
        let sender = HeaderOCRCandidate(text: "Synthetic Peer", bounds: CGRect(x: 320, y: 68, width: 90, height: 15))
        let privateTitle = HeaderOCRCandidate(text: "Synthetic Peer", bounds: CGRect(x: 315, y: 19, width: 100, height: 16))
        precondition(VisualParameters.headerTitleRect(imageWidth: 880, imageHeight: 640) ==
                     CGRect(x: 300, y: 8, width: 420, height: 42))
        precondition(VisualParameters.headerOCRRect(imageWidth: 880, imageHeight: 640) ==
                     CGRect(x: 300, y: 0, width: 420, height: 90))
        precondition(VisualParameters.headerTitleRect(imageWidth: 879, imageHeight: 640) == nil)
        precondition(VisualParameters.uniqueHeaderTitle([title, sender], target: "测试群",
                                                        imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([title, sender], target: "Synthetic Peer",
                                                         imageWidth: 880, imageHeight: 640))
        precondition(VisualParameters.uniqueHeaderTitle([privateTitle, sender], target: "Synthetic Peer",
                                                        imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([privateTitle, privateTitle], target: "Synthetic Peer",
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([privateTitle, title], target: "Synthetic Peer",
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([sender], target: "Synthetic Peer",
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([privateTitle], target: "Synthetic Peer",
                                                         imageWidth: 880, imageHeight: 639))
        let crossingBoundary = HeaderOCRCandidate(text: "Synthetic Peer",
            bounds: CGRect(x: 315, y: 43, width: 100, height: 14))
        let clippedLeftEdge = HeaderOCRCandidate(text: "Synthetic Peer",
            bounds: CGRect(x: 300, y: 19, width: 100, height: 16))
        let clippedTopEdge = HeaderOCRCandidate(text: "Synthetic Peer",
            bounds: CGRect(x: 315, y: 8, width: 100, height: 16))
        precondition(!VisualParameters.uniqueHeaderTitle([crossingBoundary], target: "Synthetic Peer",
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([clippedLeftEdge], target: "Synthetic Peer",
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([clippedTopEdge], target: "Synthetic Peer",
                                                         imageWidth: 880, imageHeight: 640))
        precondition(!VisualParameters.uniqueHeaderTitle([privateTitle, crossingBoundary], target: "Synthetic Peer",
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
}
