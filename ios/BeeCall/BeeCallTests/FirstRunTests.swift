import XCTest
@testable import BeeCall

/// Design D, frame 7: the first run picks the call language, then the voice, each from a list in the iOS Settings
/// look; a yellow button plays a voice's sample for 5 seconds (contract sections 5 and 6).
final class FirstRunTests: XCTestCase {
    /// The Korean voices of the examples, whose ids, names and counts the server's tests regenerate.
    @MainActor private func voices() throws -> [Voice] {
        try Examples.decode(VoiceList.self, "voices").voices
    }

    /// The language's default voice, as the file marks it.
    @MainActor private var standard: Voice { get throws { try XCTUnwrap(try voices().first(where: \.isDefault)) } }
    /// A voice that is not the default (the last one listed).
    @MainActor private var other: Voice { get throws { try XCTUnwrap(try voices().last { !$0.isDefault }) } }

    // MARK: - Language

    /// The first run starts on the language the person already picked, else on the phone's own when it is Korean
    /// or English, else on English.
    @MainActor func testTheFirstRunStartsOnThePickedLanguageElseThePhones() {
        XCTAssertEqual(CallLanguage.initial(written: nil, preferred: ["ko-KR", "en-US"]), .ko)
        XCTAssertEqual(CallLanguage.initial(written: nil, preferred: ["ja-JP", "ko-KR"]), .ko)
        XCTAssertEqual(CallLanguage.initial(written: nil, preferred: ["en-GB"]), .en)
        XCTAssertEqual(CallLanguage.initial(written: nil, preferred: ["ja-JP"]), .en)
        XCTAssertEqual(CallLanguage.initial(written: nil, preferred: []), .en)
        XCTAssertEqual(CallLanguage.initial(written: "en", preferred: ["ko-KR"]), .en)
        XCTAssertEqual(CallLanguage.initial(written: "ko", preferred: ["en-US"]), .ko)
        XCTAssertEqual(CallLanguage.initial(written: "fr", preferred: ["ko-KR"]), .ko, "an unknown value is not a pick")
    }

    /// Each row has the language's own name and, under it, its name in the other language.
    @MainActor func testEachLanguageRowNamesItselfForBothReaders() {
        XCTAssertEqual(CallLanguage.choices, [.ko, .en])
        XCTAssertEqual(CallLanguage.ko.name, "한국어")
        XCTAssertEqual(CallLanguage.ko.otherName, "Korean")
        XCTAssertEqual(CallLanguage.en.name, "English")
        XCTAssertEqual(CallLanguage.en.otherName, "영어")
    }

    /// The first run starts on the language the server holds for the person; before GET /me has come, on the
    /// phone's own.
    @MainActor func testTheFirstRunStartsOnTheServersLanguageElseThePhones() throws {
        let me = try Examples.decode(Me.self, "me")
        XCTAssertEqual(FirstRun.language(settings: me.settings, preferred: ["fr-FR"]), me.settings.language)
        XCTAssertEqual(FirstRun.language(settings: nil, preferred: ["en-US"]), .en)
        XCTAssertEqual(FirstRun.language(settings: nil, preferred: ["ko-KR"]), .ko)
    }

    // MARK: - Voice

    /// The voice shown as picked: the server's when it is in the list; null, or a voice of the other language,
    /// is the language's default (contract section 6).
    @MainActor func testThePickedVoiceIsTheServersElseTheDefault() throws {
        let voices = try voices(), other = try other, standard = try standard
        let english = try XCTUnwrap(try Examples.decode(VoiceList.self, "voices-en").voices.first?.id)
        XCTAssertEqual(VoiceChoice.effective(stored: other.id, in: voices), other)
        XCTAssertEqual(VoiceChoice.effective(stored: nil, in: voices), standard, "the entry marked default")
        XCTAssertEqual(VoiceChoice.effective(stored: english, in: voices), standard, "another language's voice")
        XCTAssertNil(VoiceChoice.effective(stored: other.id, in: []))
        let me = try Examples.decode(Me.self, "me").settings
        let theirs = try [Examples.decode(VoiceList.self, "voices"), Examples.decode(VoiceList.self, "voices-en")]
            .first { $0.language == me.language }?.voices ?? []
        let shown = me.voice == nil ? theirs.first(where: \.isDefault) : theirs.first { $0.id == me.voice }
        XCTAssertNotNil(shown, "me.json's voice is one of its language's voices")
        XCTAssertEqual(VoiceChoice.effective(stored: me.voice, in: theirs), shown, "the person's stored voice is shown")
    }

    /// "시작하기" ends the first run for good; the language and the voice went to the server when picked.
    @MainActor func testStartingEndsTheFirstRun() {
        let store = freshDefaults(self)
        XCTAssertTrue(FirstRun.isNeeded(store))
        FirstRun.finish(store)
        XCTAssertFalse(FirstRun.isNeeded(store))
    }

    // MARK: - 5-second samples

    @MainActor func testOneSamplePlaysAtATimeAndASecondTapStopsIt() throws {
        let samples = FakeSamples(), preview = VoicePreview(samples: samples)
        let first = try standard, second = try other
        preview.toggle(first)
        XCTAssertEqual(preview.playing, first.id)
        preview.toggle(second)
        XCTAssertEqual(preview.playing, second.id, "the yellow button of another voice switches to it")
        XCTAssertEqual(samples.played, [first.id, second.id])
        XCTAssertEqual(samples.stops, 1)
        preview.toggle(second)
        XCTAssertNil(preview.playing, "the stop button stops it")
        XCTAssertEqual(samples.stops, 2)
    }

    @MainActor func testASampleStopsByItselfAtItsEnd() async throws {
        let samples = FakeSamples(), preview = VoicePreview(samples: samples, length: .milliseconds(100))
        preview.toggle(try other)
        await waitUntil("the sample ends") { preview.playing == nil }
        XCTAssertEqual(samples.stops, 1)
    }

    /// The ring around the stop button: how much of the 5 seconds has played.
    @MainActor func testTheRingShowsHowMuchHasPlayed() throws {
        let preview = VoicePreview(samples: FakeSamples())
        let start = Date(timeIntervalSince1970: 1_000)
        XCTAssertEqual(preview.progress(at: start), 0)
        preview.toggle(try other, now: start)
        XCTAssertEqual(preview.progress(at: start.addingTimeInterval(2)), 0.4, accuracy: 0.001)
        XCTAssertEqual(preview.progress(at: start.addingTimeInterval(9)), 1)
    }

    /// Leaving the list stops the sample, and stopping twice stops the sound once.
    @MainActor func testLeavingTheListStopsTheSample() throws {
        let samples = FakeSamples(), preview = VoicePreview(samples: samples)
        preview.toggle(try standard)
        preview.stop()
        preview.stop()
        XCTAssertNil(preview.playing)
        XCTAssertEqual(samples.stops, 1)
        XCTAssertEqual(preview.progress(at: .now), 0)
    }

    // MARK: - Words

    /// The words in the app's language; a voice's character in both, so it reads in the app's language too.
    @MainActor func testTheFirstRunReadsInBothLanguages() throws {
        let ko = FirstRunWords(korean: true), en = FirstRunWords(korean: false), voice = try other
        XCTAssertEqual(ko.step(1), "1단계 / 2단계")
        XCTAssertEqual(en.step(2), "Step 2 of 2")
        XCTAssertEqual(ko.voices(.ko), "한국어 목소리")
        XCTAssertEqual(en.voices(.en), "English voices")
        XCTAssertEqual(ko.manner(voice, playing: true), "\(voice.character.ko) · 듣는 중")
        XCTAssertEqual(ko.manner(voice, playing: false), voice.character.ko)
        XCTAssertEqual(en.manner(voice, playing: true), "\(voice.character.en) · playing")
        XCTAssertEqual(ko.play(voice), "\(voice.name) 5초 들어 보기")
        XCTAssertEqual(ko.stop(voice), "\(voice.name) 멈추기")
        XCTAssertEqual(en.play(voice), "Hear \(voice.name) for 5 seconds")
        XCTAssertEqual(en.stop(voice), "Stop \(voice.name)")
        XCTAssertEqual(ko.next, "다음")
        XCTAssertEqual(en.start, "Get started")
    }
}

/// The 5-second samples' stand-in: what was played and how often the sound was stopped.
final class FakeSamples: VoiceSamples {
    private(set) var played: [String] = []
    private(set) var stops = 0

    func play(_ voice: Voice) {
        played.append(voice.id)
    }

    func stop() {
        stops += 1
    }
}
