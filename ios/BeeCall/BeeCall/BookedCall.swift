import Foundation

/// The words for an offer (contract section 4): the question Bee Call asks, and a call booked for a time, which the
/// person asked for out loud in a call ("call me again at 7 PM", the AI's book_call) or accepted from a
/// notification. A booked call's row says that a call is booked, when, and about what (the user's report of
/// 2026-10-15: a row with only "Call off" on it, which the user could not place, pressed, and so called off a call
/// they wanted).
///
/// Times are on the person's own clock: the server keeps a time zone per person and writes every time with its
/// offset, so the row shows the time the person booked even on a phone set to another zone. The topic is the
/// server's, never empty: the app adds no wording of its own. The words follow the person's call language, as the
/// server's notifications about the same call do.
extension Offer {
    /// "오늘 저녁 7:00 전화 예정", "Call booked for today, 7:00 PM".
    func bookedLine(language: String, now: Date = .now) -> String {
        let korean = language == "ko"
        guard let callAt else { return korean ? "전화 예정" : "Call booked" }
        let when = callAt.spoken(korean: korean, now: now)
        return korean ? "\(when) 전화 예정" : "Call booked for \(when)"
    }

    /// The question's line: "오늘 저녁 8:30에 잠깐 통화할까요?", "A quick call today, 8:30 PM?".
    func askedLine(language: String, now: Date = .now) -> String {
        let korean = language == "ko"
        guard let suggestedAt else { return korean ? "잠깐 통화할까요?" : "A quick call?" }
        let when = suggestedAt.spoken(korean: korean, now: now)
        return korean ? "\(when)에 잠깐 통화할까요?" : "A quick call \(when)?"
    }

    /// The question's first answer, the suggested time: "8:30에", "At 8:30 PM". Nil without one.
    func laterButton(language: String) -> String? {
        guard let suggestedAt else { return nil }
        let korean = language == "ko"
        let clock = suggestedAt.clock(korean: korean)
        return korean ? "\(clock)에" : "At \(clock)"
    }

    /// A call being placed now, which can no longer be called off.
    static func callingLine(language: String) -> String {
        language == "ko" ? "지금 전화하는 중" : "Calling you now"
    }

    /// The row's action, and the alert's button that confirms it: it says what it cancels.
    static func cancelButton(language: String) -> String {
        language == "ko" ? "전화 취소" : "Cancel call"
    }

    /// The system alert's question before anything is sent: "오늘 저녁 7:00 전화를 취소할까요?".
    func cancelQuestion(language: String, now: Date = .now) -> String {
        let korean = language == "ko"
        guard let callAt else { return korean ? "예약된 전화를 취소할까요?" : "Cancel the booked call?" }
        let when = callAt.spoken(korean: korean, now: now)
        return korean ? "\(when) 전화를 취소할까요?" : "Cancel the call for \(when)?"
    }

    /// The alert's other button, which sends nothing.
    static func keepButton(language: String) -> String {
        language == "ko" ? "그대로 두기" : "Keep the call"
    }
}

extension ZonedTime {
    /// The day and time on the person's clock: "오늘 저녁 7:00", "today, 7:00 PM", "10월 18일 일요일 밤 9:00",
    /// "Sunday, October 18, 9:00 PM".
    func spoken(korean: Bool, now: Date) -> String {
        let calendar = calendar
        let parts = calendar.dateComponents([.month, .day, .weekday], from: date)
        let month = parts.month ?? 1, day = parts.day ?? 1, weekday = parts.weekday ?? 1
        let daysAway = calendar.dateComponents([.day], from: calendar.startOfDay(for: now),
                                               to: calendar.startOfDay(for: date)).day
        if korean {
            let date = daysAway == 0 ? "오늘" : daysAway == 1 ? "내일" : "\(month)월 \(day)일 \(Self.koreanWeekdays[weekday - 1])"
            return "\(date) \(clock(korean: true, period: true))"
        }
        let date = daysAway == 0 ? "today" : daysAway == 1 ? "tomorrow"
            : "\(Self.weekdays[weekday - 1]), \(Self.months[month - 1]) \(day)"
        return "\(date), \(clock(korean: false))"
    }

    /// The time on the person's clock: "8:30" (Korean, without the time of day unless asked for, as "8:30에"),
    /// "저녁 7:00", "8:30 PM" (English, which never breaks before PM).
    func clock(korean: Bool, period: Bool = false) -> String {
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        let hour = parts.hour ?? 0, minute = parts.minute ?? 0
        let clock = "\(hour % 12 == 0 ? 12 : hour % 12):" + String(format: "%02d", minute)
        guard korean else { return "\(clock)\u{00A0}\(hour < 12 ? "AM" : "PM")" }
        guard period else { return clock }
        // As the server's (voice/offers.py clock_label), but 새벽 after midnight: "내일 밤 12:20" would read as the
        // night of tomorrow, a day later than meant.
        let name = hour < 5 ? "새벽" : hour >= 20 ? "밤" : hour >= 17 ? "저녁" : hour >= 12 ? "오후" : "오전"
        return "\(name) \(clock)"
    }

    private static let koreanWeekdays = ["일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일"]
    private static let weekdays = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    private static let months = ["January", "February", "March", "April", "May", "June", "July", "August",
                                 "September", "October", "November", "December"]
}
