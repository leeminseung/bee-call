import Foundation
import Observation

/// Design D, frame 7: the first run, shown once before the home. Step 1 picks the call language, step 2 the voice
/// heard on calls, each from a list in the iOS Settings look; the settings sheet (frame 8) opens the same lists. Both
/// belong to the app after D (contract section 6): each pick goes to the server at once (PUT /settings,
/// PersonStore), and the server's GET /me says what is picked. Only "the first run is over" stays on the phone.
enum FirstRun {
    static let doneKey = "first_run_done"

    static func isNeeded(_ store: UserDefaults) -> Bool {
        !store.bool(forKey: doneKey)
    }

    /// The language the first run starts on: the one the server holds for the person; before GET /me has come, the
    /// phone's own.
    static func language(settings: PersonSettings?, preferred: [String]) -> CallLanguage {
        if let language = settings?.language, language != .unrecognized { return language }
        return CallLanguage.initial(written: nil, preferred: preferred)
    }

    /// "시작하기": the first run is over for good.
    static func finish(_ store: UserDefaults) {
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
    /// The list's name: its language's voices.
    func voices(_ language: CallLanguage) -> String {
        switch (language, korean) {
        case (.ko, true): "한국어 목소리"
        case (_, true): "영어 목소리"
        case (.ko, false): "Korean voices"
        case (_, false): "English voices"
        }
    }

    var voicesFailed: String { korean ? "목소리를 불러오지 못했어요." : "The voices could not be loaded." }
    var tryAgain: String { korean ? "다시 시도" : "Try again" }
    func notSaved(_ problem: String) -> String { korean ? "저장하지 못했어요: \(problem)" : "Not saved: \(problem)" }
    var next: String { korean ? "다음" : "Next" }
    var start: String { korean ? "시작하기" : "Get started" }

    /// The line under a voice's name: how it sounds, in the app's language, and whether its sample is playing.
    func manner(_ voice: Voice, playing: Bool) -> String {
        let character = korean ? voice.character.ko : voice.character.en
        return playing ? "\(character) · \(korean ? "듣는 중" : "playing")" : character
    }

    func play(_ voice: Voice) -> String { korean ? "\(voice.name) 5초 들어 보기" : "Hear \(voice.name) for 5 seconds" }
    func stop(_ voice: Voice) -> String { korean ? "\(voice.name) 멈추기" : "Stop \(voice.name)" }
}
