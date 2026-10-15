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
}
