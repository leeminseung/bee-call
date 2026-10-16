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

    // MARK: - A line still arriving (dark-mode design: a blinking bar at its end, never faded words)

    /// The AI's line comes as one stream in chunks: it is arriving until its stream ends.
    @MainActor func testTheAIsLineArrivesUntilItsStreamEnds() {
        var list = CaptionList()
        list.show("Good,", line: "agent/a1", isMine: false, sentAt: at(0))
        XCTAssertEqual(list.captions.last?.arriving, true)
        list.show("Good, the trail is open.", line: "agent/a1", isMine: false, sentAt: at(0))
        list.streamEnded(line: "agent/a1", final: false) // the voice server marks only its closing header final
        XCTAssertEqual(list.captions.last?.arriving, false)
        XCTAssertEqual(list.lines.map(\.arriving), [false])
    }

    /// A user line is re-sent in full each time it improves: it is arriving until the version marked final.
    @MainActor func testAUserLineArrivesUntilItsFinalVersion() {
        var list = CaptionList()
        list.show("Dana says", line: "matt/u1", isMine: true, sentAt: at(1))
        list.streamEnded(line: "matt/u1", final: false)
        XCTAssertEqual(list.captions.last?.arriving, true, "a better version is still to come")
        list.show("Dana says her knee is better", line: "matt/u1", isMine: true, sentAt: at(2))
        list.streamEnded(line: "matt/u1", final: true)
        XCTAssertEqual(list.captions.last?.arriving, false)
    }

    /// A bubble of several lines is arriving while its last line is.
    @MainActor func testAJoinedBubbleArrivesWithItsLastLine() {
        var list = CaptionList()
        list.show("Yes, now is fine.", line: "matt/u1", isMine: true, sentAt: at(1))
        list.streamEnded(line: "matt/u1", final: true)
        list.show("Dana says", line: "matt/u2", isMine: true, sentAt: at(2))
        XCTAssertEqual(list.captions.last?.arriving, true)
        XCTAssertEqual(list.lines.map(\.arriving), [false, true])
    }

    // MARK: - Caption numbers (contract section 3)

    /// Each speaker's lines are numbered 1, 2, 3... in the order they first arrive in the room; a better version
    /// of a line keeps its number, so the person's growing words keep theirs.
    @MainActor func testEachSpeakersLinesAreNumberedInTheOrderTheyArrive() {
        var list = CaptionList()
        list.show("Hi.", line: "agent/a1", isMine: false, sentAt: at(0))
        list.show("Yes,", line: "matt/u1", isMine: true, sentAt: at(1))
        list.show("now is fine.", line: "matt/u2", isMine: true, sentAt: at(2))
        list.show("Yes, it is", line: "matt/u1", isMine: true, sentAt: at(3))
        list.show("Great.", line: "agent/a2", isMine: false, sentAt: at(4))
        XCTAssertEqual(list.lines.map { CaptionLine(speaker: $0.speaker, number: $0.number, text: $0.text) }, [
            CaptionLine(speaker: .ai, number: 1, text: "Hi."),
            CaptionLine(speaker: .person, number: 1, text: "Yes, it is"),
            CaptionLine(speaker: .person, number: 2, text: "now is fine."),
            CaptionLine(speaker: .ai, number: 2, text: "Great."),
        ])
    }

    /// Rule 6: the numbers line up with the server's counts only while one connection lasts and one voice server
    /// is in the room. A reconnect of the phone's room, the voice server leaving or a new one joining, and the
    /// voice server's own reconnect (its bee_call.captions attribute changing) end that for the rest of the call.
    @MainActor func testTheNumbersHoldUntilTheRoomOrTheVoiceServerBreaksThem() {
        var numbers = CaptionNumbering()
        numbers.voiceServerJoined(captions: "1")
        numbers.captionsMark("1")
        XCTAssertTrue(numbers.holds, "the voice server's first join and its first mark are the start")
        for breaking in [{ (n: inout CaptionNumbering) in n.roomReconnecting() },
                         { (n: inout CaptionNumbering) in n.voiceServerLeft() },
                         { (n: inout CaptionNumbering) in n.voiceServerJoined(captions: "1") },
                         { (n: inout CaptionNumbering) in n.captionsMark("2") }] {
            var each = numbers
            breaking(&each)
            XCTAssertFalse(each.holds)
            each.captionsMark("1")
            XCTAssertFalse(each.holds, "for the rest of the call")
        }
        // The voice server joins before it picks up and sets its first mark with "answered" (fix-server 9a0cd79,
        // voice/agent.py): the first mark is the start, not a reconnect (review of 2026-10-15, second round,
        // finding 1: captions went off at every pick-up).
        var unmarked = CaptionNumbering()
        unmarked.voiceServerJoined(captions: nil)
        unmarked.captionsMark("1")
        XCTAssertTrue(unmarked.holds, "the first mark, set at pick-up")
        unmarked.captionsMark("2")
        XCTAssertFalse(unmarked.holds, "a later change is its reconnect")
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
