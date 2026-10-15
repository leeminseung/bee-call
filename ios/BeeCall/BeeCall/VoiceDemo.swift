#if DEBUG
import Foundation

/// Debug builds only: launched with `-voiceDemo person`, `-voiceDemo ai` or `-voiceDemo both`, the app shows the
/// call screen in a pretend call whose voice indicator follows synthetic levels, to see the indicator and take
/// screenshots without a server, a call or a device. Launch arguments of that form land in UserDefaults.
enum VoiceDemo: String {
    case person, ai, both

    static func requested(in store: UserDefaults = .standard) -> VoiceDemo? {
        store.string(forKey: "voiceDemo").flatMap(VoiceDemo.init(rawValue:))
    }

    /// Both voices' levels `seconds` into the demo; in `both`, they take turns every 4 s.
    func levels(at seconds: Double) -> (person: Float, ai: Float) {
        let speech = Self.speech(at: seconds)
        switch self {
        case .person: return (speech, 0)
        case .ai: return (0, speech)
        case .both: return Int(seconds / 4).isMultiple(of: 2) ? (speech, 0) : (0, speech)
        }
    }

    /// Speech-like loudness between 0.45 and 0.95: about five syllables a second, in phrases that swell and fade.
    private static func speech(at seconds: Double) -> Float {
        let syllable = 0.5 + 0.5 * sin(seconds * 2 * .pi * 5)
        let phrase = 0.75 + 0.25 * sin(seconds * 2 * .pi * 0.7)
        return Float(0.45 + 0.5 * syllable * phrase)
    }
}
#endif
