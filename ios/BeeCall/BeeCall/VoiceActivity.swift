import AVFoundation
import LiveKit
import Observation
import os

/// Keeps the loudest level one voice reached since it was last read. LiveKit calls `render(pcmBuffer:)` on its
/// audio thread for every 10 ms buffer; the call screen takes the level about 30 times a second.
nonisolated final class VoiceLevelTap: NSObject, AudioRenderer, Sendable {
    private let loudest = OSAllocatedUnfairLock<Float>(initialState: 0)

    func render(pcmBuffer: AVAudioPCMBuffer) {
        note(VoiceLevel.of(pcmBuffer))
    }

    func note(_ level: Float) {
        loudest.withLock { $0 = max($0, level) }
    }

    /// The loudest level since the last read; the next read starts from silence.
    func take() -> Float {
        loudest.withLock { level in
            defer { level = 0 }
            return level
        }
    }
}

/// Both voices' live levels in a call, for the indicator around the call screen's main button
/// (VoiceIndicator.swift). It only reads LiveKit's audio as it passes: the microphone after echo cancellation
/// (the local renderer) and the room's audio as it is played (the remote renderer). It never touches the
/// audio session or the published track. From the start of a call to its end it updates about 30 times a
/// second, publishing only changes.
@Observable
final class VoiceActivity {
    static let updatesPerSecond = 30.0

    private(set) var indicator = VoiceIndicator.rest
    /// The AI's side counts only once the voice server has answered: before that the room's audio is the
    /// ringing a caller hears (voice/ringback.py).
    @ObservationIgnored var aiHasAnswered = false
    /// While muted, sound at the microphone is not the person talking to the AI.
    @ObservationIgnored var microphoneOn = true
    @ObservationIgnored let person = VoiceLevelTap()
    @ObservationIgnored let ai = VoiceLevelTap()
    @ObservationIgnored private var mix = VoiceMix()
    @ObservationIgnored private var lastTick: ContinuousClock.Instant?
    @ObservationIgnored private var ticker: Task<Void, Never>?

    /// From the moment the call joins its room.
    func start() {
        guard ticker == nil else { return }
        AudioManager.shared.add(localAudioRenderer: person)
        AudioManager.shared.add(remoteAudioRenderer: ai)
        ticker = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                self.tick()
                try? await Task.sleep(for: .seconds(1 / Self.updatesPerSecond))
            }
        }
    }

    /// At the end of the call: the indicator rests, and the next call starts unmuted and before the AI answers.
    func stop() {
        ticker?.cancel()
        ticker = nil
        AudioManager.shared.remove(localAudioRenderer: person)
        AudioManager.shared.remove(remoteAudioRenderer: ai)
        _ = person.take()
        _ = ai.take()
        mix = VoiceMix()
        lastTick = nil
        aiHasAnswered = false
        microphoneOn = true
        indicator = .rest
    }

    /// One update, from the loudest level of each voice since the last one.
    func tick(at now: ContinuousClock.Instant = .now) {
        let seconds = lastTick.map { $0.duration(to: now) / .seconds(1) } ?? 1 / Self.updatesPerSecond
        lastTick = now
        let personLevel = person.take(), aiLevel = ai.take()
        let next = mix.update(person: microphoneOn ? personLevel : 0, ai: aiHasAnswered ? aiLevel : 0,
                              after: seconds)
        if next != indicator { indicator = next }
    }
}
