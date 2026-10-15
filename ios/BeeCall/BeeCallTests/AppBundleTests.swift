import UIKit
import XCTest
@testable import BeeCall

/// What the app bundle itself promises (BC-146, BC-148).
final class AppBundleTests: XCTestCase {
    /// BC-146: the app never uses the camera, and the camera text says so. (LiveKit's library has video
    /// features, so iOS wants the text, but no flow asks for the camera.)
    @MainActor func testTheCameraTextSaysTheCameraIsNeverUsed() throws {
        let text = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey: "NSCameraUsageDescription") as? String)
        XCTAssertTrue(text.contains("never uses the camera"), text)
    }

    /// BC-148: Bee yellow, with a dark speech bubble and voice bars in it.
    @MainActor func testTheIconIsBeeYellowWithADarkBubble() throws {
        let icon = try XCTUnwrap(UIImage(named: "AppIcon60x60"), "the app's icon")
        let corner = try XCTUnwrap(color(of: icon, at: CGPoint(x: 0.1, y: 0.1)))
        XCTAssertTrue(corner.red > 0.85 && corner.green > 0.7 && corner.blue < 0.3, "corner \(corner)")
        let bubble = try XCTUnwrap(color(of: icon, at: CGPoint(x: 0.25, y: 0.45)))
        XCTAssertTrue(max(bubble.red, bubble.green, bubble.blue) < 0.3, "bubble \(bubble)")
    }

    /// The colour at a point given as fractions of the image's width and height.
    private func color(of image: UIImage, at point: CGPoint) -> (red: CGFloat, green: CGFloat, blue: CGFloat)? {
        guard let picture = image.cgImage else { return nil }
        let width = picture.width, height = picture.height
        var pixels = [UInt8](repeating: 0, count: width * height * 4)
        let drawn: Bool = pixels.withUnsafeMutableBytes { bytes in
            guard let context = CGContext(data: bytes.baseAddress, width: width, height: height, bitsPerComponent: 8,
                                          bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
                                          bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            context.draw(picture, in: CGRect(x: 0, y: 0, width: width, height: height))
            return true
        }
        guard drawn else { return nil }
        let index = (Int(point.y * CGFloat(height)) * width + Int(point.x * CGFloat(width))) * 4
        return (CGFloat(pixels[index]) / 255, CGFloat(pixels[index + 1]) / 255, CGFloat(pixels[index + 2]) / 255)
    }
}
