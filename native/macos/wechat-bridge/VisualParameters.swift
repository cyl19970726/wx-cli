import Foundation
import CoreGraphics
import CryptoKit
import ImageIO

struct EmptyComposerReference: Codable, Equatable {
    let path: String
    let sha256: String

    enum Invalid: Error { case reference }

    func load(in directory: URL, rect: CGRect) throws -> CGImage {
        let url = URL(fileURLWithPath: path)
        guard path.hasPrefix("/"), url.standardizedFileURL.path == path,
              url.deletingLastPathComponent().path == directory.path, url.pathExtension == "png",
              rect == rect.integral, rect.width > 0, rect.height > 0,
              rect.width * rect.height <= 1_048_576,
              sha256.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil else {
            throw Invalid.reference
        }
        let fd = open(path, O_RDONLY | O_NOFOLLOW)
        guard fd >= 0 else { throw Invalid.reference }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close() }
        var info = stat()
        guard fstat(fd, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == geteuid(), info.st_mode & 0o777 == 0o600,
              info.st_nlink == 1, info.st_size <= 2 * 1024 * 1024,
              let data = try handle.read(upToCount: 2 * 1024 * 1024 + 1),
              data.count <= 2 * 1024 * 1024,
              data.starts(with: [137, 80, 78, 71, 13, 10, 26, 10]),
              SHA256.hash(data: data).map({ String(format: "%02x", $0) }).joined() == sha256,
              let source = CGImageSourceCreateWithData(data as CFData, nil),
              CGImageSourceGetCount(source) == 1,
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              properties[kCGImagePropertyPixelWidth] as? Int == Int(rect.width),
              properties[kCGImagePropertyPixelHeight] as? Int == Int(rect.height),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            throw Invalid.reference
        }
        return image
    }
}

struct GroupOCRCandidate: Equatable {
    let text: String
    let x: Double
    let y: Double
}

struct HeaderOCRCandidate: Equatable {
    let text: String
    let bounds: CGRect
}

struct SearchOCRCandidate: Equatable {
    let text: String
    let bounds: CGRect
}

enum TargetKind: String, Codable {
    case group
    case `private`
}

enum SearchCategory: String, Codable {
    case contacts
    case groupChats = "group-chats"
    case mostUsed = "most-used"

    func accepts(_ kind: TargetKind) -> Bool {
        self == .mostUsed || (self == .contacts && kind == .private) ||
            (self == .groupChats && kind == .group)
    }
}

struct SearchSelection: Equatable {
    let row: SearchOCRCandidate
    let category: SearchCategory
}

enum SearchPopupDisposition {
    case stable
    case pollAgain
    case refuse
}

struct FocusWindowDiagnostic: Codable {
    let id: UInt32
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct FocusPollDiagnostic: Codable {
    let elapsedMs: Int
    let windowCount: Int
    let windows: [FocusWindowDiagnostic]
    let popupPresent: Bool
    let popupSelected: Bool
}

enum VisualParameters {
    static func sendButtonCrop(at point: CGPoint, imageWidth: Int, imageHeight: Int) -> CGRect? {
        guard point.x.isFinite, point.y.isFinite else { return nil }
        let crop = CGRect(x: point.x - 34, y: point.y - 17, width: 68, height: 34)
        guard crop == crop.integral, crop.minX >= 0, crop.minY >= 0,
              crop.maxX <= CGFloat(imageWidth), crop.maxY <= CGFloat(imageHeight) else { return nil }
        return crop
    }

    static func uniqueSendLabel(_ labels: [String], atButton: Bool) -> Bool {
        let matches = labels.filter { ["Send", "发送"].contains($0) }
        return matches.count == 1 && (!atButton || labels.count == 1)
    }

    static func sendLabelAtButton(_ labels: [HeaderOCRCandidate], point: CGPoint,
                                  crop: CGRect) -> Bool {
        guard labels.count == 1, uniqueSendLabel(labels.map(\.text), atButton: true),
              crop.contains(point), crop.contains(labels[0].bounds),
              labels[0].bounds.width > 0, labels[0].bounds.height > 0 else { return false }
        let center = CGPoint(x: labels[0].bounds.midX, y: labels[0].bounds.midY)
        return abs(center.x - point.x) <= 16 && abs(center.y - point.y) <= 10
    }

    // Compare every calibrated composer pixel, including guidance and any draft.
    // There is no text whitelist, tolerance, caret mask or partial-region match.
    static func matchesEmptyComposer(_ image: CGImage, rect: CGRect, reference: CGImage) -> Bool {
        guard rect == rect.integral, rect.minX >= 0, rect.minY >= 0,
              rect.maxX <= CGFloat(image.width), rect.maxY <= CGFloat(image.height),
              rect.width == CGFloat(reference.width), rect.height == CGFloat(reference.height),
              rect.width * rect.height <= 1_048_576,
              let crop = image.cropping(to: rect) else { return false }
        func pixels(_ image: CGImage) -> [UInt8]? {
            var bytes = [UInt8](repeating: 0, count: image.width * image.height * 4)
            let drawn = bytes.withUnsafeMutableBytes { buffer -> Bool in
                guard let space = CGColorSpace(name: CGColorSpace.sRGB),
                      let context = CGContext(data: buffer.baseAddress, width: image.width,
                        height: image.height, bitsPerComponent: 8, bytesPerRow: image.width * 4,
                        space: space, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
                context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
                return true
            }
            return drawn ? bytes : nil
        }
        guard let current = pixels(crop), let expected = pixels(reference) else { return false }
        return current == expected
    }

    static func withinFocusSearchBudget(_ elapsed: TimeInterval) -> Bool {
        elapsed.isFinite && elapsed >= 0 && elapsed < 6
    }

    // Search results can resize or replace their popup while loading. An
    // observation may wait for another capture, but the pre-click confirmation
    // must reject any change. Both windows have already passed navigation guards.
    static func searchPopupDisposition(capturedID: UInt32, capturedFrame: CGRect,
                                       currentID: UInt32?, currentFrame: CGRect?,
                                       beforeClick: Bool) -> SearchPopupDisposition {
        if currentID == capturedID, let frame = currentFrame,
           abs(frame.minX - capturedFrame.minX) <= 3,
           abs(frame.minY - capturedFrame.minY) <= 3,
           abs(frame.width - capturedFrame.width) <= 3,
           abs(frame.height - capturedFrame.height) <= 3 {
            return .stable
        }
        return beforeClick ? .refuse : .pollAgain
    }

    // Calibrated against the 880x640 WeChat window. The title sits near y=27;
    // the message pane starts below y=50 (a sender was observed at y=75).
    static func headerTitleRect(imageWidth: Int, imageHeight: Int) -> CGRect? {
        guard imageWidth == 880, imageHeight == 640 else { return nil }
        return CGRect(x: 300, y: 8, width: 420, height: 42)
    }

    // OCR includes pixels below the title band so crossing text has an observable full box.
    static func headerOCRRect(imageWidth: Int, imageHeight: Int) -> CGRect? {
        guard imageWidth == 880, imageHeight == 640 else { return nil }
        return CGRect(x: 300, y: 0, width: 420, height: 90)
    }

    static func uniqueHeaderTitle(_ candidates: [HeaderOCRCandidate], target: String,
                                  kind: TargetKind,
                                  imageWidth: Int, imageHeight: Int) -> Bool {
        guard let rect = headerTitleRect(imageWidth: imageWidth, imageHeight: imageHeight) else {
            return false
        }
        let overlapping = candidates.filter { $0.bounds.intersects(rect) }
        guard overlapping.count == 1 else { return false }
        let candidate = overlapping[0]
        let inside = rect.insetBy(dx: 3, dy: 3)
        guard !candidate.bounds.isEmpty,
              candidate.bounds.minX >= inside.minX,
              candidate.bounds.maxX <= inside.maxX,
              candidate.bounds.minY >= inside.minY,
              candidate.bounds.maxY <= inside.maxY else { return false }
        return exactHeaderTitle(candidate.text, target: target, kind: kind)
    }

    static func exactHeaderTitle(_ value: String, target: String, kind: TargetKind) -> Bool {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if kind == .private { return trimmed == target }
        let pattern = "^" + NSRegularExpression.escapedPattern(for: target) +
            "(?:\\s*\\([0-9０-９]+\\)|\\s*（[0-9０-９]+）)$"
        return trimmed.range(of: pattern, options: .regularExpression) != nil
    }

    static func exactGroupTitle(_ value: String, target: String) -> Bool {
        let pattern = "^" + NSRegularExpression.escapedPattern(for: target) +
            "(?:\\s*\\([0-9０-９]+\\)|\\s*（[0-9０-９]+）)?$"
        return value.trimmingCharacters(in: .whitespacesAndNewlines)
            .range(of: pattern, options: .regularExpression) != nil
    }

    static func uniqueExactGroup(_ candidates: [GroupOCRCandidate], target: String,
                                 minimumY: Double) -> GroupOCRCandidate? {
        let matches = candidates.filter { $0.y >= minimumY && exactGroupTitle($0.text, target: target) }
        return matches.count == 1 ? matches[0] : nil
    }

    // The list's timestamp and preview are outside this column. OCR over the
    // entire list can join a title and timestamp into one false candidate.
    static func conversationNameRect(_ list: CGRect) -> CGRect? {
        guard list.width >= 180, list.height >= 100 else { return nil }
        let left = list.minX + min(50, list.width * 0.22)
        let right = min(list.maxX - 15, 255)
        guard right - left >= 90 else { return nil }
        return CGRect(x: left, y: list.minY, width: right - left, height: list.height)
    }

    static func plausibleSearchPopup(_ popup: CGRect, main: CGRect, search: CGPoint) -> Bool {
        let globalSearch = CGPoint(x: main.minX + search.x, y: main.minY + search.y)
        return popup.width >= 300 && popup.width <= min(500, main.width * 0.6) &&
            popup.height >= 160 && popup.height <= main.height &&
            popup.minX >= main.minX - 5 && popup.minX <= globalSearch.x + 45 &&
            popup.maxX <= main.minX + main.width * 0.65 &&
            popup.minY >= globalSearch.y - 15 && popup.minY <= globalSearch.y + 90 &&
            // The separate search window can extend a few pixels past the
            // main window's bottom edge (observed 9 px on the tall popup).
            popup.maxY <= main.maxY + 16
    }

    static func uniqueSearchResult(_ candidates: [SearchOCRCandidate], target: String,
                                   kind: TargetKind,
                                   popupSize: CGSize) -> SearchSelection? {
        let ordered = candidates.sorted { $0.bounds.midY < $1.bounds.midY }
        var sections: [(Int, SearchCategory?)] = ordered.enumerated().compactMap { index, candidate in
            let value = candidate.text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            let contacts = ["contacts", "联系人"]
            let groups = ["group chats", "群聊", "群组"]
            let common = ["most used", "常用", "最常使用"]
            let rejected = ["web", "search the web", "internet search results", "网络搜索",
                            "搜索网络", "网页", "moments", "朋友圈", "official accounts",
                            "公众号", "chat history", "聊天记录", "more", "更多"]
            if contacts.contains(value) { return (index, .contacts) }
            if groups.contains(value) { return (index, .groupChats) }
            if common.contains(value) { return (index, .mostUsed) }
            if rejected.contains(value) { return (index, nil) }
            return nil
        }
        guard !sections.isEmpty else { return nil }
        let recognizedSections = sections
        // A misread heading must not extend a recognized category into history
        // or web results. Text aligned with the heading column closes the
        // section; only an exact recognized heading can open an eligible one.
        for (index, candidate) in ordered.enumerated()
            where !recognizedSections.contains(where: { $0.0 == index }) {
            if recognizedSections.contains(where: { section in
                let heading = ordered[section.0].bounds
                return candidate.bounds.height > 0 && heading.height > 0 &&
                    abs(candidate.bounds.minX - heading.minX) <=
                        min(candidate.bounds.height, heading.height) / 2
            }) {
                sections.append((index, nil))
            }
        }
        sections.sort { $0.0 < $1.0 }
        let matches = ordered.enumerated().compactMap { index, candidate -> SearchSelection? in
            guard exactGroupTitle(candidate.text, target: target),
                  candidate.bounds.minX >= 8, candidate.bounds.maxX <= popupSize.width - 8,
                  candidate.bounds.minY >= 0, candidate.bounds.maxY <= popupSize.height,
                  let section = sections.last(where: { $0.0 < index }),
                  let category = section.1,
                  candidate.bounds.minY >= ordered[section.0].bounds.maxY + 3 else { return nil }
            return SearchSelection(row: candidate, category: category)
        }
        guard matches.count == 1, let match = matches.first,
              match.category.accepts(kind),
              let section = sections.last(where: { ordered[$0.0].bounds.midY < match.row.bounds.midY }),
              match.row.bounds.minY - ordered[section.0].bounds.maxY <= 80 else { return nil }
        return match
    }

    static func canonicalNewlines(_ value: String) -> String {
        value.replacingOccurrences(of: "\r\n", with: "\n")
             .replacingOccurrences(of: "\r", with: "\n")
    }

    static func acceptsText(_ value: String, allowEmpty: Bool) -> Bool {
        guard value.count <= 2_000, value.utf8.count <= 16_000,
              allowEmpty || !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return false }
        return value.unicodeScalars.allSatisfy { scalar in
            let code = scalar.value
            return code == 0x09 || code == 0x0A || code == 0x0D ||
                   (code >= 0x20 && code != 0x7F && !(0x80...0x9F).contains(code))
        }
    }

    static func acceptsGroup(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 100 &&
        value.range(of: "[（(][0-9０-９]+[）)]$", options: .regularExpression) == nil &&
        value.unicodeScalars.allSatisfy { scalar in
            let code = scalar.value
            return code >= 0x20 && code != 0x7F && !(0x80...0x9F).contains(code)
        }
    }

    static func sameTransactionText(text: String, draft: String, group: String,
                                    otherText: String, otherDraft: String, otherGroup: String) -> Bool {
        text == otherText && draft == otherDraft && group == otherGroup
    }

    static func copyIsExpected(expected: String, sentinel: String,
                               beforeCount: Int, afterCount: Int,
                               clipboardText: String?, visualBlank: Bool) -> Bool {
        if afterCount == beforeCount + 1 {
            guard let clipboardText else { return false }
            return canonicalNewlines(clipboardText) == canonicalNewlines(expected)
        }
        return expected.isEmpty && visualBlank && afterCount == beforeCount && clipboardText == sentinel
    }

    static func mayRestoreClipboard(ownedCount: Int, currentCount: Int) -> Bool {
        ownedCount == currentCount
    }
}
