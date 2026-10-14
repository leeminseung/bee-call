import XCTest
@testable import BeeCall

/// The call screen's live captions.
final class CaptionsTests: XCTestCase {
    private let start = Date(timeIntervalSince1970: 1_791_816_000)

    @MainActor private func at(_ seconds: Double) -> Date { start.addingTimeInterval(seconds) }

    @MainActor func testEachSideHasItsOwnBubble() {
        var list = CaptionList()
        list.show("Hi, it's Bee Call.", line: "agent/a1", isMine: false, sentAt: at(0))
        list.show("Hi!", line: "matt/u1", isMine: true, sentAt: at(2))
        XCTAssertEqual(list.captions.map(\.text), ["Hi, it's Bee Call.", "Hi!"])
        XCTAssertEqual(list.captions.map(\.isMine), [false, true])
    }

    /// A user line is re-sent in full each time the transcription improves.
    @MainActor func testABetterVersionOfALineReplacesIt() {
        var list = CaptionList()
        list.show("Dana says her", line: "matt/u1", isMine: true, sentAt: at(1))
        list.show("Dana says her knee is better", line: "matt/u1", isMine: true, sentAt: at(2))
        XCTAssertEqual(list.captions.map(\.text), ["Dana says her knee is better"])
    }

    @MainActor func testAnOlderVersionArrivingLateIsDropped() {
        var list = CaptionList()
        list.show("Dana says her knee is better", line: "matt/u1", isMine: true, sentAt: at(2))
        list.show("Dana says her", line: "matt/u1", isMine: true, sentAt: at(1))
        XCTAssertEqual(list.captions.map(\.text), ["Dana says her knee is better"])
    }

    @MainActor func testOnlyTheNewestThirtyBubblesStay() {
        var list = CaptionList()
        for n in 0..<31 {
            list.show("line \(n)", line: n.isMultiple(of: 2) ? "agent/\(n)" : "matt/\(n)", isMine: !n.isMultiple(of: 2),
                      sentAt: at(Double(n)))
        }
        XCTAssertEqual(list.captions.count, 30)
        XCTAssertEqual(list.captions.first?.text, "line 1")
    }
}
