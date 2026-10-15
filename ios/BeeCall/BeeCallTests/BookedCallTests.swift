import XCTest
@testable import BeeCall

/// A call booked for a time: the person asked for it out loud in a call ("call me again at 7 PM") or accepted the
/// time a notification suggested. Its row on the first screen lists it with "Call off" (the server takes "decline"
/// until the call's room exists, contract section 4).
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
        let offers = OfferCenter(transport: server, noteStatus: { _ in })
        await offers.refresh()
        let booked = try XCTUnwrap(offers.open.first)
        XCTAssertEqual(booked.state, "booked")
        XCTAssertEqual(booked.callAtEpoch, 1792058400)
    }
}
