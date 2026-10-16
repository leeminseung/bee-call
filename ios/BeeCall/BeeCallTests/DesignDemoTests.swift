import XCTest
@testable import BeeCall

/// Debug builds' design frames (`-designD <frame>`) show the contract's example content, so they take what they
/// point at from the example files, which the server's tests regenerate, never from values copied out of them.
final class DesignDemoTests: XCTestCase {
    /// The live frames and the record frames are in the live example's call.
    @MainActor func testTheLiveFramesAreInTheLiveExamplesCall() throws {
        XCTAssertEqual(DesignDemo.liveCall, try Examples.decode(CallView.self, "call-view-live").call.callID)
    }

    /// Frame 7's voice step plays the sample of the voice shown as the person's: me.json's, else the default.
    @MainActor func testTheVoiceFramePlaysThePersonsVoice() throws {
        let me = try Examples.decode(Me.self, "me").settings
        let voices = try [Examples.decode(VoiceList.self, "voices"), Examples.decode(VoiceList.self, "voices-en")]
            .first { $0.language == me.language }?.voices ?? []
        let shown = try XCTUnwrap(VoiceChoice.effective(stored: me.voice, in: voices))
        XCTAssertEqual(DesignDemo.sampleVoice(settings: me, in: voices), shown)
        XCTAssertEqual(DesignDemo.sampleVoice(settings: nil, in: voices), voices.first(where: \.isDefault))
    }

    /// The conversation frame is the contract's worked example: the person's line after the live view's counts, still
    /// arriving, is what the ended view's version of their last speech adds to the live one.
    @MainActor func testTheGrowingLineIsWhatTheEndedViewAdds() throws {
        let live = try Examples.decode(CallView.self, "call-view-live"), ended = try Examples.decode(CallView.self, "call-view")
        let growing = try XCTUnwrap(DesignDemo.growingLine(live: live, ended: ended))
        let last = try XCTUnwrap(live.items.lazy.compactMap { item -> Speech? in
            if case .speech(let speech) = item, speech.speaker == .person { speech } else { nil }
        }.last)
        let after = try XCTUnwrap(ended.items.lazy.compactMap { item -> Speech? in
            if case .speech(let speech) = item, speech.id == last.id { speech } else { nil }
        }.first)
        XCTAssertEqual(last.text + " " + growing.text, after.text)
        XCTAssertEqual(growing.speaker, .person)
        XCTAssertEqual(growing.number, live.captionsIncluded.person + 1)
        XCTAssertTrue(growing.arriving)
    }
}
