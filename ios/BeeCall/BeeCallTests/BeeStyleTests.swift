import UIKit
import XCTest
@testable import BeeCall

/// Design D's colours, light and dark (scratch/2026-10-15-ui-mockups/dark-mode.html, "색 토큰" and "글자 대비").
/// In dark mode a sheet is one level up, as iOS draws its elevated backgrounds, so its edge shows over the black home.
final class BeeStyleTests: XCTestCase {
    enum Look: CaseIterable {
        case light, dark, darkSheet

        @MainActor var traits: UITraitCollection {
            UITraitCollection { traits in
                traits.userInterfaceStyle = self == .light ? .light : .dark
                traits.userInterfaceLevel = self == .darkSheet ? .elevated : .base
            }
        }
    }

    @MainActor private func rgba(_ color: UIColor, _ look: Look) -> (Double, Double, Double, Double) {
        var red: CGFloat = 0, green: CGFloat = 0, blue: CGFloat = 0, alpha: CGFloat = 0
        color.resolvedColor(with: look.traits).getRed(&red, green: &green, blue: &blue, alpha: &alpha)
        return (red, green, blue, alpha)
    }

    @MainActor private func hex(_ color: UIColor, _ look: Look) -> String {
        let (red, green, blue, alpha) = rgba(color, look)
        let rgb = String(format: "#%02X%02X%02X", Int((red * 255).rounded()), Int((green * 255).rounded()),
                         Int((blue * 255).rounded()))
        return alpha < 1 ? rgb + String(format: "@%.2f", alpha) : rgb
    }

    // MARK: - The table

    @MainActor func testTheTokensAreTheDesignsTable() {
        let table: [(UIColor, String, light: String, dark: String, darkSheet: String)] = [
            (BeeStyle.Palette.background, "background", "#FFFFFF", "#000000", "#1C1C1E"),
            (BeeStyle.Palette.card, "card", "#F2F2F7", "#1C1C1E", "#2C2C2E"),
            (BeeStyle.Palette.raised, "raised", "#FFFFFF", "#2C2C2E", "#3A3A3C"),
            (BeeStyle.Palette.controlFill, "controlFill", "#F2F2F7", "#2C2C2E", "#3A3A3C"),
            (BeeStyle.Palette.aiBubble, "aiBubble", "#FFF4C2", "#2C2C2E", "#3A3A3C"),
            (BeeStyle.Palette.aiBubbleText, "aiBubbleText", "#21200B", "#FFFFFF", "#FFFFFF"),
            (BeeStyle.Palette.personBlue, "personBlue", "#0071E3", "#0071E3", "#0071E3"),
            (BeeStyle.Palette.ink, "ink", "#21200B", "#FFFFFF", "#FFFFFF"),
            (BeeStyle.Palette.secondaryInk, "secondaryInk", "#6F6E60", "#ABABB0", "#ABABB0"),
            (BeeStyle.Palette.yellow, "yellow", "#FFD900", "#FFD900", "#FFD900"),
            (BeeStyle.Palette.onYellow, "onYellow", "#21200B", "#21200B", "#21200B"),
            (BeeStyle.Palette.red, "red", "#FF3B30", "#FF453A", "#FF453A"),
            (BeeStyle.Palette.redText, "redText", "#D70015", "#FF6961", "#FF6961"),
            (BeeStyle.Palette.separator, "separator", "#C6C6C8", "#38383A", "#48484A"),
            (BeeStyle.Palette.glass, "glass", "#FFFFFF@0.76", "#1E1E20@0.74", "#1E1E20@0.74"),
        ]
        for (color, name, light, dark, darkSheet) in table {
            XCTAssertEqual(hex(color, .light), light, name)
            XCTAssertEqual(hex(color, .dark), dark, name)
            XCTAssertEqual(hex(color, .darkSheet), darkSheet, name)
        }
    }

    // MARK: - Contrast

    /// WCAG 2's relative luminance of an opaque colour.
    private func luminance(_ rgb: (Double, Double, Double)) -> Double {
        func linear(_ c: Double) -> Double { c <= 0.03928 ? c / 12.92 : pow((c + 0.055) / 1.055, 2.4) }
        return 0.2126 * linear(rgb.0) + 0.7152 * linear(rgb.1) + 0.0722 * linear(rgb.2)
    }

    /// `color` laid over `under`, as the screen shows a translucent fill.
    @MainActor private func over(_ color: UIColor, _ under: (Double, Double, Double), _ look: Look) -> (Double, Double, Double) {
        let (red, green, blue, alpha) = rgba(color, look)
        return (red * alpha + under.0 * (1 - alpha), green * alpha + under.1 * (1 - alpha),
                blue * alpha + under.2 * (1 - alpha))
    }

    /// A background as drawn: each fill laid over the one before, on the screen's background.
    @MainActor private func surface(_ fills: [UIColor], _ look: Look) -> (Double, Double, Double) {
        fills.reduce(over(BeeStyle.Palette.background, (0, 0, 0), look)) { under, fill in over(fill, under, look) }
    }

    @MainActor private func ratio(_ text: UIColor, on fills: [UIColor], _ look: Look) -> Double {
        let back = surface(fills, look), front = over(text, back, look)
        let (a, b) = (luminance(front), luminance(back))
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)
    }

    /// Every text and the surfaces it sits on, in light, dark and a dark sheet: each pair reaches WCAG AA, 4.5:1.
    @MainActor func testEveryTextPairReachesAA() {
        typealias P = BeeStyle.Palette
        let surfaces: [(String, [UIColor])] = [
            ("background", []), ("card", [P.card]), ("raised", [P.raised]), ("controlFill", [P.controlFill]),
            ("glass", [P.glass]),
        ]
        var pairs: [(String, UIColor, [UIColor], [Look])] = []
        for (name, fills) in surfaces {
            pairs.append(("ink on \(name)", P.ink, fills, Look.allCases))
            pairs.append(("secondaryInk on \(name)", P.secondaryInk, fills, Look.allCases))
            // Red text never sits on the sheet's raised grey (4.02:1 there), the design's one exception.
            let red: [Look] = name == "raised" || name == "controlFill" ? [.light, .dark] : Look.allCases
            pairs.append(("redText on \(name)", P.redText, fills, red))
        }
        pairs += [
            ("aiBubbleText on aiBubble", P.aiBubbleText, [P.aiBubble], Look.allCases),
            ("white on personBlue", .white, [P.personBlue], Look.allCases),
            ("onYellow on yellow", P.onYellow, [P.yellow], Look.allCases),
            ("redText on the pinned call's cancel button", P.redText, [P.glass, P.subtleFill], Look.allCases),
        ]
        for (name, text, fills, looks) in pairs {
            for look in looks {
                XCTAssertGreaterThanOrEqual(ratio(text, on: fills, look), 4.5, "\(name), \(look)")
            }
        }
    }

    /// The light pairs the design's note found under 4.5:1 (white on #007AFF 4.02, red #FF3B30 3.55, the system's
    /// secondary grey 3.44, the call screen's faded last line 4.11), as the note computes them now.
    @MainActor func testTheLightPairsTheNoteFixed() {
        XCTAssertEqual(ratio(.white, on: [BeeStyle.Palette.personBlue], .light), 4.70, accuracy: 0.01)
        XCTAssertEqual(ratio(BeeStyle.Palette.redText, on: [], .light), 5.38, accuracy: 0.01)
        XCTAssertEqual(ratio(BeeStyle.Palette.secondaryInk, on: [], .light), 5.15, accuracy: 0.01)
    }
}
