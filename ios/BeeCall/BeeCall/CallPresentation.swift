import Observation

/// Which of design D's two views of a call is up (frame 4): the call screen, or the conversation under the call
/// bar. A call starts on the call screen, made for walking and not looking, and keeps the view the person last
/// picked until it ends, also when the app comes back to the front; the next call starts on the call screen
/// again.
@Observable
final class CallPresentation {
    enum Mode { case callScreen, conversation }

    private(set) var mode = Mode.callScreen
    @ObservationIgnored private var wasInCall = false

    /// Each time the call's state is seen: a call that has just begun starts on the call screen.
    func callChanged(inCall: Bool) {
        if inCall && !wasInCall { mode = .callScreen }
        wasInCall = inCall
    }

    /// The person's pick: "대화 보기" or a swipe down, or the call bar's "통화 화면".
    func show(_ mode: Mode) {
        self.mode = mode
    }

    func showsCallScreen(inCall: Bool) -> Bool {
        inCall && mode == .callScreen
    }
}
