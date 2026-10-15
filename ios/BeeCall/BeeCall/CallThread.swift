import Foundation

/// Design D's home thread, as the contract gives it (sections 1 to 4). The server builds every call's view; this
/// only puts the pieces in drawing order and applies the caption rules of section 3, so nothing here interprets a
/// call's events.
enum CallThread {
    /// A section of the thread: a call, the open question at its time, or the person's own call before the
    /// server has a record of it (a call the person placed is recorded once the AI picks up).
    enum Section: Identifiable, Hashable {
        case call(CallEntry)
        case question(Offer)
        case ownCall(room: String)

        var id: String {
            switch self {
            case .call(let entry): "call/\(entry.call.callID)"
            case .question(let offer): "question/\(offer.id)"
            case .ownCall(let room): "call/\(room)"
            }
        }

        var at: Date {
            switch self {
            case .call(let entry): entry.call.startedAt.date
            case .question(let offer): offer.createdAt.date
            case .ownCall: .distantFuture
            }
        }
    }

    /// What a call's section draws: an item of its view, or a bubble of speech with live caption text in it.
    enum Entry: Identifiable, Hashable {
        case item(CallItem)
        case bubble(id: String, speaker: Speaker, text: String)

        var id: String {
            switch self {
            case .item(let item): item.id
            case .bubble(let id, _, _): id
            }
        }
    }

    /// The calls oldest first, as a conversation reads (the list comes newest first), with the open question
    /// placed among them at its created_at. Booked calls are pinned above the thread, not placed in it.
    static func sections(calls: [CallEntry], offers: [Offer]) -> [Section] {
        let questions = offers.filter { $0.state == .asked }.map(Section.question)
        return (calls.reversed().map(Section.call) + questions).enumerated()
            .sorted { ($0.element.at, $0.offset) < ($1.element.at, $1.offset) }
            .map(\.element)
    }

    /// A call that is over, folded: its jobs and Bee writes, as the list gives them.
    static func folded(_ entry: CallEntry) -> [Entry] {
        entry.results.filter(\.isDrawn).map(Entry.item)
    }

    /// Whether "대화 펼치기" has anything to show: a call with a record and a conversation.
    static func canUnfold(_ call: CallSummary) -> Bool {
        (call.state == .ended || call.state == .dropped) && call.titleSource != .none
    }

    /// A call unfolded, or in its record sheet: its view's items.
    static func expanded(_ view: CallView) -> [Entry] {
        view.drawnItems.map(Entry.item)
    }

    /// The person's own call while it is on (section 3): the view's items, then the caption lines the view does
    /// not hold yet, each joining the last bubble drawn when that is the same speaker's, then the running jobs.
    /// Captions are left out when `captionsShown` is false: the call is over, or its numbers no longer line up.
    static func live(view: CallView?, captions: [CaptionLine], captionsShown: Bool) -> [Entry] {
        let items = view?.drawnItems ?? []
        let running = items.filter { if case .job(let job) = $0 { job.state == .running } else { false } }
        var entries = items.filter { !running.contains($0) }.map(Entry.item)
        if captionsShown {
            let held = view?.captionsIncluded ?? CaptionCounts(person: 0, ai: 0)
            for line in captions where line.number > (line.speaker == .person ? held.person : held.ai) {
                if let last = entries.last, let (id, speaker, text) = speech(last), speaker == line.speaker {
                    entries[entries.count - 1] = .bubble(id: id, speaker: speaker, text: text + " " + line.text)
                } else {
                    entries.append(.bubble(id: "caption-\(line.speaker.rawValue)-\(line.number)", speaker: line.speaker,
                                           text: line.text))
                }
            }
        }
        return entries + running.map(Entry.item)
    }

    private static func speech(_ entry: Entry) -> (String, Speaker, String)? {
        switch entry {
        case .item(.speech(let speech)): (speech.id, speech.speaker, speech.text)
        case .bubble(let id, let speaker, let text): (id, speaker, text)
        case .item: nil
        }
    }
}

/// The words around a call: the line above it ("오늘 오전 1:23 · 내가 건 전화 · 7분 40초"), its title, how it
/// ended, and the end mark of its record. Times are on the person's clock, the offset the server writes.
enum CallLine {
    static func line(_ call: CallSummary, korean: Bool, now: Date) -> String {
        let when = "\(day(call.startedAt, korean: korean, now: now)) \(time(call.startedAt, korean: korean))"
        let who = switch (call.startedBy, korean) {
        case (.ai, true): "Bee Call이 건 전화"
        case (.ai, false): "Bee Call called"
        case (.person, true): "내가 건 전화"
        case (.person, false): "You called"
        case (.unrecognized, _): "" // a starter a later server adds: left out, not drawn as the person
        }
        let how: String
        switch call.state {
        case .live: how = korean ? "통화 중" : "In a call"
        case .missed: how = korean ? "받지 못함" : "Missed"
        case .declined: how = korean ? "거절함" : "Declined"
        case .dropped: how = [call.durationS.map { length($0, korean: korean) }, korean ? "끊김" : "dropped"]
            .compactMap { $0 }.joined(separator: " · ")
        default: how = call.durationS.map { length($0, korean: korean) } ?? ""
        }
        return [when, who, how].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    /// The call's one line, or "대화 없음" for a call without a conversation.
    static func title(_ call: CallSummary, korean: Bool) -> String {
        call.title ?? (korean ? "대화 없음" : "No conversation")
    }

    /// How a call ended, for its record's header.
    static func ending(_ state: CallState, korean: Bool) -> String? {
        switch state {
        case .ended: korean ? "정상 종료" : "Ended normally"
        case .dropped: korean ? "연결 끊김" : "Dropped"
        case .missed: korean ? "받지 못함" : "Missed"
        case .declined: korean ? "거절함" : "Declined"
        case .live: korean ? "통화 중" : "In a call"
        case .unrecognized: nil
        }
    }

    /// The record sheet's line under its title: the line above the call, and how it ended.
    static func sheetLine(_ call: CallSummary, korean: Bool, now: Date) -> String {
        let when = "\(day(call.startedAt, korean: korean, now: now)) \(time(call.startedAt, korean: korean))"
        let who: String? = switch call.startedBy {
        case .ai: korean ? "Bee Call이 건 전화" : "Bee Call called"
        case .person: korean ? "내가 건 전화" : "You called"
        case .unrecognized: nil
        }
        return [when, who, call.durationS.map { length($0, korean: korean) }, ending(call.state, korean: korean)]
            .compactMap { $0 }.joined(separator: " · ")
    }

    /// The mark at the end of a call's record: "통화 끝 · 7분 40초".
    static func endMark(_ call: CallSummary, korean: Bool) -> String {
        let done = korean ? "통화 끝" : "Call ended"
        guard let seconds = call.durationS else { return done }
        return "\(done) · \(length(seconds, korean: korean))"
    }

    /// 오늘, 어제, or the date: "10월 13일", "Oct 13", on the person's clock.
    static func day(_ time: ZonedTime, korean: Bool, now: Date) -> String {
        let calendar = time.calendar
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: time.date),
                                           to: calendar.startOfDay(for: now)).day ?? 0
        if days == 0 { return korean ? "오늘" : "Today" }
        if days == 1 { return korean ? "어제" : "Yesterday" }
        let parts = calendar.dateComponents([.month, .day], from: time.date)
        let month = parts.month ?? 1, day = parts.day ?? 1
        let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return korean ? "\(month)월 \(day)일" : "\(months[month - 1]) \(day)"
    }

    /// "오전 1:23", "1:23 AM": the time as the phone writes it, on the person's clock.
    static func time(_ time: ZonedTime, korean: Bool) -> String {
        let parts = time.calendar.dateComponents([.hour, .minute], from: time.date)
        let hour = parts.hour ?? 0, minute = parts.minute ?? 0
        let clock = "\(hour % 12 == 0 ? 12 : hour % 12):" + String(format: "%02d", minute)
        return korean ? "\(hour < 12 ? "오전" : "오후") \(clock)" : "\(clock)\u{00A0}\(hour < 12 ? "AM" : "PM")"
    }

    /// "7분 40초", "16초", "1분"; "7 min 40 s".
    static func length(_ seconds: Int, korean: Bool) -> String {
        let total = max(seconds, 0)
        let (minutes, rest) = (total / 60, total % 60)
        var parts: [String] = []
        if minutes > 0 { parts.append(korean ? "\(minutes)분" : "\(minutes) min") }
        if rest > 0 || minutes == 0 { parts.append(korean ? "\(rest)초" : "\(rest) s") }
        return parts.joined(separator: " ")
    }
}
