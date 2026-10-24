import AppKit
import Foundation

struct ClipboardFailure: Error, CustomStringConvertible {
    let description: String
    init(_ message: String) { description = message }
}

struct ClipboardSnapshot {
    let items: [[(NSPasteboard.PasteboardType, Data)]]
    let changeCount: Int
    let board: NSPasteboard

    static func capture(board: NSPasteboard = .general) throws -> ClipboardSnapshot {
        let count = board.changeCount
        var saved: [[(NSPasteboard.PasteboardType, Data)]] = []
        var byteCount = 0
        let sourceItems = board.pasteboardItems
        guard sourceItems != nil || (board.types ?? []).isEmpty else {
            throw ClipboardFailure("Clipboard has types but its items are unavailable")
        }
        for item in sourceItems ?? [] {
            guard !item.types.isEmpty else { throw ClipboardFailure("Clipboard item has no readable types") }
            var savedItem: [(NSPasteboard.PasteboardType, Data)] = []
            for type in item.types {
                guard let data = item.data(forType: type) else { throw ClipboardFailure("Cannot snapshot a clipboard item") }
                byteCount += data.count
                guard byteCount <= 16_000_000 else { throw ClipboardFailure("Clipboard snapshot exceeds 16 MB") }
                savedItem.append((type, data))
            }
            saved.append(savedItem)
        }
        guard board.changeCount == count else { throw ClipboardFailure("Clipboard changed during snapshot") }
        return ClipboardSnapshot(items: saved, changeCount: count, board: board)
    }

    func restore(ifCurrentCount count: Int) throws {
        guard VisualParameters.mayRestoreClipboard(ownedCount: count, currentCount: board.changeCount) else {
            throw ClipboardFailure("Clipboard changed externally; preserving its current contents")
        }
        var objects: [NSPasteboardItem] = []
        for saved in items {
            let item = NSPasteboardItem()
            for (type, data) in saved {
                guard item.setData(data, forType: type) else { throw ClipboardFailure("Cannot rebuild clipboard item") }
            }
            objects.append(item)
        }
        guard board.changeCount == count else { throw ClipboardFailure("Clipboard changed before restoration") }
        board.clearContents()
        let clearedCount = board.changeCount
        if !objects.isEmpty {
            guard board.changeCount == clearedCount else { throw ClipboardFailure("Clipboard changed before item restoration") }
            guard board.writeObjects(objects) else { throw ClipboardFailure("Could not restore original clipboard") }
        }
    }
}

func setClipboardString(_ value: String, snapshot: ClipboardSnapshot) throws -> Int {
    let board = snapshot.board
    guard board.changeCount == snapshot.changeCount else { throw ClipboardFailure("Clipboard changed before helper write") }
    board.clearContents()
    let clearedCount = board.changeCount
    guard board.changeCount == clearedCount else { throw ClipboardFailure("Clipboard changed before temporary text write") }
    guard board.setString(value, forType: .string) else {
        try? snapshot.restore(ifCurrentCount: clearedCount)
        throw ClipboardFailure("Could not set temporary plain text clipboard")
    }
    return board.changeCount
}

func setClipboardImage(_ image: LoadedImage, snapshot: ClipboardSnapshot) throws -> Int {
    let board = snapshot.board
    guard board.changeCount == snapshot.changeCount else {
        throw ClipboardFailure("Clipboard changed before image write")
    }
    // Both representations contain the already validated decoded image. A
    // file URL would invite the receiver to open a path after our validation.
    let png = try MediaSupport.pngData(image.image)
    guard png.count <= MediaSupport.maximumBytes * 12,
          let tiff = NSBitmapImageRep(cgImage: image.image).tiffRepresentation,
          tiff.count <= MediaSupport.maximumBytes * 12 else {
        throw ClipboardFailure("Native image representation is too large")
    }
    let item = NSPasteboardItem()
    guard item.setData(png, forType: .png), item.setData(tiff, forType: .tiff) else {
        throw ClipboardFailure("Cannot create native image pasteboard item")
    }
    board.clearContents()
    let clearedCount = board.changeCount
    guard board.changeCount == clearedCount, board.writeObjects([item]) else {
        try? snapshot.restore(ifCurrentCount: clearedCount)
        throw ClipboardFailure("Could not write native image pasteboard item")
    }
    do {
        _ = try MediaSupport.copiedImage(from: board, expected: image.pixels)
    } catch {
        try? snapshot.restore(ifCurrentCount: board.changeCount)
        throw error
    }
    return board.changeCount
}
