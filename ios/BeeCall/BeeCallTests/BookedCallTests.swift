import XCTest
@testable import BeeCall

/// The open offers (contract section 4): the question Bee Call asks, and a call booked for a time, whose row says
/// when and about what, with "Cancel call" asking before it sends "decline" (which the server takes until the
/// call's room exists).
final class BookedCallTests: XCTestCase {
    /// 18:30 on Thursday 2026-10-15, Seoul time.
    private let now = Date(timeIntervalSince1970: 1792056600)

    /// An offer as GET /offers lists it.
    @MainActor private func offer(_ state: String = "booked", callAt: String? = "2026-10-15T19:00:00+09:00",
                                  suggestedAt: String? = nil, topic: String = "토요일 등산 계획") throws -> Offer {
        let body: [String: Any] = ["id": "20261015-183000-ab12", "state": state, "topic": topic,
                                   "created_at": "2026-10-15T18:30:00+09:00", "suggested_at": suggestedAt ?? NSNull(),
                                   "call_at": callAt ?? NSNull()]
        return try Contract.decode(Offer.self, from: JSONSerialization.data(withJSONObject: body))
    }

    @MainActor func testTheOffersAreReadFromTheContractsListing() async throws {
        let server = FakeServer(), listing = try Examples.decode(OpenOffers.self, "offers")
        server.replies["GET /offers"] = .status(200, try Examples.text("offers"))
        let offers = OfferCenter(server: server.client())
        await offers.refresh()
        XCTAssertEqual(offers.open, listing.offers)
        XCTAssertTrue(offers.open.contains { $0.state == .booked } && offers.open.contains { $0.state == .asked },
                      "the examples have a booked call and a question")
    }

    /// The offers carry a version: asked again with If-None-Match, and a 304 keeps them; /changes moves them on.
    @MainActor func testTheOffersAreAskedAgainWithTheirVersion() async throws {
        let server = FakeServer(), listing = try Examples.decode(OpenOffers.self, "offers")
        server.replies["GET /offers"] = .status(200, try Examples.text("offers"))
        let offers = OfferCenter(server: server.client())
        await offers.refresh()
        server.replies["GET /offers"] = .status(304)
        await offers.refresh()
        XCTAssertEqual(server.requests.last?.value(forHTTPHeaderField: "If-None-Match"), "\"\(listing.version)\"")
        XCTAssertEqual(offers.open, listing.offers, "304: kept")
        let asked = server.requests.count
        await offers.changed(to: listing.version)
        XCTAssertEqual(server.requests.count, asked, "the version held already")
        await offers.changed(to: Examples.otherVersion(than: listing.version))
        XCTAssertEqual(server.requests.count, asked + 1)
    }

    /// Fresh-context review of round1 7c1361a, finding 2: one fetch of the offers at a time. A change announced
    /// during it waits for it and fetches again, so an older answer landing last cannot bring back closed offers.
    @MainActor func testTheOffersAreFetchedOnceAtATime() async throws {
        let server = FakeServer(), listing = try Examples.decode(OpenOffers.self, "offers")
        let olderVersion = Examples.otherVersion(than: listing.version)
        server.replies["GET /offers"] = .slow(.milliseconds(200), try Examples.changed("offers") { body in
            body["version"] = olderVersion
        })
        let offers = OfferCenter(server: server.client())
        let first = Task { await offers.refresh() }
        try await Task.sleep(for: .milliseconds(50))
        server.replies["GET /offers"] = .status(200, try Examples.changed("offers") { body in
            body["offers"] = [Any]() // the offers closed meanwhile
        })
        await offers.refresh() // while the first is out
        XCTAssertEqual(offers.open, [], "the newer listing, once this returns")
        await first.value
        XCTAssertEqual(offers.open, [], "not the older answer, landing last")
        let asked = server.requests.filter { $0.url?.path == "/offers" }
        XCTAssertEqual(asked.count, 2)
        XCTAssertEqual(asked.last?.value(forHTTPHeaderField: "If-None-Match"), "\"\(olderVersion)\"")
    }

    // MARK: - What a booked call's row says

    @MainActor func testTheRowSaysWhatIsBookedAndWhen() throws {
        let call = try offer()
        XCTAssertEqual(call.bookedLine(language: "ko", now: now), "오늘 저녁 7:00 전화 예정")
        XCTAssertEqual(call.bookedLine(language: "en", now: now), "Call booked for today, 7:00\u{00A0}PM")
        XCTAssertEqual(call.topic, "토요일 등산 계획", "the server's topic, with no wording of the app's own")
    }

    @MainActor func testTomorrowAndLaterDays() throws {
        let tomorrow = try offer(callAt: "2026-10-16T08:30:00+09:00")
        XCTAssertEqual(tomorrow.bookedLine(language: "ko", now: now), "내일 오전 8:30 전화 예정")
        XCTAssertEqual(tomorrow.bookedLine(language: "en", now: now), "Call booked for tomorrow, 8:30\u{00A0}AM")
        let sunday = try offer(callAt: "2026-10-18T21:00:00+09:00") // a call can be booked up to a week ahead
        XCTAssertEqual(sunday.bookedLine(language: "ko", now: now), "10월 18일 일요일 밤 9:00 전화 예정")
        XCTAssertEqual(sunday.bookedLine(language: "en", now: now), "Call booked for Sunday, October 18, 9:00\u{00A0}PM")
    }

    /// The Korean times of day: 오전, 오후, 저녁, 밤, and 새벽 after midnight. "내일 밤 12:20" reads as the night of
    /// tomorrow, a day later than meant (round 2's review); "내일 새벽 12:20" is the early hours of tomorrow.
    @MainActor func testTimesAroundNoonAndMidnight() throws {
        let afternoon = try offer(callAt: "2026-10-15T13:05:00+09:00")
        XCTAssertEqual(afternoon.bookedLine(language: "ko", now: now), "오늘 오후 1:05 전화 예정")
        XCTAssertEqual(afternoon.bookedLine(language: "en", now: now), "Call booked for today, 1:05\u{00A0}PM")
        let pastMidnight = try offer(callAt: "2026-10-16T00:20:00+09:00")
        XCTAssertEqual(pastMidnight.bookedLine(language: "ko", now: now), "내일 새벽 12:20 전화 예정")
        XCTAssertEqual(pastMidnight.bookedLine(language: "en", now: now), "Call booked for tomorrow, 12:20\u{00A0}AM")
        let late = try offer(callAt: "2026-10-15T23:50:00+09:00")
        XCTAssertEqual(late.bookedLine(language: "ko", now: now), "오늘 밤 11:50 전화 예정")
    }

    /// The person's own clock, the offset the server writes: a phone set to another zone still shows the time the
    /// person booked, and "today" is the person's today.
    @MainActor func testTheTimeIsOnThePersonsClockNotThePhones() throws {
        let call = try offer(callAt: "2026-10-15T19:00:00-07:00") // 7 PM in Los Angeles, 11 AM the next day in Seoul
        let laNow = Date(timeIntervalSince1970: 1792112400) // 2026-10-15 18:00 in Los Angeles
        XCTAssertEqual(call.bookedLine(language: "en", now: laNow), "Call booked for today, 7:00\u{00A0}PM")
    }

    // MARK: - The question

    @MainActor func testTheQuestionSaysWhenAndOffersThatTime() throws {
        let asked = try offer("asked", callAt: nil, suggestedAt: "2026-10-15T20:30:00+09:00", topic: "데모 촬영 일정")
        XCTAssertEqual(asked.askedLine(language: "ko", now: now), "오늘 밤 8:30에 잠깐 통화할까요?")
        XCTAssertEqual(asked.askedLine(language: "en", now: now), "A quick call today, 8:30\u{00A0}PM?")
        XCTAssertEqual(asked.laterButton(language: "ko"), "8:30에")
        XCTAssertEqual(asked.laterButton(language: "en"), "At 8:30\u{00A0}PM")
        XCTAssertNil(try offer(callAt: "2026-10-15T19:00:00+09:00").laterButton(language: "ko"))
        XCTAssertEqual(Offer.callingLine(language: "ko"), "지금 전화하는 중")
        XCTAssertEqual(Offer.callingLine(language: "en"), "Calling you now")
    }

    // MARK: - Cancelling a booked call

    /// The row's action says what it cancels, and the system alert names the time before anything is sent.
    @MainActor func testTheCancelActionAndItsQuestionNameTheCall() throws {
        let call = try offer()
        XCTAssertEqual(Offer.cancelButton(language: "ko"), "전화 취소")
        XCTAssertEqual(Offer.cancelButton(language: "en"), "Cancel call")
        XCTAssertEqual(call.cancelQuestion(language: "ko", now: now), "오늘 저녁 7:00 전화를 취소할까요?")
        XCTAssertEqual(call.cancelQuestion(language: "en", now: now), "Cancel the call for today, 7:00\u{00A0}PM?")
        XCTAssertEqual(Offer.keepButton(language: "ko"), "그대로 두기")
        XCTAssertEqual(Offer.keepButton(language: "en"), "Keep the call")
    }

    /// A tap on "Cancel call" only asks; the call is called off ("decline") once the person confirms, and keeping
    /// it sends nothing.
    @MainActor func testCancellingAsksFirstAndSendsNothingUntilConfirmed() async throws {
        let server = FakeServer()
        let offers = OfferCenter(server: server.client())
        let call = try offer()
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
    /// the offer is gone. While it is still booked, the question stays.
    @MainActor func testTheQuestionGoesAwayWhenTheCallCanNoLongerBeCancelled() async throws {
        let call = try offer()
        for (state, staysAsked) in [("booked", true), ("calling", false), (nil, false)] {
            let server = FakeServer()
            let entry: [String: Any] = ["id": call.id, "state": state ?? "booked", "topic": call.topic,
                                        "created_at": "2026-10-15T18:30:00+09:00", "suggested_at": NSNull(),
                                        "call_at": "2026-10-15T19:00:00+09:00"]
            server.replies["GET /offers"] = .json(200, ["offers": state == nil ? [] : [entry],
                                                         "version": "0123456789abcdef"])
            let offers = OfferCenter(server: server.client())
            offers.askToCancel(call)
            await offers.refresh()
            XCTAssertEqual(offers.cancelling != nil, staysAsked, String(describing: state))
        }
    }
}
