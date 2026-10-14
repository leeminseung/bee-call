import XCTest
@testable import BeeCall

/// The switches of the Settings app reach the server as PUT /settings (contract section 1).
final class SettingsSyncTests: XCTestCase {
    @MainActor private func sync(_ store: UserDefaults, _ server: FakeServer, signedIn: Bool = true,
                                 statuses: @escaping (Int) -> Void = { _ in }) -> SettingsSync {
        SettingsSync(store: store, transport: server, isSignedIn: { signedIn }, noteStatus: statuses)
    }

    @MainActor func testUntouchedSwitchesSendTheirDefaults() async throws {
        let server = FakeServer()
        await sync(freshDefaults(self), server).send()
        XCTAssertEqual(server.sent, ["PUT /settings"])
        let body = try server.body(of: "PUT /settings")
        XCTAssertEqual(body["language"] as? String, "en")
        XCTAssertEqual(body["call_time"] as? String, "21:00")
        XCTAssertEqual(body["record_on_server"] as? Bool, true)
        XCTAssertEqual(body["record_on_phone"] as? Bool, false)
        XCTAssertNil(body["use_bee"], "Bee is connected with /bee/connect, not with a setting")
    }

    @MainActor func testChangedSwitchesAreSent() async throws {
        let store = freshDefaults(self), server = FakeServer()
        store.set("ko", forKey: "language")
        store.set("07:30", forKey: "call_time")
        store.set(false, forKey: "record_on_server")
        store.set(true, forKey: "record_on_phone")
        await sync(store, server).send()
        let body = try server.body(of: "PUT /settings")
        XCTAssertEqual(body["language"] as? String, "ko")
        XCTAssertEqual(body["call_time"] as? String, "07:30")
        XCTAssertEqual(body["record_on_server"] as? Bool, false)
        XCTAssertEqual(body["record_on_phone"] as? Bool, true)
    }

    @MainActor func testATimeTheServerWouldRefuseIsLeftOut() async throws {
        let store = freshDefaults(self), server = FakeServer()
        store.set("9pm", forKey: "call_time")
        await sync(store, server).send()
        XCTAssertNil(try server.body(of: "PUT /settings")["call_time"])
    }

    @MainActor func testNothingIsSentWhileSignedOut() async {
        let server = FakeServer()
        await sync(freshDefaults(self), server, signedIn: false).send()
        XCTAssertEqual(server.sent, [])
    }

    @MainActor func testTheAnswerGoesThroughTheSignInRule() async {
        let server = FakeServer()
        server.replies["PUT /settings"] = .status(401)
        var noted: [Int] = []
        await sync(freshDefaults(self), server, statuses: { noted.append($0) }).send()
        XCTAssertEqual(noted, [401])
    }
}
