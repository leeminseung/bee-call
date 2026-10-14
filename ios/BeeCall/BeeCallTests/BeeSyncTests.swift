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
                                statuses: @escaping (Int) -> Void = { _ in }) -> BeeSync {
        BeeSync(store: store, transport: server, open: { opened($0) }, noteStatus: statuses)
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

    @MainActor func testAnUnreachableServerChangesNothing() async {
        let server = FakeServer()
        server.replies["GET /me"] = .unreachable
        let bee = bee(store(useBee: true), server)
        await bee.sync()
        XCTAssertEqual(server.sent, ["GET /me"])
        XCTAssertEqual(bee.status, "")
    }
}
