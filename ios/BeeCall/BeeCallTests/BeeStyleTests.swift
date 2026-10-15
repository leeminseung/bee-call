import UIKit
import XCTest
@testable import BeeCall

/// Design D's colours (scratch/2026-10-15-ui-mockups/d-chat-and-call.html, "색과 글자"), in light and dark.
final class BeeStyleTests: XCTestCase {
    private func hex(_ color: UIColor, dark: Bool) -> String {
        let resolved = color.resolvedColor(with: UITraitCollection(userInterfaceStyle: dark ? .dark : .light))
        var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
        resolved.getRed(&red, green: &green, blue: &blue, alpha: &alpha)
        return String(format: "#%02X%02X%02X", Int((red * 255).rounded()), Int((green * 255).rounded()),
                      Int((blue * 255).rounded()))
    }

    @MainActor func testTheLightColoursAreTheDesigns() {
        XCTAssertEqual(hex(BeeStyle.Palette.yellow, dark: false), "#FFD900") // the AI, the call, highlights
        XCTAssertEqual(hex(BeeStyle.Palette.aiBubble, dark: false), "#FFF4C2")
        XCTAssertEqual(hex(BeeStyle.Palette.personBlue, dark: false), "#007AFF")
        XCTAssertEqual(hex(BeeStyle.Palette.ink, dark: false), "#21200B")
        XCTAssertEqual(hex(BeeStyle.Palette.card, dark: false), "#F2F2F7")
        XCTAssertEqual(hex(BeeStyle.Palette.red, dark: false), "#FF3B30")
        XCTAssertEqual(hex(BeeStyle.Palette.background, dark: false), "#FFFFFF")
    }

    /// Dark: a black background, #1C1C1E cards, #0A84FF for the person; the yellow stays, to glow.
    @MainActor func testTheDarkColoursFollowTheDesignsNote() {
        XCTAssertEqual(hex(BeeStyle.Palette.background, dark: true), "#000000")
        XCTAssertEqual(hex(BeeStyle.Palette.card, dark: true), "#1C1C1E")
        XCTAssertEqual(hex(BeeStyle.Palette.personBlue, dark: true), "#0A84FF")
        XCTAssertEqual(hex(BeeStyle.Palette.yellow, dark: true), "#FFD900")
        XCTAssertEqual(hex(BeeStyle.Palette.ink, dark: true), "#FFFFFF")
    }

    /// Yellow is a fill; text on it is always the dark ink, in both modes.
    @MainActor func testTextOnYellowIsTheDarkInkInBothModes() {
        XCTAssertEqual(hex(BeeStyle.Palette.onYellow, dark: false), "#21200B")
        XCTAssertEqual(hex(BeeStyle.Palette.onYellow, dark: true), "#21200B")
    }
}
