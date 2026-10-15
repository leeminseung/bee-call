#if DEBUG
import XCTest
@testable import BeeCall

/// Debug builds only: synthetic voice levels that drive the call screen's indicator without a call.
final class VoiceDemoTests: XCTestCase {
    @MainActor func testTheDemoIsChosenWithALaunchArgument() {
        let store = freshDefaults(self)
        XCTAssertNil(VoiceDemo.requested(in: store))
        store.set("ai", forKey: "voiceDemo") // what `-voiceDemo ai` puts into UserDefaults
        XCTAssertEqual(VoiceDemo.requested(in: store), .ai)
        store.set("nonsense", forKey: "voiceDemo")
        XCTAssertNil(VoiceDemo.requested(in: store))
    }

    /// Speech-like levels, always loud enough to light the indicator.
    @MainActor func testEachDemoSpeaksOnItsOwnSide() {
        for second in stride(from: 0.0, to: 8, by: 0.05) {
            let person = VoiceDemo.person.levels(at: second), ai = VoiceDemo.ai.levels(at: second)
            XCTAssertGreaterThan(VoiceIndicator.amount(for: person.person), 0)
            XCTAssertEqual(person.ai, 0)
            XCTAssertEqual(ai.person, 0)
            XCTAssertGreaterThan(VoiceIndicator.amount(for: ai.ai), 0)
        }
    }

    @MainActor func testTheLevelsRiseAndFallLikeSyllables() {
        let levels = stride(from: 0.0, to: 1, by: 0.02).map { VoiceDemo.person.levels(at: $0).person }
        XCTAssertGreaterThan(levels.max()! - levels.min()!, 0.3)
    }

    @MainActor func testBothVoicesTakeTurns() {
        XCTAssertGreaterThan(VoiceDemo.both.levels(at: 1).person, 0)
        XCTAssertEqual(VoiceDemo.both.levels(at: 1).ai, 0)
        XCTAssertEqual(VoiceDemo.both.levels(at: 5).person, 0)
        XCTAssertGreaterThan(VoiceDemo.both.levels(at: 5).ai, 0)
    }
}
#endif
