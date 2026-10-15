import XCTest
@testable import BeeCall

/// CallStore is the only place that fetches the call list and the call views (contract sections 1 to 3): it keeps
/// the last body of each with its version, asks again with If-None-Match, and fetches what /changes announces only
/// when the app holds that resource. Ids, versions and counts come from the contract's example files.
final class CallStoreTests: XCTestCase {
    @MainActor private func ifNoneMatch(_ server: FakeServer) -> String? {
        server.requests.last?.value(forHTTPHeaderField: "If-None-Match")
    }

    /// The call of the ended and live examples, and the call of the AI's example.
    @MainActor private func ids() throws -> (person: String, ai: String) {
        (try Examples.decode(CallView.self, "call-view").call.callID, try Examples.decode(CallView.self, "call-view-ai").call.callID)
    }

    @MainActor func testTheListIsFetchedAndAskedAgainWithItsVersion() async throws {
        let server = FakeServer(), list = try Examples.decode(CallList.self, "calls")
        server.replies["GET /calls"] = .status(200, try Examples.text("calls"))
        let store = CallStore(server: server.client())
        await store.refreshList()
        XCTAssertEqual(store.calls.map(\.id), list.calls.map(\.id))
        XCTAssertNil(ifNoneMatch(server), "the first ask holds no version")
        server.replies["GET /calls"] = .status(304)
        await store.refreshList()
        XCTAssertEqual(ifNoneMatch(server), "\"\(list.version)\"")
        XCTAssertEqual(store.calls.count, list.calls.count, "304: nothing changed")
        XCTAssertNil(store.problem)
    }

    @MainActor func testAViewIsFetchedAndAskedAgainWithItsVersion() async throws {
        let server = FakeServer(), view = try Examples.decode(CallView.self, "call-view"), id = view.call.callID
        server.replies["GET /calls/\(id)"] = .status(200, try Examples.text("call-view"))
        let store = CallStore(server: server.client())
        await store.refreshView(id)
        XCTAssertEqual(store.views[id], view)
        server.replies["GET /calls/\(id)"] = .status(304)
        await store.refreshView(id)
        XCTAssertEqual(ifNoneMatch(server), "\"\(view.version)\"")
        XCTAssertEqual(store.views[id], view, "304: kept")
    }

    /// A call the person placed has no record until the AI picks up, and a missed call never has one: 404.
    @MainActor func testAViewNotRecordedYetIsMissingUntilItIs() async throws {
        let server = FakeServer(), id = try ids().person
        server.replies["GET /calls/\(id)"] = .status(404)
        let store = CallStore(server: server.client())
        await store.refreshView(id)
        XCTAssertNil(store.views[id])
        XCTAssertTrue(store.missing.contains(id))
        server.replies["GET /calls/\(id)"] = .status(200, try Examples.text("call-view-live"))
        await store.refreshView(id)
        XCTAssertEqual(store.views[id]?.call.state, .live)
        XCTAssertFalse(store.missing.contains(id))
    }

    /// /changes: a view the app holds is fetched again when its version moves; one it does not hold is only noted,
    /// and fetched when it is shown.
    @MainActor func testChangesFetchOnlyWhatTheAppHolds() async throws {
        let server = FakeServer(), (held, other) = try ids()
        let ended = try Examples.decode(CallView.self, "call-view"), ai = try Examples.decode(CallView.self, "call-view-ai")
        server.replies["GET /calls/\(held)"] = .status(200, try Examples.text("call-view-live"))
        server.replies["GET /calls/\(other)"] = .status(200, try Examples.text("call-view-ai"))
        let store = CallStore(server: server.client())
        await store.refreshView(held)
        server.replies["GET /calls/\(held)"] = .status(200, try Examples.text("call-view"))
        await store.changed(Change(path: "/calls/\(held)", version: ended.version))
        XCTAssertEqual(store.views[held], ended, "held, and its version moved: fetched")
        let asked = server.requests.count
        await store.changed(Change(path: "/calls/\(held)", version: ended.version))
        XCTAssertEqual(server.requests.count, asked, "the version drawn already")
        await store.changed(Change(path: "/calls/\(other)", version: ai.version))
        XCTAssertEqual(server.requests.count, asked, "not held: only noted")
        await store.show(other)
        XCTAssertEqual(store.views[other], ai)
        await store.show(other)
        XCTAssertEqual(server.requests.count, asked + 1, "shown again with the version noted: nothing to fetch")
    }

    @MainActor func testAChangedListIsFetched() async throws {
        let server = FakeServer(), version = try Examples.decode(CallList.self, "calls").version
        server.replies["GET /calls"] = .status(200, try Examples.text("calls"))
        let store = CallStore(server: server.client())
        await store.refreshList()
        let asked = server.requests.count
        await store.changed(Change(path: "/calls", version: version))
        XCTAssertEqual(server.requests.count, asked, "the list drawn already")
        await store.changed(Change(path: "/calls", version: Examples.otherVersion(than: version)))
        XCTAssertEqual(server.requests.count, asked + 1)
    }

    @MainActor func testOlderCallsComeAfterTheFirstPage() async throws {
        let server = FakeServer(), list = try Examples.decode(CallList.self, "calls")
        server.replies["GET /calls"] = .status(200, try Examples.text("calls"))
        let store = CallStore(server: server.client())
        await store.refreshList()
        XCTAssertEqual(store.hasOlder, list.more)
        let older = """
        {"calls": [], "more": false, "before": null, "version": "0000000000000001"}
        """
        server.replies["GET /calls"] = .status(200, older)
        await store.loadOlder()
        guard let before = list.before else { return } // the example has one page: nothing older to ask for
        let query = URLComponents(url: try XCTUnwrap(server.requests.last?.url), resolvingAgainstBaseURL: false)?.queryItems
        XCTAssertEqual(query?.first { $0.name == "before" }?.value, before)
        XCTAssertFalse(store.hasOlder)
        XCTAssertEqual(store.calls.count, list.calls.count)
    }

    /// An answer that breaks the contract keeps what was there, and says so.
    @MainActor func testAnAnswerThatCannotBeReadKeepsTheLastOne() async throws {
        let server = FakeServer(), count = try Examples.decode(CallList.self, "calls").calls.count
        server.replies["GET /calls"] = .status(200, try Examples.text("calls"))
        let store = CallStore(server: server.client())
        await store.refreshList()
        server.replies["GET /calls"] = .status(200, "{\"calls\": 3}")
        await store.refreshList()
        XCTAssertEqual(store.calls.count, count)
        XCTAssertNotNil(store.problem)
        server.replies["GET /calls"] = .status(304)
        await store.refreshList()
        XCTAssertNil(store.problem)
    }

    /// The person's own call: the newest call whose id is the room, or the room and a dot after a voice server
    /// restarted in it (section 1).
    @MainActor func testTheOwnCallIsTheNewestCallOfItsRoom() throws {
        let calls = try Examples.decode(CallList.self, "calls").calls, room = try ids().person
        XCTAssertEqual(CallStore.ownCall(room: room, in: calls)?.call.callID, room)
        XCTAssertNil(CallStore.ownCall(room: String(room.dropLast(2)), in: calls), "a prefix is not the room")
        XCTAssertNil(CallStore.ownCall(room: "call-nobody-20261015-203002", in: calls))
        let restarted = try Examples.changed("calls") { body in // a second record of the room, newest
            var entries = body["calls"] as? [[String: Any]] ?? []
            var record = try XCTUnwrap(entries.first { $0["call_id"] as? String == room })
            record["call_id"] = room + ".2"
            entries.insert(record, at: 0)
            body["calls"] = entries
        }
        let again = try Contract.decode(CallList.self, from: Data(restarted.utf8))
        XCTAssertEqual(CallStore.ownCall(room: room, in: again.calls)?.call.callID, room + ".2", "the newest record of the room")
    }

    // MARK: - Second review of the D wiring, finding 6

    /// A new call pushes the first page's last call down to where the older pages begin: it stays in the thread.
    @MainActor func testACallPushedOffTheFirstPageStays() async throws {
        let server = FakeServer()
        var first = try Examples.object("calls")
        server.replies["GET /calls"] = .json(200, first)
        let store = CallStore(server: server.client())
        await store.refreshList()
        let pushedOff = try XCTUnwrap(store.calls.last?.call.callID)
        var calls = try XCTUnwrap(first["calls"] as? [[String: Any]])
        var newest = calls[0]
        newest["call_id"] = "call-minseung-20991231-180000"
        newest["started_at"] = "2099-12-31T18:00:00+09:00"
        calls = [newest] + calls.dropLast() // a page of the same size: the last call moves to the next page
        first["calls"] = calls
        first["version"] = Examples.otherVersion(than: first["version"] as? String ?? "")
        server.replies["GET /calls"] = .json(200, first)
        await store.refreshList()
        XCTAssertEqual(store.calls.first?.call.callID, "call-minseung-20991231-180000")
        XCTAssertTrue(store.calls.contains { $0.call.callID == pushedOff }, "not lost between the pages")
        XCTAssertEqual(Set(store.calls.map(\.call.callID)).count, store.calls.count, "no call twice")
    }

    @MainActor func testTwoTapsLoadAnOlderPageOnce() async throws {
        let server = FakeServer()
        server.replies["GET /calls"] = .status(200, try Examples.changed("calls") { body in
            body["more"] = true // an older page to ask for, whatever the example says
            if body["before"] is NSNull { body["before"] = "older" }
        })
        let store = CallStore(server: server.client())
        await store.refreshList()
        server.replies["GET /calls"] = .slow(.milliseconds(200),
                                             "{\"calls\": [], \"more\": false, \"before\": null, \"version\": \"0000000000000001\"}")
        async let one: Void = store.loadOlder()
        async let two: Void = store.loadOlder()
        _ = await (one, two)
        XCTAssertEqual(server.requests.filter { $0.url?.query?.contains("before=") == true }.count, 1)
    }

    /// One fetch of a view at a time; a change announced during it fetches again after it, so the newer body is the
    /// one kept, never an older answer that lands last.
    @MainActor func testAViewIsFetchedOnceAtATimeAndAgainAfterAChange() async throws {
        let server = FakeServer(), id = try ids().person
        server.replies["GET /calls/\(id)"] = .slow(.milliseconds(200), try Examples.text("call-view-live"))
        let store = CallStore(server: server.client())
        let first = Task { await store.refreshView(id) }
        try await Task.sleep(for: .milliseconds(50))
        server.replies["GET /calls/\(id)"] = .status(200, try Examples.text("call-view"))
        await store.refreshView(id) // while the first is out: it waits for it, then asks again
        await first.value
        await waitUntil("the newer view is kept") { store.views[id]?.call.state == .ended }
        XCTAssertEqual(server.requests.count, 2)
    }
}
