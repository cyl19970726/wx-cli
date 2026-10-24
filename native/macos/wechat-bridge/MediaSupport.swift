import AppKit
import CoreImage
import CryptoKit
import Foundation
import ImageIO

struct ImageMedia: Codable, Equatable {
    let path: String
    let sha256: String
    let mimeType: String
    let sizeBytes: Int

    enum CodingKeys: String, CodingKey {
        case path, sha256
        case mimeType = "mime_type"
        case sizeBytes = "size_bytes"
    }
}

struct ImagePixels: Equatable {
    let width: Int
    let height: Int
    let sha256: String
}

struct LoadedImage {
    let source: ImageMedia
    let data: Data
    let image: CGImage
    let pixels: ImagePixels

    var evidence: [String: Any] {
        ["sha256": source.sha256, "pixel_sha256": pixels.sha256,
         "width": pixels.width, "height": pixels.height,
         "mime_type": source.mimeType, "size_bytes": source.sizeBytes]
    }
}

enum MediaFailure: Error, CustomStringConvertible {
    case invalid(String)
    var description: String {
        switch self { case .invalid(let reason): return reason }
    }
}

enum MediaSupport {
    static let maximumBytes = 16 * 1024 * 1024
    static let maximumSide = 8192
    static let maximumPixels = 40_000_000

    static func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func load(_ media: ImageMedia) throws -> LoadedImage {
        let url = URL(fileURLWithPath: media.path)
        guard media.path.hasPrefix("/"), url.standardizedFileURL.path == media.path,
              url.resolvingSymlinksInPath().path == media.path,
              media.sha256.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
              ["image/png", "image/jpeg"].contains(media.mimeType),
              media.sizeBytes > 0, media.sizeBytes <= maximumBytes else {
            throw MediaFailure.invalid("Image source metadata is invalid")
        }
        // O_NONBLOCK makes a FIFO or other unexpected special file refuse
        // immediately at fstat instead of waiting for another process.
        let fd = open(media.path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK)
        guard fd >= 0 else { throw MediaFailure.invalid("Image source cannot be opened safely") }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        defer { try? handle.close() }
        var before = stat()
        guard fstat(fd, &before) == 0, before.st_mode & S_IFMT == S_IFREG,
              before.st_uid == geteuid(), before.st_mode & 0o777 == 0o600,
              before.st_nlink == 1,
              before.st_size == media.sizeBytes,
              let data = try handle.read(upToCount: maximumBytes + 1),
              data.count == media.sizeBytes,
              digest(data) == media.sha256 else {
            throw MediaFailure.invalid("Image source size, ownership or SHA-256 changed")
        }
        var after = stat()
        guard fstat(fd, &after) == 0, before.st_dev == after.st_dev,
              before.st_ino == after.st_ino, before.st_size == after.st_size,
              before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
              before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec else {
            throw MediaFailure.invalid("Image source changed while reading")
        }
        let png = data.starts(with: [137, 80, 78, 71, 13, 10, 26, 10])
        let jpeg = data.starts(with: [0xff, 0xd8, 0xff])
        guard (media.mimeType == "image/png" && png) ||
              (media.mimeType == "image/jpeg" && jpeg) else {
            throw MediaFailure.invalid("Image magic does not match declared MIME type")
        }
        let expectedType = png ? "public.png" : "public.jpeg"
        let decoded = try decode(data, allowedTypes: [expectedType])
        return LoadedImage(source: media, data: data, image: decoded.0, pixels: decoded.1)
    }

    static func decode(_ data: Data, allowedTypes: Set<String>) throws -> (CGImage, ImagePixels) {
        guard !data.isEmpty, data.count <= maximumBytes * 12,
              let source = CGImageSourceCreateWithData(data as CFData, nil),
              let type = CGImageSourceGetType(source) as String?, allowedTypes.contains(type),
              CGImageSourceGetCount(source) == 1,
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width > 0, height > 0, width <= maximumSide, height <= maximumSide,
              width * height <= maximumPixels,
              let image = CGImageSourceCreateImageAtIndex(source, 0,
                  [kCGImageSourceShouldCacheImmediately: true] as CFDictionary),
              image.width == width, image.height == height else {
            throw MediaFailure.invalid("Image cannot be fully decoded as one bounded frame")
        }
        let orientation = properties[kCGImagePropertyOrientation] as? Int ?? 1
        guard (1...8).contains(orientation) else {
            throw MediaFailure.invalid("Image has an invalid EXIF orientation")
        }
        let canonicalImage: CGImage
        if orientation == 1 {
            canonicalImage = image
        } else {
            // Normalize orientation before hashing and pasting. Exported
            // PNG/TIFF pixels then represent the source's displayed image.
            let oriented = CIImage(cgImage: image).oriented(forExifOrientation: Int32(orientation))
            let renderer = CIContext(options: [.useSoftwareRenderer: true])
            guard let transformed = renderer.createCGImage(oriented, from: oriented.extent) else {
                throw MediaFailure.invalid("Image EXIF orientation cannot be rendered")
            }
            canonicalImage = transformed
        }
        let canonicalWidth = canonicalImage.width, canonicalHeight = canonicalImage.height
        guard canonicalWidth > 0, canonicalHeight > 0,
              canonicalWidth <= maximumSide, canonicalHeight <= maximumSide,
              canonicalWidth * canonicalHeight == width * height else {
            throw MediaFailure.invalid("Oriented image dimensions are invalid")
        }
        let byteCount = canonicalWidth * canonicalHeight * 4
        var rgba = [UInt8](repeating: 0, count: byteCount)
        let drawn = rgba.withUnsafeMutableBytes { buffer -> Bool in
            guard let colorSpace = CGColorSpace(name: CGColorSpace.sRGB),
                  let context = CGContext(data: buffer.baseAddress, width: canonicalWidth, height: canonicalHeight,
                      bitsPerComponent: 8, bytesPerRow: canonicalWidth * 4, space: colorSpace,
                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            context.draw(canonicalImage, in: CGRect(x: 0, y: 0,
                                                     width: canonicalWidth, height: canonicalHeight))
            return true
        }
        guard drawn else { throw MediaFailure.invalid("Image pixels cannot be decoded") }
        return (canonicalImage, ImagePixels(width: canonicalWidth, height: canonicalHeight,
                                            sha256: digest(Data(rgba))))
    }

    static func pngData(_ image: CGImage) throws -> Data {
        let result = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(result, "public.png" as CFString, 1, nil) else {
            throw MediaFailure.invalid("Cannot encode image for native pasteboard")
        }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else {
            throw MediaFailure.invalid("Cannot finish native PNG pasteboard image")
        }
        return result as Data
    }

    static func copiedImage(from board: NSPasteboard, expected: ImagePixels) throws -> String {
        guard let items = board.pasteboardItems, items.count == 1, let item = items.first else {
            throw MediaFailure.invalid("Composer copy did not contain one image item")
        }
        let imageTypes: Set<NSPasteboard.PasteboardType> = [.png, .tiff]
        guard !item.types.isEmpty, item.types.allSatisfy({ imageTypes.contains($0) }) else {
            throw MediaFailure.invalid("Composer copy contains text, file, or other non-image payload")
        }
        var provenTypes: [String] = []
        for (type, allowed) in [(NSPasteboard.PasteboardType.png, Set(["public.png"])),
                                (.tiff, Set(["public.tiff"]))] {
            guard let data = item.data(forType: type) else { continue }
            let pixels = try decode(data, allowedTypes: allowed).1
            guard pixels == expected else {
                throw MediaFailure.invalid("Composer image pixels differ from source")
            }
            provenTypes.append(type.rawValue)
        }
        guard !provenTypes.isEmpty else {
            throw MediaFailure.invalid("Composer copy has no decodable PNG or TIFF image evidence")
        }
        return provenTypes.joined(separator: ",")
    }
}
