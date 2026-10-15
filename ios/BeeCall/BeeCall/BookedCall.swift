import Foundation

/// A call booked for a time: the person asked for it out loud in a call ("call me again at 7 PM", the AI's
/// book_call) or accepted the time a notification suggested. Its row on the first screen says that a call is
/// booked, when, and about what. The user's report of 2026-10-15: a row with only "Call off" on it, which the user
/// could not place, pressed, and so called off a call they wanted.
///
/// The time is on the person's own clock. The server keeps a time zone per person and writes call_at in it
/// (voice/offers.py Offer.view), so the row shows the time the person booked even on a phone set to another zone;
/// an offer stored before the server kept a zone carries none, and is shown on the phone's clock. The words follow
/// the person's call language (Settings: Language), as the server's notifications about the same call do.
extension OfferCenter.Offer {
    /// "오늘 저녁 7:00 전화 예정", "Call booked for today, 7:00 PM".
    func bookedLine(language: String, now: Date = .now, phoneZone: TimeZone = .current) -> String {
        let korean = language == "ko"
        guard let when = when(korean: korean, now: now, phoneZone: phoneZone) else {
            return korean ? "전화 예정" : "Call booked"
        }
        return korean ? "\(when) 전화 예정" : "Call booked for \(when)"
    }

    /// What the call is about: the topic as the server gives it, or else what kind of call it is.
    func about(language: String) -> String {
        let korean = language == "ko"
        if let topic = topic?.trimmingCharacters(in: .whitespacesAndNewlines), !topic.isEmpty { return topic }
        if source == "call" { return korean ? "통화 중에 잡은 전화" : "Booked during a call" }
        if kind == "voice note" { return korean ? "새 음성 메모 이야기" : "About your new voice note" }
        return korean ? "오늘 하루 이야기" : "About your day"
    }

    /// The person's clock: the offset call_at carries ("+09:00"), or the phone's zone when it carries none.
    func personsZone(phone: TimeZone) -> TimeZone {
        guard let callAt else { return phone }
        if callAt.hasSuffix("Z") { return TimeZone(secondsFromGMT: 0) ?? phone }
        guard let offset = callAt.firstMatch(of: /T.*([+-])(\d{2}):(\d{2})$/),
              let hours = Int(offset.2), let minutes = Int(offset.3) else { return phone }
        let seconds = (hours * 3600 + minutes * 60) * (offset.1 == "-" ? -1 : 1)
        return TimeZone(secondsFromGMT: seconds) ?? phone
    }

    /// The call's day and time on the person's clock: "오늘 저녁 7:00", "today, 7:00 PM". Nil without a time.
    private func when(korean: Bool, now: Date, phoneZone: TimeZone) -> String? {
        guard let callAtEpoch else { return nil }
        let moment = Date(timeIntervalSince1970: TimeInterval(callAtEpoch))
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = personsZone(phone: phoneZone)
        let parts = calendar.dateComponents([.month, .day, .weekday, .hour, .minute], from: moment)
        guard let month = parts.month, let day = parts.day, let weekday = parts.weekday,
              let hour = parts.hour, let minute = parts.minute else { return nil }
        let daysAway = calendar.dateComponents([.day], from: calendar.startOfDay(for: now),
                                               to: calendar.startOfDay(for: moment)).day
        let clock = "\(hour % 12 == 0 ? 12 : hour % 12):" + String(format: "%02d", minute)
        if korean {
            let date = daysAway == 0 ? "오늘" : daysAway == 1 ? "내일" : "\(month)월 \(day)일 \(Self.koreanWeekdays[weekday - 1])"
            // The server's times of day (voice/offers.py clock_label).
            let period = hour >= 20 || hour < 5 ? "밤" : hour >= 17 ? "저녁" : hour >= 12 ? "오후" : "오전"
            return "\(date) \(period) \(clock)"
        }
        let date = daysAway == 0 ? "today" : daysAway == 1 ? "tomorrow"
            : "\(Self.weekdays[weekday - 1]), \(Self.months[month - 1]) \(day)"
        return "\(date), \(clock) \(hour < 12 ? "AM" : "PM")"
    }

    private static let koreanWeekdays = ["일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일"]
    private static let weekdays = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
    private static let months = ["January", "February", "March", "April", "May", "June", "July", "August",
                                 "September", "October", "November", "December"]
}
