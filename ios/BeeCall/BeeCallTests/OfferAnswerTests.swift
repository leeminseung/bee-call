import XCTest
@testable import BeeCall

/// The person's answer to an offer, from the app's list or a notification button (contract section 4).
final class OfferAnswerTests: XCTestCase {
    private let offer = "20261015-2100-1"

    @MainActor func testAnAcceptedAnswer() async throws {
        let server = FakeServer()
        server.replies["POST /offers/\(offer)"] = .json(200, ["offer": ["id": offer, "state": "now"]])
        let offers = OfferCenter(transport: server, noteStatus: { _ in })
        let accepted = await offers.answer(offer, .now)
        XCTAssertTrue(accepted)
        XCTAssertNil(offers.notice)
        XCTAssertEqual(try server.body(of: "POST /offers/\(offer)") as NSDictionary, ["choice": "now"] as NSDictionary)
    }

    @MainActor func testAClosedOffer() async {
        let server = FakeServer()
        server.replies["POST /offers/\(offer)"] = .status(409, "the offer is expired")
        let offers = OfferCenter(transport: server, noteStatus: { _ in })
        let accepted = await offers.answer(offer, .later)
        XCTAssertFalse(accepted)
        XCTAssertEqual(offers.notice, "That offer has closed. You can call Bee Call any time.")
    }

    @MainActor func testAnUnreachableServer() async {
        let server = FakeServer()
        server.replies["POST /offers/\(offer)"] = .unreachable
        let offers = OfferCenter(transport: server, noteStatus: { _ in })
        let accepted = await offers.answer(offer, .decline)
        XCTAssertFalse(accepted)
        XCTAssertEqual(offers.notice, "Couldn't reach Bee Call. Try again from here.")
    }
}
