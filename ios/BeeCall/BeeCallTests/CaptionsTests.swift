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

    /// Speech recognition cuts continuous speech into several lines; before the AI's next line they are one
    /// bubble (the owner's report of 2026-10-15).
    @MainActor func testConsecutiveUserLinesAreOneBubble() {
        var list = CaptionList()
        list.show("Is now still good?", line: "agent/a1", isMine: false, sentAt: at(0))
        list.show("Yes, now is fine.", line: "matt/u1", isMine: true, sentAt: at(2))
        list.show("Dana says her knee is better,", line: "matt/u2", isMine: true, sentAt: at(3))
        list.show("but she still wants an easy route.", line: "matt/u3", isMine: true, sentAt: at(4))
        XCTAssertEqual(list.captions.map(\.text), [
            "Is now still good?",
            "Yes, now is fine. Dana says her knee is better, but she still wants an easy route.",
        ])
    }

    @MainActor func testTheAIsNextLineStartsANewUserBubbleAfterIt() {
        var list = CaptionList()
        list.show("Yes.", line: "matt/u1", isMine: true, sentAt: at(1))
        list.show("Great.", line: "agent/a1", isMine: false, sentAt: at(2))
        list.show("And one more thing.", line: "matt/u2", isMine: true, sentAt: at(3))
        XCTAssertEqual(list.captions.map(\.text), ["Yes.", "Great.", "And one more thing."])
    }

    /// A better version of an earlier line in a joined bubble still replaces that line, in place.
    @MainActor func testALineInAJoinedBubbleImprovesInPlace() {
        var list = CaptionList()
        list.show("Dana says", line: "matt/u1", isMine: true, sentAt: at(1))
        list.show("but she wants", line: "matt/u2", isMine: true, sentAt: at(2))
        list.show("Dana says her knee is better,", line: "matt/u1", isMine: true, sentAt: at(3))
        XCTAssertEqual(list.captions.map(\.text), ["Dana says her knee is better, but she wants"])
    }

    /// The AI's own lines stay one bubble each.
    @MainActor func testTheAIsLinesAreNotJoined() {
        var list = CaptionList()
        list.show("Done.", line: "agent/a1", isMine: false, sentAt: at(1))
        list.show("Your trail check is back.", line: "agent/a2", isMine: false, sentAt: at(2))
        XCTAssertEqual(list.captions.count, 2)
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
