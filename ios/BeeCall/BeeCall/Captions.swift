import Foundation

/// One caption bubble on the call screen.
struct Caption: Identifiable {
    /// One transcribed line in a bubble, as the voice server sends it: an id (the sender and LiveKit's segment
    /// id) and its newest text.
    struct Line {
        let id: String
        let number: Int // the speaker's 1, 2, 3... in the order lines first arrived (contract section 3)
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
    private var counts: [Bool: Int] = [:] // lines so far per side, keyed by isMine

    /// Every line, numbered per speaker, in the order they first arrived: what the thread draws beyond the
    /// lines the call view holds already.
    var lines: [CaptionLine] {
        captions.flatMap { caption in
            caption.lines.map { CaptionLine(speaker: caption.isMine ? .person : .ai, number: $0.number, text: $0.text) }
        }
    }

    /// The newest text of the line `id`. A version older than the one shown is dropped.
    mutating func show(_ text: String, line id: String, isMine: Bool, sentAt: Date) {
        if let (bubble, line) = position(of: id) {
            guard sentAt >= captions[bubble].lines[line].sentAt else { return } // an older version of this line
            captions[bubble].lines[line].text = text
            captions[bubble].lines[line].sentAt = sentAt
            return
        }
        let number = (counts[isMine] ?? 0) + 1
        counts[isMine] = number
        let line = Caption.Line(id: id, number: number, text: text, sentAt: sentAt)
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

/// One caption line as the thread uses it: its speaker, its number among that speaker's lines, and its text.
struct CaptionLine: Hashable {
    let speaker: Speaker
    let number: Int
    let text: String
}

/// Whether the caption numbers still line up with the server's counts (contract section 3, rule 6). They do while
/// one connection lasts and one voice server is in the room. A reconnect of the phone's room loses the lines sent
/// meanwhile; a voice server leaving, or a new one joining, is a restart, whose cut-off reply was captioned but
/// never recorded; and the voice server marks its own reconnect by changing its bee_call.captions attribute. After
/// any of these the thread draws the call view alone for the rest of the call.
struct CaptionNumbering {
    private(set) var holds = true
    private var voiceServerSeen = false
    private var mark: String?? = .none // the voice server's bee_call.captions as first seen

    mutating func roomReconnecting() {
        holds = false
    }

    /// The voice server is in the room: when the phone joins, or when it arrives later. A second arrival is a
    /// restart.
    mutating func voiceServerJoined(captions value: String?) {
        if voiceServerSeen { holds = false }
        voiceServerSeen = true
        captionsMark(value)
    }

    mutating func voiceServerLeft() {
        holds = false
    }

    mutating func captionsMark(_ value: String?) {
        guard let first = mark else {
            mark = .some(value)
            return
        }
        if first != value { holds = false }
    }
}
