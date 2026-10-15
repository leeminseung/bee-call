#if DEBUG
import Foundation

/// Debug builds only: launched with `-offerDemo booked`, the app shows its first screen with one call booked for
/// 19:00 (today, or tomorrow once that is less than ten minutes away), answered by a stand-in for the server, so
/// the booked call's row and its cancel alert can be seen and screenshot without a server or a sign-in. A
/// confirmed cancel takes the row away, as the server's answer would. Launch arguments of that form land in
/// UserDefaults.
enum OfferDemo {
    static func requested(in store: UserDefaults = .standard) -> Bool {
        store.string(forKey: "offerDemo") == "booked"
    }

    /// Answers GET /offers and POST /offers/<id> as voice/token_server.py does, for one call booked in a call.
    final class StandIn: HTTPTransport {
        private let offer: [String: Any]
        private var cancelled = false

        init(language: String = AppSettings(.standard).language, now: Date = .now, zone: TimeZone = .current) {
            var calendar = Calendar(identifier: .gregorian)
            calendar.timeZone = zone
            var at = calendar.date(bySettingHour: 19, minute: 0, second: 0, of: now) ?? now
            if at.timeIntervalSince(now) < 600 { at = calendar.date(byAdding: .day, value: 1, to: at) ?? at }
            let iso = ISO8601DateFormatter()
            iso.timeZone = zone // written on the person's clock, as the server writes call_at
            offer = ["id": "20261015-183000-demo", "state": "booked", "source": "call", "kind": NSNull(),
                     "topic": language == "ko" ? "토요일 등산 계획" : "Saturday hike plan",
                     "suggested_at": NSNull(), "suggested_epoch": NSNull(),
                     "call_at": iso.string(from: at), "call_epoch": Int(at.timeIntervalSince1970)]
        }

        func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
            if request.httpMethod == "POST" { cancelled = true } // the one answer a booked call takes: decline
            let body: [String: Any] = request.httpMethod == "POST"
                ? ["offer": offer.merging(["state": "declined"]) { $1 }]
                : ["offers": cancelled ? [] : [offer]]
            let response = HTTPURLResponse(url: request.url ?? URL(fileURLWithPath: "/"), statusCode: 200,
                                           httpVersion: "HTTP/1.1", headerFields: nil)
            return (try JSONSerialization.data(withJSONObject: body), response ?? URLResponse())
        }
    }
}
#endif
