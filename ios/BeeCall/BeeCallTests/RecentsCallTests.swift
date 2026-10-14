import Intents
import XCTest
@testable import BeeCall

/// Tapping a Bee Call entry in the Phone app's Recents calls Bee Call again (the owner's decision of
/// 2026-10-15, review finding 13). CallKit lists the app's calls there; a tap opens the app with an
/// INStartCallIntent in an NSUserActivity.
final class RecentsCallTests: XCTestCase {
    /// What iOS hands the app for a tap in Recents: an activity of the intent's type.
    @MainActor private func recentsTap() -> NSUserActivity {
        NSUserActivity(activityType: "INStartCallIntent")
    }

    @MainActor func testTheActivityIsTheIntentsOwnType() {
        XCTAssertEqual(RecentsCall.activityType, NSStringFromClass(INStartCallIntent.self))
    }

    /// iOS hands the app only the activity types its Info.plist lists.
    @MainActor func testTheAppSaysItContinuesTheActivity() throws {
        let types = try XCTUnwrap(Bundle.main.object(forInfoDictionaryKey: "NSUserActivityTypes") as? [String])
        XCTAssertTrue(types.contains(RecentsCall.activityType))
    }

    @MainActor func testATapCallsWhenSignedInAndFree() {
        XCTAssertTrue(RecentsCall.startsCall(recentsTap(), signedIn: true, inCall: false))
    }

    @MainActor func testATapWhileSignedOutOnlyOpensTheApp() {
        XCTAssertFalse(RecentsCall.startsCall(recentsTap(), signedIn: false, inCall: false))
    }

    @MainActor func testATapDuringACallChangesNothing() {
        XCTAssertFalse(RecentsCall.startsCall(recentsTap(), signedIn: true, inCall: true))
    }

    @MainActor func testAnotherActivityIsNotACall() {
        XCTAssertFalse(RecentsCall.startsCall(NSUserActivity(activityType: "com.leeminseung.beecall.other"),
                                              signedIn: true, inCall: false))
    }
}
