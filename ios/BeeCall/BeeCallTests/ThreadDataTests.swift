import CallKit
import XCTest
@testable import BeeCall

/// The home thread's content in the app: the call list, the offers and the person through the stores, and during
/// the person's own call, that call by its room (contract sections 1 to 3).
final class ThreadDataTests: XCTestCase {
    private let room = "call-matt-20261015-183000" // the room FakeRoom joins

    /// An example file, with the live example's call moved to `room` when one is given.
    @MainActor private func text(_ name: String, room: String? = nil) throws -> String {
        let text = try Examples.text(name), live = try Examples.decode(CallView.self, "call-view-live").call.callID
        return room.map { text.replacingOccurrences(of: live, with: $0) } ?? text
    }

    /// A call the person starts, joined: the thread's own call is its room.
    @MainActor private func data(_ server: FakeServer) async throws -> ThreadData {
        let callKit = FakeCallKit(), calls = CallManager(callKit: callKit, calls: callKit, microphone: { true },
                                                         room: FakeRoom(), beforeAnswering: {},
                                                         decline: CallDecline(server: server.client()))
        let data = ThreadData(calls: calls, store: CallStore(server: server.client()),
                              offers: OfferCenter(server: server.client()), person: PersonStore(server: server.client()))
        await calls.startCall()
        calls.perform(try XCTUnwrap(callKit.requested.last as? CXStartCallAction))
        await waitUntil("the room is joined") { calls.ownRoom != nil }
        return data
    }

    /// Before the server has a record (404), the call that is on is a section of its own, last.
    @MainActor func testTheCallThatIsOnIsLastBeforeItsRecordExists() async throws {
        let server = FakeServer()
        server.replies["GET /calls"] = .status(200, try text("calls"))
        server.replies["GET /calls/\(room)"] = .status(404)
        let data = try await data(server)
        await data.store.refreshList()
        await data.show(room)
        XCTAssertEqual(data.ownCall, room)
        XCTAssertEqual(data.sections.last, .ownCall(room: room))
        XCTAssertTrue(data.isMissing(room))
    }

    /// Once recorded but not yet in the list, the call is drawn from its view's summary; once listed, from the list.
    @MainActor func testTheCallThatIsOnComesFromItsViewThenFromTheList() async throws {
        let server = FakeServer()
        server.replies["GET /calls"] = .status(200, try text("calls"))
        server.replies["GET /calls/\(room)"] = .status(200, try text("call-view-live", room: room))
        let data = try await data(server)
        await data.store.refreshList()
        await data.show(room)
        guard case .call(let entry) = data.sections.last else { return XCTFail("the call that is on, last") }
        XCTAssertEqual(entry.call.callID, room)
        XCTAssertEqual(entry.call.state, .live)
        let running = try Examples.decode(CallView.self, "call-view-live").items.last {
            if case .job(let job) = $0 { job.state == .running } else { false }
        }
        XCTAssertEqual(data.ownEntries.last?.id, try XCTUnwrap(running).id, "its running job docked last")

        server.replies["GET /calls"] = .status(200, try text("calls", room: room))
        let version = try Examples.decode(CallList.self, "calls").version
        await data.store.changed(Change(path: "/calls", version: Examples.otherVersion(than: version)))
        XCTAssertEqual(data.sections.filter { $0.id == "call/\(room)" }.count, 1, "listed once, where the list has it")
        XCTAssertEqual(data.ownCall, room)
    }

    /// Second review of the D wiring, finding 2: the content is kept fresh by ThreadData itself, switched on and off
    /// by the app (on screen, or in a call), not by a view the call screen covers.
    @MainActor func testTheContentIsKeptFreshWhileSwitchedOn() async throws {
        let server = FakeServer()
        server.replies["GET /calls"] = .status(200, try text("calls"))
        server.replies["GET /offers"] = .status(200, try text("offers"))
        server.replies["GET /me"] = .status(200, try text("me"))
        let data = try await data(server)
        let before = server.requests.count
        data.keepFresh(true)
        await waitUntil("the list, the offers and the person are fetched") { server.requests.count == before + 3 }
        data.keepFresh(true)
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(server.requests.count, before + 3, "already on")
        data.keepFresh(false)
        data.keepFresh(true)
        await waitUntil("fetched again on coming back") { server.requests.count == before + 6 }
    }
}
