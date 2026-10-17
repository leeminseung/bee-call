import XCTest
@testable import BeeCall

/// The calls list (scratch/2026-10-15-ui-mockups/calls-list.html): every call grouped by day, newest first, each day
/// on the person's clock as the server writes the times; a row says what the call was, when, who placed it, and how it
/// went, with its results. Structure is checked on the contract's examples, words on calls made here.
final class CallsListTests: XCTestCase {
    /// A call for the words: the list's first call with the fields a test is about changed.
    @MainActor private func call(_ startedAt: String, by starter: String = "person", state: String = "ended",
                                 seconds: Int? = 372, title: String? = "도서관 가는 길, 오늘 할 일 정리",
                                 titleSource: String = "summary", marks: [String: Any]? = nil,
                                 results: [[String: Any]] = [], id: String = "call-test-1") throws -> CallEntry {
        var body = try XCTUnwrap((try Examples.object("calls")["calls"] as? [[String: Any]])?.first)
        body.merge([
            "call_id": id, "started_at": startedAt, "started_by": starter, "state": state,
            "duration_s": seconds ?? NSNull(), "title": title ?? NSNull(), "title_source": titleSource,
            "marks": marks ?? ["report": false, "table": false, "chart": false, "bee_writes": 0], "results": results,
        ]) { $1 }
        return try Contract.decode(CallEntry.self, from: JSONSerialization.data(withJSONObject: body))
    }

    /// 14:45 on Thursday 2026-10-15 in Seoul.
    private let now = Date(timeIntervalSince1970: 1_792_043_100)

    // MARK: - Days

    /// Days come from each call's own time as the server writes it (its date part is the person's date), newest
    /// first, and the calls of a day keep the list's order.
    @MainActor func testTheExamplesGroupByThePersonsDays() throws {
        let raw = try XCTUnwrap(try Examples.object("calls")["calls"] as? [[String: Any]])
        let calls = try Examples.decode(CallList.self, "calls").calls
        let latest = try XCTUnwrap(calls.map(\.call.startedAt).max())
        let days = CallsList.days(calls, korean: true, now: latest.date.addingTimeInterval(3600))
        var expected: [(String, [String])] = []
        for entry in raw {
            let date = String((entry["started_at"] as? String ?? "").prefix(10)), id = entry["call_id"] as? String ?? ""
            if expected.last?.0 == date { expected[expected.count - 1].1.append(id) } else { expected.append((date, [id])) }
        }
        XCTAssertEqual(days.map(\.id), expected.map(\.0))
        XCTAssertEqual(days.map { $0.calls.map(\.id) }, expected.map(\.1))
        XCTAssertEqual(days.first?.title, "오늘", "the newest call's day is today")
    }

    @MainActor func testTodayYesterdayAndOlderDays() throws {
        let calls = [try call("2026-10-15T14:22:13+09:00", id: "a"), try call("2026-10-14T21:45:06+09:00", id: "b"),
                     try call("2026-10-13T23:10:00+09:00", id: "c"), try call("2025-12-31T09:00:00+09:00", id: "d")]
        let korean = CallsList.days(calls, korean: true, now: now)
        XCTAssertEqual(korean.map(\.title), ["오늘", "어제", "10월 13일", "2025년 12월 31일"])
        XCTAssertEqual(korean.map(\.weekday), [nil, nil, "화요일", "수요일"])
        let english = CallsList.days(calls, korean: false, now: now)
        XCTAssertEqual(english.map(\.title), ["Today", "Yesterday", "October 13", "December 31, 2025"])
        XCTAssertEqual(english.map(\.weekday), [nil, nil, "Tuesday", "Wednesday"])
    }

    /// 00:30 in Seoul is the day before in UTC: the call belongs to the person's day.
    @MainActor func testADayIsThePersonsNotUTCs() throws {
        let days = CallsList.days([try call("2026-10-15T00:30:00+09:00")], korean: true, now: now)
        XCTAssertEqual(days.map(\.id), ["2026-10-15"])
        XCTAssertEqual(days.first?.title, "오늘")
    }

    // MARK: - A row

    @MainActor func testARowSaysWhenWhoAndHowLong() throws {
        let row = CallsList.Row(try call("2026-10-15T14:22:13+09:00"), korean: true, ownCall: nil)
        XCTAssertEqual(row.title, "도서관 가는 길, 오늘 할 일 정리")
        XCTAssertFalse(row.quietTitle)
        XCTAssertEqual(row.line, "오후 2:22 · 내가 건 전화 · 6분 12초")
        XCTAssertNil(row.state)
        XCTAssertEqual(row.icon, .outgoing)
        XCTAssertEqual(row.opens, .record)
        let english = CallsList.Row(try call("2026-10-15T14:22:13+09:00"), korean: false, ownCall: nil)
        XCTAssertEqual(english.line, "2:22\u{00A0}PM · You called · 6 min 12 s")
    }

    /// The words of the home thread's line: 끊김 and 받지 못함 in red, 거절함 quiet, a live call's yellow pill.
    @MainActor func testEachStateHasItsWordsAndLook() throws {
        let dropped = CallsList.Row(try call("2026-10-13T23:10:00+09:00", state: "dropped", seconds: 130),
                                    korean: true, ownCall: nil)
        XCTAssertEqual(dropped.line, "오후 11:10 · 내가 건 전화 · 2분 10초")
        XCTAssertEqual(dropped.state, CallsList.StateWord(text: "끊김", look: .red))
        XCTAssertEqual(dropped.opens, .record)
        let missed = CallsList.Row(try call("2026-10-14T08:31:00+09:00", by: "ai", state: "missed", seconds: nil,
                                            titleSource: "topic"), korean: true, ownCall: nil)
        XCTAssertEqual(missed.line, "오전 8:31 · Bee Call이 건 전화")
        XCTAssertEqual(missed.state, CallsList.StateWord(text: "받지 못함", look: .red))
        XCTAssertEqual(missed.icon, .missed)
        XCTAssertEqual(missed.opens, .nothing, "no record to open")
        let declined = CallsList.Row(try call("2026-10-13T19:30:00+09:00", by: "ai", state: "declined", seconds: nil,
                                             titleSource: "topic"), korean: true, ownCall: nil)
        XCTAssertEqual(declined.state, CallsList.StateWord(text: "거절함", look: .quiet))
        XCTAssertEqual(declined.icon, .incoming)
        XCTAssertEqual(declined.opens, .nothing)
    }

    /// The person's own call while it is on: the orb, the pill, and a tap goes back to the call. Before it has words
    /// it reads "지금 통화 중".
    @MainActor func testTheCallThatIsOnGoesBackToTheCall() throws {
        let live = try call("2026-10-15T14:48:00+09:00", state: "live", seconds: nil, title: nil, titleSource: "none",
                            id: "call-test-live")
        let row = CallsList.Row(live, korean: true, ownCall: "call-test-live")
        XCTAssertEqual(row.title, "지금 통화 중")
        XCTAssertFalse(row.quietTitle)
        XCTAssertEqual(row.line, "오후 2:48 · 내가 건 전화")
        XCTAssertEqual(row.state, CallsList.StateWord(text: "통화 중", look: .livePill))
        XCTAssertEqual(row.icon, .live)
        XCTAssertEqual(row.opens, .call)
        XCTAssertEqual(CallsList.Row(live, korean: true, ownCall: nil).opens, .record,
                       "a call on elsewhere opens its record as it stands")
    }

    @MainActor func testACallWithoutAConversationIsQuiet() throws {
        let row = CallsList.Row(try call("2026-10-14T21:45:06+09:00", seconds: 16, title: nil, titleSource: "none"),
                                korean: true, ownCall: nil)
        XCTAssertEqual(row.title, "대화 없음")
        XCTAssertTrue(row.quietTitle)
        XCTAssertEqual(row.line, "오후 9:45 · 내가 건 전화 · 16초")
    }

    /// What the call made: the marks' report, table and chart, then its Bee writes in Bee now, by kind.
    @MainActor func testTheResultsPills() throws {
        let write: (String, String, String) -> [String: Any] = { id, kind, state in
            ["type": "bee_write", "id": id, "at": "2026-10-15T01:25:00+09:00", "kind": kind, "text": "x", "state": state,
             "will_retry": false, "undoable": false]
        }
        let entry = try call("2026-10-15T01:23:05+09:00",
                             marks: ["report": true, "table": true, "chart": true, "bee_writes": 3],
                             results: [write("w1", "todo_create", "written"), write("w2", "todo_accept", "written"),
                                       write("w3", "fact_confirm", "written"), write("w4", "todo_create", "undone")])
        XCTAssertEqual(CallsList.Row(entry, korean: true, ownCall: nil).pills.map(\.text),
                       ["보고서", "표", "차트", "Bee 할 일 2", "Bee 사실 1"])
        XCTAssertEqual(CallsList.Row(entry, korean: false, ownCall: nil).pills.map(\.text),
                       ["Report", "Table", "Chart", "Bee to-do 2", "Bee fact 1"])
        XCTAssertEqual(CallsList.Row(try call("2026-10-15T01:23:05+09:00"), korean: true, ownCall: nil).pills, [])
        // What is in Bee after the call: a completed to-do and a new fact count; a turned-down suggestion, a deleted
        // fact and a kind this build does not know take nothing in, so they do not.
        let more = try call("2026-10-15T01:23:05+09:00",
                            results: [write("w1", "todo_complete", "written"), write("w2", "fact_create", "written"),
                                      write("w3", "todo_dismiss", "written"), write("w4", "fact_delete", "written"),
                                      write("w5", "fact_merge", "written")])
        XCTAssertEqual(CallsList.Row(more, korean: true, ownCall: nil).pills.map(\.text), ["Bee 할 일 1", "Bee 사실 1"])
    }

    /// The examples' rows, each from its own file entry: the marks and writes the file gives become its pills.
    @MainActor func testTheExamplesRows() throws {
        let raw = try XCTUnwrap(try Examples.object("calls")["calls"] as? [[String: Any]])
        let calls = try Examples.decode(CallList.self, "calls").calls
        for (entry, file) in zip(calls, raw) {
            let row = CallsList.Row(entry, korean: true, ownCall: nil)
            let marks = try XCTUnwrap(file["marks"] as? [String: Any])
            let files = [("report", "보고서"), ("table", "표"), ("chart", "차트")].filter { marks[$0.0] as? Bool == true }
                .map(\.1)
            XCTAssertEqual(Array(row.pills.map(\.text).prefix(files.count)), files, entry.id)
            XCTAssertEqual(row.opens == .nothing, ["missed", "declined"].contains(file["state"] as? String), entry.id)
        }
    }

    // MARK: - What the sheet shows

    /// Review of the calls list, finding 3: before the first page, a spinner; if it could not be had, the failure line
    /// with its retry; once loaded, the days, or iOS's empty view when there are no calls at all.
    @MainActor func testWhatTheSheetShows() throws {
        let calls = try Examples.decode(CallList.self, "calls").calls
        XCTAssertEqual(CallsList.Content([], loaded: false, older: .done, problem: nil, korean: true, now: now), .loading)
        XCTAssertEqual(CallsList.Content([], loaded: false, older: .done, problem: "Bee Call's server cannot be reached.",
                                         korean: true, now: now), .failed)
        XCTAssertEqual(CallsList.Content([], loaded: true, older: .done, problem: nil, korean: true, now: now), .empty)
        XCTAssertEqual(CallsList.Content(calls, loaded: true, older: .more, problem: nil, korean: true, now: now),
                       .days(CallsList.days(calls, korean: true, now: now)))
        XCTAssertEqual(CallsList.Content(calls, loaded: true, older: .failed, problem: "x", korean: true, now: now),
                       .days(CallsList.days(calls, korean: true, now: now)), "the calls stay; the foot says the failure")
    }
}
