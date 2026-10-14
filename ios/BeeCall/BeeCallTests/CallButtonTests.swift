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

    @MainActor func testWhileTheAICallRingsItDeclines() {
        XCTAssertEqual(CallButton.shown(isRinging: true, isInCall: true), .decline)
    }
}
