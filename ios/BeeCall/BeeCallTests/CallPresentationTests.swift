import XCTest
@testable import BeeCall

/// Which of design D's two views of a call is up (frame 4): the call screen, or the conversation under the
/// call bar. A call starts on the call screen and keeps the view the person last picked until it ends.
final class CallPresentationTests: XCTestCase {
    @MainActor func testACallStartsOnTheCallScreen() {
        let presentation = CallPresentation()
        XCTAssertFalse(presentation.showsCallScreen(inCall: false), "no call, no call screen")
        presentation.callChanged(inCall: true)
        XCTAssertTrue(presentation.showsCallScreen(inCall: true))
    }

    @MainActor func testThePersonsPickStaysForTheRestOfTheCall() {
        let presentation = CallPresentation()
        presentation.callChanged(inCall: true)
        presentation.show(.conversation)
        presentation.callChanged(inCall: true) // the same call, seen again (the app came back to the front)
        XCTAssertFalse(presentation.showsCallScreen(inCall: true))
        presentation.show(.callScreen)
        XCTAssertTrue(presentation.showsCallScreen(inCall: true))
    }

    @MainActor func testTheNextCallStartsOnTheCallScreenAgain() {
        let presentation = CallPresentation()
        presentation.callChanged(inCall: true)
        presentation.show(.conversation)
        presentation.callChanged(inCall: false)
        presentation.callChanged(inCall: true)
        XCTAssertTrue(presentation.showsCallScreen(inCall: true))
    }
}
