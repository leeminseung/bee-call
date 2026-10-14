import XCTest
@testable import BeeCall

/// The person's answer to an offer, from the app's list or a notification button (contract section 4,
/// review finding 5): one place turns the server's reply into a result with its own message.
final class OfferAnswerTests: XCTestCase {
    private let offer = "20261015-2100-1"

    @MainActor func testEachReplyHasItsOwnResult() {
        let cases: [(Int?, OfferAnswer)] = [
            (200, .accepted),
            (401, .signedOut), // the sign-in has ended
            (404, .closed), // no such offer
            (409, .closed), // closed or already answered
            (nil, .unreachable),
            (400, .failed(400)),
            (429, .failed(429)),
            (500, .failed(500)),
            (502, .failed(502)), // the server restarting behind its proxy
        ]
        for (status, result) in cases {
            XCTAssertEqual(OfferAnswer(status: status), result, "status \(String(describing: status))")
        }
    }

    @MainActor func testAnAcceptedAnswer() async throws {
        let server = FakeServer()
        server.replies["POST /offers/\(offer)"] = .json(200, ["offer": ["id": offer, "state": "now"]])
        let offers = OfferCenter(transport: server, noteStatus: { _ in })
        let result = await offers.answer(offer, .now)
        XCTAssertEqual(result, .accepted)
        XCTAssertNil(offers.notice)
        XCTAssertEqual(try server.body(of: "POST /offers/\(offer)") as NSDictionary, ["choice": "now"] as NSDictionary)
    }

    @MainActor func testAClosedOffer() async {
        for status in [404, 409] {
            let server = FakeServer()
            server.replies["POST /offers/\(offer)"] = .status(status)
            let offers = OfferCenter(transport: server, noteStatus: { _ in })
            let result = await offers.answer(offer, .later)
            XCTAssertEqual(result, .closed)
            XCTAssertEqual(offers.notice, "That offer has closed. You can call Bee Call any time.")
        }
    }

    @MainActor func testAnUnreachableServer() async {
        let server = FakeServer()
        server.replies["POST /offers/\(offer)"] = .unreachable
        let offers = OfferCenter(transport: server, noteStatus: { _ in })
        let result = await offers.answer(offer, .decline)
        XCTAssertEqual(result, .unreachable)
        XCTAssertEqual(offers.notice, "Couldn't reach Bee Call. Try again from here.")
    }

    @MainActor func testAnEndedSignInSaysSoAndSignsOut() async {
        let server = FakeServer()
        server.replies["POST /offers/\(offer)"] = .status(401, "sign in first")
        var noted: [Int] = []
        let offers = OfferCenter(transport: server, noteStatus: { noted.append($0) })
        let result = await offers.answer(offer, .now)
        XCTAssertEqual(result, .signedOut)
        XCTAssertEqual(offers.notice, "Your sign-in ended. Sign in again to answer.")
        XCTAssertEqual(noted.first, 401)
    }

    /// A server error is not a closed offer: the answer may still be wanted.
    @MainActor func testAServerFailureIsNotAClosedOffer() async {
        let server = FakeServer()
        server.replies["POST /offers/\(offer)"] = .status(502)
        let offers = OfferCenter(transport: server, noteStatus: { _ in })
        let result = await offers.answer(offer, .now)
        XCTAssertEqual(result, .failed(502))
        XCTAssertEqual(offers.notice, "Bee Call couldn't take that answer (error 502). Try again from here.")
    }

    @MainActor func testAnOffersButtonsAreOffWhileItsAnswerIsOnItsWay() async {
        let server = FakeServer()
        let offers = OfferCenter(transport: server, noteStatus: { _ in })
        var duringRequest: Set<String> = []
        server.onRequest = { if $0.httpMethod == "POST" { duringRequest = offers.answering } }
        await offers.answer(offer, .now)
        XCTAssertEqual(duringRequest, [offer])
        XCTAssertEqual(offers.answering, [])
    }

    /// A double tap on "Now" sends one answer; the second tap would get a 409 and say the offer closed.
    @MainActor func testASecondTapWhileTheAnswerIsOnItsWaySendsNothing() async {
        let server = FakeServer()
        server.replies["POST /offers/\(offer)"] = .after(.milliseconds(200), 200)
        let offers = OfferCenter(transport: server, noteStatus: { _ in })
        let id = offer
        async let first = offers.answer(id, .now)
        async let second = offers.answer(id, .now)
        let results = await [first, second]
        XCTAssertEqual(server.sent.filter { $0.hasPrefix("POST") }, ["POST /offers/\(offer)"])
        XCTAssertEqual(results.compactMap { $0 }, [.accepted])
        XCTAssertNil(offers.notice)
    }
}
