import Foundation
import Observation

/// Design D, frame 7: the first run, shown once before the home. Step 1 picks the call language, step 2 the voice
/// heard on calls, each from a list in the iOS Settings look. The settings sheet (frame 8) opens the same lists.
/// The language is the app's "language" setting, which goes to the server with the others (SettingsSync); the
/// voice is kept on the phone until the server's contract says how it travels.
enum FirstRun {
    static let doneKey = "first_run_done"
    static let voiceKey = "voice"

    static func isNeeded(_ store: UserDefaults) -> Bool {
        !store.bool(forKey: doneKey)
    }

    /// Writes the language the first run starts on (CallLanguage.initial), so its screens and the server follow
    /// it at once.
    static func start(_ store: UserDefaults, written: String?, preferred: [String]) {
        store.set(CallLanguage.initial(written: written, preferred: preferred).rawValue, forKey: "language")
    }

    /// "시작하기": the voice shown as picked is the one kept, and the first run is over for good.
    static func finish(_ store: UserDefaults, voice: CallVoice?) {
        if let voice { store.set(voice.id, forKey: voiceKey) }
        store.set(true, forKey: doneKey)
    }
}

/// The two languages Bee Call speaks (the contract's `language`). A row shows the language's own name and, under
/// it, its name in the other language, so a reader of either finds theirs.
enum CallLanguage: String, ContractValue, Identifiable {
    case ko, en, unrecognized

    /// The languages to pick from, in the list's order.
    static let choices: [CallLanguage] = [.ko, .en]

    var id: String { rawValue }
    var name: String {
        switch self {
        case .ko: "한국어"
        case .en: "English"
        case .unrecognized: ""
        }
    }

    var otherName: String {
        switch self {
        case .ko: "Korean"
        case .en: "영어"
        case .unrecognized: ""
        }
    }

    /// The language the person already picked; else the phone's own, when it is Korean or English; else English.
    static func initial(written: String?, preferred: [String]) -> CallLanguage {
        if let written, let picked = CallLanguage(rawValue: written) { return picked }
        for language in preferred {
            if language.hasPrefix("ko") { return .ko }
            if language.hasPrefix("en") { return .en }
        }
        return .en
    }
}

/// A voice for calls: a name and a few words on how it sounds, such as 하린, 친근함.
struct CallVoice: Identifiable, Equatable {
    let id: String
    let name: String
    let manner: String
}

/// Where the voices of a language come from: the server, once its contract is in.
protocol VoiceCatalog {
    func voices(for language: CallLanguage) -> [CallVoice]
}

/// Plays and stops a voice's sample. The server will hold the samples; until then nothing plays.
protocol VoiceSamples {
    func play(_ voice: CallVoice)
    func stop()
}

enum VoiceChoice {
    /// The voice shown as picked: the stored one when it is in the list, else the list's first (nothing stored
    /// yet, or a voice of the other language after the language changed).
    static func effective(stored: String, in voices: [CallVoice]) -> CallVoice? {
        voices.first { $0.id == stored } ?? voices.first
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
    func toggle(_ voice: CallVoice, now: Date = .now) {
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

/// The first run's words, in the call language.
struct FirstRunWords {
    let korean: Bool

    func step(_ number: Int) -> String { korean ? "\(number)단계 / 2단계" : "Step \(number) of 2" }
    var languageTitle: String { korean ? "통화 언어" : "Call language" }
    var languageLead: String {
        korean ? "Bee Call과 이야기할 언어를 고르세요. 나중에 설정에서 바꿀 수 있어요."
            : "Pick the language you speak with Bee Call. You can change it later in Settings."
    }
    var languageBack: String { korean ? "언어" : "Language" }
    var voiceTitle: String { korean ? "목소리" : "Voice" }
    var voiceLead: String {
        korean ? "통화에서 들을 목소리를 고르세요. 노란 버튼을 누르면 5초 동안 들려 드려요."
            : "Pick the voice you hear on calls. The yellow button plays it for 5 seconds."
    }
    var voices: String { korean ? "한국어 목소리" : "English voices" }
    var next: String { korean ? "다음" : "Next" }
    var start: String { korean ? "시작하기" : "Get started" }

    /// The line under a voice's name: how it sounds, and whether its sample is playing.
    func manner(_ voice: CallVoice, playing: Bool) -> String {
        playing ? "\(voice.manner) · \(korean ? "듣는 중" : "playing")" : voice.manner
    }

    func play(_ voice: CallVoice) -> String { korean ? "\(voice.name) 5초 들어 보기" : "Hear \(voice.name) for 5 seconds" }
    func stop(_ voice: CallVoice) -> String { korean ? "\(voice.name) 멈추기" : "Stop \(voice.name)" }
}
