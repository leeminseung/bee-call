import CryptoKit
import XCTest
@testable import BeeCall

/// Sign in with Apple against the server: a one-time value from GET /auth/nonce, its SHA-256 in Apple's
/// request, and the value with Apple's identity token in POST /auth/apple.
final class AppleSignInTests: XCTestCase {
    private func sha256(_ text: String) -> String {
        SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    @MainActor private func signIn(_ server: FakeServer, nonce: String = "n1") async -> AppleSignIn {
        server.replies["GET /auth/nonce"] = .json(200, ["nonce": nonce])
        let signIn = AppleSignIn(transport: server)
        _ = await signIn.prepare()
        return signIn
    }

    @MainActor func testAppleGetsTheValuesHash() async {
        let signIn = await signIn(FakeServer())
        XCTAssertEqual(signIn.hashForRequest(), sha256("n1"))
    }

    @MainActor func testAServerThatCannotGiveAValueSaysSo() async {
        let server = FakeServer()
        server.replies["GET /auth/nonce"] = .unreachable
        let ready = await AppleSignIn(transport: server).prepare()
        XCTAssertFalse(ready)
    }

    @MainActor func testTheServerGetsAppleTokenAndTheValue() async throws {
        let server = FakeServer()
        let signIn = await signIn(server)
        server.replies["POST /auth/apple"] = .json(200, ["token": "t0ken", "person": "matt"])
        let outcome = await signIn.finish(identityToken: "apple-jwt", cancelled: false)
        XCTAssertEqual(outcome, .signedIn(token: "t0ken", person: "matt"))
        XCTAssertEqual(try server.body(of: "POST /auth/apple") as NSDictionary,
                       ["identity_token": "apple-jwt", "nonce": "n1"] as NSDictionary)
    }

    @MainActor func testAPersonNotApprovedYetWaitsWithACode() async {
        let server = FakeServer()
        let signIn = await signIn(server)
        server.replies["POST /auth/apple"] = .json(403, ["waiting": "ABC123"])
        let outcome = await signIn.finish(identityToken: "apple-jwt", cancelled: false)
        XCTAssertEqual(outcome, .waiting(code: "ABC123"))
    }

    @MainActor func testARefusedOrUnreachableServerSaysSo() async {
        for (reply, message) in [(FakeServer.Reply.status(401), "The server did not accept the sign-in."),
                                 (.unreachable, "The server cannot be reached right now.")] {
            let server = FakeServer()
            let signIn = await signIn(server)
            server.replies["POST /auth/apple"] = reply
            let outcome = await signIn.finish(identityToken: "apple-jwt", cancelled: false)
            XCTAssertEqual(outcome, .failed(message))
        }
    }

    @MainActor func testAnAppleSheetThatDidNotFinishSaysSoUnlessThePersonClosedIt() async {
        let signIn = await signIn(FakeServer())
        let unfinished = await signIn.finish(identityToken: nil, cancelled: false)
        XCTAssertEqual(unfinished, .failed("Sign in with Apple did not finish."))
        let closed = await signIn.finish(identityToken: nil, cancelled: true)
        XCTAssertEqual(closed, .cancelled)
    }
}
