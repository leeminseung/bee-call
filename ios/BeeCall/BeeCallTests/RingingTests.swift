import CallKit
import XCTest
@testable import BeeCall

/// The AI's call rings through CallKit, from the ring stream (app open) or a VoIP push (app closed or locked).
/// iOS ends an app that does not report one call to CallKit for every VoIP push, so every push is reported at
/// once, exactly once, and PushKit's completion is handed back exactly once.
final class RingingTests: XCTestCase {
    /// Stand-ins for everything outside the app: CallKit, the room, and the server a decline goes to.
    @MainActor private func calls(_ callKit: FakeCallKit) -> CallManager {
        CallManager(callKit: callKit, calls: callKit, microphone: { true }, room: FakeRoom(), beforeAnswering: {},
                    decline: CallDecline(server: FakeServer().client()))
    }

    // MARK: - VoIP push

    @MainActor func testAPushedRingRingsOnceAndHandsBackItsCompletionOnce() async {
        let callKit = FakeCallKit(), calls = calls(callKit)
        var completions = 0
        calls.reportPushedCall(ring()) { completions += 1 }
        await waitUntil("PushKit's completion is handed back") { completions == 1 }
        XCTAssertEqual(callKit.incoming.map(\.name), ["Bee Call · Saturday hike"])
        XCTAssertEqual(callKit.incoming.map(\.handle), ["Bee Call"])
        XCTAssertTrue(calls.isRinging)
        XCTAssertEqual(calls.status, "Bee Call is calling about: Saturday hike")
        XCTAssertTrue(callKit.ended.isEmpty)
    }

    /// Reported and ended at once: expired, unreadable, or during another call.
    @MainActor func testAPushThatCannotRingIsStillReportedOnceAndEnded() async {
        for push in [ring(expiringIn: 3), nil] {
            let callKit = FakeCallKit(), calls = calls(callKit)
            var completions = 0
            calls.reportPushedCall(push) { completions += 1 }
            await waitUntil("the stand-in call ends") { callKit.ended.count == 1 }
            XCTAssertEqual(callKit.incoming.count, 1)
            XCTAssertEqual(callKit.ended.values.first, .unanswered)
            XCTAssertEqual(completions, 1)
            XCTAssertFalse(calls.isRinging)
        }
    }

    @MainActor func testAPushDuringAnotherCallIsReportedAndEndedAndTheFirstCallRingsOn() async {
        let callKit = FakeCallKit(), calls = calls(callKit)
        calls.reportPushedCall(ring("call-matt-20261015-210000")) {}
        calls.reportPushedCall(ring("call-matt-20261015-210500")) {}
        await waitUntil("the second call is ended") { callKit.ended.count == 1 }
        XCTAssertEqual(callKit.incoming.count, 2)
        XCTAssertEqual(callKit.ended.keys.first, callKit.incoming.last?.call)
        XCTAssertTrue(calls.isRinging)
    }

    /// The ring stream rang the call first; the push for it reports that same call, so the screen shows one.
    @MainActor func testThePushForACallTheRingStreamRangReportsThatSameCall() async {
        let callKit = FakeCallKit(), calls = calls(callKit)
        calls.ringPhone(for: ring())
        await waitUntil("the ring stream's call is reported") { callKit.incoming.count == 1 }
        var completions = 0
        calls.reportPushedCall(ring()) { completions += 1 }
        await waitUntil("PushKit's completion is handed back") { completions == 1 }
        XCTAssertEqual(callKit.incoming.count, 2)
        XCTAssertEqual(Set(callKit.incoming.map(\.call)).count, 1)
        XCTAssertTrue(callKit.ended.isEmpty)
    }

    /// BC-136: a late push for a call that already rang and is over is still reported to CallKit, and ended at once.
    @MainActor func testALatePushForACallThatIsOverIsReportedAndEnded() async throws {
        let callKit = FakeCallKit(), calls = calls(callKit)
        calls.ringPhone(for: ring())
        await waitUntil("the ring stream's call rings") { callKit.incoming.count == 1 }
        calls.perform(CXEndCallAction(call: try XCTUnwrap(callKit.incoming.first?.call))) // declined
        await waitUntil("the call is over") { !calls.isInCall }
        var completions = 0
        calls.reportPushedCall(ring()) { completions += 1 }
        await waitUntil("the late push's call is ended") { callKit.ended.count == 1 }
        XCTAssertEqual(callKit.incoming.count, 2)
        XCTAssertEqual(callKit.ended[callKit.incoming[1].call], .unanswered)
        XCTAssertEqual(completions, 1)
        XCTAssertFalse(calls.isRinging)
    }

    /// BC-137: the ringing ends with the call: a ring nobody answers stops in time for the AI's deadline.
    @MainActor func testARingNobodyAnswersStopsAtItsDeadline() async throws {
        let callKit = FakeCallKit(), calls = calls(callKit)
        let soon = ring(expiringIn: 7.4) // answerBy is 7 s before expires_at: 0.4 s from now
        calls.ringPhone(for: soon)
        await waitUntil("the call rings") { callKit.incoming.count == 1 }
        let id = try XCTUnwrap(callKit.incoming.first?.call)
        await waitUntil("the ringing stops", timeout: .seconds(3)) { callKit.ended[id] != nil }
        XCTAssertEqual(callKit.ended[id], .unanswered)
        await waitUntil("the call screen says so") { !calls.isInCall }
        XCTAssertEqual(calls.status, "Missed a call from Bee Call")
    }

    // MARK: - Ring stream

    @MainActor func testTheRingStreamRingsARoomOnce() async throws {
        let callKit = FakeCallKit(), calls = calls(callKit)
        calls.ringPhone(for: ring())
        calls.ringPhone(for: ring()) // the stream replays the last minute
        await waitUntil("the call is reported") { callKit.incoming.count == 1 }
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertEqual(callKit.incoming.count, 1)
        XCTAssertEqual(calls.status, "Bee Call is calling about: Saturday hike")
    }

    @MainActor func testAnExpiredRingFromTheStreamDoesNotRing() async throws {
        let callKit = FakeCallKit(), calls = calls(callKit)
        calls.ringPhone(for: ring(expiringIn: 3))
        try await Task.sleep(for: .milliseconds(200))
        XCTAssertTrue(callKit.incoming.isEmpty)
        XCTAssertFalse(calls.isRinging)
    }

    @MainActor func testAFocusThatFiltersTheCallSaysSo() async {
        let callKit = FakeCallKit(), calls = calls(callKit)
        callKit.incomingError = CXErrorCodeIncomingCallError(.filteredByDoNotDisturb)
        calls.reportPushedCall(ring()) {}
        await waitUntil("the call is given up") { !calls.isRinging }
        XCTAssertEqual(calls.status, "Missed a call from Bee Call: Do Not Disturb or a Focus is on")
    }

    // MARK: - Calls the person starts

    /// The name shows on CallKit's call screen and in the Phone app's Recents, where a tap calls back.
    @MainActor func testACallThePersonStartsIsNamedBeeCall() async throws {
        let callKit = FakeCallKit(), calls = calls(callKit)
        await calls.startCall()
        let start = try XCTUnwrap(callKit.requested.first as? CXStartCallAction)
        XCTAssertEqual(start.handle.value, "Bee Call")
        XCTAssertEqual(start.handle.type, .generic)
    }
}
