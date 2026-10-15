import XCTest
@testable import BeeCall

/// Design D, frame 7: the first run picks the call language, then the voice, each from a list in the iOS Settings
/// look; a yellow button plays a voice's sample for 5 seconds (contract sections 5 and 6).
final class FirstRunTests: XCTestCase {
    @MainActor private func voices() throws -> [Voice] {
        try Contract.decode(VoiceList.self, from: ContractTests.example("voices")).voices
    }

    @MainActor private var harin: Voice { get throws { try voices()[0] } }
    @MainActor private var daon: Voice { get throws { try voices()[2] } }

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
        let me = try Contract.decode(Me.self, from: ContractTests.example("me"))
        XCTAssertEqual(FirstRun.language(me: me, preferred: ["en-US"]), .ko)
        XCTAssertEqual(FirstRun.language(me: nil, preferred: ["en-US"]), .en)
        XCTAssertEqual(FirstRun.language(me: nil, preferred: ["ko-KR"]), .ko)
    }

    // MARK: - Voice

    /// The voice shown as picked: the server's when it is in the list; null, or a voice of the other language,
    /// is the language's default (contract section 6).
    @MainActor func testThePickedVoiceIsTheServersElseTheDefault() throws {
        let voices = try voices()
        XCTAssertEqual(VoiceChoice.effective(stored: "ko-daon", in: voices), try daon)
        XCTAssertEqual(VoiceChoice.effective(stored: nil, in: voices), try harin, "the entry marked default")
        XCTAssertEqual(VoiceChoice.effective(stored: "en-zara", in: voices), try harin)
        XCTAssertNil(VoiceChoice.effective(stored: "ko-daon", in: []))
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
        let harin = try harin, daon = try daon
        preview.toggle(harin)
        XCTAssertEqual(preview.playing, "ko-harin")
        preview.toggle(daon)
        XCTAssertEqual(preview.playing, "ko-daon", "the yellow button of another voice switches to it")
        XCTAssertEqual(samples.played, ["ko-harin", "ko-daon"])
        XCTAssertEqual(samples.stops, 1)
        preview.toggle(daon)
        XCTAssertNil(preview.playing, "the stop button stops it")
        XCTAssertEqual(samples.stops, 2)
    }

    @MainActor func testASampleStopsByItselfAtItsEnd() async throws {
        let samples = FakeSamples(), preview = VoicePreview(samples: samples, length: .milliseconds(100))
        preview.toggle(try daon)
        await waitUntil("the sample ends") { preview.playing == nil }
        XCTAssertEqual(samples.stops, 1)
    }

    /// The ring around the stop button: how much of the 5 seconds has played.
    @MainActor func testTheRingShowsHowMuchHasPlayed() throws {
        let preview = VoicePreview(samples: FakeSamples())
        let start = Date(timeIntervalSince1970: 1_000)
        XCTAssertEqual(preview.progress(at: start), 0)
        preview.toggle(try daon, now: start)
        XCTAssertEqual(preview.progress(at: start.addingTimeInterval(2)), 0.4, accuracy: 0.001)
        XCTAssertEqual(preview.progress(at: start.addingTimeInterval(9)), 1)
    }

    /// Leaving the list stops the sample, and stopping twice stops the sound once.
    @MainActor func testLeavingTheListStopsTheSample() throws {
        let samples = FakeSamples(), preview = VoicePreview(samples: samples)
        preview.toggle(try harin)
        preview.stop()
        preview.stop()
        XCTAssertNil(preview.playing)
        XCTAssertEqual(samples.stops, 1)
        XCTAssertEqual(preview.progress(at: .now), 0)
    }

    // MARK: - Words

    /// The words in the app's language; a voice's character in both, so it reads in the app's language too.
    @MainActor func testTheFirstRunReadsInBothLanguages() throws {
        let ko = FirstRunWords(korean: true), en = FirstRunWords(korean: false), daon = try daon
        XCTAssertEqual(ko.step(1), "1단계 / 2단계")
        XCTAssertEqual(en.step(2), "Step 2 of 2")
        XCTAssertEqual(ko.voices(.ko), "한국어 목소리")
        XCTAssertEqual(en.voices(.en), "English voices")
        XCTAssertEqual(ko.manner(daon, playing: true), "또렷함 · 듣는 중")
        XCTAssertEqual(ko.manner(daon, playing: false), "또렷함")
        XCTAssertEqual(en.manner(daon, playing: true), "Clear · playing")
        XCTAssertEqual(ko.play(daon), "다온 5초 들어 보기")
        XCTAssertEqual(ko.stop(daon), "다온 멈추기")
        XCTAssertEqual(en.play(daon), "Hear 다온 for 5 seconds")
        XCTAssertEqual(en.stop(daon), "Stop 다온")
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
