import Foundation
import CoreGraphics
import ImageIO
import Vision

struct OCRItem: Codable {
    let text: String
    let confidence: Float
    let bounds: [Int]
}

struct OCRPayload: Codable {
    let width: Int
    let height: Int
    let items: [OCRItem]
    let visualRegions: [VisualRegion]
    let qrCodes: [String]
}

struct VisualRegion: Codable {
    let confidence: Float
    let bounds: [Int]
}

enum OCRError: Error, CustomStringConvertible {
    case missingImagePath
    case unreadableImage

    var description: String {
        switch self {
        case .missingImagePath:
            return "usage: phone-ocr <image.png>"
        case .unreadableImage:
            return "unable to read image dimensions"
        }
    }
}

func pixelBounds(_ box: CGRect, width: Int, height: Int) -> [Int] {
    let left = max(0, min(width, Int((box.minX * CGFloat(width)).rounded(.down))))
    let right = max(0, min(width, Int((box.maxX * CGFloat(width)).rounded(.up))))
    let top = max(0, min(height, Int(((1 - box.maxY) * CGFloat(height)).rounded(.down))))
    let bottom = max(0, min(height, Int(((1 - box.minY) * CGFloat(height)).rounded(.up))))
    return [left, top, right, bottom]
}

func detectVisualRegions(_ image: CGImage, width: Int, height: Int) -> [VisualRegion] {
    guard let context = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: width * 4,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ), let base = context.data else {
        return []
    }
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    let pixels = base.assumingMemoryBound(to: UInt8.self)
    let bytesPerPixel = 4
    let bytesPerRow = context.bytesPerRow

    @inline(__always) func luma(_ offset: Int) -> Double {
        Double(pixels[offset]) * 0.299
            + Double(pixels[offset + 1]) * 0.587
            + Double(pixels[offset + 2]) * 0.114
    }

    let tile = 16
    let columns = (width + tile - 1) / tile
    let rows = (height + tile - 1) / tile
    var mask = [Bool](repeating: false, count: rows * columns)

    // Scalar accumulators: this runs over every screenshot, so keep the inner
    // loop free of heap allocation.
    for tileY in 0..<rows {
        for tileX in 0..<columns {
            let left = tileX * tile
            let top = tileY * tile
            let right = min(width, left + tile)
            let bottom = min(height, top + tile)
            var count = 0.0
            var sumR = 0.0, sumG = 0.0, sumB = 0.0
            var squareR = 0.0, squareG = 0.0, squareB = 0.0
            var saturation = 0.0
            var gradient = 0.0
            var gradientCount = 0.0

            for y in stride(from: top, to: bottom, by: 2) {
                for x in stride(from: left, to: right, by: 2) {
                    let offset = y * bytesPerRow + x * bytesPerPixel
                    let red = Double(pixels[offset])
                    let green = Double(pixels[offset + 1])
                    let blue = Double(pixels[offset + 2])
                    let current = red * 0.299 + green * 0.587 + blue * 0.114
                    sumR += red; sumG += green; sumB += blue
                    squareR += red * red; squareG += green * green; squareB += blue * blue
                    saturation += max(red, green, blue) - min(red, green, blue)
                    count += 1
                    if x >= left + 2 {
                        gradient += abs(current - luma(offset - 2 * bytesPerPixel))
                        gradientCount += 1
                    }
                    if y >= top + 2 {
                        gradient += abs(current - luma(offset - 2 * bytesPerRow))
                        gradientCount += 1
                    }
                }
            }
            guard count > 0 else { continue }
            let deviation = (
                sqrt(max(0, squareR / count - (sumR / count) * (sumR / count)))
                + sqrt(max(0, squareG / count - (sumG / count) * (sumG / count)))
                + sqrt(max(0, squareB / count - (sumB / count) * (sumB / count)))
            ) / 3.0
            let meanSaturation = saturation / count
            let meanGradient = gradientCount > 0 ? gradient / gradientCount : 0
            mask[tileY * columns + tileX] = (
                (deviation > 25 && meanGradient > 9)
                || (meanSaturation > 38 && deviation > 18)
            )
        }
    }

    var visited = [Bool](repeating: false, count: mask.count)
    var regions: [VisualRegion] = []
    let neighbors = [(-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1)]
    for startY in 0..<rows {
        for startX in 0..<columns {
            let start = startY * columns + startX
            if !mask[start] || visited[start] { continue }
            visited[start] = true
            var stack = [(startX, startY)]
            var minX = startX
            var maxX = startX
            var minY = startY
            var maxY = startY
            var tileCount = 0
            while let (x, y) = stack.popLast() {
                tileCount += 1
                minX = min(minX, x)
                maxX = max(maxX, x)
                minY = min(minY, y)
                maxY = max(maxY, y)
                for (dx, dy) in neighbors {
                    let nx = x + dx
                    let ny = y + dy
                    if nx < 0 || ny < 0 || nx >= columns || ny >= rows { continue }
                    let index = ny * columns + nx
                    if mask[index] && !visited[index] {
                        visited[index] = true
                        stack.append((nx, ny))
                    }
                }
            }

            let regionWidth = (maxX - minX + 1) * tile
            let regionHeight = (maxY - minY + 1) * tile
            let boxTiles = (maxX - minX + 1) * (maxY - minY + 1)
            let density = Double(tileCount) / Double(boxTiles)
            let aspect = Double(regionHeight) / Double(max(1, regionWidth))
            let pixelTop = minY * tile
            let pixelBottom = min(height, (maxY + 1) * tile)
            if regionWidth >= 160
                && regionHeight >= 160
                && aspect >= 0.45
                && aspect <= 2.5
                && density >= 0.22
                && pixelTop >= Int(Double(height) * 0.07)
                && pixelBottom <= Int(Double(height) * 0.90)
            {
                regions.append(VisualRegion(
                    confidence: Float(min(1.0, density + 0.45)),
                    bounds: [
                        minX * tile,
                        pixelTop,
                        min(width, (maxX + 1) * tile),
                        pixelBottom,
                    ]
                ))
            }
        }
    }
    var merged: [VisualRegion] = []
    for region in regions.sorted(by: { $0.bounds[1] < $1.bounds[1] }) {
        if let last = merged.last {
            let overlap = min(last.bounds[2], region.bounds[2])
                - max(last.bounds[0], region.bounds[0])
            let smallerWidth = min(
                last.bounds[2] - last.bounds[0],
                region.bounds[2] - region.bounds[0]
            )
            let verticalGap = region.bounds[1] - last.bounds[3]
            if overlap >= Int(Double(smallerWidth) * 0.4) && verticalGap <= 32 {
                merged[merged.count - 1] = VisualRegion(
                    confidence: max(last.confidence, region.confidence),
                    bounds: [
                        min(last.bounds[0], region.bounds[0]),
                        min(last.bounds[1], region.bounds[1]),
                        max(last.bounds[2], region.bounds[2]),
                        max(last.bounds[3], region.bounds[3]),
                    ]
                )
                continue
            }
        }
        merged.append(region)
    }
    return merged
}

func recognize(_ image: CGImage) throws -> OCRPayload {
    let width = image.width
    let height = image.height

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    request.minimumTextHeight = 0.008

    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])

    let observations = request.results ?? []
    let items = observations.compactMap { observation -> OCRItem? in
        guard let candidate = observation.topCandidates(1).first else {
            return nil
        }
        let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            return nil
        }
        return OCRItem(
            text: text,
            confidence: candidate.confidence,
            bounds: pixelBounds(observation.boundingBox, width: width, height: height)
        )
    }.sorted {
        if $0.bounds[1] == $1.bounds[1] {
            return $0.bounds[0] < $1.bounds[0]
        }
        return $0.bounds[1] < $1.bounds[1]
    }

    let visualRegions = detectVisualRegions(image, width: width, height: height)
    let barcodeRequest = VNDetectBarcodesRequest()
    barcodeRequest.symbologies = [.qr]
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([barcodeRequest])
    let qrCodes = Array(Set((barcodeRequest.results ?? []).compactMap {
        $0.payloadStringValue
    })).sorted()
    return OCRPayload(
        width: width, height: height, items: items, visualRegions: visualRegions,
        qrCodes: qrCodes
    )
}

func decodeImage(format: String, data: Data, width: Int, height: Int) -> CGImage? {
    switch format {
    case "png":
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else {
            return nil
        }
        return CGImageSourceCreateImageAtIndex(source, 0, nil)
    case "rgba":
        // Android `screencap` without -p: tightly packed RGBA, alpha ignored.
        guard width > 0, height > 0, data.count >= width * height * 4,
              let provider = CGDataProvider(data: data as CFData) else {
            return nil
        }
        return CGImage(
            width: width,
            height: height,
            bitsPerComponent: 8,
            bitsPerPixel: 32,
            bytesPerRow: width * 4,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
            provider: provider,
            decode: nil,
            shouldInterpolate: false,
            intent: .defaultIntent
        )
    default:
        return nil
    }
}

func readLineData() -> Data? {
    var line = Data()
    while true {
        let byte = FileHandle.standardInput.readData(ofLength: 1)
        if byte.isEmpty { return line.isEmpty ? nil : line }
        if byte[0] == 0x0A { return line }
        line.append(byte)
    }
}

func readExactly(_ count: Int) -> Data? {
    var data = Data()
    while data.count < count {
        let chunk = FileHandle.standardInput.readData(ofLength: count - data.count)
        if chunk.isEmpty { return nil }
        data.append(chunk)
    }
    return data
}

func emit(_ data: Data) {
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

func emitError(_ message: String) {
    let body = try? JSONSerialization.data(withJSONObject: ["error": message])
    emit(body ?? Data("{\"error\":\"unknown\"}".utf8))
}

/// Long-lived mode: one JSON header line per request, then `length` image
/// bytes on stdin; one JSON line back. Keeping the process (and Vision's
/// loaded models) alive avoids a cold start for every screenshot.
func serve() {
    while let line = readLineData() {
        autoreleasepool {
            guard
                let header = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                let format = header["format"] as? String,
                let length = header["length"] as? Int, length >= 0,
                let data = readExactly(length)
            else {
                emitError("bad request")
                return
            }
            guard let image = decodeImage(
                format: format,
                data: data,
                width: header["width"] as? Int ?? 0,
                height: header["height"] as? Int ?? 0
            ) else {
                emitError("unreadable image")
                return
            }
            do {
                emit(try JSONEncoder().encode(recognize(image)))
            } catch {
                emitError("\(error)")
            }
        }
    }
}

if CommandLine.arguments.count == 2 && CommandLine.arguments[1] == "--serve" {
    serve()
    exit(0)
}

do {
    guard CommandLine.arguments.count == 2 else {
        throw OCRError.missingImagePath
    }

    let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
    guard
        let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
        let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        throw OCRError.unreadableImage
    }
    emit(try JSONEncoder().encode(recognize(image)))
} catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8))
    exit(1)
}
