import XCTest
@testable import BeeCall

/// Debug builds only: `-offerDemo booked` shows the first screen with a booked call answered by a stand-in for the
/// server, to see its row and its cancel alert without a server or a sign-in.
final class OfferDemoTests: XCTestCase {
    private let seoul = TimeZone(identifier: "Asia/Seoul")!

    @MainActor func testTheDemoIsAskedForByItsLaunchArgument() {
        let store = freshDefaults(self)
        XCTAssertFalse(OfferDemo.requested(in: store))
        store.set("booked", forKey: "offerDemo")
        XCTAssertTrue(OfferDemo.requested(in: store))
    }

    /// One call booked for 19:00, listed as the server lists it, until a confirmed cancel takes it away.
    @MainActor func testTheDemoListsABookedCallUntilItIsCancelled() async throws {
        let now = Date(timeIntervalSince1970: 1792056600) // 18:30 in Seoul
        let offers = OfferCenter(server: ServerClient(transport: OfferDemo.StandIn(language: "ko", now: now, zone: seoul)))
        await offers.refresh()
        let call = try XCTUnwrap(offers.open.first)
        XCTAssertEqual(call.bookedLine(language: "ko", now: now), "오늘 저녁 7:00 전화 예정")
        XCTAssertEqual(call.about(language: "ko"), "토요일 등산 계획")
        await offers.confirmCancel(call)
        XCTAssertEqual(offers.open, [])
    }

    /// Less than ten minutes before 19:00, the demo's call is tomorrow's, as a call booked then would be.
    @MainActor func testCloseToSevenTheDemoCallIsTomorrows() async throws {
        let now = Date(timeIntervalSince1970: 1792058100) // 18:55 in Seoul
        let offers = OfferCenter(server: ServerClient(transport: OfferDemo.StandIn(language: "en", now: now, zone: seoul)))
        await offers.refresh()
        let call = try XCTUnwrap(offers.open.first)
        XCTAssertEqual(call.bookedLine(language: "en", now: now), "Call booked for tomorrow, 7:00\u{00A0}PM")
        XCTAssertEqual(call.about(language: "en"), "Saturday hike plan")
    }
}
