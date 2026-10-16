import Foundation

struct GroupOCRCandidate: Equatable {
    let text: String
    let x: Double
    let y: Double
}

enum VisualParameters {
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
