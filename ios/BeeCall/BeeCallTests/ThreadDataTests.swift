import CallKit
import XCTest
@testable import BeeCall

/// The home thread's content in the app: the call list, the offers and the person through the stores, and during
/// the person's own call, that call by its room (contract sections 1 to 3).
final class ThreadDataTests: XCTestCase {
    private let room = "call-matt-20261015-183000" // the room FakeRoom joins

    @MainActor private func text(_ name: String, room: String? = nil) throws -> String {
        let text = String(decoding: try ContractTests.example(name), as: UTF8.self)
        return room.map { text.replacingOccurrences(of: "call-minseung-20261015-012301", with: $0) } ?? text
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
        XCTAssertEqual(data.ownEntries.last?.id, "job-01", "its running job docked last")

        server.replies["GET /calls"] = .status(200, try text("calls", room: room))
        await data.store.changed(Change(path: "/calls", version: "0123456789abcdef"))
        XCTAssertEqual(data.sections.filter { $0.id == "call/\(room)" }.count, 1, "listed once, where the list has it")
        XCTAssertEqual(data.ownCall, room)
    }
}
