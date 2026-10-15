import XCTest
@testable import BeeCall

/// Design D, frame 5: work finished during a call always lands as a card in the conversation; on the call screen a
/// "결과 도착 · 대화에서 보기" pill leads to it, and the card lights up when the person gets there.
final class ResultNoticeTests: XCTestCase {
    @MainActor func testAResultOnTheCallScreenWaitsUntilTheConversationOpensAndThenLightsUp() {
        let notice = ResultNotice()
        notice.update(done: [], callScreenUp: true)
        notice.update(done: ["job-1"], callScreenUp: true)
        XCTAssertEqual(notice.unseen, "job-1", "the pill turns yellow")
        XCTAssertNil(notice.glowing)
        XCTAssertEqual(notice.openedConversation(), "job-1", "the conversation scrolls to the card")
        XCTAssertEqual(notice.glowing, "job-1")
        XCTAssertNil(notice.unseen)
        XCTAssertNil(notice.openedConversation(), "seen: nothing to jump to the next time")
        notice.glowEnded("job-1")
        XCTAssertNil(notice.glowing)
    }

    @MainActor func testAResultInTheConversationLightsUpWhereItIs() {
        let notice = ResultNotice()
        notice.update(done: [], callScreenUp: false)
        notice.update(done: ["job-1"], callScreenUp: false)
        XCTAssertNil(notice.unseen)
        XCTAssertEqual(notice.glowing, "job-1")
    }

    /// Jobs that were done before the call's view knew them (opening the app mid-call) are not news.
    @MainActor func testResultsAlreadyThereAreNotNews() {
        let notice = ResultNotice()
        notice.update(done: ["job-0"], callScreenUp: true)
        XCTAssertNil(notice.unseen)
        XCTAssertNil(notice.glowing)
        notice.update(done: ["job-0"], callScreenUp: true)
        XCTAssertNil(notice.unseen)
    }

    @MainActor func testTheNextCallStartsWithNothing() {
        let notice = ResultNotice()
        notice.update(done: [], callScreenUp: true)
        notice.update(done: ["job-1"], callScreenUp: true)
        notice.callEnded()
        XCTAssertNil(notice.unseen)
        XCTAssertNil(notice.glowing)
        notice.update(done: [], callScreenUp: true)
        notice.update(done: ["job-2"], callScreenUp: true)
        XCTAssertEqual(notice.unseen, "job-2")
    }
}
