import Accelerate
import AVFoundation
import SwiftUI

/// How loud an audio buffer is, from 0 (silence and room noise, -50 dBFS and below) to 1 (loud speech, -10 dBFS
/// and above), on a decibel scale, which is closer to how loudness is heard. Runs on LiveKit's audio thread for
/// every 10 ms buffer, so it only reads the samples: no allocation, no lock.
nonisolated enum VoiceLevel {
    static let floorDB: Float = -50
    static let ceilingDB: Float = -10

    static func of(_ buffer: AVAudioPCMBuffer) -> Float {
        fromRMS(rms(of: buffer))
    }

    /// The root mean square of every sample in every channel, full scale being 1.
    static func rms(of buffer: AVAudioPCMBuffer) -> Float {
        let frames = Int(buffer.frameLength), channels = Int(buffer.format.channelCount), stride = buffer.stride
        guard frames > 0, channels > 0 else { return 0 }
        var meanSquares: Float = 0
        switch buffer.format.commonFormat {
        case .pcmFormatFloat32:
            guard let data = buffer.floatChannelData else { return 0 }
            for channel in 0..<channels {
                var meanSquare: Float = 0
                vDSP_measqv(data[channel], stride, &meanSquare, vDSP_Length(frames))
                meanSquares += meanSquare
            }
        case .pcmFormatInt16: // what LiveKit's renderers get
            guard let data = buffer.int16ChannelData else { return 0 }
            for channel in 0..<channels {
                var total: Float = 0
                for frame in 0..<frames {
                    let sample = Float(data[channel][frame * stride]) / 32_768
                    total += sample * sample
                }
                meanSquares += total / Float(frames)
            }
        case .pcmFormatInt32:
            guard let data = buffer.int32ChannelData else { return 0 }
            for channel in 0..<channels {
                var total: Float = 0
                for frame in 0..<frames {
                    let sample = Float(data[channel][frame * stride]) / 2_147_483_648
                    total += sample * sample
                }
                meanSquares += total / Float(frames)
            }
        default:
            return 0
        }
        return (meanSquares / Float(channels)).squareRoot()
    }

    static func fromRMS(_ rms: Float) -> Float {
        guard rms > 0 else { return 0 }
        let decibels = 20 * log10(rms)
        return min(max((decibels - floorDB) / (ceilingDB - floorDB), 0), 1)
    }
}

/// Smooths a voice level so the glow follows speech without jitter: it rises fast when a sound starts (attack)
/// and falls more slowly when it stops (release), so it does not blink out between words.
struct LevelSmoother {
    static let attackSeconds = 0.04
    static let releaseSeconds = 0.25

    private(set) var value: Float

    init(value: Float = 0) {
        self.value = value
    }

    /// The level after `seconds` more of `input`: it moves toward the input, never past it.
    mutating func update(toward input: Float, after seconds: Double) -> Float {
        guard seconds > 0 else { return value }
        let timeConstant = input > value ? Self.attackSeconds : Self.releaseSeconds
        value += (input - value) * Float(1 - exp(-seconds / timeConstant))
        return value
    }
}

/// Whose voice the indicator shows.
enum VoiceSide: Equatable {
    case person, ai
}

/// The live "who is speaking" indicator around the call screen's main button: which voice it shows, and how
/// strongly (0, at rest, to 1).
struct VoiceIndicator: Equatable {
    var side: VoiceSide?
    var amount: Float

    static let rest = VoiceIndicator(side: nil, amount: 0)
    /// A level at or below this (-40 dBFS) is breath or room noise: no glow.
    static let quiet: Float = 0.25
    /// How much louder the other voice must be to take the indicator over, so the colour does not flicker when
    /// both sound at once (the person cutting in, or "mm-hm" over the AI).
    static let takeOverMargin: Float = 0.15

    /// How strongly a smoothed level shows: nothing up to `quiet`, then growing to 1 with the level.
    static func amount(for level: Float) -> Float {
        min(max((level - quiet) / (1 - quiet), 0), 1)
    }

    /// The voice to show from both voices' amounts: neither in silence, the only one sounding, and when both
    /// sound, the louder one, though the one already shown stays until the other is clearly louder.
    static func side(person: Float, ai: Float, current: VoiceSide?) -> VoiceSide? {
        switch (person > 0, ai > 0) {
        case (false, false): return nil
        case (true, false): return .person
        case (false, true): return .ai
        case (true, true):
            let louder: VoiceSide = person >= ai ? .person : .ai
            guard let current, current != louder else { return louder }
            return abs(person - ai) > takeOverMargin ? louder : current
        }
    }
}

/// One update of the indicator from both voices' newest levels: smoothed, gated, and one side chosen.
struct VoiceMix {
    private var person = LevelSmoother()
    private var ai = LevelSmoother()
    private var side: VoiceSide?

    mutating func update(person personLevel: Float, ai aiLevel: Float, after seconds: Double) -> VoiceIndicator {
        let personAmount = VoiceIndicator.amount(for: person.update(toward: personLevel, after: seconds))
        let aiAmount = VoiceIndicator.amount(for: ai.update(toward: aiLevel, after: seconds))
        side = VoiceIndicator.side(person: personAmount, ai: aiAmount, current: side)
        switch side {
        case .person?: return VoiceIndicator(side: .person, amount: personAmount)
        case .ai?: return VoiceIndicator(side: .ai, amount: aiAmount)
        case nil: return .rest
        }
    }
}
