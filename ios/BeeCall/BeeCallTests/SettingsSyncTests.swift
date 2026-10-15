import UIKit
import XCTest
@testable import BeeCall

/// The switches of the Settings app reach the server as PUT /settings (contract section 1, review finding 1).
final class SettingsSyncTests: XCTestCase {
    @MainActor private func sync(_ store: UserDefaults, _ server: FakeServer, tokenReadable: @escaping () -> Bool = { true },
                                 statuses: @escaping (Int) -> Void = { _ in },
                                 timeZone: @escaping () -> TimeZone = { TimeZone(identifier: "Asia/Seoul")! }) -> SettingsSync {
        SettingsSync(store: store, transport: server, tokenReadable: tokenReadable, noteStatus: statuses,
                     timeZone: timeZone)
    }

    // MARK: - What is sent

    @MainActor func testUntouchedSwitchesSendTheirDefaults() async throws {
        let server = FakeServer()
        await sync(freshDefaults(self), server).send().value
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
        await sync(store, server).send().value
        let body = try server.body(of: "PUT /settings")
        XCTAssertEqual(body["language"] as? String, "ko")
        XCTAssertEqual(body["call_time"] as? String, "07:30")
        XCTAssertEqual(body["record_on_server"] as? Bool, false)
        XCTAssertEqual(body["record_on_phone"] as? Bool, true)
    }

    @MainActor func testATimeTheServerWouldRefuseIsLeftOut() async throws {
        let store = freshDefaults(self), server = FakeServer()
        store.set("9pm", forKey: "call_time")
        await sync(store, server).send().value
        XCTAssertNil(try server.body(of: "PUT /settings")["call_time"])
    }

    /// The Settings app keeps showing a typed time the server would refuse, so the app says it was not saved
    /// (review finding 10).
    @MainActor func testATimeTheServerWouldRefuseIsShown() async {
        let store = freshDefaults(self), server = FakeServer()
        store.set("9pm", forKey: "call_time")
        let sync = sync(store, server)
        await sync.send().value
        XCTAssertEqual(sync.problem, "The evening call time “9pm” was not saved: write it like 21:00 in Settings.")
    }

    @MainActor func testFixingTheTimeClearsTheProblem() async throws {
        let store = freshDefaults(self), server = FakeServer()
        store.set("9pm", forKey: "call_time")
        let sync = sync(store, server)
        await sync.send().value
        store.set("21:30", forKey: "call_time") // fixed in the Settings app
        await waitUntil("the fixed time is sent") { server.sent.count == 2 }
        await waitUntil("the problem is gone") { sync.problem == nil }
        XCTAssertEqual(try server.body(of: "PUT /settings")["call_time"] as? String, "21:30")
    }

    @MainActor func testSettingsTheServerRefusesSaySoWithItsReason() async {
        let server = FakeServer()
        server.replies["PUT /settings"] = .status(400, "time_zone must be an IANA time zone name, like Asia/Seoul\n")
        let sync = sync(freshDefaults(self), server)
        await sync.send().value
        XCTAssertEqual(sync.problem,
                       "Your settings were not saved: time_zone must be an IANA time zone name, like Asia/Seoul.")
        server.replies["PUT /settings"] = nil
        await sync.send().value
        XCTAssertNil(sync.problem)
    }

    @MainActor func testEverySendCarriesThePhonesTimeZone() async throws {
        let server = FakeServer()
        await sync(freshDefaults(self), server, timeZone: { TimeZone(identifier: "America/New_York")! }).send().value
        XCTAssertEqual(try server.body(of: "PUT /settings")["time_zone"] as? String, "America/New_York")
    }

    @MainActor func testTheTimeZoneIsThePhonesOwnByDefault() async throws {
        let server = FakeServer()
        await SettingsSync(store: freshDefaults(self), transport: server, tokenReadable: { true },
                           noteStatus: { _ in }).send().value
        XCTAssertEqual(try server.body(of: "PUT /settings")["time_zone"] as? String, TimeZone.current.identifier)
    }

    // MARK: - When it is sent

    @MainActor func testNothingIsSentWithoutASignInToken() async {
        let server = FakeServer()
        await sync(freshDefaults(self), server, tokenReadable: { false }).send().value
        XCTAssertEqual(server.sent, [])
    }

    /// A VoIP push can launch the app before the phone's first unlock, when the Keychain cannot be read yet.
    @MainActor func testALaunchBeforeTheTokenCanBeReadSendsOnceItCan() async {
        let server = FakeServer()
        var readable = false
        let sync = sync(freshDefaults(self), server, tokenReadable: { readable })
        await sync.send().value
        XCTAssertEqual(server.sent, [])
        readable = true
        NotificationCenter.default.post(name: UIApplication.protectedDataDidBecomeAvailableNotification, object: nil)
        await waitUntil("the settings are sent") { server.sent == ["PUT /settings"] }
    }

    /// The Settings app changes the app's defaults; the app hears it as UserDefaults.didChangeNotification.
    @MainActor func testAChangeInTheSettingsAppIsSentAtOnce() async throws {
        let store = freshDefaults(self), server = FakeServer()
        let sync = sync(store, server)
        await sync.send().value // at launch
        store.set(false, forKey: "record_on_server")
        await waitUntil("the change is sent") { server.sent.count == 2 }
        XCTAssertEqual(try server.body(of: "PUT /settings")["record_on_server"] as? Bool, false)
        _ = sync
    }

    @MainActor func testAChangeToSomethingElseSendsNothing() async throws {
        let store = freshDefaults(self), server = FakeServer()
        let sync = sync(store, server)
        await sync.send().value
        store.set("matt", forKey: "person")
        store.set(true, forKey: "use_bee") // Bee follows its own requests, not PUT /settings
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(server.sent, ["PUT /settings"])
        _ = sync
    }

    @MainActor func testAChangeAfterAFailedSendIsSentAgain() async throws {
        let store = freshDefaults(self), server = FakeServer()
        server.replies["PUT /settings"] = .unreachable
        let sync = sync(store, server)
        await sync.send().value
        server.replies["PUT /settings"] = nil
        store.set("matt", forKey: "person") // any change notice: the server still lacks the switches
        await waitUntil("the switches are sent again") { server.sent.count == 2 }
        _ = sync
    }

    /// Sends go out one at a time, in order, so an older value never lands after a newer one.
    @MainActor func testAChangeDuringASendGoesOutAfterItWithTheNewValue() async throws {
        let store = freshDefaults(self), server = FakeServer()
        server.replies["PUT /settings"] = .after(.milliseconds(300), 200)
        let sync = sync(store, server)
        let first = sync.send()
        await waitUntil("the first send is out") { server.sent.count == 1 }
        store.set("ko", forKey: "language")
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(server.sent.count, 1, "the second send waits for the first")
        await first.value
        await waitUntil("the second send is out") { server.sent.count == 2 }
        XCTAssertEqual(try server.body(of: "PUT /settings")["language"] as? String, "ko")
    }

    // MARK: - Before an answered call joins

    @MainActor func testAnAnsweredCallWaitsAtMostTwoSeconds() {
        XCTAssertEqual(SettingsSync.answerWait, .seconds(2))
    }

    @MainActor func testAnsweringWaitsForTheServersAnswer() async {
        let server = FakeServer()
        server.replies["PUT /settings"] = .after(.milliseconds(300), 200)
        let start = ContinuousClock.now
        await sync(freshDefaults(self), server).sendBeforeAnswering(within: .seconds(2))
        XCTAssertEqual(server.sent, ["PUT /settings"])
        XCTAssertGreaterThanOrEqual(ContinuousClock.now - start, .milliseconds(300))
    }

    @MainActor func testASlowServerDoesNotHoldTheCall() async {
        let server = FakeServer()
        server.replies["PUT /settings"] = .hang
        let start = ContinuousClock.now
        await sync(freshDefaults(self), server).sendBeforeAnswering(within: .milliseconds(300))
        XCTAssertEqual(server.sent, ["PUT /settings"])
        XCTAssertLessThan(ContinuousClock.now - start, .seconds(1))
    }

    // MARK: - Answers

    @MainActor func testTheAnswerGoesThroughTheSignInRule() async {
        let server = FakeServer()
        server.replies["PUT /settings"] = .status(401)
        var noted: [Int] = []
        await sync(freshDefaults(self), server, statuses: { noted.append($0) }).send().value
        XCTAssertEqual(noted, [401])
    }
}
