import XCTest
@testable import BeeCall

/// The ring a VoIP push carries (voice/dispatch.py place_call writes the same line to the ring stream). D reads
/// `room`, `topic` and `expires_at` only (contract section 7): a time to the second with the person's offset.
final class RingStreamTests: XCTestCase {
    @MainActor func testAPushRingsUntilItsExpiresAt() throws {
        let ring = try XCTUnwrap(RingStream.ring(fromPush: [
            "room": "call-matt-20261015-210000", "topic": "  Saturday hike  ",
            "expires_at": "2026-10-15T21:01:00+09:00", "expires_epoch": 1, // the pre-D field, no longer read
        ]))
        XCTAssertEqual(ring.room, "call-matt-20261015-210000")
        XCTAssertEqual(ring.topic, "Saturday hike")
        XCTAssertEqual(ring.expiresAt, Date(timeIntervalSince1970: 1_792_065_660))
    }

    @MainActor func testAnEmptyTopicIsNoTopic() throws {
        let ring = try XCTUnwrap(RingStream.ring(fromPush: [
            "room": "call-matt-20261015-210000", "topic": " ", "expires_at": "2026-10-15T21:01:00+09:00",
        ]))
        XCTAssertNil(ring.topic)
    }

    /// The contract's own ring example rings for its room and topic until its expires_at.
    @MainActor func testTheContractsRingExampleRings() throws {
        let payload = try Examples.object("ring")
        let ring = try XCTUnwrap(RingStream.ring(fromPush: payload))
        XCTAssertEqual(ring.room, payload["room"] as? String)
        XCTAssertEqual(ring.topic, (payload["topic"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines))
        XCTAssertEqual(ring.expiresAt, ZonedTime(try XCTUnwrap(payload["expires_at"] as? String))?.date)
    }

    /// A time with no offset says nothing certain about when the ring ends, and an epoch alone is the pre-D form:
    /// neither rings.
    @MainActor func testATimeWithoutItsOffsetOrAnEpochAloneIsNotARing() {
        XCTAssertNil(RingStream.ring(fromPush: ["room": "call-matt-20261015-210000", "expires_at": "2026-10-15T21:01:00"]))
        XCTAssertNil(RingStream.ring(fromPush: ["room": "call-matt-20261015-210000", "expires_at": "2026-10-15T21:01:00",
                                                "expires_epoch": 1_791_816_060]))
    }

    @MainActor func testAPushWithoutARoomIsNotARing() {
        XCTAssertNil(RingStream.ring(fromPush: ["topic": "x", "expires_at": "2026-10-15T21:01:00+09:00"]))
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
