import AVFoundation
import Foundation
import Observation

/// GET /voices?language= (contract section 5): one call language's voices, 1 to 12, the default first, for the
/// first run's second step and the settings sheet. Asked for once per language, and again after a failure.
@Observable
final class VoiceCatalog {
    private(set) var voices: [CallLanguage: [Voice]] = [:]
    private(set) var failed: Set<CallLanguage> = []
    @ObservationIgnored private let server: ServerClient

    init(server: ServerClient = ContractServer.client) {
        self.server = server
    }

    func load(_ language: CallLanguage) async {
        guard voices[language] == nil, language != .unrecognized else { return }
        guard case .ok(200, let data) = await server.send("/voices?language=\(language.rawValue)"),
              let list = try? Contract.decode(VoiceList.self, from: data), !list.voices.isEmpty else {
            failed.insert(language)
            return
        }
        voices[language] = list.voices
        failed.remove(language)
    }
}

enum VoiceChoice {
    /// The voice shown as picked: the server's when it is in the list; null, or a voice of another language, is
    /// the language's default (contract section 6).
    static func effective(stored: String?, in voices: [Voice]) -> Voice? {
        voices.first { $0.id == stored } ?? voices.first(where: \.isDefault) ?? voices.first
    }
}

/// Plays and stops a voice's sample.
protocol VoiceSamples {
    func play(_ voice: Voice)
    func stop()
}

/// The samples in the app: AVPlayer streams `sample_url` from the token server, which serves samples without the
/// sign-in. A sample plays as media, never while a call is on, when CallKit owns the audio session.
final class SamplePlayer: VoiceSamples {
    private var player: AVPlayer?
    private let callIsOn: () -> Bool

    init(callIsOn: @escaping () -> Bool) {
        self.callIsOn = callIsOn
    }

    static func url(of voice: Voice) -> URL? {
        URL(string: Server.api + voice.sampleURL)
    }

    func play(_ voice: Voice) {
        guard !callIsOn(), let url = Self.url(of: voice) else { return }
        let session = AVAudioSession.sharedInstance()
        try? session.setCategory(.playback, mode: .spokenAudio)
        try? session.setActive(true)
        player = AVPlayer(url: url)
        player?.play()
    }

    func stop() {
        player?.pause()
        player = nil
        guard !callIsOn() else { return }
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
    }
}

/// The one sample playing in a voice list: the yellow button plays a voice for 5 seconds, or stops it.
@Observable
final class VoicePreview {
    private(set) var playing: String?
    private var startedAt: Date?
    @ObservationIgnored private let samples: any VoiceSamples
    @ObservationIgnored private let length: Duration
    @ObservationIgnored private var ending: Task<Void, Never>?

    init(samples: any VoiceSamples, length: Duration = .seconds(5)) {
        self.samples = samples
        self.length = length
    }

    /// Plays the voice's sample, stopping any other; on the voice that is playing, stops it.
    func toggle(_ voice: Voice, now: Date = .now) {
        let wasPlaying = playing == voice.id
        stop()
        guard !wasPlaying else { return }
        playing = voice.id
        startedAt = now
        samples.play(voice)
        ending = Task { [weak self, length] in
            try? await Task.sleep(for: length)
            guard !Task.isCancelled else { return }
            self?.stop()
        }
    }

    /// Stops the sample, as at its end or when the list goes away.
    func stop() {
        ending?.cancel()
        ending = nil
        guard playing != nil else { return }
        samples.stop()
        playing = nil
        startedAt = nil
    }

    /// How much of the sample has played, 0 to 1: the ring around the stop button.
    func progress(at now: Date) -> Double {
        guard let startedAt else { return 0 }
        let seconds = Double(length.components.seconds) + Double(length.components.attoseconds) / 1e18
        return min(max(now.timeIntervalSince(startedAt) / seconds, 0), 1)
    }
}
