import XCTest
@testable import BeeCall

/// The ring a VoIP push carries (voice/dispatch.py place_call writes the same line to the ring stream).
final class RingStreamTests: XCTestCase {
    @MainActor func testAPushWithAnEpochRingsUntilThatMoment() throws {
        let ring = try XCTUnwrap(RingStream.ring(fromPush: [
            "room": "call-matt-20261015-210000", "topic": "  Saturday hike  ",
            "expires_at": "2026-10-15T21:01:00", "expires_epoch": 1_791_816_060,
        ]))
        XCTAssertEqual(ring.room, "call-matt-20261015-210000")
        XCTAssertEqual(ring.topic, "Saturday hike")
        XCTAssertEqual(ring.expiresAt, Date(timeIntervalSince1970: 1_791_816_060))
    }

    @MainActor func testAnEmptyTopicIsNoTopic() throws {
        let ring = try XCTUnwrap(RingStream.ring(fromPush: [
            "room": "call-matt-20261015-210000", "topic": " ", "expires_at": "2026-10-15T21:01:00",
            "expires_epoch": 1_791_816_060,
        ]))
        XCTAssertNil(ring.topic)
    }

    @MainActor func testAPushWithoutARoomIsNotARing() {
        XCTAssertNil(RingStream.ring(fromPush: ["topic": "x", "expires_at": "2026-10-15T21:01:00"]))
    }
}

/// How long the phone rings for the AI's call.
final class RingDeadlineTests: XCTestCase {
    /// The voice server gives up at expires_at. Answering first sends the settings (at most
    /// SettingsSync.answerWait), then gets a pass and joins (about 5 s at worst), so the ringing stops early
    /// enough for all three.
    @MainActor func testARingStopsInTimeForTheSettingsThePassAndTheJoin() {
        let ring = Ring(room: "call-matt-20261015-210000", topic: nil, expiresAt: Date(timeIntervalSince1970: 1_791_816_060))
        XCTAssertEqual(CallManager.answerBy(ring), ring.expiresAt.addingTimeInterval(-7))
    }
}
