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

    /// /changes: a view the app holds is fetched again when its version moves; one it does not hold is fetched
    /// when it is shown, and asked again with its version each time it is shown after that.
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
        XCTAssertEqual(server.requests.count, asked, "not held: not fetched")
        await store.show(other)
        XCTAssertEqual(store.views[other], ai)
        server.replies["GET /calls/\(other)"] = .status(304)
        await store.show(other)
        XCTAssertEqual(server.requests.count, asked + 2)
        XCTAssertEqual(ifNoneMatch(server), "\"\(ai.version)\"", "shown again: asked with its version")
        XCTAssertEqual(store.views[other], ai, "304: kept")
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

    // MARK: - Fresh-context review of round1 7c1361a

    /// Finding 1: a view held from during a call, shown after the call ended while /changes was closed (and later
    /// than /changes announces ended calls on connect), is asked again with its version: the ended call replaces
    /// it, and an unchanged view is a 304.
    @MainActor func testAHeldViewIsAskedAgainWhenShown() async throws {
        let server = FakeServer(), id = try ids().person
        let live = try Examples.decode(CallView.self, "call-view-live"), ended = try Examples.decode(CallView.self, "call-view")
        server.replies["GET /calls/\(id)"] = .status(200, try Examples.text("call-view-live"))
        let store = CallStore(server: server.client())
        await store.refreshView(id)
        server.replies["GET /calls/\(id)"] = .status(200, try Examples.text("call-view"))
        await store.show(id)
        XCTAssertEqual(ifNoneMatch(server), "\"\(live.version)\"", "asked with the version held")
        XCTAssertEqual(store.views[id], ended, "the call ended meanwhile")
        server.replies["GET /calls/\(id)"] = .status(304)
        await store.show(id)
        XCTAssertEqual(ifNoneMatch(server), "\"\(ended.version)\"")
        XCTAssertEqual(store.views[id], ended, "304: kept")
    }

    /// Finding 1: the list and every view held are asked again with their versions at once (when /changes opens).
    @MainActor func testTheListAndEveryHeldViewAreAskedAgain() async throws {
        let server = FakeServer(), (person, ai) = try ids(), list = try Examples.decode(CallList.self, "calls")
        let view = try Examples.decode(CallView.self, "call-view-ai")
        server.replies["GET /calls"] = .status(200, try Examples.text("calls"))
        server.replies["GET /calls/\(person)"] = .status(200, try Examples.text("call-view-live"))
        server.replies["GET /calls/\(ai)"] = .status(200, try Examples.text("call-view-ai"))
        let store = CallStore(server: server.client())
        await store.refreshList()
        await store.refreshView(person)
        await store.refreshView(ai)
        server.replies["GET /calls"] = .status(304)
        server.replies["GET /calls/\(person)"] = .status(200, try Examples.text("call-view"))
        server.replies["GET /calls/\(ai)"] = .status(304)
        let asked = server.requests.count
        await store.refreshHeld()
        let again = server.requests.dropFirst(asked)
        func version(_ path: String) -> String? {
            again.first { $0.url?.path == path }?.value(forHTTPHeaderField: "If-None-Match")
        }
        XCTAssertEqual(again.count, 3, "the list and the two views, once each")
        XCTAssertEqual(version("/calls"), "\"\(list.version)\"")
        XCTAssertEqual(version("/calls/\(person)"), "\"\(try Examples.decode(CallView.self, "call-view-live").version)\"")
        XCTAssertEqual(version("/calls/\(ai)"), "\"\(view.version)\"")
        XCTAssertEqual(store.views[person]?.call.state, .ended)
        XCTAssertEqual(store.views[ai], view)
    }

    /// A record the server deleted (a call hung up before its first words, BC-027) is not kept: after a 404 its
    /// view is gone and the call reads as missing.
    @MainActor func testAViewWhoseRecordIsGoneIsDropped() async throws {
        let server = FakeServer(), id = try ids().person
        server.replies["GET /calls/\(id)"] = .status(200, try Examples.text("call-view-live"))
        let store = CallStore(server: server.client())
        await store.refreshView(id)
        server.replies["GET /calls/\(id)"] = .status(404)
        await store.show(id)
        XCTAssertNil(store.views[id])
        XCTAssertTrue(store.missing.contains(id))
    }

    /// The first page with a call of its own added, the newest, live: one the server lists for a moment before it
    /// deletes the record.
    @MainActor private func pageWithAPassingCall() throws -> (page: [String: Any], id: String) {
        var page = try Examples.object("calls")
        var calls = try XCTUnwrap(page["calls"] as? [[String: Any]])
        var passing = calls[0]
        passing["call_id"] = "call-test-20991231-180000"
        passing["started_at"] = "2099-12-31T18:00:00+09:00"
        passing["state"] = "live"
        passing["duration_s"] = NSNull()
        passing["results"] = [Any]()
        calls.insert(passing, at: 0)
        page["calls"] = calls
        page["version"] = Examples.otherVersion(than: page["version"] as? String ?? "")
        return (page, "call-test-20991231-180000")
    }

    /// Finding 2: a call /changes announced in the moment before its record was deleted leaves the thread once the
    /// first page no longer has it. Calls older than the page, from older pages, stay.
    @MainActor func testACallTheFirstPageNoLongerHasLeaves() async throws {
        let server = FakeServer(), list = try Examples.decode(CallList.self, "calls"), made = try pageWithAPassingCall()
        var first = made.page
        let passing = made.id
        first["more"] = true // an older page to ask for, whatever the example says
        if first["before"] is NSNull { first["before"] = "older" }
        server.replies["GET /calls"] = .json(200, first)
        let store = CallStore(server: server.client())
        await store.refreshList()
        XCTAssertTrue(store.calls.contains { $0.id == passing })
        var older = try XCTUnwrap((first["calls"] as? [[String: Any]])?.last)
        older["call_id"] = "call-test-19990101-090000"
        older["started_at"] = "1999-01-01T09:00:00+09:00"
        server.replies["GET /calls"] = .json(200, ["calls": [older], "more": false, "before": NSNull(),
                                                   "version": "0000000000000001"])
        await store.loadOlder()
        XCTAssertTrue(store.calls.contains { $0.id == "call-test-19990101-090000" })
        server.replies["GET /calls"] = .status(200, try Examples.changed("calls") { body in
            body["more"] = true // older calls exist, as before
            if body["before"] is NSNull { body["before"] = "older" }
        })
        await store.refreshList()
        XCTAssertFalse(store.calls.contains { $0.id == passing }, "not on the first page any more: gone")
        XCTAssertEqual(Array(store.calls.prefix(list.calls.count)).map(\.id), list.calls.map(\.id))
        XCTAssertTrue(store.calls.contains { $0.id == "call-test-19990101-090000" }, "an older page's call stays")
    }

    /// Finding 2: a first page that is the whole list (no older calls) leaves nothing else.
    @MainActor func testAWholeListLeavesNothingElse() async throws {
        let server = FakeServer()
        let (first, passing) = try pageWithAPassingCall()
        server.replies["GET /calls"] = .json(200, first)
        let store = CallStore(server: server.client())
        await store.refreshList()
        XCTAssertTrue(store.calls.contains { $0.id == passing })
        server.replies["GET /calls"] = .json(200, ["calls": [], "more": false, "before": NSNull(),
                                                   "version": "0000000000000002"])
        await store.refreshList()
        XCTAssertTrue(store.calls.isEmpty, "no calls at all")
        XCTAssertFalse(store.calls.contains { $0.id == passing })
    }

    /// Finding 2: one fetch of the list at a time. A change announced during it waits for it and fetches again,
    /// so the newer list is the one kept, never an older answer that lands last.
    @MainActor func testTheListIsFetchedOnceAtATimeAndAgainAfterAChange() async throws {
        let server = FakeServer(), list = try Examples.decode(CallList.self, "calls")
        let olderVersion = Examples.otherVersion(than: list.version)
        server.replies["GET /calls"] = .slow(.milliseconds(200), try Examples.changed("calls") { body in
            body["version"] = olderVersion
        })
        let store = CallStore(server: server.client())
        let first = Task { await store.refreshList() }
        try await Task.sleep(for: .milliseconds(50))
        server.replies["GET /calls"] = .status(200, try Examples.text("calls"))
        await store.refreshList() // while the first is out
        XCTAssertEqual(store.list?.version, list.version, "the newer list, once this returns")
        await first.value
        XCTAssertEqual(store.list?.version, list.version, "not the older answer, landing last")
        XCTAssertEqual(server.requests.count, 2)
        XCTAssertEqual(ifNoneMatch(server), "\"\(olderVersion)\"", "asked again with what the first brought")
    }

    // MARK: - The calls list's older pages

    /// The list's foot says where the older pages are: more to load, loading ("더 불러오는 중"), failed ("불러오지
    /// 못했어요 · 다시 시도"), or all loaded ("처음 통화까지 다 봤어요").
    @MainActor func testOlderPagesSayWhereTheyAre() async throws {
        let server = FakeServer()
        server.replies["GET /calls"] = .status(200, try Examples.changed("calls") { body in
            body["more"] = true
            if body["before"] is NSNull { body["before"] = "older" }
        })
        let store = CallStore(server: server.client())
        XCTAssertEqual(store.olderPages, .done, "nothing listed yet: no foot")
        await store.refreshList()
        XCTAssertEqual(store.olderPages, .more)
        server.replies["GET /calls"] = .status(503)
        await store.loadOlder()
        XCTAssertEqual(store.olderPages, .failed)
        server.replies["GET /calls"] = .slow(.milliseconds(200),
                                             "{\"calls\": [], \"more\": false, \"before\": null, \"version\": \"0000000000000001\"}")
        let again = Task { await store.loadOlder() }
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertEqual(store.olderPages, .loading)
        await again.value
        XCTAssertEqual(store.olderPages, .done)
    }
}
