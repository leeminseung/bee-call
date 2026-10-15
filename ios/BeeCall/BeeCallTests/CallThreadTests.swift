import XCTest
@testable import BeeCall

/// Design D's home thread, drawn from the contract (sections 1 to 4): every call a section, oldest first, folded to
/// its results once it is over; the open question among the calls at its time; and during the person's own call,
/// the view's items, then the caption lines the view does not hold yet, then the running job.
final class CallThreadTests: XCTestCase {
    /// 14:45 on Thursday 2026-10-15 in Seoul, just after the open question of the examples.
    private let now = Date(timeIntervalSince1970: 1_792_043_100)

    @MainActor private func list() throws -> [CallEntry] {
        try Contract.decode(CallList.self, from: ContractTests.example("calls")).calls
    }

    @MainActor private func view(_ name: String) throws -> CallView {
        try Contract.decode(CallView.self, from: ContractTests.example(name))
    }

    @MainActor private func offers() throws -> [Offer] {
        try Contract.decode(OpenOffers.self, from: ContractTests.example("offers")).offers
    }

    // MARK: - Sections

    /// Oldest first, as a conversation reads; the open question sits at its created_at among the calls, and a
    /// booked call is pinned above the thread instead (section 4).
    @MainActor func testTheThreadRunsOldestFirstWithTheQuestionAtItsTime() throws {
        let sections = CallThread.sections(calls: try list(), offers: try offers())
        XCTAssertEqual(sections.map(\.id), [
            "call/call-minseung-20261014-083100", // missed, 08:31 yesterday
            "call/call-minseung-20261014-122604",
            "call/call-minseung-20261014-180158",
            "call/call-minseung-20261014-214502",
            "call/call-minseung-20261015-012301",
            "call/call-minseung-20261015-142209",
            "question/20261015-144102-a91f", // asked at 14:41 today
        ])
    }

    @MainActor func testAQuestionAskedBetweenTwoCallsSitsBetweenThem() throws {
        var asked = try XCTUnwrap(try offers().last)
        asked = try Contract.decode(Offer.self, from: JSONSerialization.data(withJSONObject: [
            "id": asked.id, "state": "asked", "topic": asked.topic, "created_at": "2026-10-15T09:00:00+09:00",
            "suggested_at": "2026-10-15T20:30:00+09:00", "call_at": NSNull(),
        ]))
        let sections = CallThread.sections(calls: try list(), offers: [asked])
        XCTAssertEqual(sections.firstIndex { $0.id == "question/\(asked.id)" }, 5, "after 01:23, before 14:22")
    }

    // MARK: - Inside a call's section

    /// A call that is over is folded to its results: the jobs and Bee writes the list gives with it.
    @MainActor func testAFoldedCallShowsItsResults() throws {
        let macbook = try XCTUnwrap(try list().first { $0.call.callID == "call-minseung-20261015-012301" })
        XCTAssertEqual(CallThread.folded(macbook).map(\.id), ["write-w1", "job-01"])
        XCTAssertTrue(CallThread.canUnfold(macbook.call))
        let silent = try XCTUnwrap(try list().first { $0.call.titleSource == .none })
        XCTAssertFalse(CallThread.canUnfold(silent.call), "no conversation to unfold")
        let missed = try XCTUnwrap(try list().first { $0.call.state == .missed })
        XCTAssertFalse(CallThread.canUnfold(missed.call), "a missed call has no record")
    }

    @MainActor func testAnUnfoldedCallShowsItsWholeView() throws {
        let entries = CallThread.expanded(try view("call-view"))
        XCTAssertEqual(entries.count, 23)
        XCTAssertEqual(entries.first?.id, "speech-9")
        XCTAssertEqual(entries[15].id, "job-01", "a finished job where it finished")
    }

    // MARK: - The call that is on (section 3)

    /// The worked example: the view holds the person's 8 lines and the AI's 7, so the phone draws its caption
    /// line 9, the third piece still growing, inside the same bubble, above the docked job.
    @MainActor func testTheGrowingLineJoinsItsBubbleAboveTheRunningJob() throws {
        let live = try view("call-view-live")
        let captions = (1...8).map { CaptionLine(speaker: .person, number: $0, text: "p\($0)") }
            + (1...7).map { CaptionLine(speaker: .ai, number: $0, text: "a\($0)") }
            + [CaptionLine(speaker: .person, number: 9, text: "도서관이나 카페에서")]
        let entries = CallThread.live(view: live, captions: captions, captionsShown: true)
        XCTAssertEqual(entries.count, 16)
        XCTAssertEqual(entries.last?.id, "job-01", "the running job is docked last")
        XCTAssertEqual(entries[14], .bubble(id: "speech-86", speaker: .person,
                                            text: "주로 파이썬이고 머신러닝도 조금 해요. 도서관이나 카페에서"))
    }

    /// A caption line of the other speaker starts a new bubble after the items; the next line of that speaker
    /// joins it.
    @MainActor func testACaptionOfTheOtherSpeakerStartsANewBubble() throws {
        let live = try view("call-view-live")
        let captions = [CaptionLine(speaker: .ai, number: 8, text: "좋아요."),
                        CaptionLine(speaker: .ai, number: 9, text: "가격표를 보는 중이에요.")]
        let entries = CallThread.live(view: live, captions: captions, captionsShown: true)
        XCTAssertEqual(entries[15], .bubble(id: "caption-ai-8", speaker: .ai, text: "좋아요. 가격표를 보는 중이에요."))
        XCTAssertEqual(entries.last?.id, "job-01")
    }

    /// Lines the view holds already are not drawn twice.
    @MainActor func testLinesTheViewHoldsAreNotDrawnAgain() throws {
        let live = try view("call-view-live")
        let captions = [CaptionLine(speaker: .person, number: 8, text: "주로 파이썬이고 머신러닝도 조금 해요."),
                        CaptionLine(speaker: .ai, number: 7, text: "지금 애플 한국 가격표를 보고 있어요.")]
        XCTAssertEqual(CallThread.live(view: live, captions: captions, captionsShown: true),
                       CallThread.live(view: live, captions: [], captionsShown: true))
    }

    /// Before the record begins (404), the call is its captions; with the numbers broken (rule 6) or the call over
    /// (rule 5), the view alone.
    @MainActor func testWithoutAViewTheCaptionsAloneAndAfterABreakTheViewAlone() throws {
        let captions = [CaptionLine(speaker: .ai, number: 1, text: "안녕하세요."),
                        CaptionLine(speaker: .person, number: 1, text: "안녕하세요,"),
                        CaptionLine(speaker: .person, number: 2, text: "오늘 날씨 어때요?")]
        XCTAssertEqual(CallThread.live(view: nil, captions: captions, captionsShown: true), [
            .bubble(id: "caption-ai-1", speaker: .ai, text: "안녕하세요."),
            .bubble(id: "caption-person-1", speaker: .person, text: "안녕하세요, 오늘 날씨 어때요?"),
        ])
        let live = try view("call-view-live")
        XCTAssertEqual(CallThread.live(view: live, captions: captions + [CaptionLine(speaker: .person, number: 9,
                                                                                    text: "x")],
                                       captionsShown: false).count, 16)
    }

    // MARK: - Words

    /// The line above each call: when (on the person's clock), who placed it, and how it went.
    @MainActor func testTheLineAboveACall() throws {
        let calls = try list()
        func line(_ id: String, korean: Bool) -> String? {
            calls.first { $0.call.callID == id }.map { CallLine.line($0.call, korean: korean, now: now) }
        }
        XCTAssertEqual(line("call-minseung-20261015-012301", korean: true), "오늘 오전 1:23 · 내가 건 전화 · 7분 40초")
        XCTAssertEqual(line("call-minseung-20261015-012301", korean: false),
                       "Today 1:23\u{00A0}AM · You called · 7 min 40 s")
        XCTAssertEqual(line("call-minseung-20261014-180158", korean: true), "어제 오후 6:02 · Bee Call이 건 전화 · 2분 4초")
        XCTAssertEqual(line("call-minseung-20261014-083100", korean: true), "어제 오전 8:31 · Bee Call이 건 전화 · 받지 못함")
        XCTAssertEqual(line("call-minseung-20261014-083100", korean: false), "Yesterday 8:31\u{00A0}AM · Bee Call called · Missed")
        XCTAssertEqual(line("call-minseung-20261014-122604", korean: true), "어제 오후 12:26 · 내가 건 전화 · 1분")
    }

    @MainActor func testEachStateHasItsWords() throws {
        let live = try view("call-view-live").call
        XCTAssertEqual(CallLine.line(live, korean: true, now: now), "오늘 오전 1:23 · 내가 건 전화 · 통화 중")
        XCTAssertEqual(CallLine.ending(.ended, korean: true), "정상 종료")
        XCTAssertEqual(CallLine.ending(.dropped, korean: true), "연결 끊김")
        XCTAssertEqual(CallLine.ending(.ended, korean: false), "Ended normally")
        XCTAssertEqual(CallLine.ending(.dropped, korean: false), "Dropped")
        XCTAssertEqual(CallLine.ending(.declined, korean: true), "거절함")
    }

    /// The record sheet's header and the thread's end mark (2.1).
    @MainActor func testTheSheetSaysHowTheCallEnded() throws {
        let ended = try view("call-view").call
        XCTAssertEqual(CallLine.sheetLine(ended, korean: true, now: now), "오늘 오전 1:23 · 내가 건 전화 · 7분 40초 · 정상 종료")
        XCTAssertEqual(CallLine.endMark(ended, korean: true), "통화 끝 · 7분 40초")
        XCTAssertEqual(CallLine.endMark(ended, korean: false), "Call ended · 7 min 40 s")
    }

    /// The title, or "대화 없음" for a call with no conversation.
    @MainActor func testATitleOrNoConversation() throws {
        let calls = try list()
        XCTAssertEqual(CallLine.title(calls[1].call, korean: true), "맥북 에어와 프로, 코딩용으로 뭐가 나을까")
        XCTAssertEqual(CallLine.title(calls[2].call, korean: true), "대화 없음")
        XCTAssertEqual(CallLine.title(calls[2].call, korean: false), "No conversation")
    }

    @MainActor func testLengthsReadAsSpoken() {
        XCTAssertEqual(CallLine.length(16, korean: true), "16초")
        XCTAssertEqual(CallLine.length(60, korean: true), "1분")
        XCTAssertEqual(CallLine.length(372, korean: true), "6분 12초")
        XCTAssertEqual(CallLine.length(16, korean: false), "16 s")
        XCTAssertEqual(CallLine.length(372, korean: false), "6 min 12 s")
    }
}
