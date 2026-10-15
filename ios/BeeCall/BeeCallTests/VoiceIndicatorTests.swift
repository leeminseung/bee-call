import AVFoundation
import SwiftUI
import XCTest
@testable import BeeCall

/// The live "who is speaking" indicator around the call screen's main button: levels from audio buffers,
/// smoothing, the glow they make, and which voice it shows.
final class VoiceIndicatorTests: XCTestCase {
    // MARK: - Level from a buffer

    /// A 10 ms buffer of a 440 Hz sine whose RMS is `dbfs` decibels below full scale.
    private func sine(dbfs: Float, format: AVAudioCommonFormat = .pcmFormatFloat32, channels: AVAudioChannelCount = 1,
                      frames: AVAudioFrameCount = 480) -> AVAudioPCMBuffer {
        let audio = AVAudioFormat(commonFormat: format, sampleRate: 48_000, channels: channels, interleaved: false)!
        let buffer = AVAudioPCMBuffer(pcmFormat: audio, frameCapacity: frames)!
        buffer.frameLength = frames
        let amplitude = powf(10, dbfs / 20) * Float(2).squareRoot() // a sine's RMS is its amplitude / sqrt(2)
        for channel in 0..<Int(channels) {
            for i in 0..<Int(frames) {
                let sample = amplitude * sinf(2 * .pi * 440 * Float(i) / 48_000)
                switch format {
                case .pcmFormatInt16: buffer.int16ChannelData![channel][i] = Int16(sample * 32_767)
                default: buffer.floatChannelData![channel][i] = sample
                }
            }
        }
        return buffer
    }

    @MainActor func testSilenceHasNoLevel() {
        XCTAssertEqual(VoiceLevel.of(sine(dbfs: -200)), 0)
    }

    @MainActor func testTheLevelFollowsLoudnessInDecibels() {
        XCTAssertEqual(VoiceLevel.of(sine(dbfs: -50)), 0, accuracy: 0.02) // the quiet end
        XCTAssertEqual(VoiceLevel.of(sine(dbfs: -30)), 0.5, accuracy: 0.02) // ordinary speech
        XCTAssertEqual(VoiceLevel.of(sine(dbfs: -10)), 1, accuracy: 0.02) // loud
        XCTAssertEqual(VoiceLevel.of(sine(dbfs: -3)), 1) // never more than 1
        XCTAssertEqual(VoiceLevel.of(sine(dbfs: -70)), 0) // room noise
    }

    /// LiveKit hands its renderers 16-bit buffers; other paths may hand float ones.
    @MainActor func testSixteenBitAndFloatBuffersReadAlike() {
        XCTAssertEqual(VoiceLevel.of(sine(dbfs: -30, format: .pcmFormatInt16)), VoiceLevel.of(sine(dbfs: -30)),
                       accuracy: 0.01)
    }

    @MainActor func testEveryChannelCounts() {
        XCTAssertEqual(VoiceLevel.of(sine(dbfs: -30, channels: 2)), 0.5, accuracy: 0.02)
    }

    @MainActor func testAnEmptyBufferHasNoLevel() {
        XCTAssertEqual(VoiceLevel.of(sine(dbfs: -10, frames: 0)), 0)
    }

    // MARK: - Smoothing: a fast attack and a slower release

    private let tick = 1.0 / 30 // the indicator updates about 30 times a second

    @MainActor func testTheLevelRisesFast() {
        var smoother = LevelSmoother()
        var value: Float = 0
        for _ in 0..<3 { value = smoother.update(toward: 1, after: tick) } // 100 ms
        XCTAssertGreaterThan(value, 0.9)
    }

    @MainActor func testTheLevelFallsSlowly() {
        var smoother = LevelSmoother(value: 1)
        var value: Float = 1
        for _ in 0..<3 { value = smoother.update(toward: 0, after: tick) } // 100 ms: still well lit
        XCTAssertGreaterThan(value, 0.5)
        for _ in 0..<27 { value = smoother.update(toward: 0, after: tick) } // 1 s in all: nearly out
        XCTAssertLessThan(value, 0.05)
    }

    @MainActor func testTheLevelNeverOvershoots() {
        var smoother = LevelSmoother()
        for _ in 0..<60 { XCTAssertLessThanOrEqual(smoother.update(toward: 0.7, after: tick), 0.7) }
        for _ in 0..<60 { XCTAssertGreaterThanOrEqual(smoother.update(toward: 0.2, after: tick), 0.2) }
    }

    @MainActor func testNoTimeNoChange() {
        var smoother = LevelSmoother(value: 0.4)
        XCTAssertEqual(smoother.update(toward: 1, after: 0), 0.4)
    }

    // MARK: - From level to glow

    @MainActor func testQuietSoundsMakeNoGlow() {
        XCTAssertEqual(VoiceIndicator.amount(for: 0), 0)
        XCTAssertEqual(VoiceIndicator.amount(for: VoiceIndicator.quiet), 0)
    }

    @MainActor func testTheGlowGrowsWithTheLevel() {
        XCTAssertEqual(VoiceIndicator.amount(for: 1), 1)
        let levels: [Float] = [0.3, 0.45, 0.6, 0.75, 0.9]
        let amounts = levels.map(VoiceIndicator.amount(for:))
        XCTAssertEqual(amounts, amounts.sorted())
        XCTAssertEqual(Set(amounts).count, levels.count, "every step brighter")
    }

    // MARK: - Which voice the indicator shows

    @MainActor func testSilenceShowsNeither() {
        XCTAssertNil(VoiceIndicator.side(person: 0, ai: 0, current: nil))
        XCTAssertNil(VoiceIndicator.side(person: 0, ai: 0, current: .person))
    }

    @MainActor func testTheOnlyVoiceIsShown() {
        XCTAssertEqual(VoiceIndicator.side(person: 0.5, ai: 0, current: nil), .person)
        XCTAssertEqual(VoiceIndicator.side(person: 0, ai: 0.5, current: nil), .ai)
        XCTAssertEqual(VoiceIndicator.side(person: 0, ai: 0.2, current: .person), .ai)
    }

    @MainActor func testWhenBothSoundTheClearlyLouderOneIsShown() {
        XCTAssertEqual(VoiceIndicator.side(person: 0.7, ai: 0.3, current: nil), .person)
        XCTAssertEqual(VoiceIndicator.side(person: 0.3, ai: 0.7, current: .person), .ai)
        XCTAssertEqual(VoiceIndicator.side(person: 0.9, ai: 0.5, current: .ai), .person) // the person cuts in
    }

    /// Close levels keep the side already shown, so the colour does not flicker.
    @MainActor func testCloseLevelsKeepTheSideShown() {
        XCTAssertEqual(VoiceIndicator.side(person: 0.45, ai: 0.55, current: .person), .person)
        XCTAssertEqual(VoiceIndicator.side(person: 0.55, ai: 0.45, current: .ai), .ai)
    }

    // MARK: - One update of the indicator

    @MainActor func testSpeechLightsTheIndicatorAndSilenceRestsIt() {
        var mix = VoiceMix()
        var shown = mix.update(person: 0.9, ai: 0, after: tick)
        for _ in 0..<5 { shown = mix.update(person: 0.9, ai: 0, after: tick) }
        XCTAssertEqual(shown.side, .person)
        XCTAssertGreaterThan(shown.amount, 0.5)
        for _ in 0..<45 { shown = mix.update(person: 0, ai: 0, after: tick) } // 1.5 s of silence
        XCTAssertNil(shown.side)
        XCTAssertEqual(shown.amount, 0)
    }

    @MainActor func testTheAIsVoiceShowsAsTheAIs() {
        var mix = VoiceMix()
        var shown = VoiceIndicator.rest
        for _ in 0..<6 { shown = mix.update(person: 0, ai: 0.8, after: tick) }
        XCTAssertEqual(shown.side, .ai)
        XCTAssertGreaterThan(shown.amount, 0.4)
    }

    // MARK: - The glow it makes

    @MainActor func testAtRestThereIsNoGlow() {
        XCTAssertEqual(VoiceGlowLook(VoiceIndicator.rest, reduceMotion: false).opacity, 0)
        XCTAssertEqual(VoiceGlowLook(VoiceIndicator.rest, reduceMotion: true).opacity, 0)
    }

    @MainActor func testThePersonGlowsBlueAndTheAIBeeYellow() {
        XCTAssertEqual(VoiceGlowLook(VoiceIndicator(side: .person, amount: 0.5), reduceMotion: false).color,
                       VoiceGlowLook.personBlue)
        XCTAssertEqual(VoiceGlowLook(VoiceIndicator(side: .ai, amount: 0.5), reduceMotion: false).color,
                       Color(red: 1, green: 0xD9 / 255.0, blue: 0)) // Bee yellow, #FFD900
    }

    @MainActor func testALouderVoiceGlowsWiderAndBrighter() {
        let soft = VoiceGlowLook(VoiceIndicator(side: .person, amount: 0.2), reduceMotion: false)
        let loud = VoiceGlowLook(VoiceIndicator(side: .person, amount: 0.9), reduceMotion: false)
        XCTAssertGreaterThan(loud.opacity, soft.opacity)
        XCTAssertGreaterThan(loud.blur, soft.blur)
        XCTAssertGreaterThan(loud.scale, soft.scale)
        XCTAssertTrue(loud.animates)
    }

    /// With Reduce Motion on, only the opacity follows the voice: no growing, no animation.
    @MainActor func testReduceMotionOnlyChangesOpacity() {
        let soft = VoiceGlowLook(VoiceIndicator(side: .ai, amount: 0.2), reduceMotion: true)
        let loud = VoiceGlowLook(VoiceIndicator(side: .ai, amount: 0.9), reduceMotion: true)
        XCTAssertGreaterThan(loud.opacity, soft.opacity)
        XCTAssertEqual(loud.blur, soft.blur)
        XCTAssertEqual(loud.scale, soft.scale)
        XCTAssertFalse(loud.animates)
    }
}
