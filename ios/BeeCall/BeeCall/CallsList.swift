import Foundation

/// The calls list (scratch/2026-10-15-ui-mockups/calls-list.html): every call the list holds, grouped by day, newest
/// first. A day is the person's, as the server writes each time with the person's offset (contract section 0, Times);
/// a row says what the call was, when, who placed it, and how it went, with what it made. The words are the home
/// thread's (CallLine), so the two never disagree.
enum CallsList {
    struct Day: Identifiable, Equatable {
        let id: String // "2026-10-15", the person's date
        let title: String // 오늘, 어제, 10월 13일
        let weekday: String? // 화요일, beside an older day's date
        let calls: [CallEntry]
    }

    /// What the sheet shows: a spinner until the first page comes; the failure line with its retry when it could not
    /// be had; iOS's empty view when there are no calls at all; else the days.
    enum Content: Equatable {
        case loading, failed, empty
        case days([Day])

        init(_ calls: [CallEntry], loaded: Bool, older: CallStore.OlderPages, problem: String?, korean: Bool, now: Date) {
            let days = CallsList.days(calls, korean: korean, now: now)
            if !days.isEmpty {
                self = .days(days)
            } else if !loaded {
                self = problem == nil ? .loading : .failed
            } else {
                self = older == .done ? .empty : .days([])
            }
        }
    }

    /// The calls by day, newest day first, each day's calls in the list's order (newest first).
    static func days(_ calls: [CallEntry], korean: Bool, now: Date) -> [Day] {
        var days: [Day] = []
        for entry in calls {
            let time = entry.call.startedAt
            let parts = time.calendar.dateComponents([.year, .month, .day], from: time.date)
            let id = String(format: "%04d-%02d-%02d", parts.year ?? 0, parts.month ?? 0, parts.day ?? 0)
            if let index = days.firstIndex(where: { $0.id == id }) {
                days[index] = Day(id: id, title: days[index].title, weekday: days[index].weekday,
                                  calls: days[index].calls + [entry])
            } else {
                let (title, weekday) = header(time, korean: korean, now: now)
                days.append(Day(id: id, title: title, weekday: weekday, calls: [entry]))
            }
        }
        return days.sorted { $0.id > $1.id }
    }

    /// 오늘 and 어제 alone; an older day its date and, quieter beside it, its weekday; another year's with the year.
    private static func header(_ time: ZonedTime, korean: Bool, now: Date) -> (String, String?) {
        let calendar = time.calendar
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: time.date),
                                           to: calendar.startOfDay(for: now)).day ?? 0
        if days == 0 { return (korean ? "오늘" : "Today", nil) }
        if days == 1 { return (korean ? "어제" : "Yesterday", nil) }
        let parts = calendar.dateComponents([.year, .month, .day, .weekday], from: time.date)
        let year = parts.year ?? 0, month = parts.month ?? 1, day = parts.day ?? 1, weekday = parts.weekday ?? 1
        let thisYear = calendar.component(.year, from: now) == year
        let months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                      "November", "December"]
        let weekdays = korean ? ["일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일"]
            : ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        let date = korean ? (thisYear ? "" : "\(year)년 ") + "\(month)월 \(day)일"
            : "\(months[month - 1]) \(day)" + (thisYear ? "" : ", \(year)")
        return (date, weekdays[weekday - 1])
    }

    /// How a row's state word looks: red (끊김, 받지 못함), quiet (거절함), or the live call's yellow pill.
    enum Look: Equatable {
        case red, quiet, livePill
    }

    struct StateWord: Equatable {
        let text: String
        let look: Look
    }

    /// What a call made: a report, a table, a chart, and its writes in Bee, by kind.
    struct Pill: Equatable, Identifiable {
        enum Kind: Equatable {
            case report, table, chart, beeTodo, beeFact
        }

        let kind: Kind
        let text: String
        var id: String { text }
    }

    enum Icon: Equatable {
        case outgoing, incoming, missed, live
    }

    /// Where a tap goes: the call's record, back to the call that is on, or nowhere (a missed or declined call has
    /// no record).
    enum Opens: Equatable {
        case record, call, nothing
    }

    struct Row: Equatable {
        let title: String
        let quietTitle: Bool
        let line: String // "오후 2:22 · 내가 건 전화 · 6분 12초", without the state word
        let state: StateWord?
        let pills: [Pill]
        let icon: Icon
        let opens: Opens

        /// `ownCall`: the person's call that is on, which a tap goes back to.
        init(_ entry: CallEntry, korean: Bool, ownCall: String?) {
            let call = entry.call
            let live = call.state == .live
            title = live && call.title == nil ? (korean ? "지금 통화 중" : "In a call now") : CallLine.title(call, korean: korean)
            quietTitle = !live && call.titleSource == .none
            let who: String? = switch call.startedBy {
            case .person: korean ? "내가 건 전화" : "You called"
            case .ai: korean ? "Bee Call이 건 전화" : "Bee Call called"
            case .unrecognized: nil
            }
            let length = call.state == .ended || call.state == .dropped ? call.durationS.map { CallLine.length($0, korean: korean) }
                : nil
            line = [CallLine.time(call.startedAt, korean: korean), who, length].compactMap { $0 }.joined(separator: " · ")
            state = switch call.state {
            case .live: StateWord(text: korean ? "통화 중" : "In a call", look: .livePill)
            case .missed: StateWord(text: korean ? "받지 못함" : "Missed", look: .red)
            case .dropped: StateWord(text: korean ? "끊김" : "dropped", look: .red)
            case .declined: StateWord(text: korean ? "거절함" : "Declined", look: .quiet)
            case .ended, .unrecognized: nil
            }
            pills = Self.pills(entry, korean: korean)
            icon = live ? .live : call.state == .missed ? .missed : call.startedBy == .ai ? .incoming : .outgoing
            opens = switch call.state {
            case .ended, .dropped: .record
            case .live: call.callID == ownCall ? .call : .record
            case .missed, .declined, .unrecognized: .nothing
            }
        }

        private static func pills(_ entry: CallEntry, korean: Bool) -> [Pill] {
            let marks = entry.call.marks
            var pills: [Pill] = []
            if marks.report { pills.append(Pill(kind: .report, text: korean ? "보고서" : "Report")) }
            if marks.table { pills.append(Pill(kind: .table, text: korean ? "표" : "Table")) }
            if marks.chart { pills.append(Pill(kind: .chart, text: korean ? "차트" : "Chart")) }
            let inBee = entry.results.compactMap { item -> BeeWriteKind? in
                if case .beeWrite(let write) = item, write.state == .written { write.kind } else { nil }
            }
            let todos = inBee.filter { $0 == .todoCreate || $0 == .todoAccept }.count
            let facts = inBee.filter { $0 == .factConfirm || $0 == .factUpdate }.count
            if todos > 0 { pills.append(Pill(kind: .beeTodo, text: korean ? "Bee 할 일 \(todos)" : "Bee to-do \(todos)")) }
            if facts > 0 { pills.append(Pill(kind: .beeFact, text: korean ? "Bee 사실 \(facts)" : "Bee fact \(facts)")) }
            return pills
        }
    }
}
