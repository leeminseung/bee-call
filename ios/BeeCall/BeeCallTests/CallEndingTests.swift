import CallKit
import LiveKit
import XCTest
@testable import BeeCall

/// What the call screen says, and what CallKit is told, when LiveKit ends a call (contract section 7).
final class CallEndingTests: XCTestCase {
    @MainActor func testTheRoomDeleted() {
        XCTAssertEqual(CallEnding.afterDisconnect(LiveKitError(.roomDeleted)),
                       CallEnding(reason: .remoteEnded, message: "The call dropped"))
    }

    @MainActor func testANetworkLoss() {
        XCTAssertEqual(CallEnding.afterDisconnect(LiveKitError(.network)),
                       CallEnding(reason: .remoteEnded, message: "The call dropped"))
    }
}
