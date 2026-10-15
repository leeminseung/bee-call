import Observation

/// Design D, frame 5: work that finishes during a call always lands as a card in the conversation, wherever the
/// person is. On the call screen the job pill turns into a yellow "결과 도착 · 대화에서 보기" (`unseen`), and
/// opening the conversation scrolls to the card and lights it up; in the conversation the card lights up where it
/// is (`glowing`, for about two seconds, which the view ends).
@Observable
final class ResultNotice {
    private(set) var unseen: String? // a result that arrived while the call screen was up
    private(set) var glowing: String? // the card to light up in the conversation
    @ObservationIgnored private var known: Set<String>? // finished jobs already accounted for; nil before the first look

    /// The call's finished jobs, each time they are seen, and whether the call screen is up.
    func update(done: [String], callScreenUp: Bool) {
        guard let known else {
            self.known = Set(done) // what was there before the person could see it is not news
            return
        }
        for id in done where !known.contains(id) {
            if callScreenUp { unseen = id } else { glowing = id }
        }
        self.known = known.union(done)
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
        unseen = nil
        glowing = nil
        known = nil
    }
}
