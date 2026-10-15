import XCTest
@testable import BeeCall

/// The call's pass comes from the token server's GET /token, built like every request from the build's one
/// address (ServerClient).
final class CallPassTests: XCTestCase {
    @MainActor func testEachKindOfCallAsksForItsPass() {
        XCTAssertEqual(LiveKitClient.passPath(answering: nil, continuing: nil), "/token")
        XCTAssertEqual(LiveKitClient.passPath(answering: "call-matt-20261015-210000", continuing: nil),
                       "/token?room=call-matt-20261015-210000&answer=1")
        XCTAssertEqual(LiveKitClient.passPath(answering: nil, continuing: "2026-10-14/0055"),
                       "/token?continue=2026-10-14/0055")
    }

    @MainActor func testThePassRequestCarriesTheSignInFromTheOneAddress() throws {
        let request = try XCTUnwrap(FakeServer().client().request(LiveKitClient.passPath(answering: nil,
                                                                                        continuing: "2026-10-14/0055")))
        XCTAssertEqual(request.url?.absoluteString, Server.api + "/token?continue=2026-10-14/0055")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
    }
}
