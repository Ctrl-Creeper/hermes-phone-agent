import Foundation
import CoreImage
import ImageIO
import UniformTypeIdentifiers
import AppKit

let filter = CIFilter(name: "CIQRCodeGenerator")!
filter.setValue(Data("https://example.org/phone-test?value=42".utf8), forKey: "inputMessage")
let code = filter.outputImage!.transformed(by: CGAffineTransform(scaleX: 10, y: 10))
let extent = code.extent.insetBy(dx: -40, dy: -40)
let white = CIImage(color: CIColor(red: 1, green: 1, blue: 1)).cropped(to: extent)
let context = CIContext()
let qrImage = context.createCGImage(code.composited(over: white), from: extent)!
let canvas = NSImage(size: NSSize(width: 1000, height: 1000))
canvas.lockFocus()
NSColor.white.setFill()
NSRect(x: 0, y: 0, width: 1000, height: 1000).fill()
NSImage(cgImage: qrImage, size: NSSize(width: 600, height: 600)).draw(
    in: NSRect(x: 200, y: 100, width: 600, height: 600))
("PHONE IMAGE TEST 42" as NSString).draw(
    at: NSPoint(x: 150, y: 820),
    withAttributes: [.font: NSFont.systemFont(ofSize: 48), .foregroundColor: NSColor.black])
canvas.unlockFocus()
let image = canvas.cgImage(forProposedRect: nil, context: nil, hints: nil)!
let destination = CGImageDestinationCreateWithURL(
    URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL, UTType.png.identifier as CFString, 1, nil)!
CGImageDestinationAddImage(destination, image, nil)
assert(CGImageDestinationFinalize(destination))
