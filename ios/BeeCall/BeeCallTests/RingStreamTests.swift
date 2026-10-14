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
