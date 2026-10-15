import XCTest
@testable import BeeCall

/// Second review of the D wiring, finding 7: the problems the home and the call screen show read in the call
/// language; a reason that came from elsewhere keeps its own words.
final class MessageWordsTests: XCTestCase {
    @MainActor func testTheHomesMessagesReadInKorean() {
        XCTAssertEqual(MessageWords.words(BeeStep.notConnected, korean: true),
                       "Bee가 연결되지 않았어요. 다시 하려면 ‘Bee 사용’을 끄고 Bee Call로 돌아왔다가 다시 켜 주세요.")
        XCTAssertEqual(MessageWords.words("Couldn't reach Bee Call. Try again from here.", korean: true),
                       "Bee Call에 연결하지 못했어요. 여기서 다시 해 주세요.")
        XCTAssertEqual(MessageWords.words("Bee Call couldn't take that answer (error 502). Try again from here.", korean: true),
                       "Bee Call이 그 답을 받지 못했어요(오류 502). 여기서 다시 해 주세요.")
        XCTAssertEqual(MessageWords.words("The evening call time “9pm” was not saved: write it like 21:00 in Settings.",
                                          korean: true),
                       "저녁 전화 시간 “9pm”을 저장하지 못했어요. 설정에서 21:00처럼 써 주세요.")
        XCTAssertEqual(MessageWords.words("Recording on this iPhone did not start: The disk is full.", korean: true),
                       "이 iPhone에서 녹음을 시작하지 못했어요: The disk is full.")
        XCTAssertEqual(MessageWords.words("Bee Call's server cannot be reached.", korean: true), "Bee Call 서버에 연결할 수 없어요.")
    }

    @MainActor func testEnglishAndUnknownMessagesStayAsTheyAre() {
        XCTAssertEqual(MessageWords.words("Bee Call's server cannot be reached.", korean: false),
                       "Bee Call's server cannot be reached.")
        XCTAssertEqual(MessageWords.words("voice is not one of en's voices", korean: true), "voice is not one of en's voices")
    }
}
