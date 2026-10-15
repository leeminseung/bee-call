import XCTest
@testable import BeeCall

/// A call booked for a time: the person asked for it out loud in a call ("call me again at 7 PM") or accepted the
/// time a notification suggested. Its row on the first screen says when and about what, and "Cancel call" asks
/// before it sends "decline" (which the server takes until the call's room exists, contract section 4).
final class BookedCallTests: XCTestCase {
    /// GET /offers with one call booked in a call at 18:30 for 19:00 Seoul time, as voice/offers.py Offer.view()
    /// writes it (printed from that code).
    private let listing: [String: Any] = ["offers": [[
        "id": "20261015-183000-ab12", "state": "booked", "topic": "Saturday hike plan", "kind": NSNull(),
        "source": "call", "suggested_at": NSNull(), "call_at": "2026-10-15T19:00:00+09:00",
        "suggested_epoch": NSNull(), "call_epoch": 1792058400,
    ]]]

    @MainActor func testTheBookedTimeIsReadFromTheServersListing() async throws {
        let server = FakeServer()
        server.replies["GET /offers"] = .json(200, listing)
        let offers = OfferCenter(server: server.client())
        await offers.refresh()
        let booked = try XCTUnwrap(offers.open.first)
        XCTAssertEqual(booked.state, "booked")
        XCTAssertEqual(booked.callAtEpoch, 1792058400)
        XCTAssertEqual(booked.callAt, "2026-10-15T19:00:00+09:00")
    }

    // MARK: - What the row says

    /// 18:30 on Thursday 2026-10-15, Seoul time.
    private let now = Date(timeIntervalSince1970: 1792056600)

    @MainActor private func booked(_ callAt: String?, _ epoch: Int?, topic: String? = "Saturday hike plan",
                                   kind: String? = nil, source: String = "call") -> OfferCenter.Offer {
        OfferCenter.Offer(id: "20261015-183000-ab12", state: "booked", topic: topic, kind: kind, source: source,
                          suggestedEpoch: nil, callAtEpoch: epoch, callAt: callAt)
    }

    @MainActor func testTheRowSaysWhatIsBookedAndWhen() {
        let call = booked("2026-10-15T19:00:00+09:00", 1792058400)
        XCTAssertEqual(call.bookedLine(language: "ko", now: now), "오늘 저녁 7:00 전화 예정")
        XCTAssertEqual(call.bookedLine(language: "en", now: now), "Call booked for today, 7:00\u{00A0}PM")
    }

    @MainActor func testTomorrowAndLaterDays() {
        let tomorrow = booked("2026-10-16T08:30:00+09:00", 1792107000)
        XCTAssertEqual(tomorrow.bookedLine(language: "ko", now: now), "내일 오전 8:30 전화 예정")
        XCTAssertEqual(tomorrow.bookedLine(language: "en", now: now), "Call booked for tomorrow, 8:30\u{00A0}AM")
        let sunday = booked("2026-10-18T21:00:00+09:00", 1792324800) // a call can be booked up to a week ahead
        XCTAssertEqual(sunday.bookedLine(language: "ko", now: now), "10월 18일 일요일 밤 9:00 전화 예정")
        XCTAssertEqual(sunday.bookedLine(language: "en", now: now), "Call booked for Sunday, October 18, 9:00\u{00A0}PM")
    }

    /// The Korean times of day: 오전, 오후, 저녁, 밤, and 새벽 after midnight. "내일 밤 12:20" reads as the night of
    /// tomorrow, a day later than meant (round 2's review); "내일 새벽 12:20" is the early hours of tomorrow.
    @MainActor func testTimesAroundNoonAndMidnight() {
        let afternoon = booked("2026-10-15T13:05:00+09:00", 1792037100)
        XCTAssertEqual(afternoon.bookedLine(language: "ko", now: now), "오늘 오후 1:05 전화 예정")
        XCTAssertEqual(afternoon.bookedLine(language: "en", now: now), "Call booked for today, 1:05\u{00A0}PM")
        let pastMidnight = booked("2026-10-16T00:20:00+09:00", 1792077600)
        XCTAssertEqual(pastMidnight.bookedLine(language: "ko", now: now), "내일 새벽 12:20 전화 예정")
        let late = booked("2026-10-15T23:50:00+09:00", 1792075800)
        XCTAssertEqual(late.bookedLine(language: "ko", now: now), "오늘 밤 11:50 전화 예정")
        XCTAssertEqual(pastMidnight.bookedLine(language: "en", now: now), "Call booked for tomorrow, 12:20\u{00A0}AM")
    }

    /// The person's own clock, which the server keeps per person and writes into call_at: a phone set to another
    /// zone still shows the time the person booked.
    @MainActor func testTheTimeIsOnThePersonsClockNotThePhones() throws {
        let call = booked("2026-10-15T19:00:00+09:00", 1792058400)
        let losAngeles = try XCTUnwrap(TimeZone(identifier: "America/Los_Angeles"))
        XCTAssertEqual(call.bookedLine(language: "en", now: now, phoneZone: losAngeles),
                       "Call booked for today, 7:00\u{00A0}PM")
    }

    /// An offer stored before the server kept a zone per person carries none: the phone's clock reads it.
    @MainActor func testAnOfferWithoutAZoneIsShownOnThePhonesClock() throws {
        let call = booked("2026-10-15T19:00:00", 1792058400)
        let utc = try XCTUnwrap(TimeZone(identifier: "UTC"))
        XCTAssertEqual(call.bookedLine(language: "en", now: now, phoneZone: utc), "Call booked for today, 10:00\u{00A0}AM")
    }

    @MainActor func testABookedCallWithoutATime() {
        XCTAssertEqual(booked(nil, nil).bookedLine(language: "ko", now: now), "전화 예정")
        XCTAssertEqual(booked(nil, nil).bookedLine(language: "en", now: now), "Call booked")
    }

    /// The topic as the server gives it; without one, what kind of call it is.
    @MainActor func testTheRowSaysWhatTheCallIsAbout() {
        let at = "2026-10-15T19:00:00+09:00"
        XCTAssertEqual(booked(at, 1792058400).about(language: "ko"), "Saturday hike plan")
        XCTAssertEqual(booked(at, 1792058400).about(language: "en"), "Saturday hike plan")
        for topic in [nil, " "] {
            let inCall = booked(at, 1792058400, topic: topic)
            XCTAssertEqual(inCall.about(language: "ko"), "통화 중에 잡은 전화")
            XCTAssertEqual(inCall.about(language: "en"), "Booked during a call")
        }
        let voiceNote = booked(at, 1792058400, topic: nil, kind: "voice note", source: "bee")
        XCTAssertEqual(voiceNote.about(language: "ko"), "새 음성 메모 이야기")
        XCTAssertEqual(voiceNote.about(language: "en"), "About your new voice note")
        let day = booked(at, 1792058400, topic: nil, kind: "conversation", source: "bee")
        XCTAssertEqual(day.about(language: "ko"), "오늘 하루 이야기")
        XCTAssertEqual(day.about(language: "en"), "About your day")
    }

    // MARK: - Cancelling it

    /// The row's action says what it cancels, and the system alert names the time before anything is sent.
    @MainActor func testTheCancelActionAndItsQuestionNameTheCall() {
        let call = booked("2026-10-15T19:00:00+09:00", 1792058400)
        XCTAssertEqual(OfferCenter.Offer.cancelButton(language: "ko"), "전화 취소")
        XCTAssertEqual(OfferCenter.Offer.cancelButton(language: "en"), "Cancel call")
        XCTAssertEqual(call.cancelQuestion(language: "ko", now: now), "오늘 저녁 7:00 전화를 취소할까요?")
        XCTAssertEqual(call.cancelQuestion(language: "en", now: now), "Cancel the call for today, 7:00\u{00A0}PM?")
        XCTAssertEqual(OfferCenter.Offer.keepButton(language: "ko"), "그대로 두기")
        XCTAssertEqual(OfferCenter.Offer.keepButton(language: "en"), "Keep the call")
        XCTAssertEqual(booked(nil, nil).cancelQuestion(language: "ko", now: now), "예약된 전화를 취소할까요?")
        XCTAssertEqual(booked(nil, nil).cancelQuestion(language: "en", now: now), "Cancel the booked call?")
    }

    /// A tap on "Cancel call" only asks; the call is called off ("decline", contract section 4) once the person
    /// confirms, and keeping it sends nothing.
    @MainActor func testCancellingAsksFirstAndSendsNothingUntilConfirmed() async throws {
        let server = FakeServer()
        let offers = OfferCenter(server: server.client())
        let call = booked("2026-10-15T19:00:00+09:00", 1792058400)
        offers.askToCancel(call)
        XCTAssertEqual(offers.cancelling, call)
        offers.keepBooked()
        XCTAssertNil(offers.cancelling)
        XCTAssertEqual(server.sent, [])

        offers.askToCancel(call)
        XCTAssertEqual(server.sent, [])
        await offers.confirmCancel(call)
        XCTAssertNil(offers.cancelling)
        XCTAssertEqual(server.sent.first, "POST /offers/\(call.id)")
        XCTAssertEqual(try server.body(of: "POST /offers/\(call.id)") as NSDictionary,
                       ["choice": "decline"] as NSDictionary)
    }

    /// The question goes away once the call can no longer be called off: its room is being made ("calling"), or
    /// the offer is gone, such as called off in a call. While it is still booked, the question stays.
    @MainActor func testTheQuestionGoesAwayWhenTheCallCanNoLongerBeCancelled() async throws {
        let call = booked("2026-10-15T19:00:00+09:00", 1792058400)
        var entry = try XCTUnwrap((listing["offers"] as? [[String: Any]])?.first)
        for (state, staysAsked) in [("booked", true), ("calling", false), (nil, false)] {
            let server = FakeServer()
            entry["state"] = state
            server.replies["GET /offers"] = .json(200, ["offers": state == nil ? [] : [entry]])
            let offers = OfferCenter(server: server.client())
            offers.askToCancel(call)
            await offers.refresh()
            XCTAssertEqual(offers.cancelling != nil, staysAsked, String(describing: state))
        }
    }
}
