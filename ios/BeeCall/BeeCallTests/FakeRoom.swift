@testable import BeeCall

/// Stands in for a call's LiveKit room: joining succeeds at once (or fails with `joinError`), and the test says
/// when the AI picks up.
@MainActor
final class FakeRoom: CallRoom {
    var onLinkChange: ((CallLink) -> Void)?
    var onAgentAnswered: (() -> Void)?
    /// The AI has picked up before the phone finishes joining, as in the AI's own call once it is answered.
    var aiAnsweredBeforeJoin = false
    var joinError: Error?
    private(set) var joins: [String?] = [] // the room asked for: nil for a call the person starts
    private(set) var leaves = 0

    func connect(tokenServerURL: String, answering roomName: String?, continuing earlierCall: String?) async throws
        -> String {
        joins.append(roomName)
        if let joinError { throw joinError }
        if aiAnsweredBeforeJoin { onAgentAnswered?() }
        return roomName ?? "call-matt-20261015-183000"
    }

    func setMicrophone(enabled: Bool) async throws {}

    func disconnect() async {
        leaves += 1
    }

    /// The AI picks up now.
    func aiAnswers() {
        onAgentAnswered?()
    }
}
