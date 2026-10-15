import XCTest
@testable import BeeCall

/// CallStore is the only place that fetches the call list and the call views (contract sections 1 to 3): it keeps
/// the last body of each with its version, asks again with If-None-Match, and fetches what /changes announces only
/// when the app holds that resource.
final class CallStoreTests: XCTestCase {
    @MainActor private func text(_ name: String) throws -> String {
        String(decoding: try ContractTests.example(name), as: UTF8.self)
    }

    @MainActor private func ifNoneMatch(_ server: FakeServer) -> String? {
        server.requests.last?.value(forHTTPHeaderField: "If-None-Match")
    }

    @MainActor func testTheListIsFetchedAndAskedAgainWithItsVersion() async throws {
        let server = FakeServer()
        server.replies["GET /calls"] = .status(200, try text("calls"))
        let store = CallStore(server: server.client())
        await store.refreshList()
        XCTAssertEqual(store.calls.count, 6)
        XCTAssertNil(ifNoneMatch(server), "the first ask holds no version")
        server.replies["GET /calls"] = .status(304)
        await store.refreshList()
        XCTAssertEqual(ifNoneMatch(server), "\"f46f5990ebfadcab\"")
        XCTAssertEqual(store.calls.count, 6, "304: nothing changed")
        XCTAssertNil(store.problem)
    }

    @MainActor func testAViewIsFetchedAndAskedAgainWithItsVersion() async throws {
        let server = FakeServer(), id = "call-minseung-20261015-012301"
        server.replies["GET /calls/\(id)"] = .status(200, try text("call-view"))
        let store = CallStore(server: server.client())
        await store.refreshView(id)
        XCTAssertEqual(store.views[id]?.call.state, .ended)
        server.replies["GET /calls/\(id)"] = .status(304)
        await store.refreshView(id)
        XCTAssertEqual(ifNoneMatch(server), "\"1ed2665193df9075\"")
        XCTAssertEqual(store.views[id]?.items.count, 23)
    }

    /// A call the person placed has no record until the AI picks up, and a missed call never has one: 404.
    @MainActor func testAViewNotRecordedYetIsMissingUntilItIs() async throws {
        let server = FakeServer(), id = "call-minseung-20261015-012301"
        server.replies["GET /calls/\(id)"] = .status(404)
        let store = CallStore(server: server.client())
        await store.refreshView(id)
        XCTAssertNil(store.views[id])
        XCTAssertTrue(store.missing.contains(id))
        server.replies["GET /calls/\(id)"] = .status(200, try text("call-view-live"))
        await store.refreshView(id)
        XCTAssertEqual(store.views[id]?.call.state, .live)
        XCTAssertFalse(store.missing.contains(id))
    }

    /// /changes: a view the app holds is fetched again when its version moves; one it does not hold is only noted,
    /// and fetched when it is shown.
    @MainActor func testChangesFetchOnlyWhatTheAppHolds() async throws {
        let server = FakeServer(), held = "call-minseung-20261015-012301", other = "call-minseung-20261014-180158"
        server.replies["GET /calls/\(held)"] = .status(200, try text("call-view-live"))
        server.replies["GET /calls/\(other)"] = .status(200, try text("call-view-ai"))
        let store = CallStore(server: server.client())
        await store.refreshView(held)
        server.replies["GET /calls/\(held)"] = .status(200, try text("call-view"))
        await store.changed(Change(path: "/calls/\(held)", version: "1ed2665193df9075"))
        XCTAssertEqual(store.views[held]?.call.state, .ended)
        let asked = server.requests.count
        await store.changed(Change(path: "/calls/\(held)", version: "1ed2665193df9075"))
        XCTAssertEqual(server.requests.count, asked, "the version drawn already")
        await store.changed(Change(path: "/calls/\(other)", version: "9d76748e784b51b8"))
        XCTAssertEqual(server.requests.count, asked, "not held: only noted")
        await store.show(other)
        XCTAssertNotNil(store.views[other])
        await store.show(other)
        XCTAssertEqual(server.requests.count, asked + 1, "shown again with the version noted: nothing to fetch")
    }

    @MainActor func testAChangedListIsFetched() async throws {
        let server = FakeServer()
        server.replies["GET /calls"] = .status(200, try text("calls"))
        let store = CallStore(server: server.client())
        await store.refreshList()
        let asked = server.requests.count
        await store.changed(Change(path: "/calls", version: "f46f5990ebfadcab"))
        XCTAssertEqual(server.requests.count, asked, "the list drawn already")
        await store.changed(Change(path: "/calls", version: "0123456789abcdef"))
        XCTAssertEqual(server.requests.count, asked + 1)
    }

    @MainActor func testOlderCallsComeAfterTheFirstPage() async throws {
        let server = FakeServer()
        server.replies["GET /calls"] = .status(200, try text("calls"))
        let store = CallStore(server: server.client())
        await store.refreshList()
        XCTAssertTrue(store.hasOlder)
        let older = """
        {"calls": [], "more": false, "before": null, "version": "0000000000000001"}
        """
        server.replies["GET /calls"] = .status(200, older)
        await store.loadOlder()
        let query = server.requests.last?.url?.query ?? ""
        XCTAssertTrue(query.contains("before=MjAyNi0xMC0xNFQwODozMTowMCswOTowMHxjYWxsLW1pbnNldW5nLTIwMjYxMDE0LTA4MzEwMA"))
        XCTAssertFalse(store.hasOlder)
        XCTAssertEqual(store.calls.count, 6)
    }

    /// An answer that breaks the contract keeps what was there, and says so.
    @MainActor func testAnAnswerThatCannotBeReadKeepsTheLastOne() async throws {
        let server = FakeServer()
        server.replies["GET /calls"] = .status(200, try text("calls"))
        let store = CallStore(server: server.client())
        await store.refreshList()
        server.replies["GET /calls"] = .status(200, "{\"calls\": 3}")
        await store.refreshList()
        XCTAssertEqual(store.calls.count, 6)
        XCTAssertNotNil(store.problem)
        server.replies["GET /calls"] = .status(304)
        await store.refreshList()
        XCTAssertNil(store.problem)
    }

    /// The person's own call: the newest call whose id is the room, or the room and a dot after a voice server
    /// restarted in it (section 1).
    @MainActor func testTheOwnCallIsTheNewestCallOfItsRoom() throws {
        let list = try Contract.decode(CallList.self, from: ContractTests.example("calls"))
        XCTAssertEqual(CallStore.ownCall(room: "call-minseung-20261015-012301", in: list.calls)?.call.callID,
                       "call-minseung-20261015-012301")
        XCTAssertNil(CallStore.ownCall(room: "call-minseung-20261015-0123", in: list.calls), "a prefix is not the room")
        XCTAssertNil(CallStore.ownCall(room: "call-minseung-20261015-203002", in: list.calls))
        let restarted = try text("calls").replacingOccurrences(of: "call-minseung-20261015-142209",
                                                                with: "call-minseung-20261015-012301.2")
        let again = try Contract.decode(CallList.self, from: Data(restarted.utf8))
        XCTAssertEqual(CallStore.ownCall(room: "call-minseung-20261015-012301", in: again.calls)?.call.callID,
                       "call-minseung-20261015-012301.2", "the newest record of the room")
    }

    // MARK: - Second review of the D wiring, finding 6

    /// A new call pushes the first page's last call down to where the older pages begin: it stays in the thread.
    @MainActor func testACallPushedOffTheFirstPageStays() async throws {
        let server = FakeServer()
        var first = try XCTUnwrap(JSONSerialization.jsonObject(with: ContractTests.example("calls")) as? [String: Any])
        server.replies["GET /calls"] = .json(200, first)
        let store = CallStore(server: server.client())
        await store.refreshList()
        let pushedOff = try XCTUnwrap(store.calls.last?.call.callID)
        var calls = try XCTUnwrap(first["calls"] as? [[String: Any]])
        var newest = calls[0]
        newest["call_id"] = "call-minseung-20261015-180000"
        newest["started_at"] = "2026-10-15T18:00:00+09:00"
        calls = [newest] + calls.dropLast() // a page of the same size: the last call moves to the next page
        first["calls"] = calls
        first["version"] = "0123456789abcdef"
        server.replies["GET /calls"] = .json(200, first)
        await store.refreshList()
        XCTAssertEqual(store.calls.first?.call.callID, "call-minseung-20261015-180000")
        XCTAssertTrue(store.calls.contains { $0.call.callID == pushedOff }, "not lost between the pages")
        XCTAssertEqual(Set(store.calls.map(\.call.callID)).count, store.calls.count, "no call twice")
    }

    @MainActor func testTwoTapsLoadAnOlderPageOnce() async throws {
        let server = FakeServer()
        server.replies["GET /calls"] = .status(200, try text("calls"))
        let store = CallStore(server: server.client())
        await store.refreshList()
        server.replies["GET /calls"] = .slow(.milliseconds(200), "{\"calls\": [], \"more\": false, \"before\": null, \"version\": \"0000000000000001\"}")
        async let one: Void = store.loadOlder()
        async let two: Void = store.loadOlder()
        _ = await (one, two)
        XCTAssertEqual(server.requests.filter { $0.url?.query?.contains("before=") == true }.count, 1)
    }

    /// One fetch of a view at a time; a change announced during it fetches again after it, so the newer body is the
    /// one kept, never an older answer that lands last.
    @MainActor func testAViewIsFetchedOnceAtATimeAndAgainAfterAChange() async throws {
        let server = FakeServer(), id = "call-minseung-20261015-012301"
        server.replies["GET /calls/\(id)"] = .slow(.milliseconds(200), try text("call-view-live"))
        let store = CallStore(server: server.client())
        let first = Task { await store.refreshView(id) }
        try await Task.sleep(for: .milliseconds(50))
        server.replies["GET /calls/\(id)"] = .status(200, try text("call-view"))
        await store.refreshView(id) // while the first is out: it waits for it, then asks again
        await first.value
        await waitUntil("the newer view is kept") { store.views[id]?.call.state == .ended }
        XCTAssertEqual(server.requests.count, 2)
    }
}
