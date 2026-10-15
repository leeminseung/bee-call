import AVFoundation
import XCTest
@testable import BeeCall

/// Both voices' live levels in a call, as LiveKit's audio renderers hand them over (VoiceActivity.swift).
final class VoiceActivityTests: XCTestCase {
    /// A 10 ms 16-bit buffer, as LiveKit hands its renderers, at `dbfs` RMS (a square wave keeps it exact).
    private func buffer(dbfs: Float) -> AVAudioPCMBuffer {
        let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 48_000, channels: 1, interleaved: false)!
        let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: 480)!
        buffer.frameLength = 480
        let sample = Int16(powf(10, dbfs / 20) * 32_768)
        for i in 0..<480 { buffer.int16ChannelData![0][i] = i.isMultiple(of: 2) ? sample : -sample }
        return buffer
    }

    private let start = ContinuousClock.now

    /// Runs `ticks` updates 1/30 s apart, rendering `person` and `ai` buffers before each.
    @MainActor private func run(_ voices: VoiceActivity, ticks: Int, person: Float? = nil, ai: Float? = nil,
                                from first: Int = 0) {
        for n in first..<(first + ticks) {
            if let person { voices.person.render(pcmBuffer: buffer(dbfs: person)) }
            if let ai { voices.ai.render(pcmBuffer: buffer(dbfs: ai)) }
            voices.tick(at: start + .milliseconds(n * 33))
        }
    }

    @MainActor func testTheLoudestBufferSinceTheLastReadCounts() {
        let tap = VoiceLevelTap()
        tap.render(pcmBuffer: buffer(dbfs: -45))
        tap.render(pcmBuffer: buffer(dbfs: -20))
        tap.render(pcmBuffer: buffer(dbfs: -45))
        XCTAssertEqual(tap.take(), 0.75, accuracy: 0.02) // -20 dBFS
        XCTAssertEqual(tap.take(), 0, "each read starts over")
    }

    @MainActor func testThePersonsVoiceLightsTheIndicator() {
        let voices = VoiceActivity()
        run(voices, ticks: 6, person: -15)
        XCTAssertEqual(voices.indicator.side, .person)
        XCTAssertGreaterThan(voices.indicator.amount, 0.5)
    }

    /// Before the voice server answers a call the person started, the room's audio is the ringing tone.
    @MainActor func testTheAIsVoiceCountsOnlyOnceTheAIAnswered() {
        let voices = VoiceActivity()
        run(voices, ticks: 6, ai: -15)
        XCTAssertEqual(voices.indicator, .rest)
        voices.aiHasAnswered = true
        run(voices, ticks: 6, ai: -15, from: 6)
        XCTAssertEqual(voices.indicator.side, .ai)
    }

    @MainActor func testAMutedMicrophoneShowsNothing() {
        let voices = VoiceActivity()
        voices.microphoneOn = false
        run(voices, ticks: 6, person: -15)
        XCTAssertEqual(voices.indicator, .rest)
    }

    @MainActor func testSilenceAfterSpeechRests() {
        let voices = VoiceActivity()
        run(voices, ticks: 6, person: -15)
        run(voices, ticks: 45, from: 6) // 1.5 s with nothing rendered
        XCTAssertEqual(voices.indicator, .rest)
    }

    @MainActor func testTheEndOfACallRestsTheIndicatorAndForgetsTheCall() {
        let voices = VoiceActivity()
        voices.aiHasAnswered = true
        voices.microphoneOn = false
        run(voices, ticks: 6, ai: -15)
        voices.stop()
        XCTAssertEqual(voices.indicator, .rest)
        XCTAssertFalse(voices.aiHasAnswered, "the next call starts before the AI answers")
        XCTAssertTrue(voices.microphoneOn, "the next call starts unmuted")
    }

    @MainActor func testTheScreenIsUpdatedAboutThirtyTimesASecond() {
        XCTAssertEqual(VoiceActivity.updatesPerSecond, 30)
    }
}
