import XCTest
@testable import BeeCall

/// The one way to the token server: each answer read once (review finding 5).
final class ServerClientTests: XCTestCase {
    @MainActor func testEachAnswerIsReadOnce() async {
        let cases: [(FakeServer.Reply, ServerAnswer)] = [
            (.json(200, ["ok": true]), .ok(200, Data(#"{"ok":true}"#.utf8))),
            (.status(204), .ok(204, Data())),
            (.status(409, "closed"), .refused(409, Data("closed".utf8))),
            (.status(502), .refused(502, Data())),
            (.unreachable, .unreachable),
        ]
        for (reply, answer) in cases {
            let server = FakeServer()
            server.replies["GET /me"] = reply
            var ended = 0
            let got = await server.client(signInEnded: { ended += 1 }).send("/me")
            XCTAssertEqual(got, answer, "\(reply)")
            XCTAssertEqual(ended, 0)
        }
    }

    /// A 401 to a request that carried the sign-in: the sign-in has ended, and the person is signed out.
    @MainActor func testA401ToASignedRequestEndsTheSignIn() async {
        let server = FakeServer()
        server.replies["GET /me"] = .status(401)
        var ended = 0
        let answer = await server.client(signInEnded: { ended += 1 }).send("/me")
        XCTAssertEqual(answer, .signedOut)
        XCTAssertEqual(ended, 1)
        XCTAssertEqual(server.requests.first?.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
    }

    /// Review finding 17: a request goes without the token when the Keychain could not give it (locked until the
    /// first unlock after a restart). The server's 401 to it says nothing about the sign-in, which it used to end.
    @MainActor func testA401ToARequestSentWithoutTheTokenLeavesTheSignInAlone() async {
        let server = FakeServer()
        server.replies["GET /me"] = .status(401)
        var ended = 0
        let answer = await server.client(token: nil, signInEnded: { ended += 1 }).send("/me")
        XCTAssertNil(server.requests.first?.value(forHTTPHeaderField: "Authorization"))
        XCTAssertEqual(answer, .refused(401, Data()))
        XCTAssertEqual(ended, 0)
    }
}
