import SwiftUI
import UIKit

/// Design D's look (scratch/2026-10-15-ui-mockups/d-chat-and-call.html, "색과 글자"): Bee yellow for the AI, the
/// call and highlights, a light yellow for the AI's bubbles, iOS blue for the person, dark ink for text, and
/// always dark ink on yellow, which is a fill and never a text colour. Dark mode, from the design's note: a black
/// background, #1C1C1E cards and #0A84FF for the person; the orb and the yellow button stay as they are, to glow on
/// the dark background. The AI's bubble in dark mode is not in the design: a dim yellow with light text.
enum BeeStyle {
    enum Palette {
        static let yellow = UIColor(hex: 0xFFD900)
        static let aiBubble = UIColor(light: 0xFFF4C2, dark: 0x3A3312)
        static let personBlue = UIColor(light: 0x007AFF, dark: 0x0A84FF)
        static let ink = UIColor(light: 0x21200B, dark: 0xFFFFFF)
        static let onYellow = UIColor(hex: 0x21200B)
        static let card = UIColor(light: 0xF2F2F7, dark: 0x1C1C1E)
        static let red = UIColor(light: 0xFF3B30, dark: 0xFF453A)
        static let background = UIColor(light: 0xFFFFFF, dark: 0x000000)
    }

    static let yellow = Color(uiColor: Palette.yellow)
    static let aiBubble = Color(uiColor: Palette.aiBubble)
    static let personBlue = Color(uiColor: Palette.personBlue)
    static let ink = Color(uiColor: Palette.ink)
    static let onYellow = Color(uiColor: Palette.onYellow)
    static let card = Color(uiColor: Palette.card)
    static let red = Color(uiColor: Palette.red)
    static let background = Color(uiColor: Palette.background)
}

extension UIColor {
    convenience init(hex: UInt32) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: 1)
    }

    /// One colour for light mode and one for dark.
    convenience init(light: UInt32, dark: UInt32) {
        self.init { traits in UIColor(hex: traits.userInterfaceStyle == .dark ? dark : light) }
    }
}
