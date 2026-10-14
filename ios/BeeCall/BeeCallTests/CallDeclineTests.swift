import XCTest
@testable import BeeCall

/// Declining the AI's ringing call tells the server (contract section 2, review finding 18).
final class CallDeclineTests: XCTestCase {
    @MainActor func testDecliningTellsTheServerWhichCall() async throws {
        let server = FakeServer()
        await CallDecline(transport: server, noteStatus: { _ in }).send(room: "call-matt-20261015-210000")
        XCTAssertEqual(server.sent, ["POST /calls/decline"])
        let body = try server.body(of: "POST /calls/decline")
        XCTAssertEqual(body as NSDictionary, ["room": "call-matt-20261015-210000"] as NSDictionary)
    }

    @MainActor func testTheAnswerGoesThroughTheSignInRule() async {
        let server = FakeServer()
        server.replies["POST /calls/decline"] = .status(401)
        var noted: [Int] = []
        await CallDecline(transport: server, noteStatus: { noted.append($0) }).send(room: "call-matt-20261015-210000")
        XCTAssertEqual(noted, [401])
    }

    /// A decline that does not arrive only means the server waits out the ring, as before.
    @MainActor func testAnUnreachableServerIsLeftAlone() async {
        let server = FakeServer()
        server.replies["POST /calls/decline"] = .unreachable
        var noted: [Int] = []
        await CallDecline(transport: server, noteStatus: { noted.append($0) }).send(room: "call-matt-20261015-210000")
        XCTAssertEqual(server.sent, ["POST /calls/decline"])
        XCTAssertEqual(noted, [])
    }
}
