import AVFoundation
import CallKit
import XCTest
@testable import BeeCall

/// A call from CallKit's start or answer to its end, with stand-ins for CallKit and the LiveKit room.
final class CallLifecycleTests: XCTestCase {
    @MainActor private func calls(_ callKit: FakeCallKit, _ room: FakeRoom, server: FakeServer = FakeServer(),
                                  microphone: Bool = true, speaker: @escaping (Bool) throws -> Void = { _ in },
                                  speakerRoute: @escaping () -> Bool = { false }) -> CallManager {
        CallManager(callKit: callKit, calls: callKit, microphone: { microphone }, room: room, beforeAnswering: {},
                    decline: CallDecline(server: server.client()), speaker: speaker, speakerRoute: speakerRoute,
                    pickUpWait: .milliseconds(300))
    }

    /// What iOS says when the call's sound moves: CallKit's own speaker button, AirPods put in, a route picked.
    @MainActor private func routeChanges() {
        NotificationCenter.default.post(name: AVAudioSession.routeChangeNotification, object: nil)
    }

    /// The person taps Call; CallKit hands the start back, as it does once the call is on its screen.
    @MainActor private func start(_ calls: CallManager, _ callKit: FakeCallKit) async throws -> UUID {
        await calls.startCall()
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

    /// Review finding 15: a hang-up while the call joins must not start the recording on this iPhone, which would
    /// then run on into the next call's file.
    @MainActor func testACallEndedWhileItJoinsIsNotRecorded() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        room.holdsJoin = true
        let id = try await start(calls, callKit)
        await waitUntil("the join has begun") { room.joins.count == 1 }
        calls.perform(CXEndCallAction(call: id))
        await waitUntil("the call has ended") { !calls.isInCall }
        room.releaseJoin()
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(room.recordingStarts, 0)
        XCTAssertEqual(calls.status, "Call ended")
    }

    /// Review finding 15: with "Save on this iPhone" on, a recording that could not start left no file and no
    /// word. The call screen now says so, until the next call.
    @MainActor func testARecordingThatCannotStartIsShownUntilTheNextCall() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        room.recordingProblem = "Recording on this iPhone did not start: there isn't enough space."
        let id = try await start(calls, callKit)
        await waitUntil("the room is joined") { calls.status == "Ringing…" }
        XCTAssertEqual(calls.phoneRecordingProblem, "Recording on this iPhone did not start: there isn't enough space.")
        room.aiAnswers()
        calls.perform(CXEndCallAction(call: id))
        await waitUntil("the call has ended") { !calls.isInCall }
        XCTAssertNotNil(calls.phoneRecordingProblem) // still there after the call: the file is missing

        room.recordingProblem = nil
        _ = try await start(calls, callKit)
        await waitUntil("the next room is joined") { room.joins.count == 2 && calls.status == "Ringing…" }
        XCTAssertNil(calls.phoneRecordingProblem)
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

    // MARK: - CallKit's own controls (the call screen, the lock screen, AirPods)

    /// BC-143, contract section 2: declining the AI's call where CallKit shows it tells the server which call.
    @MainActor func testDecliningTheAIsCallTellsTheServerAndSaysDeclined() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), server = FakeServer()
        let calls = calls(callKit, room, server: server)
        calls.ringPhone(for: ring())
        await waitUntil("the call rings") { callKit.incoming.count == 1 }
        calls.perform(CXEndCallAction(call: try XCTUnwrap(callKit.incoming.first?.call)))
        await waitUntil("the decline is sent") { server.sent == ["POST /calls/decline"] }
        XCTAssertEqual(try server.body(of: "POST /calls/decline") as NSDictionary,
                       ["room": "call-matt-20261015-210000"] as NSDictionary)
        await waitUntil("the call screen says so") { !calls.isInCall }
        XCTAssertEqual(calls.status, "Declined")
        XCTAssertTrue(room.joins.isEmpty)
    }

    @MainActor func testEndingACallOnCallKitsScreenLeavesTheRoom() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), server = FakeServer()
        let calls = calls(callKit, room, server: server)
        let id = try await start(calls, callKit)
        await waitUntil("the room is joined") { calls.status == "Ringing…" }
        room.aiAnswers()
        calls.perform(CXEndCallAction(call: id))
        await waitUntil("the call has ended") { !calls.isInCall }
        XCTAssertEqual(calls.status, "Call ended")
        XCTAssertEqual(room.leaves, 1)
        XCTAssertEqual(server.sent, []) // only a ringing AI call is declined
    }

    @MainActor func testMutingOnCallKitsScreenMutesTheMicrophone() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        let id = try await start(calls, callKit)
        await waitUntil("the room is joined") { calls.status == "Ringing…" }
        calls.perform(CXSetMutedCallAction(call: id, muted: true))
        calls.perform(CXSetMutedCallAction(call: id, muted: false))
        await waitUntil("both reach the room") { room.microphone.count == 2 }
        XCTAssertEqual(room.microphone, [false, true])
    }

    // MARK: - Design D's call screen: its own mute, speaker and clock

    /// The call screen's mute goes through CallKit, so CallKit's own screen shows the same.
    @MainActor func testMutingFromTheCallScreenGoesThroughCallKit() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        let id = try await start(calls, callKit)
        await waitUntil("the room is joined") { calls.status == "Ringing…" }
        room.aiAnswers()
        await calls.setMuted(true)
        let mute = try XCTUnwrap(callKit.requested.last as? CXSetMutedCallAction)
        XCTAssertEqual(mute.callUUID, id)
        XCTAssertTrue(mute.isMuted)
        XCTAssertFalse(calls.isMuted, "muted once CallKit performs it")
        calls.perform(mute) // what CallKit does with the request
        await waitUntil("the microphone is off") { room.microphone == [false] }
        XCTAssertTrue(calls.isMuted)
    }

    @MainActor func testTheSpeakerTurnsOnAndOffAndIsOffAfterTheCall() async throws {
        let callKit = FakeCallKit(), room = FakeRoom()
        var routes: [Bool] = []
        let calls = calls(callKit, room, speaker: { routes.append($0) })
        let id = try await start(calls, callKit)
        await waitUntil("the room is joined") { calls.status == "Ringing…" }
        calls.setSpeaker(true)
        XCTAssertTrue(calls.isSpeakerOn)
        calls.setSpeaker(false)
        XCTAssertFalse(calls.isSpeakerOn)
        calls.setSpeaker(true)
        calls.perform(CXEndCallAction(call: id))
        await waitUntil("the call has ended") { !calls.isInCall }
        XCTAssertFalse(calls.isSpeakerOn)
        XCTAssertEqual(routes, [true, false, true, false], "the loudspeaker is let go with the call")
    }

    /// Review of 2026-10-15, finding 2: the speaker button followed only the app's own taps. CallKit's screen has a
    /// speaker button too, and AirPods or a picked route move the sound without the app.
    @MainActor func testTheSpeakerButtonFollowsWhereTheSoundIs() async throws {
        var loud = false
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room, speakerRoute: { loud })
        _ = try await start(calls, callKit)
        await waitUntil("the room is joined") { calls.status == "Ringing…" }
        loud = true // CallKit's own speaker button
        routeChanges()
        await waitUntil("the call screen shows the speaker on") { calls.isSpeakerOn }
        calls.setSpeaker(true)
        loud = false // AirPods put in
        routeChanges()
        await waitUntil("the call screen shows the speaker off") { !calls.isSpeakerOn }
    }

    /// Turning the speaker off can fail too; then the sound is still on the loudspeaker, and the button says so.
    @MainActor func testASpeakerThatCannotBeTurnedOffStaysOn() async throws {
        struct Stuck: Error {}
        var loud = false
        let callKit = FakeCallKit(), room = FakeRoom()
        let calls = calls(callKit, room, speaker: { on in
            guard on else { throw Stuck() }
            loud = true
        }, speakerRoute: { loud })
        _ = try await start(calls, callKit)
        calls.setSpeaker(true)
        XCTAssertTrue(calls.isSpeakerOn)
        calls.setSpeaker(false)
        XCTAssertTrue(calls.isSpeakerOn)
    }

    /// A mute CallKit asked of a call that has ended since must not mark the next call muted.
    @MainActor func testALateMuteOfAnEndedCallLeavesTheNextCallAlone() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        let first = try await start(calls, callKit)
        room.holdsMicrophone = true
        calls.perform(CXSetMutedCallAction(call: first, muted: true))
        calls.perform(CXEndCallAction(call: first))
        await waitUntil("the first call has ended") { !calls.isInCall }
        _ = try await start(calls, callKit)
        room.releaseMicrophone()
        await waitUntil("the late mute reaches the room") { room.microphone == [false] }
        try await Task.sleep(for: .milliseconds(50))
        XCTAssertFalse(calls.isMuted)
    }

    @MainActor func testASpeakerThatCannotBeTurnedOnStaysOff() async throws {
        struct NoSpeaker: Error {}
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room, speaker: { _ in throw NoSpeaker() })
        _ = try await start(calls, callKit)
        calls.setSpeaker(true)
        XCTAssertFalse(calls.isSpeakerOn)
    }

    /// The call screen's clock runs from the moment the AI is there.
    @MainActor func testTheCallsClockStartsWhenTheAIIsThereAndStopsWithTheCall() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room)
        let id = try await start(calls, callKit)
        await waitUntil("the room is joined") { calls.status == "Ringing…" }
        XCTAssertNil(calls.connectedAt)
        room.aiAnswers()
        XCTAssertNotNil(calls.connectedAt)
        calls.perform(CXEndCallAction(call: id))
        await waitUntil("the call has ended") { !calls.isInCall }
        XCTAssertNil(calls.connectedAt)
    }

    // MARK: - Microphone access (BC-142)

    @MainActor func testWithoutMicrophoneAccessACallThePersonStartsSaysSoAndDoesNotStart() async {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room, microphone: false)
        await calls.startCall()
        XCTAssertEqual(calls.status, "Microphone access is off. Turn it on in Settings.")
        XCTAssertTrue(callKit.requested.isEmpty)
        XCTAssertFalse(calls.isInCall)
    }

    /// The ring stream's ring (app open) asks first; a call that could not be heard does not ring.
    @MainActor func testWithoutMicrophoneAccessTheRingStreamsCallDoesNotRing() async throws {
        let callKit = FakeCallKit(), room = FakeRoom(), calls = calls(callKit, room, microphone: false)
        calls.ringPhone(for: ring())
        await waitUntil("the call is given up") { !calls.isInCall }
        XCTAssertEqual(calls.status, "Microphone access is off. Turn it on in Settings.")
        XCTAssertTrue(callKit.incoming.isEmpty)
    }
}
