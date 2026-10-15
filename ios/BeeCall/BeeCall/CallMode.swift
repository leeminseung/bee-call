import Foundation

/// The two lines on design D's call screen (frame 2): the speaking side's newest line large, the other side's last
/// line small and faint. In a pause, whoever spoke last stays large.
struct CallLines {
    var current: Caption?
    var other: Caption?
    var currentSide: VoiceSide?

    init(captions: [Caption], speaking: VoiceSide?) {
        let side = speaking ?? captions.last.map { $0.isMine ? .person : .ai }
        currentSide = side
        guard let side else { return }
        let mine = side == .person
        current = captions.last { $0.isMine == mine }
        other = captions.last { $0.isMine != mine }
    }
}

/// The call's clock, as the Phone app writes it: 0:05, 3:38, 12:05, 1:02:03. Empty before the AI is there.
enum CallClock {
    static func text(since start: Date?, now: Date) -> String {
        guard let start else { return "" }
        let seconds = max(Int(now.timeIntervalSince(start)), 0)
        let (hours, minutes, rest) = (seconds / 3600, seconds / 60 % 60, seconds % 60)
        return hours > 0 ? String(format: "%d:%02d:%02d", hours, minutes, rest)
            : String(format: "%d:%02d", minutes, rest)
    }
}

/// The orb on the call screen (design D, from B): yellow for the AI, blue for the person. It swells and glows
/// with the voice; in silence it rests, still, in the colour of whoever spoke last (the AI before anyone has).
/// With Reduce Motion on it keeps its size and only its glow follows the voice, without animation.
struct OrbLook: Equatable {
    var side: VoiceSide
    var scale: CGFloat
    var glow: Double // 0 to 1: the halo around the orb
    var animates: Bool

    init(_ indicator: VoiceIndicator, lastSide: VoiceSide?, reduceMotion: Bool) {
        side = indicator.side ?? lastSide ?? .ai
        let amount = indicator.side == nil ? 0 : CGFloat(indicator.amount)
        glow = 0.35 + 0.65 * Double(amount)
        scale = reduceMotion ? 1 : 1 + 0.12 * amount
        animates = !reduceMotion
    }
}
