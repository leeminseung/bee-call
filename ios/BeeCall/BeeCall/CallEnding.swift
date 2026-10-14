import CallKit
import LiveKit

/// What LiveKit says about a room's connection while the app is in it.
enum CallLink {
    /// The connection dropped and LiveKit is getting it back.
    case reconnecting
    case reconnected
    /// LiveKit left the room without the app asking it to, with its reason (nil when it gave none).
    case lost(LiveKitError?)
}

/// How a call that LiveKit ended is shown on the call screen, and why CallKit is told it ended.
struct CallEnding: Equatable {
    let reason: CXCallEndedReason
    let message: String

    /// LiveKit left the room without the app asking it to. The AI ends a call by deleting the room, which is a
    /// normal end; any other reason, a lost network above all, means the call dropped.
    static func afterDisconnect(_ error: LiveKitError?) -> CallEnding {
        switch error?.type {
        case .roomDeleted?, .participantRemoved?:
            CallEnding(reason: .remoteEnded, message: "Call ended")
        default:
            CallEnding(reason: .failed, message: "The call dropped")
        }
    }
}
