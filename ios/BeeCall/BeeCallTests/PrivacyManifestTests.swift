import XCTest
@testable import BeeCall

/// The app's privacy manifest (PrivacyInfo.xcprivacy), which App Store review reads (review finding 19).
final class PrivacyManifestTests: XCTestCase {
    /// What goes to the server, tied to the person, only to make the app work, and never for tracking: the call's
    /// audio (kept on the server unless "Save on the server" is off), the Sign in with Apple user id, and the
    /// person's own content (the call's words, settings and answers).
    @MainActor func testTheManifestDeclaresWhatTheAppSendsToItsServer() throws {
        let url = try XCTUnwrap(Bundle.main.url(forResource: "PrivacyInfo", withExtension: "xcprivacy"))
        let manifest = try XCTUnwrap(NSDictionary(contentsOf: url) as? [String: Any])
        XCTAssertEqual(manifest["NSPrivacyTracking"] as? Bool, false)
        let collected = try XCTUnwrap(manifest["NSPrivacyCollectedDataTypes"] as? [[String: Any]])
        XCTAssertEqual(Set(collected.compactMap { $0["NSPrivacyCollectedDataType"] as? String }),
                       ["NSPrivacyCollectedDataTypeAudioData", "NSPrivacyCollectedDataTypeUserID",
                        "NSPrivacyCollectedDataTypeOtherUserContent"])
        for entry in collected {
            let type = entry["NSPrivacyCollectedDataType"] as? String ?? "?"
            XCTAssertEqual(entry["NSPrivacyCollectedDataTypeLinked"] as? Bool, true, type)
            XCTAssertEqual(entry["NSPrivacyCollectedDataTypeTracking"] as? Bool, false, type)
            XCTAssertEqual(entry["NSPrivacyCollectedDataTypePurposes"] as? [String],
                           ["NSPrivacyCollectedDataTypePurposeAppFunctionality"], type)
        }
    }
}
