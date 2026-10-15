/// What CallManager does with a call's room: join it, mute, leave, and hear when the AI picks up or the link
/// changes. LiveKitClient in the app, a stand-in in tests.
protocol CallRoom: AnyObject {
    var onLinkChange: ((CallLink) -> Void)? { get set }
    var onAgentAnswered: (() -> Void)? { get set }
    /// Joins a new room for a call the person starts, or with `answering`, the room of the AI's call. Returns the
    /// room's name.
    func connect(tokenServerURL: String, answering roomName: String?, continuing earlierCall: String?) async throws
        -> String
    func setMicrophone(enabled: Bool) async throws
    func disconnect() async
}

extension LiveKitClient: CallRoom {}
