import CallKit
import LiveKit

/// How a call that LiveKit ended is shown on the call screen, and why CallKit is told it ended.
struct CallEnding: Equatable {
    let reason: CXCallEndedReason
    let message: String

    /// LiveKit left the room without the app asking it to.
    static func afterDisconnect(_ error: LiveKitError?) -> CallEnding {
        CallEnding(reason: .remoteEnded, message: "The call dropped")
    }
}
