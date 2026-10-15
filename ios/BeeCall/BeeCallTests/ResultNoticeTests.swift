import XCTest
@testable import BeeCall

/// Design D, frame 5: work finished during a call always lands as a card in the conversation; on the call screen a
/// "결과 도착 · 대화에서 보기" pill leads to it, and the card lights up when the person gets there.
final class ResultNoticeTests: XCTestCase {
    @MainActor func testAResultOnTheCallScreenWaitsUntilTheConversationOpensAndThenLightsUp() {
        let notice = ResultNotice()
        notice.update(call: "c1", done: [], callScreenUp: true)
        notice.update(call: "c1", done: ["job-1"], callScreenUp: true)
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
        notice.update(call: "c1", done: [], callScreenUp: false)
        notice.update(call: "c1", done: ["job-1"], callScreenUp: false)
        XCTAssertNil(notice.unseen)
        XCTAssertEqual(notice.glowing, "job-1")
    }

    /// Jobs that were done before the call's view knew them (opening the app mid-call) are not news.
    @MainActor func testResultsAlreadyThereAreNotNews() {
        let notice = ResultNotice()
        notice.update(call: "c1", done: ["job-0"], callScreenUp: true)
        XCTAssertNil(notice.unseen)
        XCTAssertNil(notice.glowing)
        notice.update(call: "c1", done: ["job-0"], callScreenUp: true)
        XCTAssertNil(notice.unseen)
    }

    /// Review of 2026-10-15, finding 1: the call appears with nothing done, and its first finished job is news. The
    /// baseline is the call's own first look, not the first finished job.
    @MainActor func testTheFirstResultOfACallIsNews() {
        let notice = ResultNotice()
        notice.update(call: nil, done: [], callScreenUp: true) // between calls
        notice.update(call: "c1", done: [], callScreenUp: true) // the call's section appears
        notice.update(call: "c1", done: ["job-01"], callScreenUp: true)
        XCTAssertEqual(notice.unseen, "job-01")
    }

    /// A voice server restarted in the room: the call goes on as a call of its own (room.2), whose jobs start anew.
    @MainActor func testAnotherCallStartsItsOwnBaseline() {
        let notice = ResultNotice()
        notice.update(call: "c1", done: [], callScreenUp: false)
        notice.update(call: "c1", done: ["job-01"], callScreenUp: false)
        XCTAssertEqual(notice.glowing, "job-01")
        notice.update(call: "c1.2", done: ["job-01"], callScreenUp: false)
        XCTAssertNil(notice.glowing, "what the other call had is not news")
        notice.update(call: "c1.2", done: ["job-01", "job-02"], callScreenUp: false)
        XCTAssertEqual(notice.glowing, "job-02")
    }

    @MainActor func testTheNextCallStartsWithNothing() {
        let notice = ResultNotice()
        notice.update(call: "c1", done: [], callScreenUp: true)
        notice.update(call: "c1", done: ["job-1"], callScreenUp: true)
        notice.callEnded()
        XCTAssertNil(notice.unseen)
        XCTAssertNil(notice.glowing)
        notice.update(call: "c1", done: [], callScreenUp: true)
        notice.update(call: "c1", done: ["job-2"], callScreenUp: true)
        XCTAssertEqual(notice.unseen, "job-2")
    }
}
