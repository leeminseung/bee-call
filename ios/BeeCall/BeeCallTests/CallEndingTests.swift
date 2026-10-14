import CallKit
import LiveKit
import XCTest
@testable import BeeCall

/// What the call screen says, and what CallKit is told, when LiveKit ends a call or loses its connection
/// (contract section 7, review finding 2). The AI ends a call by deleting the room.
final class CallEndingTests: XCTestCase {
    private let ended = CallEnding(reason: .remoteEnded, message: "Call ended")
    private let dropped = CallEnding(reason: .failed, message: "The call dropped")

    @MainActor func testTheAIDeletingTheRoomIsANormalEnd() {
        XCTAssertEqual(CallEnding.afterDisconnect(LiveKitError(.roomDeleted)), ended)
    }

    @MainActor func testBeingRemovedFromTheRoomIsANormalEnd() {
        XCTAssertEqual(CallEnding.afterDisconnect(LiveKitError(.participantRemoved)), ended)
    }

    @MainActor func testANetworkLossIsADroppedCall() {
        XCTAssertEqual(CallEnding.afterDisconnect(LiveKitError(.network)), dropped)
        XCTAssertEqual(CallEnding.afterDisconnect(LiveKitError(.timedOut)), dropped)
    }

    /// Anything else LiveKit can report is not the AI hanging up, so it reads as a dropped call too.
    @MainActor func testOtherReasonsAreADroppedCall() {
        XCTAssertEqual(CallEnding.afterDisconnect(nil), dropped)
        XCTAssertEqual(CallEnding.afterDisconnect(LiveKitError(.serverShutdown)), dropped)
        XCTAssertEqual(CallEnding.afterDisconnect(LiveKitError(.unknown)), dropped)
    }

    /// LiveKit announces every reconnect, quick or full, with didStartReconnectWithMode and
    /// didCompleteReconnectWithMode; roomIsReconnecting comes only for a full one, the last of its attempts
    /// (RoomDelegate in SDK 2.17.0).
    @MainActor func testTheClientHearsQuickReconnectsToo() {
        let client = LiveKitClient()
        XCTAssertTrue(client.responds(to: #selector(RoomDelegate.room(_:didStartReconnectWithMode:))))
        XCTAssertTrue(client.responds(to: #selector(RoomDelegate.room(_:didCompleteReconnectWithMode:))))
    }

    @MainActor func testTheStatusSaysReconnectingWhileLiveKitReconnects() {
        let calls = CallManager()
        XCTAssertEqual(calls.statusLine, "Ready")
        calls.liveKit.onLinkChange?(.reconnecting)
        XCTAssertEqual(calls.statusLine, "Reconnecting…")
        calls.liveKit.onLinkChange?(.reconnected)
        XCTAssertEqual(calls.statusLine, "Ready")
    }

    @MainActor func testALostConnectionStopsSayingReconnecting() {
        let calls = CallManager()
        calls.liveKit.onLinkChange?(.reconnecting)
        calls.liveKit.onLinkChange?(.lost(LiveKitError(.network)))
        XCTAssertFalse(calls.isReconnecting)
    }
}
