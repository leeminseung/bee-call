import SwiftUI
import UIKit

/// Design D's colours (scratch/2026-10-15-ui-mockups/dark-mode.html, "색 토큰"), each for light mode, dark mode, and
/// dark mode inside a sheet, which iOS draws one level up (its elevated backgrounds) so that a sheet's edge shows over
/// the black home. Bee yellow is a fill and a glow only, never a large surface and never a text colour: text on it is
/// always the dark ink. In dark mode the AI's bubble is a grey one step lighter than the cards, and the orb and the
/// yellow buttons glow on black. Every text pair reaches WCAG AA, 4.5:1, in all three (BeeStyleTests).
enum BeeStyle {
    enum Palette {
        /// The screen's background.
        static let background = UIColor(light: 0xFFFFFF, dark: 0x000000, sheet: 0x1C1C1E)
        /// Result, Bee, offer and prep cards, and list rows.
        static let card = UIColor(light: 0xF2F2F7, dark: 0x1C1C1E, sheet: 0x2C2C2E)
        /// What sits on a card: file chips, answer buttons, a chart's plate.
        static let raised = UIColor(light: 0xFFFFFF, dark: 0x2C2C2E, sheet: 0x3A3A3C)
        /// Round call buttons, the job pill, "대화 펼치기".
        static let controlFill = UIColor(light: 0xF2F2F7, dark: 0x2C2C2E, sheet: 0x3A3A3C)
        static let aiBubble = UIColor(light: 0xFFF4C2, dark: 0x2C2C2E, sheet: 0x3A3A3C)
        static let aiBubbleText = UIColor(light: 0x21200B, dark: 0xFFFFFF)
        /// The person's bubble (white text) and blue dot, in both modes: white on iOS's #007AFF is only 4.02:1.
        static let personBlue = UIColor(hex: 0x0071E3)
        static let ink = UIColor(light: 0x21200B, dark: 0xFFFFFF)
        /// Times, eyebrows, values and other quieter text, in place of the system's secondary grey (3.44:1 in light).
        static let secondaryInk = UIColor(light: 0x6F6E60, dark: 0xABABB0)
        static let yellow = UIColor(hex: 0xFFD900)
        static let onYellow = UIColor(hex: 0x21200B)
        /// A red fill: the end button (with a white X) and the missed call's icon.
        static let red = UIColor(light: 0xFF3B30, dark: 0xFF453A)
        /// Red text: "전화 취소", "끊김", "받지 못함". Never on the sheet's raised grey.
        static let redText = UIColor(light: 0xD70015, dark: 0xFF6961)
        static let separator = UIColor(light: 0xC6C6C8, dark: 0x38383A, sheet: 0x48484A)
        /// The name pill, the pinned call, the call bar and the round buttons over the conversation, before iOS 26's
        /// own glass.
        static let glass = UIColor(light: UIColor(hex: 0xFFFFFF, alpha: 0.76), dark: UIColor(hex: 0x1E1E20, alpha: 0.74))
        /// A quiet button's fill on glass, as the pinned call's "전화 취소".
        static let subtleFill = UIColor(light: UIColor(hex: 0x767680, alpha: 0.13), dark: UIColor(hex: 0xFFFFFF, alpha: 0.09))
    }

    static let background = Color(uiColor: Palette.background)
    static let card = Color(uiColor: Palette.card)
    static let raised = Color(uiColor: Palette.raised)
    static let controlFill = Color(uiColor: Palette.controlFill)
    static let aiBubble = Color(uiColor: Palette.aiBubble)
    static let aiBubbleText = Color(uiColor: Palette.aiBubbleText)
    static let personBlue = Color(uiColor: Palette.personBlue)
    static let ink = Color(uiColor: Palette.ink)
    static let secondaryInk = Color(uiColor: Palette.secondaryInk)
    static let yellow = Color(uiColor: Palette.yellow)
    static let onYellow = Color(uiColor: Palette.onYellow)
    static let red = Color(uiColor: Palette.red)
    static let redText = Color(uiColor: Palette.redText)
    static let separator = Color(uiColor: Palette.separator)
    static let glass = Color(uiColor: Palette.glass)
    static let subtleFill = Color(uiColor: Palette.subtleFill)
}

extension UIColor {
    convenience init(hex: UInt32, alpha: CGFloat = 1) {
        self.init(red: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
    }

    /// One colour for light mode and one for dark, and in dark mode another inside a sheet (iOS's elevated level).
    convenience init(light: UInt32, dark: UInt32, sheet: UInt32? = nil) {
        self.init { traits in
            guard traits.userInterfaceStyle == .dark else { return UIColor(hex: light) }
            return UIColor(hex: traits.userInterfaceLevel == .elevated ? sheet ?? dark : dark)
        }
    }

    convenience init(light: UIColor, dark: UIColor) {
        self.init { traits in traits.userInterfaceStyle == .dark ? dark : light }
    }
}

extension View {
    /// The design's glass under the name pill, the pinned call, the call bar and the round buttons over the
    /// conversation: iOS's own glass from iOS 26, else the glass colour with a soft shadow.
    @ViewBuilder
    func beeGlass<S: Shape>(in shape: S) -> some View {
        if #available(iOS 26, *) {
            glassEffect(.regular, in: shape)
        } else {
            background(BeeStyle.glass, in: shape)
                .shadow(color: .black.opacity(0.12), radius: 8, y: 2)
        }
    }
}

/// The round yellow call button's halo: a soft yellow ring in light mode, and on black a glow with a thin light rim,
/// since the ring turns into an olive band there.
struct CallButtonHalo: ViewModifier {
    @Environment(\.colorScheme) private var scheme

    func body(content: Content) -> some View {
        if scheme == .dark {
            content
                .overlay(Circle().strokeBorder(Color(red: 1, green: 0.925, blue: 0.55).opacity(0.5), lineWidth: 1)
                    .padding(-1))
                .background(Circle().fill(BeeStyle.yellow.opacity(0.34)).padding(-4).blur(radius: 14))
        } else {
            content
                .background(Circle().fill(BeeStyle.yellow.opacity(0.22)).frame(width: 98, height: 98))
        }
    }
}
