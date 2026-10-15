import XCTest
@testable import BeeCall

/// "Use Bee" in the Settings app, brought in line with the server (review finding 3).
final class BeeSyncTests: XCTestCase {
    private let link = "https://bee.computer/connect?code=TEST"

    /// GET /me as the token server answers it (voice/token_server.py me).
    private func me(connected: Bool, connecting: Bool) -> FakeServer.Reply {
        .json(200, ["person": "matt", "settings": ["bee": connected, "language": "en"], "bee_connecting": connecting])
    }

    @MainActor private func bee(_ store: UserDefaults, _ server: FakeServer, opened: @escaping (URL) -> Void = { _ in },
                                signInEnded: @escaping () -> Void = {},
                                signedIn: @escaping () -> Bool = { true }) -> BeeSync {
        BeeSync(store: store, server: server.client(signInEnded: signInEnded), open: { opened($0); return true },
                signedIn: signedIn)
    }

    @MainActor private func store(useBee: Bool) -> UserDefaults {
        let store = freshDefaults(self)
        store.set(useBee, forKey: "use_bee")
        return store
    }

    @MainActor func testTurningItOnOpensBeesApprovalPage() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        server.replies["POST /bee/connect"] = .json(200, ["link": link])
        var opened: [URL] = []
        let bee = bee(store(useBee: true), server, opened: { opened.append($0) })
        await bee.sync()
        XCTAssertEqual(server.sent, ["GET /me", "POST /bee/connect"])
        XCTAssertEqual(opened, [URL(string: link)!])
        XCTAssertEqual(bee.status, "Connecting Bee: approve Bee Call in the Bee app")
    }

    @MainActor func testAConnectTheServerRefusesSaysSo() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        server.replies["POST /bee/connect"] = .status(502, "could not start a Bee login")
        var opened: [URL] = []
        let bee = bee(store(useBee: true), server, opened: { opened.append($0) })
        await bee.sync()
        XCTAssertEqual(opened, [])
        XCTAssertEqual(bee.status, "Bee could not be connected right now")
    }

    @MainActor func testConnectedSaysSo() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: true, connecting: false)
        let bee = bee(store(useBee: true), server)
        await bee.sync()
        XCTAssertEqual(server.sent, ["GET /me"])
        XCTAssertEqual(bee.status, "Bee connected")
    }

    @MainActor func testAPendingApprovalSaysSo() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: true)
        let bee = bee(store(useBee: true), server)
        await bee.sync()
        XCTAssertEqual(server.sent, ["GET /me"])
        XCTAssertEqual(bee.status, "Waiting for your approval in the Bee app")
    }

    @MainActor func testTurningItOffWhileConnectedDisconnects() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: true, connecting: false)
        server.replies["POST /bee/disconnect"] = .status(204)
        let bee = bee(store(useBee: false), server)
        await bee.sync()
        XCTAssertEqual(server.sent, ["GET /me", "POST /bee/disconnect"])
        XCTAssertEqual(bee.status, "")
    }

    @MainActor func testOffAndNotConnectedDoesNothing() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        let bee = bee(store(useBee: false), server)
        await bee.sync()
        XCTAssertEqual(server.sent, ["GET /me"])
        XCTAssertEqual(bee.status, "")
    }

    // MARK: - The decision (wanted, connected, connecting), and whether the switch changed since it was last acted on

    @MainActor func testTheDecisionTable() {
        let notConnected = "Bee is not connected. To try again, turn Use Bee off, come back to Bee Call, and turn it on again."
        let cases: [(wanted: Bool, connected: Bool, connecting: Bool, changed: Bool, step: BeeStep)] = [
            (false, true, false, false, .disconnect),
            (false, false, true, false, .disconnect), // the approval is still pending on the server
            (false, true, true, true, .disconnect),
            (false, false, false, true, .show("")),
            (true, true, false, false, .show("Bee connected")),
            (true, true, true, false, .show("Bee connected")),
            (true, false, true, false, .show("Waiting for your approval in the Bee app")),
            (true, false, false, true, .connect), // the first time after the switch changed
            (true, false, false, false, .show(notConnected)), // the approval lapsed: no page again
        ]
        for c in cases {
            XCTAssertEqual(BeeStep.next(wanted: c.wanted, connected: c.connected, connecting: c.connecting,
                                        switchChanged: c.changed), c.step, "\(c)")
        }
    }

    @MainActor func testTurningItOffWhileAnApprovalIsPendingCancelsIt() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: true)
        let bee = bee(store(useBee: false), server)
        await bee.sync()
        XCTAssertEqual(server.sent, ["GET /me", "POST /bee/disconnect"])
        XCTAssertEqual(bee.status, "")
    }

    @MainActor func testTheStatusSaysSoWhileBeeIsTurnedOff() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: true, connecting: false)
        var statusDuringRequest: String?
        let bee = bee(store(useBee: false), server)
        server.onRequest = { if $0.url?.path == "/bee/disconnect" { statusDuringRequest = bee.status } }
        await bee.sync()
        XCTAssertEqual(statusDuringRequest, "Turning Bee off…")
    }

    @MainActor func testTheStatusClearsOnlyAfterTheServerConfirms() async {
        for failure in [FakeServer.Reply.status(500), .unreachable] {
            let server = FakeServer()
            server.replies["GET /me"] = me(connected: true, connecting: false)
            server.replies["POST /bee/disconnect"] = failure
            let bee = bee(store(useBee: false), server)
            await bee.sync()
            XCTAssertEqual(bee.status, "Bee could not be turned off right now. Bee Call tries again when you come back.")
        }
    }

    @MainActor func testALapsedApprovalDoesNotOpenBeesPageAgain() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        server.replies["POST /bee/connect"] = .json(200, ["link": link])
        var opened = 0
        let bee = bee(store(useBee: true), server, opened: { _ in opened += 1 })
        await bee.sync() // the first foreground after the switch was turned on
        await bee.sync() // the next one, after the approval window lapsed
        XCTAssertEqual(opened, 1)
        XCTAssertEqual(server.sent, ["GET /me", "POST /bee/connect", "GET /me"])
        XCTAssertEqual(bee.status,
                       "Bee is not connected. To try again, turn Use Bee off, come back to Bee Call, and turn it on again.")
    }

    @MainActor func testAFailedConnectDoesNotUseUpTheFirstForeground() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        server.replies["POST /bee/connect"] = .unreachable
        var opened = 0
        let bee = bee(store(useBee: true), server, opened: { _ in opened += 1 })
        await bee.sync()
        server.replies["POST /bee/connect"] = .json(200, ["link": link])
        await bee.sync()
        XCTAssertEqual(opened, 1)
    }

    @MainActor func testTurningItOffAndOnAgainOpensThePageAgain() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        server.replies["POST /bee/connect"] = .json(200, ["link": link])
        var opened = 0
        let store = store(useBee: true)
        let bee = bee(store, server, opened: { _ in opened += 1 })
        await bee.sync()
        store.set(false, forKey: "use_bee")
        await bee.sync()
        store.set(true, forKey: "use_bee")
        await bee.sync()
        XCTAssertEqual(opened, 2)
    }

    /// Another person may have signed in, with their own Bee.
    @MainActor func testASignInStartsOver() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        server.replies["POST /bee/connect"] = .json(200, ["link": link])
        var opened = 0
        let bee = bee(store(useBee: true), server, opened: { _ in opened += 1 })
        await bee.sync()
        bee.forgetHandledSwitch()
        await bee.sync()
        XCTAssertEqual(opened, 2)
    }

    @MainActor func testTwoSyncsAtOnceOpenThePageOnce() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        server.replies["POST /bee/connect"] = .json(200, ["link": link])
        var opened = 0
        let bee = bee(store(useBee: true), server, opened: { _ in opened += 1 })
        async let first: Void = bee.sync() // coming to the foreground right after signing in
        async let second: Void = bee.sync()
        _ = await (first, second)
        XCTAssertEqual(opened, 1)
    }

    @MainActor func testEveryAnswerGoesThroughTheSignInRule() async {
        let server = FakeServer()
        server.replies["GET /me"] = .status(401)
        var ended = 0
        await bee(store(useBee: true), server, signInEnded: { ended += 1 }).sync()
        XCTAssertEqual(ended, 1)
    }

    // MARK: - Without the app on screen: a launch for a VoIP push, a change made in the Settings app

    /// Off is honoured at once: the AI's next call may be answered from the lock screen without opening the app.
    @MainActor func testTurningItOffInTheSettingsAppDisconnectsAtOnce() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: true, connecting: false)
        let store = store(useBee: true)
        let bee = bee(store, server)
        store.set(false, forKey: "use_bee")
        await waitUntil("Bee is disconnected") { server.sent == ["GET /me", "POST /bee/disconnect"] }
        _ = bee
    }

    @MainActor func testTurningItOnInTheSettingsAppLeavesBeesPageForTheForeground() async throws {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        server.replies["POST /bee/connect"] = .json(200, ["link": link])
        var opened = 0
        let store = store(useBee: false)
        let bee = bee(store, server, opened: { _ in opened += 1 })
        store.set(true, forKey: "use_bee")
        await waitUntil("the server is asked") { server.sent == ["GET /me"] }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(server.sent, ["GET /me"], "no approval link without the app on screen")
        await bee.sync() // the app comes to the foreground
        XCTAssertEqual(opened, 1)
    }

    @MainActor func testASyncWithoutTheScreenNeverOpensBeesPageNorUsesItUp() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        server.replies["POST /bee/connect"] = .json(200, ["link": link])
        var opened = 0
        let bee = bee(store(useBee: true), server, opened: { _ in opened += 1 })
        await bee.sync(openingApprovalPage: false) // a launch for a VoIP push
        XCTAssertEqual(server.sent, ["GET /me"])
        await bee.sync()
        XCTAssertEqual(opened, 1)
    }

    /// Leaving Bee Call while /bee/connect runs: iOS cannot open Bee's page from the background.
    @MainActor func testAPageThatCouldNotOpenIsNotUsedUp() async {
        let server = FakeServer()
        server.replies["GET /me"] = me(connected: false, connecting: false)
        server.replies["POST /bee/connect"] = .json(200, ["link": link])
        var onScreen = false
        var opened = 0
        let bee = BeeSync(store: store(useBee: true), server: server.client(),
                          open: { _ in if onScreen { opened += 1 }; return onScreen })
        await bee.sync()
        XCTAssertEqual(server.sent, ["GET /me", "POST /bee/connect", "POST /bee/disconnect"],
                       "the approval nobody can see is cancelled")
        onScreen = true
        await bee.sync() // the next foreground
        XCTAssertEqual(opened, 1)
    }

    @MainActor func testAChangeWhileSignedOutSendsNothing() async throws {
        let server = FakeServer()
        let store = store(useBee: true)
        let bee = bee(store, server, signedIn: { false })
        store.set(false, forKey: "use_bee")
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertEqual(server.sent, [])
        _ = bee
    }

    @MainActor func testAnUnreachableServerChangesNothing() async {
        let server = FakeServer()
        server.replies["GET /me"] = .unreachable
        let bee = bee(store(useBee: true), server)
        await bee.sync()
        XCTAssertEqual(server.sent, ["GET /me"])
        XCTAssertEqual(bee.status, "")
    }
}
