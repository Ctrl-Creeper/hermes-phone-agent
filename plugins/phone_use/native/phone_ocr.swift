import Foundation
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

do {
    guard CommandLine.arguments.count == 2 else {
        throw OCRError.missingImagePath
    }

    let imageURL = URL(fileURLWithPath: CommandLine.arguments[1])
    guard
        let source = CGImageSourceCreateWithURL(imageURL as CFURL, nil),
        let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil)
            as? [CFString: Any],
        let width = (properties[kCGImagePropertyPixelWidth] as? NSNumber)?.intValue,
        let height = (properties[kCGImagePropertyPixelHeight] as? NSNumber)?.intValue
    else {
        throw OCRError.unreadableImage
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    request.minimumTextHeight = 0.008

    let handler = VNImageRequestHandler(url: imageURL, options: [:])
    try handler.perform([request])

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

    let data = try JSONEncoder().encode(OCRPayload(width: width, height: height, items: items))
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
} catch {
    FileHandle.standardError.write(Data("\(error)\n".utf8))
    exit(1)
}
