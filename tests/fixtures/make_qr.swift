import Foundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers

let filter = CIFilter(name: "CIQRCodeGenerator")!
filter.setValue(Data("https://example.org/phone-test?value=42".utf8), forKey: "inputMessage")
let code = filter.outputImage!.transformed(by: CGAffineTransform(scaleX: 10, y: 10))
let extent = code.extent.insetBy(dx: -40, dy: -40)
let white = CIImage(color: CIColor(red: 1, green: 1, blue: 1)).cropped(to: extent)
let context = CIContext()
let image = context.createCGImage(code.composited(over: white), from: extent)!
let destination = CGImageDestinationCreateWithURL(
    URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(destination, image, nil)
assert(CGImageDestinationFinalize(destination))
