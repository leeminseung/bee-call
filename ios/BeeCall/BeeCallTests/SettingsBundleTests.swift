import XCTest
@testable import BeeCall

/// The Bee Call page in the iOS Settings app (Settings.bundle), as the app ships it.
final class SettingsBundleTests: XCTestCase {
    private func bundle() throws -> URL {
        try XCTUnwrap(Bundle.main.url(forResource: "Settings", withExtension: "bundle"))
    }

    private func root() throws -> NSDictionary {
        try XCTUnwrap(NSDictionary(contentsOf: try bundle().appendingPathComponent("Root.plist")))
    }

    private func specifiers() throws -> [[String: Any]] {
        try XCTUnwrap(root()["PreferenceSpecifiers"] as? [[String: Any]])
    }

    /// Every text the page shows: titles, group footers and the choices of a list.
    private func texts() throws -> [String] {
        try specifiers().flatMap { item in
            [item["Title"] as? String, item["FooterText"] as? String].compactMap { $0 } + (item["Titles"] as? [String] ?? [])
        }
    }

    /// Review finding 10: the app's defaults (AppSettings.defaults, used before the Settings app has written
    /// anything) are written apart from the page's own; they must say the same.
    @MainActor func testTheAppsDefaultsAreThePagesDefaults() throws {
        var page: [String: NSObject] = [:]
        for item in try specifiers() {
            if let key = item["Key"] as? String, let value = item["DefaultValue"] as? NSObject { page[key] = value }
        }
        XCTAssertEqual(page as NSDictionary, AppSettings.defaults as NSDictionary)
    }

    /// The call model leads the Calls group: Haiku (the default, faster) or Sonnet (smarter, a little slower). The
    /// language is not on the page: it belongs to the app after D (contract section 6), set in its first run and
    /// settings sheet, and every launch used to send the page's value over the person's pick.
    @MainActor func testThePageOffersTheCallModelAndNoLanguage() throws {
        let items = try specifiers()
        let keys = items.compactMap { $0["Key"] as? String }
        XCTAssertEqual(keys.first, "call_model")
        XCTAssertFalse(keys.contains("language"))
        let model = try XCTUnwrap(items.first { $0["Key"] as? String == "call_model" })
        XCTAssertEqual(model["Type"] as? String, "PSMultiValueSpecifier")
        XCTAssertEqual(model["Title"] as? String, "Call model")
        XCTAssertEqual(model["Titles"] as? [String], ["Haiku 5.5 (faster)", "Sonnet 5.5 (smarter)"])
        XCTAssertEqual(model["Values"] as? [String], ["haiku", "sonnet"])
        XCTAssertEqual(model["DefaultValue"] as? String, "haiku")
        let footer = try XCTUnwrap(items.first { $0["Title"] as? String == "Calls" }?["FooterText"] as? String)
        XCTAssertTrue(footer.contains("Haiku answers faster. Sonnet thinks more and answers a little slower."), footer)
    }

    @MainActor func testTheCallModelReadsInKorean() throws {
        let korean = try XCTUnwrap(NSDictionary(contentsOf: try bundle().appendingPathComponent("ko.lproj/Root.strings"))
                                   as? [String: String])
        XCTAssertEqual(korean["Call model"], "통화 모델")
        XCTAssertEqual(korean["Haiku 5.5 (faster)"], "Haiku 5.5 (빠름)")
        XCTAssertEqual(korean["Sonnet 5.5 (smarter)"], "Sonnet 5.5 (더 똑똑함)")
    }

    /// The user's decision of 2026-10-15: turning "Save on the server" off keeps the recordings made before.
    @MainActor func testTheAudioFooterSaysRecordingsMadeBeforeStay() throws {
        let footer = try XCTUnwrap(specifiers().first { $0["Title"] as? String == "Call audio" }?["FooterText"] as? String)
        XCTAssertTrue(footer.contains("Turning “Save on the server” off stops recording new calls there."), footer)
        XCTAssertTrue(footer.contains("Recordings made before stay."), footer)
    }

    /// With only ko.lproj, the Settings app showed the page in Korean on a phone set to English: a bundle falls
    /// back to the only localization it has (seen on the Simulator in English, round 2's review). English is the
    /// page's own language, so it has its own table too.
    @MainActor func testThePageReadsInEnglishOnAPhoneNotSetToKorean() throws {
        let english = try XCTUnwrap(NSDictionary(contentsOf: try bundle().appendingPathComponent("en.lproj/Root.strings"))
                                    as? [String: String], "Settings.bundle/en.lproj/Root.strings")
        for text in try texts() {
            XCTAssertEqual(english[text], text)
        }
        let page = try XCTUnwrap(Bundle(url: try bundle()))
        XCTAssertEqual(Bundle.preferredLocalizations(from: page.localizations, forPreferences: ["en-US"]).first, "en")
        XCTAssertEqual(Bundle.preferredLocalizations(from: page.localizations, forPreferences: ["ko-KR"]).first, "ko")
    }

    @MainActor func testThePageReadsInKoreanToo() throws {
        // Without this key the Settings app shows the page untranslated, whatever ko.lproj holds.
        XCTAssertEqual(try root()["StringsTable"] as? String, "Root")
        let korean = try XCTUnwrap(NSDictionary(contentsOf: try bundle().appendingPathComponent("ko.lproj/Root.strings"))
                                   as? [String: String], "Settings.bundle/ko.lproj/Root.strings")
        for text in try texts() {
            XCTAssertNotNil(korean[text], "no Korean for “\(text)”")
        }
        let footer = try XCTUnwrap(korean[try XCTUnwrap(texts().first { $0.contains("Recordings made before stay.") })])
        XCTAssertTrue(footer.contains("그전에 남은 녹음은 그대로 있습니다."), footer)
    }
}
