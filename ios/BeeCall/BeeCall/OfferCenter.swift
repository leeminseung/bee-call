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
    private(set) var notice: String? // one line under the list when an answer did not go through
    @ObservationIgnored private var deviceToken: String?
    @ObservationIgnored private var tokenSent = false

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
        self.deviceToken = deviceToken.map { String(format: "%02x", $0) }.joined()
        tokenSent = false
        Task { await sendToken() }
    }

    /// Sent at launch, after a sign-in (the token belongs to the person), and again on each refresh until the
    /// server has it (it may be unreachable at launch).
    func sendToken(again: Bool = false) async {
        if again { tokenSent = false }
        guard let token = deviceToken, !tokenSent,
              let request = Server.request("/push", method: "POST",
                                           json: ["token": token, "environment": Server.pushEnvironment]) else { return }
        let response = try? await URLSession.shared.data(for: request)
        tokenSent = (response?.1 as? HTTPURLResponse)?.statusCode == 204
    }

    func refresh() async {
        await sendToken()
        guard let request = Server.request("/offers"),
              let (data, response) = try? await URLSession.shared.data(for: request) else { return }
        Server.shared.noteStatus((response as? HTTPURLResponse)?.statusCode ?? 0)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { return }
        struct Listing: Decodable { let offers: [Offer] }
        guard let listing = try? JSONDecoder().decode(Listing.self, from: data) else { return }
        if listing.offers != open { open = listing.offers }
        Self.clearClosed(keeping: Set(listing.offers.map(\.id)))
    }

    /// Takes away notifications whose offer has closed, so no button is left that can only fail.
    private nonisolated static func clearClosed(keeping openIDs: Set<String>) {
        UNUserNotificationCenter.current().getDeliveredNotifications { delivered in
            let closed = delivered.filter { note in
                (note.request.content.userInfo["offer"] as? String).map { !openIDs.contains($0) } ?? false
            }.map(\.request.identifier)
            if !closed.isEmpty { UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: closed) }
        }
    }

    /// Sends the answer; false when the token server refused it (the offer had closed) or could not be reached.
    @discardableResult
    func answer(_ offerID: String, _ choice: Choice) async -> Bool {
        guard let request = Server.request("/offers/\(offerID)", method: "POST", json: ["choice": choice.rawValue])
        else { return false }
        let response = try? await URLSession.shared.data(for: request)
        let code = (response?.1 as? HTTPURLResponse)?.statusCode
        let accepted = code == 200
        notice = accepted ? nil : code == nil ? "Couldn't reach Bee Call. Try again from here."
            : "That offer has closed. You can call Bee Call any time."
        await refresh()
        return accepted
    }

    /// After a button on a notification did not go through: say so, since the notification itself is gone.
    private func tellAnswerFailed() async {
        let content = UNMutableNotificationContent()
        content.title = "Bee Call"
        content.body = notice ?? "That answer did not go through."
        try? await UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "answer-failed", content: content, trigger: nil))
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
            // iOS keeps the app running until this returns.
            if await !answer(offerID, choice) { await tellAnswerFailed() }
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
        if !offers.open.isEmpty || offers.notice != nil {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(offers.open) { offer in
                    OfferRow(offer: offer) { choice in
                        Task { await offers.answer(offer.id, choice) }
                    }
                }
                if let notice = offers.notice {
                    Text(notice)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
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
