import XCTest
@testable import BeeCall

/// Design D, frame 7: the first run picks the call language, then the voice, each from a list in the iOS Settings
/// look; a yellow button plays a voice's sample for 5 seconds. The voices and their samples will come from the
/// server; until then these parts run on stand-ins.
final class FirstRunTests: XCTestCase {
    private let harin = CallVoice(id: "harin", name: "하린", manner: "친근함")
    private let daon = CallVoice(id: "daon", name: "다온", manner: "또렷함")

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
        XCTAssertEqual(CallLanguage.allCases, [.ko, .en])
        XCTAssertEqual(CallLanguage.ko.name, "한국어")
        XCTAssertEqual(CallLanguage.ko.otherName, "Korean")
        XCTAssertEqual(CallLanguage.en.name, "English")
        XCTAssertEqual(CallLanguage.en.otherName, "영어")
    }

    /// The language is written as the first run starts, so its screens and the server follow it at once.
    @MainActor func testTheFirstRunWritesTheLanguageItStartsOn() {
        let store = freshDefaults(self)
        FirstRun.start(store, written: nil, preferred: ["ko-KR"])
        XCTAssertEqual(store.string(forKey: "language"), "ko")
        XCTAssertTrue(FirstRun.isNeeded(store))
    }

    // MARK: - Voice

    /// The voice shown as picked: the stored one when it is in the list, else the list's first (a voice of the
    /// other language, after the language changed, or none stored yet).
    @MainActor func testThePickedVoiceIsTheStoredOneElseTheFirst() {
        let voices = [harin, daon]
        XCTAssertEqual(VoiceChoice.effective(stored: "daon", in: voices), daon)
        XCTAssertEqual(VoiceChoice.effective(stored: "sarah", in: voices), harin)
        XCTAssertEqual(VoiceChoice.effective(stored: "", in: voices), harin)
        XCTAssertNil(VoiceChoice.effective(stored: "daon", in: []))
    }

    /// "시작하기" keeps the voice shown as picked and ends the first run for good.
    @MainActor func testStartingKeepsTheVoiceAndEndsTheFirstRun() {
        let store = freshDefaults(self)
        XCTAssertTrue(FirstRun.isNeeded(store))
        FirstRun.finish(store, voice: harin)
        XCTAssertEqual(store.string(forKey: "voice"), "harin")
        XCTAssertFalse(FirstRun.isNeeded(store))
    }

    // MARK: - 5-second samples

    @MainActor func testOneSamplePlaysAtATimeAndASecondTapStopsIt() {
        let samples = FakeSamples(), preview = VoicePreview(samples: samples)
        preview.toggle(harin)
        XCTAssertEqual(preview.playing, "harin")
        preview.toggle(daon)
        XCTAssertEqual(preview.playing, "daon", "the yellow button of another voice switches to it")
        XCTAssertEqual(samples.played, ["harin", "daon"])
        XCTAssertEqual(samples.stops, 1)
        preview.toggle(daon)
        XCTAssertNil(preview.playing, "the stop button stops it")
        XCTAssertEqual(samples.stops, 2)
    }

    @MainActor func testASampleStopsByItselfAtItsEnd() async {
        let samples = FakeSamples(), preview = VoicePreview(samples: samples, length: .milliseconds(100))
        preview.toggle(daon)
        await waitUntil("the sample ends") { preview.playing == nil }
        XCTAssertEqual(samples.stops, 1)
    }

    /// The ring around the stop button: how much of the 5 seconds has played.
    @MainActor func testTheRingShowsHowMuchHasPlayed() {
        let preview = VoicePreview(samples: FakeSamples())
        let start = Date(timeIntervalSince1970: 1_000)
        XCTAssertEqual(preview.progress(at: start), 0)
        preview.toggle(daon, now: start)
        XCTAssertEqual(preview.progress(at: start.addingTimeInterval(2)), 0.4, accuracy: 0.001)
        XCTAssertEqual(preview.progress(at: start.addingTimeInterval(9)), 1)
    }

    /// Leaving the list stops the sample, and stopping twice stops the sound once.
    @MainActor func testLeavingTheListStopsTheSample() {
        let samples = FakeSamples(), preview = VoicePreview(samples: samples)
        preview.toggle(harin)
        preview.stop()
        preview.stop()
        XCTAssertNil(preview.playing)
        XCTAssertEqual(samples.stops, 1)
        XCTAssertEqual(preview.progress(at: .now), 0)
    }

    // MARK: - Words

    @MainActor func testTheFirstRunReadsInBothLanguages() {
        let ko = FirstRunWords(korean: true), en = FirstRunWords(korean: false)
        XCTAssertEqual(ko.step(1), "1단계 / 2단계")
        XCTAssertEqual(en.step(2), "Step 2 of 2")
        XCTAssertEqual(ko.voices, "한국어 목소리")
        XCTAssertEqual(en.voices, "English voices")
        XCTAssertEqual(ko.manner(daon, playing: true), "또렷함 · 듣는 중")
        XCTAssertEqual(ko.manner(daon, playing: false), "또렷함")
        XCTAssertEqual(en.manner(daon, playing: true), "또렷함 · playing")
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

    func play(_ voice: CallVoice) {
        played.append(voice.id)
    }

    func stop() {
        stops += 1
    }
}
