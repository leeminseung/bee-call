import Foundation

/// One caption bubble on the call screen.
struct Caption: Identifiable {
    /// One transcribed line in a bubble, as the voice server sends it: an id (the sender and LiveKit's segment
    /// id) and its newest text.
    struct Line {
        let id: String
        var text: String
        var sentAt: Date
    }

    let id: String // the first line's id
    let isMine: Bool
    var lines: [Line]

    var text: String {
        lines.map { $0.text.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
            .joined(separator: " ")
    }
}

/// The captions of a call, as the voice server transcribes both sides (topic lk.transcription). An AI line
/// arrives as one stream in chunks; a user line is re-sent in full as a new stream each time it improves.
/// Speech recognition cuts continuous speech into several lines, so the user's lines before the AI's next
/// line make one bubble.
struct CaptionList {
    static let limit = 30 // bubbles kept on screen

    private(set) var captions: [Caption] = []

    /// The newest text of the line `id`. A version older than the one shown is dropped.
    mutating func show(_ text: String, line id: String, isMine: Bool, sentAt: Date) {
        if let (bubble, line) = position(of: id) {
            guard sentAt >= captions[bubble].lines[line].sentAt else { return } // an older version of this line
            captions[bubble].lines[line].text = text
            captions[bubble].lines[line].sentAt = sentAt
            return
        }
        let line = Caption.Line(id: id, text: text, sentAt: sentAt)
        if isMine, let last = captions.indices.last, captions[last].isMine {
            captions[last].lines.append(line) // the user is still talking: no AI line since their last one
            return
        }
        captions.append(Caption(id: id, isMine: isMine, lines: [line]))
        if captions.count > Self.limit { captions.removeFirst() }
    }

    private func position(of id: String) -> (Int, Int)? {
        for (bubble, caption) in captions.enumerated() {
            if let line = caption.lines.firstIndex(where: { $0.id == id }) { return (bubble, line) }
        }
        return nil
    }
}
