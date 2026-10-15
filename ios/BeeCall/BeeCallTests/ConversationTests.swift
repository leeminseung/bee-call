import XCTest
@testable import BeeCall

/// Design D's conversation (frames 1, 3, 6): every call a section of one thread, folded once it is over.
final class ConversationTests: XCTestCase {
    private let seoul = TimeZone(identifier: "Asia/Seoul")!
    private var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = seoul
        return calendar
    }
    /// 14:41 on Thursday 2026-10-15 in Seoul, the moment of frame 1.
    private let now = Date(timeIntervalSince1970: 1_792_043_260)

    private func at(_ iso: String) -> Date { ISO8601DateFormatter().date(from: iso)! }

    private func call(_ started: String, by starter: ConversationCall.Starter = .person, length: TimeInterval?,
                      title: String = "맥북 에어와 프로, 코딩용으로 뭐가 나을까") -> ConversationCall {
        ConversationCall(id: "c", title: title, startedAt: at(started), starter: starter, length: length,
                         entries: [])
    }

    /// The line above each call: when, who started it, how long.
    @MainActor func testTheLineAboveACallSaysWhenWhoAndHowLong() {
        let mine = call("2026-10-15T01:23:00+09:00", length: 460)
        XCTAssertEqual(CallInfo.line(mine, korean: true, now: now, calendar: calendar),
                       "오늘 오전 1:23 · 내가 건 전화 · 7분 40초")
        XCTAssertEqual(CallInfo.line(mine, korean: false, now: now, calendar: calendar),
                       "Today 1:23\u{00A0}AM · You called · 7 min 40 s")
        let theirs = call("2026-10-14T18:02:00+09:00", by: .beeCall, length: 124)
        XCTAssertEqual(CallInfo.line(theirs, korean: true, now: now, calendar: calendar),
                       "어제 오후 6:02 · Bee Call이 건 전화 · 2분 4초")
        XCTAssertEqual(CallInfo.line(theirs, korean: false, now: now, calendar: calendar),
                       "Yesterday 6:02\u{00A0}PM · Bee Call called · 2 min 4 s")
        let older = call("2026-10-13T18:40:00+09:00", length: 60)
        XCTAssertEqual(CallInfo.line(older, korean: true, now: now, calendar: calendar),
                       "10월 13일 오후 6:40 · 내가 건 전화 · 1분")
        XCTAssertEqual(CallInfo.line(older, korean: false, now: now, calendar: calendar),
                       "Oct 13 6:40\u{00A0}PM · You called · 1 min")
    }

    @MainActor func testALiveCallSaysItIsOn() {
        let live = call("2026-10-15T01:23:00+09:00", length: nil)
        XCTAssertEqual(CallInfo.line(live, korean: true, now: now, calendar: calendar), "오늘 오전 1:23 · 내가 건 전화 · 통화 중")
        XCTAssertEqual(CallInfo.line(live, korean: false, now: now, calendar: calendar),
                       "Today 1:23\u{00A0}AM · You called · In a call")
    }

    @MainActor func testLengthsReadAsSpoken() {
        XCTAssertEqual(CallInfo.length(16, korean: true), "16초")
        XCTAssertEqual(CallInfo.length(60, korean: true), "1분")
        XCTAssertEqual(CallInfo.length(372, korean: true), "6분 12초")
        XCTAssertEqual(CallInfo.length(16, korean: false), "16 s")
        XCTAssertEqual(CallInfo.length(372, korean: false), "6 min 12 s")
    }

    /// A call that is over is folded: its results stay as cards, its turns hide until "대화 펼치기".
    @MainActor func testAFinishedCallFoldsToItsResults() {
        var finished = call("2026-10-15T01:23:00+09:00", length: 460)
        finished.entries = [
            .turn(Turn(id: "t1", mine: false, text: "안녕하세요")),
            .job(JobCard(id: "j1", title: "가격 비교", startedAt: now, finishedAt: now, step: nil, result: "에어가 낫다",
                         files: ["보고서.md"])),
            .turn(Turn(id: "t2", mine: true, text: "고마워요")),
            .beeWrite(BeeWriteCard(id: "b1", kind: "할 일", text: "대본 다듬기", added: true)),
        ]
        XCTAssertEqual(finished.shown(expanded: false).map(\.id), ["j1", "b1"])
        XCTAssertEqual(finished.shown(expanded: true).map(\.id), ["t1", "j1", "t2", "b1"])
        var live = finished
        live.length = nil
        XCTAssertEqual(live.shown(expanded: false).map(\.id), ["t1", "j1", "t2", "b1"], "a live call is never folded")
        XCTAssertTrue(finished.canUnfold)
        var silent = call("2026-10-14T21:45:00+09:00", length: 16, title: "대화 없음")
        silent.entries = []
        XCTAssertFalse(silent.canUnfold, "nothing to unfold")
    }
}
