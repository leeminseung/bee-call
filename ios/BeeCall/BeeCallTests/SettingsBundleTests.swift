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

    /// The user's decision of 2026-10-15: turning "Save on the server" off keeps the recordings made before.
    @MainActor func testTheAudioFooterSaysRecordingsMadeBeforeStay() throws {
        let footer = try XCTUnwrap(specifiers().first { $0["Title"] as? String == "Call audio" }?["FooterText"] as? String)
        XCTAssertTrue(footer.contains("Turning “Save on the server” off stops recording new calls there."), footer)
        XCTAssertTrue(footer.contains("Recordings made before stay."), footer)
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
