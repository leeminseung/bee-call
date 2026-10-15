import Observation

/// Design D, frame 5: work that finishes during a call always lands as a card in the conversation, wherever the
/// person is. On the call screen the job pill turns into a yellow "결과 도착 · 대화에서 보기" (`unseen`), and
/// opening the conversation scrolls to the card and lights it up; in the conversation the card lights up where it
/// is (`glowing`, for about two seconds, which the view ends). A result is a job whose state went from running to
/// done between two views of the call (contract section 8).
@Observable
final class ResultNotice {
    private(set) var unseen: String? // a result that arrived while the call screen was up
    private(set) var glowing: String? // the card to light up in the conversation
    @ObservationIgnored private var call: String? // the call the finished jobs below belong to
    @ObservationIgnored private var known: Set<String> = [] // that call's finished jobs already accounted for

    /// The call's finished jobs, each time the call is seen, and whether the call screen is up. The first look at a
    /// call is its baseline: what was done before the person could see it is not news (review of 2026-10-15,
    /// finding 1: the baseline was the first finished job, so a call's first result never showed).
    func update(call: String?, done: [String], callScreenUp: Bool) {
        guard let call else { return callEnded() }
        guard call == self.call else {
            self.call = call
            known = Set(done)
            unseen = nil
            glowing = nil
            return
        }
        for id in done where !known.contains(id) {
            if callScreenUp { unseen = id } else { glowing = id }
        }
        known.formUnion(done)
    }

    /// The person went to the conversation: the card to scroll to, now lit up, if a result was waiting.
    func openedConversation() -> String? {
        guard let id = unseen else { return nil }
        unseen = nil
        glowing = id
        return id
    }

    func glowEnded(_ id: String) {
        if glowing == id { glowing = nil }
    }

    func callEnded() {
        call = nil
        known = []
        unseen = nil
        glowing = nil
    }
}
