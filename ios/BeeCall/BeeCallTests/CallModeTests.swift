import SwiftUI
import XCTest
@testable import BeeCall

/// Design D's call screen (frame 2): the speaking side's line large, the other side's last line small; a clock;
/// an orb that follows the live voice, yellow for the AI and blue for the person.
final class CallModeTests: XCTestCase {
    @MainActor private func captions(_ lines: [(mine: Bool, text: String)]) -> [Caption] {
        var list = CaptionList()
        for (index, line) in lines.enumerated() {
            list.show(line.text, line: "\(line.mine ? "me" : "ai")/\(index)", isMine: line.mine,
                      sentAt: Date(timeIntervalSince1970: 1_792_056_600 + Double(index)))
        }
        return list.captions
    }

    private let talk: [(mine: Bool, text: String)] = [
        (false, "안녕하세요, 민승 님. 오늘 밤엔 무슨 이야기 할까요?"),
        (true, "13인치랑 15인치 중에 업무용으로 고민 중이에요."),
        (false, "지금 제조사 한국 가격표를 보고 있어요. 주로 어떤 작업을 하세요?"),
    ]

    @MainActor func testTheAIsLineIsLargeWhileItSpeaks() {
        let lines = CallLines(captions: captions(talk), speaking: .ai)
        XCTAssertEqual(lines.current?.text, "지금 제조사 한국 가격표를 보고 있어요. 주로 어떤 작업을 하세요?")
        XCTAssertEqual(lines.other?.text, "13인치랑 15인치 중에 업무용으로 고민 중이에요.")
        XCTAssertEqual(lines.currentSide, .ai)
    }

    /// While the person speaks, their newest words are large, and the AI's last line is the small one.
    @MainActor func testThePersonsLineIsLargeWhileTheySpeak() {
        let lines = CallLines(captions: captions(talk + [(true, "주로 문서 작업이고")]), speaking: .person)
        XCTAssertEqual(lines.current?.text, "주로 문서 작업이고")
        XCTAssertEqual(lines.other?.text, "지금 제조사 한국 가격표를 보고 있어요. 주로 어떤 작업을 하세요?")
        XCTAssertEqual(lines.currentSide, .person)
    }

    /// In a pause, the last line said stays large.
    @MainActor func testInAPauseTheLastLineStaysLarge() {
        let lines = CallLines(captions: captions(talk), speaking: nil)
        XCTAssertEqual(lines.currentSide, .ai)
        XCTAssertEqual(lines.current?.text, "지금 제조사 한국 가격표를 보고 있어요. 주로 어떤 작업을 하세요?")
    }

    /// The person may start speaking before their words arrive: their last line is large, if they have one.
    @MainActor func testWithNothingYetNothingIsShown() {
        let lines = CallLines(captions: [], speaking: .person)
        XCTAssertNil(lines.current)
        XCTAssertNil(lines.other)
    }

    @MainActor func testTheClockReadsLikeThePhoneApp() {
        let start = Date(timeIntervalSince1970: 1_792_056_600)
        XCTAssertEqual(CallClock.text(since: start, now: start.addingTimeInterval(218)), "3:38")
        XCTAssertEqual(CallClock.text(since: start, now: start.addingTimeInterval(5)), "0:05")
        XCTAssertEqual(CallClock.text(since: start, now: start.addingTimeInterval(725)), "12:05")
        XCTAssertEqual(CallClock.text(since: start, now: start.addingTimeInterval(3723)), "1:02:03")
        XCTAssertEqual(CallClock.text(since: nil, now: start), "")
    }

    /// The orb: yellow for the AI, blue for the person; it swells with the voice, and in silence it rests in the
    /// colour of whoever spoke last. With Reduce Motion it does not move.
    @MainActor func testTheOrbFollowsTheVoice() {
        let ai = OrbLook(VoiceIndicator(side: .ai, amount: 1), lastSide: .ai, reduceMotion: false)
        XCTAssertEqual(ai.side, .ai)
        XCTAssertEqual(ai.scale, 1.12, accuracy: 0.001)
        let person = OrbLook(VoiceIndicator(side: .person, amount: 0.5), lastSide: .ai, reduceMotion: false)
        XCTAssertEqual(person.side, .person)
        XCTAssertEqual(person.scale, 1.06, accuracy: 0.001)
        let silence = OrbLook(.rest, lastSide: .person, reduceMotion: false)
        XCTAssertEqual(silence.side, .person)
        XCTAssertEqual(silence.scale, 1, accuracy: 0.001)
        XCTAssertLessThan(silence.glow, person.glow)
        let still = OrbLook(VoiceIndicator(side: .ai, amount: 1), lastSide: .ai, reduceMotion: true)
        XCTAssertEqual(still.scale, 1, accuracy: 0.001)
        XCTAssertFalse(still.animates)
        XCTAssertEqual(OrbLook(.rest, lastSide: nil, reduceMotion: false).side, .ai) // before anyone spoke: the AI
    }

    /// Review of 2026-10-15, finding 8: the call screen says what the call is doing until the AI is there.
    @MainActor func testTheCallScreenSaysWhatTheCallIsDoing() {
        let ko = CallWords(korean: true), en = CallWords(korean: false)
        XCTAssertEqual(ko.state(reconnecting: false, aiIsThere: false, answered: false), "전화 거는 중…")
        XCTAssertEqual(ko.state(reconnecting: false, aiIsThere: false, answered: true), "연결하는 중…")
        XCTAssertNil(ko.state(reconnecting: false, aiIsThere: true, answered: false), "the clock runs")
        XCTAssertEqual(ko.state(reconnecting: true, aiIsThere: true, answered: false), "다시 연결하는 중…")
        XCTAssertEqual(en.state(reconnecting: true, aiIsThere: true, answered: false), "Reconnecting…")
        XCTAssertEqual(en.state(reconnecting: false, aiIsThere: false, answered: false), "Calling…")
    }
}
