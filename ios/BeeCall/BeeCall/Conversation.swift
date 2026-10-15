import Foundation

/// Design D's conversation (frames 1, 3 and 6): every call is a section of one thread, its turns as bubbles and
/// its results as cards. The server's call view will give these (its contract is still being written); until
/// then the views draw from these values and a stand-in fills them, so nothing here follows the server's events.
struct ConversationCall: Identifiable, Equatable {
    enum Starter: Equatable { case person, beeCall }

    let id: String
    var title: String
    var startedAt: Date
    var starter: Starter
    var length: TimeInterval? // nil while the call is on
    var entries: [ConversationEntry]

    var isLive: Bool { length == nil }

    /// What the thread shows of the call: everything while it is on or unfolded; once it is over and folded, only
    /// its results (finished jobs and what went into Bee).
    func shown(expanded: Bool) -> [ConversationEntry] {
        guard !isLive, !expanded else { return entries }
        return entries.filter {
            if case .turn = $0 { false } else { true }
        }
    }

    /// "대화 펼치기" shows only when unfolding would show more.
    var canUnfold: Bool {
        !isLive && entries.contains { if case .turn = $0 { true } else { false } }
    }
}

enum ConversationEntry: Identifiable, Equatable {
    case turn(Turn)
    case job(JobCard)
    case beeWrite(BeeWriteCard)

    var id: String {
        switch self {
        case .turn(let turn): turn.id
        case .job(let job): job.id
        case .beeWrite(let write): write.id
        }
    }
}

/// One turn: the AI's (a light yellow bubble) or the person's (blue).
struct Turn: Identifiable, Equatable {
    let id: String
    let mine: Bool
    var text: String
}

/// A background job: running with its newest step, or finished with its result, chart and files.
struct JobCard: Identifiable, Equatable {
    let id: String
    var title: String
    var startedAt: Date
    var finishedAt: Date?
    var step: String?
    var result: String?
    var files: [String]
    var chart: JobChart?

    var isDone: Bool { finishedAt != nil }
}

/// A small bar chart a job made, as the card draws it: groups of two bars (the design's Air and Pro).
struct JobChart: Equatable {
    struct Group: Equatable {
        let name: String
        let values: [Double]
    }

    let title: String
    let series: [String]
    let groups: [Group]
}

/// Something written to Bee during the call, which the person can take back while the call is on.
struct BeeWriteCard: Identifiable, Equatable {
    let id: String
    var kind: String // "할 일"
    var text: String
    var added: Bool
}

/// The line above a call in the conversation: "오늘 오전 1:23 · 내가 건 전화 · 7분 40초".
enum CallInfo {
    static func line(_ call: ConversationCall, korean: Bool, now: Date, calendar: Calendar) -> String {
        let when = "\(day(call.startedAt, korean: korean, now: now, calendar: calendar)) "
            + time(call.startedAt, korean: korean, calendar: calendar)
        let who = switch (call.starter, korean) {
        case (.person, true): "내가 건 전화"
        case (.beeCall, true): "Bee Call이 건 전화"
        case (.person, false): "You called"
        case (.beeCall, false): "Bee Call called"
        }
        let how = call.length.map { length($0, korean: korean) } ?? (korean ? "통화 중" : "In a call")
        return [when, who, how].joined(separator: " · ")
    }

    /// 오늘, 어제, or the date: "10월 13일", "Oct 13".
    static func day(_ date: Date, korean: Bool, now: Date, calendar: Calendar) -> String {
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: date),
                                           to: calendar.startOfDay(for: now)).day ?? 0
        if days == 0 { return korean ? "오늘" : "Today" }
        if days == 1 { return korean ? "어제" : "Yesterday" }
        let parts = calendar.dateComponents([.month, .day], from: date)
        let month = parts.month ?? 1, day = parts.day ?? 1
        let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return korean ? "\(month)월 \(day)일" : "\(months[month - 1]) \(day)"
    }

    /// "오전 1:23", "1:23 AM": the time as the phone writes it.
    static func time(_ date: Date, korean: Bool, calendar: Calendar) -> String {
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        let hour = parts.hour ?? 0, minute = parts.minute ?? 0
        let clock = "\(hour % 12 == 0 ? 12 : hour % 12):" + String(format: "%02d", minute)
        return korean ? "\(hour < 12 ? "오전" : "오후") \(clock)" : "\(clock)\u{00A0}\(hour < 12 ? "AM" : "PM")"
    }

    /// "7분 40초", "16초", "1분"; "7 min 40 s".
    static func length(_ seconds: TimeInterval, korean: Bool) -> String {
        let total = max(Int(seconds.rounded()), 0)
        let (minutes, rest) = (total / 60, total % 60)
        var parts: [String] = []
        if minutes > 0 { parts.append(korean ? "\(minutes)분" : "\(minutes) min") }
        if rest > 0 || minutes == 0 { parts.append(korean ? "\(rest)초" : "\(rest) s") }
        return parts.joined(separator: " ")
    }
}
