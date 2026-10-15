/// What CallManager does with a call's room: join it, mute, leave, and hear when the AI picks up or the link
/// changes. LiveKitClient in the app, a stand-in in tests.
protocol CallRoom: AnyObject {
    var onLinkChange: ((CallLink) -> Void)? { get set }
    var onAgentAnswered: (() -> Void)? { get set }
    /// Joins a new room for a call the person starts, or with `answering`, the room of the AI's call. Returns the
    /// room's name.
    func connect(answering roomName: String?, continuing earlierCall: String?) async throws -> String
    /// Starts this call's recording on this iPhone if the person wants one ("Save on this iPhone"); what went
    /// wrong comes back in words, nil when nothing did. Leaving the room stops it.
    func recordOnPhoneIfWanted() -> String?
    func setMicrophone(enabled: Bool) async throws
    func disconnect() async
}

extension LiveKitClient: CallRoom {}
