import SwiftUI
import UserNotifications

/// The AI asks before it calls (voice/offers.py on the Mac): a notification such as "About “Planned a Saturday
/// hike”: free for a 5-minute call at 9:00 PM today?" with three buttons, which also show on the lock screen and
/// Apple Watch. A button sends the answer to the token server, even while the app is closed; the Mac then calls
/// at once, at the accepted time, or not at all. Offers still open are listed on the app's first screen too, so
/// a missed notification can still be answered there.
@Observable
final class OfferCenter: NSObject, UNUserNotificationCenterDelegate {
    static let shared = OfferCenter()

    struct Offer: Decodable, Identifiable, Equatable {
        let id: String
        let state: String // asked, now, booked, calling
        let topic: String?
        let kind: String?
        let source: String
        let suggestedEpoch: Int?
        let callAtEpoch: Int?

        enum CodingKeys: String, CodingKey {
            case id, state, topic, kind, source
            case suggestedEpoch = "suggested_epoch"
            case callAtEpoch = "call_at_epoch"
        }

        var title: String {
            if let topic, !topic.isEmpty { return topic }
            return kind == "voice note" ? "Your new voice note" : "Your day"
        }
    }

    enum Choice: String { case now, later, decline }

    private(set) var open: [Offer] = []
    private(set) var notificationsOn = false

    // The buttons. Two sets of titles, picked by the Mac's call language (CALL_LANGUAGE).
    private static let categories: Set<UNNotificationCategory> = [
        category("CALL_OFFER", later: "At the suggested time", now: "Call me now", decline: "Not now"),
        category("CALL_OFFER_KO", later: "제안한 시간에", now: "지금 전화해 줘", decline: "괜찮아요"),
    ]

    private static func category(_ id: String, later: String, now: String, decline: String) -> UNNotificationCategory {
        // No .foreground: an answer is a message to the Mac, so it works from the lock screen and the watch.
        UNNotificationCategory(identifier: id, actions: [
            UNNotificationAction(identifier: Choice.later.rawValue, title: later),
            UNNotificationAction(identifier: Choice.now.rawValue, title: now),
            UNNotificationAction(identifier: Choice.decline.rawValue, title: decline, options: [.destructive]),
        ], intentIdentifiers: [])
    }

    /// At launch: the buttons, this object as the notification delegate, and the permission prompt (once).
    func setUp() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.setNotificationCategories(Self.categories)
        Task {
            let granted = (try? await center.requestAuthorization(options: [.alert, .sound, .badge])) ?? false
            notificationsOn = granted
            if granted { UIApplication.shared.registerForRemoteNotifications() }
        }
    }

    /// iOS's token for plain notifications (not the VoIP one), handed on by the app delegate.
    func register(deviceToken: Data) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        Task {
            guard let url = endpoint("/push") else { return }
            var request = URLRequest(url: url, timeoutInterval: 10)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            // Builds run from Xcode get pushes from Apple's development service, like the VoIP push.
            request.httpBody = try? JSONSerialization.data(withJSONObject: ["token": token, "environment": "development"])
            _ = try? await URLSession.shared.data(for: request)
        }
    }

    func refresh() async {
        guard let url = endpoint("/offers"),
              let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return }
        struct Listing: Decodable { let offers: [Offer] }
        if let listing = try? JSONDecoder().decode(Listing.self, from: data), listing.offers != open {
            open = listing.offers
        }
    }

    func answer(_ offerID: String, _ choice: Choice) async {
        guard let url = endpoint("/offers/\(offerID)") else { return }
        var request = URLRequest(url: url, timeoutInterval: 10)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["choice": choice.rawValue])
        _ = try? await URLSession.shared.data(for: request)
        await refresh()
    }

    private func endpoint(_ path: String) -> URL? {
        RingStream.endpoint(path, tokenServerURL: UserDefaults.standard.string(forKey: "tokenServerURL") ?? "")
    }

    // MARK: - UNUserNotificationCenterDelegate

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        await refresh() // the new offer shows on the first screen too
        return [.banner, .list, .sound]
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
                                            didReceive response: UNNotificationResponse) async {
        let action = response.actionIdentifier
        let offerID = response.notification.request.content.userInfo["offer"] as? String
        guard let offerID else { return }
        if let choice = Choice(rawValue: action) {
            await answer(offerID, choice) // iOS keeps the app running until this returns
        } else {
            await refresh() // the notification itself was tapped: the app opens on the offer list
        }
    }
}

/// Plain push registration lands on the app delegate; the rest lives in OfferCenter.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        OfferCenter.shared.setUp() // before launch ends, so a button tapped while the app was closed is delivered
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        OfferCenter.shared.register(deviceToken: deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: any Error) {
        print("Notification registration failed: \(error.localizedDescription)")
    }
}

/// The offers on the first screen: what the AI would like to call about, and when.
struct OfferList: View {
    let offers: OfferCenter

    var body: some View {
        if !offers.open.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(offers.open) { offer in
                    OfferRow(offer: offer) { choice in
                        Task { await offers.answer(offer.id, choice) }
                    }
                }
            }
        }
    }
}

private struct OfferRow: View {
    let offer: OfferCenter.Offer
    let answer: (OfferCenter.Choice) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(offer.title)
                .font(.headline)
                .lineLimit(2)
            Text(status)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            HStack(spacing: 8) {
                switch offer.state {
                case "asked":
                    if let at = offer.suggestedEpoch {
                        Button("At \(Self.time(at))") { answer(.later) }.buttonStyle(.borderedProminent)
                    }
                    Button("Now") { answer(.now) }.buttonStyle(.bordered)
                    Button("Not now", role: .destructive) { answer(.decline) }.buttonStyle(.bordered)
                case "booked":
                    Button("Call off", role: .destructive) { answer(.decline) }.buttonStyle(.bordered)
                default:
                    EmptyView()
                }
            }
            .controlSize(.small)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 12))
    }

    private var status: String {
        switch offer.state {
        case "asked":
            return offer.suggestedEpoch.map { "Bee Call asks: a 5-minute call \(Self.day($0)) at \(Self.time($0))?" }
                ?? "Bee Call asks for a 5-minute call"
        case "booked":
            let who = offer.source == "call" ? "You asked for a call back" : "You said yes"
            return offer.callAtEpoch.map { "\(who): \(Self.day($0)) at \(Self.time($0))" } ?? who
        default:
            return "Calling you now…"
        }
    }

    private static func time(_ epoch: Int) -> String {
        Date(timeIntervalSince1970: TimeInterval(epoch)).formatted(date: .omitted, time: .shortened)
    }

    private static func day(_ epoch: Int) -> String {
        let date = Date(timeIntervalSince1970: TimeInterval(epoch))
        return Calendar.current.isDateInToday(date) ? "today" : Calendar.current.isDateInTomorrow(date) ? "tomorrow"
            : date.formatted(.dateTime.weekday(.wide))
    }
}
