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
    /// The join waits for `releaseJoin()`, as on a slow network.
    var holdsJoin = false
    private var held: CheckedContinuation<Void, Never>?
    private(set) var joins: [String?] = [] // the room asked for: nil for a call the person starts
    private(set) var leaves = 0
    private(set) var recordingStarts = 0 // asked to record on this iPhone
    var recordingProblem: String? // what the recording on this iPhone answers

    func connect(answering roomName: String?, continuing earlierCall: String?) async throws -> String {
        joins.append(roomName)
        if holdsJoin { await withCheckedContinuation { held = $0 } }
        if let joinError { throw joinError }
        if aiAnsweredBeforeJoin { onAgentAnswered?() }
        return roomName ?? "call-matt-20261015-183000"
    }

    func recordOnPhoneIfWanted() -> String? {
        recordingStarts += 1
        return recordingProblem
    }

    func setMicrophone(enabled: Bool) async throws {}

    func disconnect() async {
        leaves += 1
    }

    func releaseJoin() {
        held?.resume()
        held = nil
    }

    /// The AI picks up now.
    func aiAnswers() {
        onAgentAnswered?()
    }
}
