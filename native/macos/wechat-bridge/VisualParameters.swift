import Foundation

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
    static func withinFocusSearchBudget(_ elapsed: TimeInterval) -> Bool {
        elapsed.isFinite && elapsed >= 0 && elapsed < 6
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
        let sections: [(Int, SearchCategory?)] = ordered.enumerated().compactMap { index, candidate in
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
