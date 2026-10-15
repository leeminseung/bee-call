import XCTest
@testable import BeeCall

/// Design D's home thread, drawn from the contract (sections 1 to 4): every call a section, oldest first, folded to
/// its results once it is over; the open question among the calls at its time; and during the person's own call,
/// the view's items, then the caption lines the view does not hold yet, then the running job. The structure is
/// checked on the contract's example files (what to expect is worked out from them); the words on calls made here.
final class CallThreadTests: XCTestCase {
    /// 14:45 on Thursday 2026-10-15 in Seoul, for the words.
    private let now = Date(timeIntervalSince1970: 1_792_043_100)

    @MainActor private func list() throws -> [CallEntry] {
        try Examples.decode(CallList.self, "calls").calls
    }

    @MainActor private func view(_ name: String) throws -> CallView {
        try Examples.decode(CallView.self, name)
    }

    @MainActor private func offers() throws -> [Offer] {
        try Examples.decode(OpenOffers.self, "offers").offers
    }

    /// A call for the words: the list's first call with the fields a test is about changed.
    @MainActor private func call(_ startedAt: String, by starter: String = "person", state: String = "ended",
                                 seconds: Int? = 460, title: String? = "맥북 에어와 프로, 코딩용으로 뭐가 나을까",
                                 titleSource: String = "summary", topic: String? = nil) throws -> CallSummary {
        var body = try XCTUnwrap((try Examples.object("calls")["calls"] as? [[String: Any]])?.first)
        let fields: [String: Any] = [
            "started_at": startedAt, "started_by": starter, "state": state, "duration_s": seconds ?? NSNull(),
            "title": title ?? NSNull(), "title_source": titleSource, "topic": topic ?? NSNull(),
        ]
        body.merge(fields) { $1 }
        return try Contract.decode(CallSummary.self, from: JSONSerialization.data(withJSONObject: body))
    }

    // MARK: - Sections

    /// Oldest first, as a conversation reads; the open question sits at its created_at among the calls, and a
    /// booked call is pinned above the thread instead (section 4).
    @MainActor func testTheThreadRunsOldestFirstWithTheQuestionAtItsTime() throws {
        let calls = try list(), offers = try offers()
        XCTAssertTrue(offers.contains { $0.state == .asked }, "the examples have an open question")
        XCTAssertTrue(offers.contains { $0.state == .booked }, "and a booked call")
        let oldestFirst = calls.sorted { $0.call.startedAt < $1.call.startedAt }
            .map { ($0.call.startedAt.date, "call/\($0.call.callID)") }
        let questions = offers.filter { $0.state == .asked }.map { ($0.createdAt.date, "question/\($0.id)") }
        let expected = (oldestFirst + questions).enumerated().sorted { ($0.element.0, $0.offset) < ($1.element.0, $1.offset) }
            .map(\.element.1)
        XCTAssertEqual(CallThread.sections(calls: calls, offers: offers).map(\.id), expected)
    }

    @MainActor func testAQuestionAskedBetweenTwoCallsSitsBetweenThem() throws {
        let calls = try list(), times = calls.map(\.call.startedAt).sorted()
        XCTAssertGreaterThanOrEqual(times.count, 2)
        let earlier = times[0], later = times[1]
        let between = Date(timeIntervalSince1970: (earlier.date.timeIntervalSince1970 + later.date.timeIntervalSince1970) / 2)
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = earlier.zone
        let text = formatter.string(from: between).replacingOccurrences(of: "Z", with: "+00:00")
        let asked = try Contract.decode(Offer.self, from: JSONSerialization.data(withJSONObject: [
            "id": "20261015-090000-abcd", "state": "asked", "topic": "사이에 물은 것", "created_at": text,
            "suggested_at": text, "call_at": NSNull(),
        ]))
        let sections = CallThread.sections(calls: calls, offers: [asked])
        XCTAssertEqual(sections.firstIndex { $0.id == "question/\(asked.id)" }, 1, "after the oldest call, before the next")
    }

    // MARK: - Inside a call's section

    /// A call that is over is folded to its results: the jobs and Bee writes the list gives with it. "대화 펼치기"
    /// only for a call with a record and a conversation.
    @MainActor func testAFoldedCallShowsItsResults() throws {
        let calls = try list()
        let withResults = try XCTUnwrap(calls.max { $0.results.count < $1.results.count })
        XCTAssertFalse(withResults.results.isEmpty, "the examples have a call with results")
        XCTAssertEqual(CallThread.folded(withResults).map(\.id), withResults.results.filter(\.isDrawn).map(\.id))
        let talked = try XCTUnwrap(calls.first { $0.call.state == .ended && $0.call.titleSource != .none })
        XCTAssertTrue(CallThread.canUnfold(talked.call))
        let silent = try XCTUnwrap(calls.first { $0.call.titleSource == .none })
        XCTAssertFalse(CallThread.canUnfold(silent.call), "no conversation to unfold")
        let missed = try XCTUnwrap(calls.first { $0.call.state == .missed })
        XCTAssertFalse(CallThread.canUnfold(missed.call), "a missed call has no record")
    }

    @MainActor func testAnUnfoldedCallShowsItsWholeView() throws {
        let view = try view("call-view")
        XCTAssertTrue(view.items.contains { if case .job(let job) = $0 { job.state != .running } else { false } },
                      "the example has a finished job")
        XCTAssertEqual(CallThread.expanded(view).map(\.id), view.items.filter(\.isDrawn).map(\.id),
                       "every item, a finished job where it finished, in the server's order")
    }

    // MARK: - The call that is on (section 3)

    /// The lines of each speaker the live example's items hold, by its counts.
    @MainActor private func held(_ live: CallView) -> [CaptionLine] {
        (0..<live.captionsIncluded.person).map { CaptionLine(speaker: .person, number: $0 + 1, text: "p\($0 + 1)") }
            + (0..<live.captionsIncluded.ai).map { CaptionLine(speaker: .ai, number: $0 + 1, text: "a\($0 + 1)") }
    }

    /// The live example's running jobs, docked last, and the speech drawn just above them.
    @MainActor private func dock(_ live: CallView) throws -> (running: [CallItem], last: Speech) {
        let running = live.items.filter { if case .job(let job) = $0 { job.state == .running } else { false } }
        XCTAssertFalse(running.isEmpty, "the live example has a running job")
        let drawn = live.drawnItems.filter { !running.contains($0) }
        let speech: Speech? = if case .speech(let speech) = drawn.last { speech } else { nil }
        return (running, try XCTUnwrap(speech, "the live example ends on a speech above its running job"))
    }

    @MainActor private func next(_ speaker: Speaker, in live: CallView) -> Int {
        (speaker == .person ? live.captionsIncluded.person : live.captionsIncluded.ai) + 1
    }

    /// The worked example: the view holds each speaker's lines up to its counts, so the next line of the speaker
    /// who spoke last, still growing, joins that bubble, above the docked job.
    @MainActor func testTheGrowingLineJoinsItsBubbleAboveTheRunningJob() throws {
        let live = try view("call-view-live"), (running, last) = try dock(live)
        let growing = CaptionLine(speaker: last.speaker, number: next(last.speaker, in: live), text: "도서관이나 카페에서")
        let entries = CallThread.live(view: live, captions: held(live) + [growing], captionsShown: true)
        XCTAssertEqual(entries.count, live.drawnItems.count, "no new bubble")
        XCTAssertEqual(entries.suffix(running.count).map(\.id), running.map(\.id), "the running job is docked last")
        XCTAssertEqual(entries[entries.count - running.count - 1],
                       .bubble(id: last.id, speaker: last.speaker, text: last.text + " 도서관이나 카페에서"))
    }

    /// A caption line of the other speaker starts a new bubble after the items; the next line of that speaker
    /// joins it.
    @MainActor func testACaptionOfTheOtherSpeakerStartsANewBubble() throws {
        let live = try view("call-view-live"), (running, last) = try dock(live)
        let other: Speaker = last.speaker == .person ? .ai : .person, first = next(other, in: live)
        let captions = [CaptionLine(speaker: other, number: first, text: "좋아요."),
                        CaptionLine(speaker: other, number: first + 1, text: "가격표를 보는 중이에요.")]
        let entries = CallThread.live(view: live, captions: captions, captionsShown: true)
        XCTAssertEqual(entries.count, live.drawnItems.count + 1)
        XCTAssertEqual(entries[entries.count - running.count - 1],
                       .bubble(id: "caption-\(other.rawValue)-\(first)", speaker: other, text: "좋아요. 가격표를 보는 중이에요."))
        XCTAssertEqual(entries.suffix(running.count).map(\.id), running.map(\.id))
    }

    /// Lines the view holds already are not drawn twice.
    @MainActor func testLinesTheViewHoldsAreNotDrawnAgain() throws {
        let live = try view("call-view-live")
        XCTAssertEqual(CallThread.live(view: live, captions: held(live), captionsShown: true),
                       CallThread.live(view: live, captions: [], captionsShown: true))
        XCTAssertEqual(CallThread.live(view: live, captions: [], captionsShown: true).count, live.drawnItems.count)
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
        let more = [CaptionLine(speaker: .person, number: live.captionsIncluded.person + 1, text: "x")]
        XCTAssertEqual(CallThread.live(view: live, captions: more, captionsShown: false).count, live.drawnItems.count)
    }

    // MARK: - Words

    /// The line above each call: when (on the person's clock), who placed it, and how it went.
    @MainActor func testTheLineAboveACall() throws {
        let mine = try call("2026-10-15T01:23:05+09:00")
        XCTAssertEqual(CallLine.line(mine, korean: true, now: now), "오늘 오전 1:23 · 내가 건 전화 · 7분 40초")
        XCTAssertEqual(CallLine.line(mine, korean: false, now: now), "Today 1:23\u{00A0}AM · You called · 7 min 40 s")
        let theirs = try call("2026-10-14T18:02:11+09:00", by: "ai", seconds: 124, topic: "토요일 촬영")
        XCTAssertEqual(CallLine.line(theirs, korean: true, now: now), "어제 오후 6:02 · Bee Call이 건 전화 · 2분 4초")
        let missed = try call("2026-10-14T08:31:00+09:00", by: "ai", state: "missed", seconds: nil, titleSource: "topic",
                              topic: "아이폰 설정")
        XCTAssertEqual(CallLine.line(missed, korean: true, now: now), "어제 오전 8:31 · Bee Call이 건 전화 · 받지 못함")
        XCTAssertEqual(CallLine.line(missed, korean: false, now: now), "Yesterday 8:31\u{00A0}AM · Bee Call called · Missed")
        let noon = try call("2026-10-14T12:26:08+09:00", seconds: 60)
        XCTAssertEqual(CallLine.line(noon, korean: true, now: now), "어제 오후 12:26 · 내가 건 전화 · 1분")
        let older = try call("2026-10-13T18:40:00+09:00", seconds: 60)
        XCTAssertEqual(CallLine.line(older, korean: true, now: now), "10월 13일 오후 6:40 · 내가 건 전화 · 1분")
        XCTAssertEqual(CallLine.line(older, korean: false, now: now), "Oct 13 6:40\u{00A0}PM · You called · 1 min")
    }

    @MainActor func testEachStateHasItsWords() throws {
        let live = try call("2026-10-15T01:23:05+09:00", state: "live", seconds: nil)
        XCTAssertEqual(CallLine.line(live, korean: true, now: now), "오늘 오전 1:23 · 내가 건 전화 · 통화 중")
        let dropped = try call("2026-10-15T01:23:05+09:00", state: "dropped", seconds: 95)
        XCTAssertEqual(CallLine.line(dropped, korean: true, now: now), "오늘 오전 1:23 · 내가 건 전화 · 1분 35초 · 끊김")
        XCTAssertEqual(CallLine.ending(.ended, korean: true), "정상 종료")
        XCTAssertEqual(CallLine.ending(.dropped, korean: true), "연결 끊김")
        XCTAssertEqual(CallLine.ending(.ended, korean: false), "Ended normally")
        XCTAssertEqual(CallLine.ending(.dropped, korean: false), "Dropped")
        XCTAssertEqual(CallLine.ending(.declined, korean: true), "거절함")
    }

    /// The record sheet's header and the thread's end mark (2.1).
    @MainActor func testTheSheetSaysHowTheCallEnded() throws {
        let ended = try call("2026-10-15T01:23:05+09:00")
        XCTAssertEqual(CallLine.sheetLine(ended, korean: true, now: now), "오늘 오전 1:23 · 내가 건 전화 · 7분 40초 · 정상 종료")
        XCTAssertEqual(CallLine.endMark(ended, korean: true), "통화 끝 · 7분 40초")
        XCTAssertEqual(CallLine.endMark(ended, korean: false), "Call ended · 7 min 40 s")
    }

    /// The title, or "대화 없음" for a call with no conversation.
    @MainActor func testATitleOrNoConversation() throws {
        let titled = try call("2026-10-15T01:23:05+09:00")
        XCTAssertEqual(CallLine.title(titled, korean: true), "맥북 에어와 프로, 코딩용으로 뭐가 나을까")
        let silent = try call("2026-10-14T21:45:06+09:00", seconds: 16, title: nil, titleSource: "none")
        XCTAssertEqual(CallLine.title(silent, korean: true), "대화 없음")
        XCTAssertEqual(CallLine.title(silent, korean: false), "No conversation")
    }

    @MainActor func testLengthsReadAsSpoken() {
        XCTAssertEqual(CallLine.length(16, korean: true), "16초")
        XCTAssertEqual(CallLine.length(60, korean: true), "1분")
        XCTAssertEqual(CallLine.length(372, korean: true), "6분 12초")
        XCTAssertEqual(CallLine.length(16, korean: false), "16 s")
        XCTAssertEqual(CallLine.length(372, korean: false), "6 min 12 s")
    }

    /// Second review of the D wiring, finding 9: a starter a later server adds is not drawn as the person's call.
    @MainActor func testAnUnknownStarterIsLeftOutOfTheLine() throws {
        let unknown = try call("2026-10-15T01:23:05+09:00", by: "agent")
        XCTAssertEqual(unknown.startedBy, .unrecognized)
        XCTAssertEqual(CallLine.line(unknown, korean: true, now: now), "오늘 오전 1:23 · 7분 40초")
    }
}
