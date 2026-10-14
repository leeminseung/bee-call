import XCTest
@testable import BeeCall

/// The big button at the bottom of the call screen.
final class CallButtonTests: XCTestCase {
    @MainActor func testOutsideACallItCalls() {
        XCTAssertEqual(CallButton.shown(isRinging: false, isInCall: false), .call)
        XCTAssertEqual(CallButton.call.title, "Call")
    }

    @MainActor func testDuringACallItEnds() {
        XCTAssertEqual(CallButton.shown(isRinging: false, isInCall: true), .end)
        XCTAssertEqual(CallButton.end.title, "End")
    }

    /// CallKit's own screen has Decline and Accept, so the app shows no button of its own while it rings
    /// (the owner's decision of 2026-10-15).
    @MainActor func testWhileTheAICallRingsThereIsNoButton() {
        XCTAssertNil(CallButton.shown(isRinging: true, isInCall: true))
    }
}
