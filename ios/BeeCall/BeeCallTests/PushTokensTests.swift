import XCTest
@testable import BeeCall

/// This phone's alert and VoIP push tokens reach the server (contract section 3).
final class PushTokensTests: XCTestCase {
    private let alertToken = Data([0xAB, 0x01])
    private let voipToken = Data([0xCD, 0x02])

    @MainActor func testANewAlertTokenIsSentAtOnce() async throws {
        let server = FakeServer()
        let tokens = PushTokens(transport: server, environment: "development")
        await tokens.received(alertToken, kind: .alert)
        XCTAssertEqual(server.sent, ["POST /push"])
        XCTAssertEqual(try server.body(of: "POST /push") as NSDictionary,
                       ["token": "ab01", "environment": "development"] as NSDictionary)
    }

    @MainActor func testANewVoIPTokenIsSentAtOnce() async throws {
        let server = FakeServer()
        let tokens = PushTokens(transport: server, environment: "production")
        await tokens.received(voipToken, kind: .voip)
        XCTAssertEqual(server.sent, ["POST /voip"])
        XCTAssertEqual(try server.body(of: "POST /voip") as NSDictionary,
                       ["token": "cd02", "environment": "production"] as NSDictionary)
        XCTAssertTrue(tokens.voipRegistered)
    }

    @MainActor func testAnAlertTokenIsSentAgainUntilTheServerHasIt() async {
        let server = FakeServer()
        server.replies["POST /push"] = .unreachable
        let tokens = PushTokens(transport: server, environment: "development")
        await tokens.received(alertToken, kind: .alert)
        server.replies["POST /push"] = nil
        await tokens.deliverIfNeeded(.alert) // the offer list's refresh
        await tokens.deliverIfNeeded(.alert)
        XCTAssertEqual(server.sent, ["POST /push", "POST /push"])
    }

    @MainActor func testARefusedVoIPTokenDoesNotRingAClosedApp() async {
        let server = FakeServer()
        server.replies["POST /voip"] = .status(400)
        let tokens = PushTokens(transport: server, environment: "development")
        await tokens.received(voipToken, kind: .voip)
        XCTAssertFalse(tokens.voipRegistered)
    }

    @MainActor func testATakenBackTokenIsNotSent() async {
        let server = FakeServer()
        let tokens = PushTokens(transport: server, environment: "development")
        await tokens.received(voipToken, kind: .voip)
        tokens.invalidated(.voip)
        await tokens.deliver(.voip)
        XCTAssertEqual(server.sent, ["POST /voip"])
        XCTAssertFalse(tokens.voipRegistered)
    }

    /// The tokens belong to the person who signed in: the server moves them to that person (contract section 3).
    @MainActor func testASignInSendsBothTokensAgain() async throws {
        let server = FakeServer()
        let tokens = PushTokens(transport: server, environment: "development")
        await tokens.received(alertToken, kind: .alert)
        await tokens.received(voipToken, kind: .voip)
        await tokens.signedIn()
        XCTAssertEqual(server.sent, ["POST /push", "POST /voip", "POST /push", "POST /voip"])
        XCTAssertEqual(try server.body(of: "POST /voip")["token"] as? String, "cd02")
    }

    @MainActor func testASignInBeforeAnyTokenSendsNothing() async {
        let server = FakeServer()
        await PushTokens(transport: server, environment: "development").signedIn()
        XCTAssertEqual(server.sent, [])
    }
}
