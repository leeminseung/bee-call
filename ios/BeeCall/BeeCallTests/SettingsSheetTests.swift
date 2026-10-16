import UIKit
import XCTest
@testable import BeeCall

/// Design D, frame 8: the home's gear opens a settings sheet in the app: the voice and the language, which open the
/// first run's lists, and a row that opens the Bee Call page of the iOS Settings app for the other switches.
final class SettingsSheetTests: XCTestCase {
    @MainActor func testTheRowsSayWhatIsPicked() throws {
        let voice = try XCTUnwrap(try Examples.decode(VoiceList.self, "voices").voices.first)
        XCTAssertEqual(SettingsWords(korean: true).voiceValue(voice), "\(voice.name) · \(voice.character.ko)")
        XCTAssertEqual(SettingsWords(korean: false).voiceValue(voice), "\(voice.name) · \(voice.character.en)",
                       "the voice's own name, and how it sounds in the app's language")
        XCTAssertEqual(SettingsWords(korean: true).voiceValue(nil), "", "no voices yet: the row shows no value")
        XCTAssertEqual(CallLanguage.ko.name, "한국어")
    }

    @MainActor func testTheSheetReadsInBothLanguages() {
        let ko = SettingsWords(korean: true), en = SettingsWords(korean: false)
        XCTAssertEqual(ko.title, "설정")
        XCTAssertEqual(ko.calls, "통화")
        XCTAssertEqual(ko.voice, "목소리")
        XCTAssertEqual(ko.language, "언어")
        XCTAssertEqual(ko.callModel, "통화 모델")
        XCTAssertEqual(ko.workerModel, "일꾼 모델")
        XCTAssertEqual(en.callModel, "Call model")
        XCTAssertEqual(en.workerModel, "Worker model")
        XCTAssertEqual(ko.openSettings, "iOS 설정 열기")
        XCTAssertEqual(ko.footer, "저녁 전화 시간, 통화 녹음 저장, Bee 사용 같은 나머지 스위치는 iOS 설정 앱의 Bee Call 페이지에 있어요.")
        XCTAssertEqual(en.title, "Settings")
        XCTAssertEqual(en.calls, "Calls")
        XCTAssertEqual(en.openSettings, "Open iOS Settings")
        XCTAssertEqual(en.footer, "The other switches, such as the evening call time, saving call audio and using Bee, are on "
                           + "the Bee Call page of the iOS Settings app.")
    }

    /// The noise cancellation switch's words: its name, and one quieter line under it.
    @MainActor func testTheNoiseSwitchReadsInBothLanguages() {
        let ko = SettingsWords(korean: true), en = SettingsWords(korean: false)
        XCTAssertEqual(ko.noise, "주변 소리 줄이기")
        XCTAssertEqual(ko.noiseDetail, "통화할 때 주변 소음과 다른 사람 목소리를 줄여요.")
        XCTAssertEqual(en.noise, "Reduce background noise")
        XCTAssertEqual(en.noiseDetail, "Filters out noise and other voices around you during calls.")
    }

    /// "iOS 설정 열기" opens the app's own page in the Settings app, where the other switches are.
    @MainActor func testTheLastRowOpensTheAppsPageInTheSettingsApp() {
        XCTAssertEqual(SettingsWords.settingsPage, URL(string: UIApplication.openSettingsURLString))
    }
}
