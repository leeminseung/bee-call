import Foundation

/// The way to design D's endpoints (contract/app-contract.md). Until the token server serves them, a Debug build
/// answers them from the contract's own example files (ContractExamples), so the screens are fed through the same
/// stores and decoding as they will be by the network; launched with `-realServer YES`, it asks the server. A
/// Release build always asks the server. Moving Debug builds to the network is this one switch.
enum ContractServer {
    #if DEBUG
    static let usesExamples = !UserDefaults.standard.bool(forKey: "realServer")
    #else
    static let usesExamples = false
    #endif

    static let client: ServerClient = {
        #if DEBUG
        if usesExamples { return ServerClient(transport: ContractExamples()) }
        #endif
        return ServerClient()
    }()
}

#if DEBUG
/// Debug builds only: answers D's requests with the files in contract/examples, which the Debug build bundles
/// (the Release build leaves them out). It holds no content of its own. The person's settings are me.json, kept in
/// memory so a language or voice picked shows, with the contract's rule that a new language without a voice goes
/// back to that language's default. Paths without an example answer 404, as the server does for what it lacks.
///
/// The examples are written for one day, 2026-10-15 in Seoul: after the calls, or 3:41 into the MacBook call (the
/// contract's worked example). Their times are moved to `now`, by whole days, or with `live` so that moment of the
/// call is now, so day words and running clocks read as they would; with `live`, the list and the offers are as
/// they were at that moment, without what came after the call.
final class ContractExamples: HTTPTransport {
    private let bundle: Bundle
    var live: Bool // the MacBook call while it is on (call-view-live.json) instead of after (call-view.json)
    var olderPagesWait = false // an older page of the list is on its way and stays so (the list's foot, loading)
    var beforeFirstCall = false // the list as it was before the person's first call: empty
    private let now: Date
    private var me: [String: Any]?

    init(bundle: Bundle = .main, live: Bool = false, now: Date = .now) {
        self.bundle = bundle
        self.live = live
        self.now = now
    }

    func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        let url = request.url ?? URL(fileURLWithPath: "/")
        let method = request.httpMethod ?? "GET"
        let path = url.path
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let reply: (Int, Data)
        switch (method, path) {
        case ("GET", "/calls") where query.contains { $0.name == "before" }:
            while olderPagesWait { try await Task.sleep(for: .seconds(1)) } // until the request is cancelled
            reply = (400, Data("The examples have one page.".utf8))
        case ("GET", "/calls"):
            reply = versioned(beforeFirstCall ? noCalls(example("calls"))
                              : asOfTheCall(example("calls"), list: "calls", time: "started_at"), for: request)
        case ("GET", _) where path.hasPrefix("/calls/") && path.split(separator: "/").count == 2:
            reply = view(String(path.dropFirst("/calls/".count))).map { versioned($0, for: request) } ?? (404, Data())
        case ("GET", "/offers"):
            reply = versioned(asOfTheCall(example("offers"), list: "offers", time: "created_at"), for: request)
        case ("POST", _) where path.hasPrefix("/offers/"):
            reply = answer(offer: String(path.dropFirst("/offers/".count)))
        case ("GET", "/voices"):
            // voices.json, or voices-<language>.json for another language, whichever lists that language
            let language = query.first { $0.name == "language" }?.value ?? ""
            let voices = [example("voices-\(language)"), example("voices")].first {
                object($0)?["language"] as? String == language
            }
            reply = voices.map { (200, $0) } ?? (404, Data())
        case ("GET", "/models"):
            let role = query.first { $0.name == "role" }?.value ?? ""
            let models = example("models-\(role)")
            reply = models.isEmpty ? (404, Data()) : (200, models)
        case ("GET", "/me"):
            reply = (200, (try? JSONSerialization.data(withJSONObject: person())) ?? Data())
        case ("PUT", "/settings"):
            reply = change(settings: request.httpBody)
        default:
            reply = (404, Data())
        }
        let response = HTTPURLResponse(url: url, statusCode: reply.0, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
        return (moved(reply.1), response)
    }

    // MARK: - The examples' moment, moved to now

    /// The live call's view, as the examples have it.
    private var liveCall: ZonedTime? {
        let call = object(example("call-view-live"))?["call"] as? [String: Any]
        return (call?["started_at"] as? String).flatMap(ZonedTime.init)
    }

    /// How far the examples' times move: whole days to today, or with `live`, so that 3:41 into the call is now.
    private var shift: (seconds: TimeInterval, days: Int) {
        guard let start = liveCall else { return (0, 0) }
        let calendar = start.calendar
        let days = calendar.dateComponents([.day], from: calendar.startOfDay(for: start.date),
                                           to: calendar.startOfDay(for: now)).day ?? 0
        guard live else { return (TimeInterval(days) * 86_400, days) }
        let seconds = now.timeIntervalSince(start.date.addingTimeInterval(221))
        let moved = calendar.dateComponents([.day], from: calendar.startOfDay(for: start.date),
                                            to: calendar.startOfDay(for: start.date.addingTimeInterval(seconds))).day
        return (seconds, moved ?? 0)
    }

    /// Every time and calendar day in a body, moved by `shift`, each time keeping its offset.
    private func moved(_ body: Data) -> Data {
        let (seconds, days) = shift
        guard seconds != 0 || days != 0, var text = String(data: body, encoding: .utf8) else { return body }
        let pattern = /"(\d{4}-\d{2}-\d{2})(T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2})?"/
        for match in text.matches(of: pattern).reversed() {
            let replacement: String?
            if let clock = match.2, let time = ZonedTime(String(match.1) + clock) {
                replacement = Self.write(time.date.addingTimeInterval(seconds), in: time.zone)
            } else if let day = CalendarDay(String(match.1)) {
                replacement = Self.write(day, plus: days)
            } else {
                replacement = nil
            }
            if let replacement { text.replaceSubrange(match.range, with: "\"\(replacement)\"") }
        }
        return Data(text.utf8)
    }

    private static func write(_ date: Date, in zone: TimeZone) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.timeZone = zone
        formatter.formatOptions = [.withInternetDateTime]
        let text = formatter.string(from: date)
        return text.hasSuffix("Z") ? String(text.dropLast()) + "+00:00" : text
    }

    private static func write(_ day: CalendarDay, plus days: Int) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0) ?? .gmt
        let start = calendar.date(from: DateComponents(year: day.year, month: day.month, day: day.day)) ?? .now
        let moved = calendar.dateComponents([.year, .month, .day], from: calendar.date(byAdding: .day, value: days,
                                                                                      to: start) ?? start)
        return String(format: "%04d-%02d-%02d", moved.year ?? day.year, moved.month ?? day.month, moved.day ?? day.day)
    }

    /// With `live`, a list as it was 3:41 into the call: without the calls and offers that came after it began.
    private func asOfTheCall(_ body: Data, list key: String, time field: String) -> Data {
        guard live, let start = liveCall, var whole = object(body), let entries = whole[key] as? [[String: Any]]
        else { return body }
        whole[key] = entries.filter { entry in
            guard let time = (entry[field] as? String).flatMap(ZonedTime.init) else { return true }
            return time.date <= start.date
        }
        return (try? JSONSerialization.data(withJSONObject: whole)) ?? body
    }

    /// The list with no call in it, as before the first.
    private func noCalls(_ body: Data) -> Data {
        guard var whole = object(body) else { return body }
        whole["calls"] = [Any]()
        whole["more"] = false
        whole["before"] = NSNull()
        return (try? JSONSerialization.data(withJSONObject: whole)) ?? body
    }

    // MARK: - The files

    private func example(_ name: String) -> Data {
        bundle.url(forResource: name, withExtension: "json").flatMap { try? Data(contentsOf: $0) } ?? Data()
    }

    private func object(_ data: Data) -> [String: Any]? {
        (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    /// The view of a call: the example whose call has this id, the live one only when asked for.
    private func view(_ callID: String) -> Data? {
        let names = ["call-view", "call-view-live", "call-view-ai"]
        let views = names.map { example($0) }.filter { data in
            let call = object(data)?["call"] as? [String: Any]
            return call?["call_id"] as? String == callID
        }
        return views.first { data in
            let state = (object(data)?["call"] as? [String: Any])?["state"] as? String
            return (state == "live") == live
        } ?? views.first
    }

    /// 304 when the request already holds this body's version (If-None-Match), as the server answers.
    private func versioned(_ body: Data, for request: URLRequest) -> (Int, Data) {
        guard let version = object(body)?["version"] as? String,
              request.value(forHTTPHeaderField: "If-None-Match") == "\"\(version)\"" else { return (200, body) }
        return (304, Data())
    }

    private func answer(offer id: String) -> (Int, Data) {
        let body = example("offer-answer")
        let offer = object(body)?["offer"] as? [String: Any]
        return offer?["id"] as? String == id ? (200, body) : (404, Data())
    }

    // MARK: - The person's settings

    private func person() -> [String: Any] {
        if let me { return me }
        let loaded = object(example("me")) ?? [:]
        me = loaded
        return loaded
    }

    private func change(settings body: Data?) -> (Int, Data) {
        var person = person()
        guard var settings = person["settings"] as? [String: Any],
              let changes = body.flatMap({ object($0) }) else { return (400, Data("Not a settings object.".utf8)) }
        if let language = changes["language"] as? String, language != settings["language"] as? String {
            settings["language"] = language
            // A new language without a voice in the same request goes to that language's default.
            if changes["voice"] == nil, (settings["voice"] as? String)?.hasPrefix(language + "-") != true {
                settings["voice"] = NSNull()
            }
        }
        for key in ["voice", "call_model", "worker_model", "noise_cancellation"] where changes[key] != nil {
            settings[key] = changes[key]
        }
        person["settings"] = settings
        me = person
        let answer = (try? JSONSerialization.data(withJSONObject: ["settings": settings])) ?? Data()
        return (200, answer)
    }
}
#endif
