import XCTest
@testable import BeeCall

/// The line under design D's call button: whether Bee Call's calls reach the phone, or what became of the last call
/// when it went wrong, in the call language.
final class HomeStatusTests: XCTestCase {
    @MainActor private func line(_ korean: Bool, call: String = "Ready", ring: String = "Waiting for calls",
                                 voip: Bool = true) -> String {
        HomeStatus.line(korean: korean, callStatus: call, ringStatus: ring, voipRegistered: voip)
    }

    @MainActor func testReadyForCallsAlsoWithTheAppClosed() {
        XCTAssertEqual(line(true), "준비됨 · 앱을 닫아도 전화가 와요")
        XCTAssertEqual(line(false), "Ready · calls come even with the app closed")
        XCTAssertEqual(line(true, voip: false), "준비됨")
        XCTAssertEqual(line(true, call: "Call ended"), "준비됨 · 앱을 닫아도 전화가 와요", "a call that ended well")
        XCTAssertEqual(line(true, call: "Declined"), "준비됨 · 앱을 닫아도 전화가 와요")
    }

    @MainActor func testTheRingStreamComingBack() {
        XCTAssertEqual(line(true, ring: "Can't reach the token server for calls. Retrying…"), "Bee Call 서버에 연결하는 중")
        XCTAssertEqual(line(false, ring: "The ring stream closed. Retrying…"), "Connecting to Bee Call")
        XCTAssertEqual(line(true, ring: ""), "", "not listening: a build without a server address")
    }

    /// A call that went wrong says so until the next call, in the call language where the app knows the words.
    @MainActor func testWhatWentWrongWithTheLastCall() {
        XCTAssertEqual(line(true, call: "Missed a call from Bee Call"), "Bee Call의 전화를 받지 못했어요")
        XCTAssertEqual(line(true, call: "Missed a call from Bee Call: Do Not Disturb or a Focus is on"),
                       "방해 금지나 집중 모드 때문에 Bee Call의 전화를 받지 못했어요")
        XCTAssertEqual(line(true, call: "Bee Call didn't pick up"), "Bee Call이 전화를 받지 않았어요")
        XCTAssertEqual(line(true, call: "That call already ended"), "그 통화는 이미 끝났어요")
        XCTAssertEqual(line(true, call: "Microphone access is off. Turn it on in Settings."),
                       "마이크 접근이 꺼져 있어요. 설정에서 켜 주세요.")
        XCTAssertEqual(line(true, call: "Call failed: The network connection was lost."),
                       "통화가 끊겼어요: The network connection was lost.")
        XCTAssertEqual(line(false, call: "Bee Call didn't pick up"), "Bee Call didn't pick up")
    }
}
