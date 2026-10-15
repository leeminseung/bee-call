import CallKit
import XCTest
@testable import BeeCall

/// A call from CallKit's start or answer to its end, with stand-ins for CallKit and the LiveKit room.
final class CallLifecycleTests: XCTestCase {
    @MainActor private func calls(_ callKit: FakeCallKit, _ room: FakeRoom) -> CallManager {
        CallManager(callKit: callKit, calls: callKit, microphone: { true }, room: room, beforeAnswering: {},
                    pickUpWait: .milliseconds(300))
    }

    /// The person taps Call; CallKit hands the start back, as it does once the call is on its screen.
    @MainActor private func start(_ calls: CallManager, _ callKit: FakeCallKit) async throws -> UUID {
        await calls.startCall(tokenServerURL: Server.tokenURL)
        let start = try XCTUnwrap(callKit.requested.last as? CXStartCallAction)
        calls.perform(start)
        return start.callUUID
    }

    // MARK: - A call the person starts

    @MainActor func testACallThePersonStartsIsConnectedWhenTheAIPicksUp() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        let id = try await start(calls, callKit)
        await waitUntil("the room is joined") { calls.status == "Ringing…" }
        XCTAssertEqual(callKit.startedConnecting, [id])
        room.aiAnswers()
        XCTAssertEqual(callKit.connected, [id])
        XCTAssertEqual(calls.status, "Connected")
        try await Task.sleep(for: .milliseconds(500)) // past the pick-up wait: the call goes on
        XCTAssertTrue(callKit.ended.isEmpty)
        XCTAssertTrue(calls.isInCall)
        XCTAssertEqual(room.recordingStarts, 1) // "Save on this iPhone", once the call has joined
    }

    @MainActor func testACallTheAINeverPicksUpFails() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        let id = try await start(calls, callKit)
        await waitUntil("the call is given up") { callKit.ended[id] != nil }
        XCTAssertEqual(callKit.ended[id], .failed)
        await waitUntil("the call screen says so") { !calls.isInCall }
        XCTAssertEqual(calls.status, "Bee Call didn't pick up")
        XCTAssertEqual(room.leaves, 1)
    }

    // MARK: - The AI's call, answered

    /// The AI's call rings, and the person answers it on CallKit's screen.
    @MainActor private func answer(_ calls: CallManager, _ callKit: FakeCallKit) async throws -> UUID {
        calls.ringPhone(for: ring())
        await waitUntil("the call rings") { callKit.incoming.count == 1 }
        let id = try XCTUnwrap(callKit.incoming.first?.call)
        calls.perform(CXAnswerCallAction(call: id))
        return id
    }

    @MainActor func testAnAnsweredCallIsConnectedWithTheAIThere() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        room.aiAnsweredBeforeJoin = true
        _ = try await answer(calls, callKit)
        await waitUntil("the room is joined") { room.joins.count == 1 }
        XCTAssertEqual(room.joins, ["call-matt-20261015-210000"])
        XCTAssertEqual(calls.status, "Connected")
        try await Task.sleep(for: .milliseconds(500)) // past the pick-up wait: the call goes on
        XCTAssertTrue(callKit.ended.isEmpty)
        XCTAssertTrue(callKit.connected.isEmpty) // CallKit counts an answered call as connected by itself
    }

    @MainActor func testAnAnsweredCallSaysConnectedOnceTheAIComes() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        _ = try await answer(calls, callKit)
        await waitUntil("the room is joined") { room.joins.count == 1 }
        XCTAssertEqual(calls.status, "Answering…")
        room.aiAnswers()
        XCTAssertEqual(calls.status, "Connected")
        try await Task.sleep(for: .milliseconds(500))
        XCTAssertTrue(callKit.ended.isEmpty)
    }

    /// Review finding 14: a join that lands after the AI gave up opens an empty room, which showed "Connected" in
    /// silence until the person hung up.
    @MainActor func testAnAnsweredCallWhoseAINeverComesEndsAsAlreadyEnded() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        let id = try await answer(calls, callKit)
        await waitUntil("the call is given up") { callKit.ended[id] != nil }
        XCTAssertEqual(callKit.ended[id], .remoteEnded)
        await waitUntil("the call screen says so") { !calls.isInCall }
        XCTAssertEqual(calls.status, "That call already ended")
        XCTAssertEqual(room.leaves, 1)
    }
}
