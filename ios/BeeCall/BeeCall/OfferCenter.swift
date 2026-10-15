import SwiftUI
import UserNotifications

/// What became of an answer to an offer: the one place that reads the server's reply to POST /offers/<id>
/// (contract section 4), and what to tell the person.
enum OfferAnswer: Equatable {
    case accepted
    case signedOut // 401: the sign-in has ended
    case closed // 404 or 409: no such offer, or it closed or was answered already
    case unreachable // no reply at all
    case failed(Int) // any other reply, such as a 502 while the server restarts

    init(status: Int?) {
        switch status {
        case nil: self = .unreachable
        case let code? where (200..<300).contains(code): self = .accepted
        case 401?: self = .signedOut
        case 404?, 409?: self = .closed
        case let code?: self = .failed(code)
        }
    }

    /// The line under the offer list, or the notification after a button's answer did not go through.
    var notice: String? {
        switch self {
        case .accepted: nil
        case .signedOut: "Your sign-in ended. Sign in again to answer."
        case .closed: "That offer has closed. You can call Bee Call any time."
        case .unreachable: "Couldn't reach Bee Call. Try again from here."
        case .failed(let code): "Bee Call couldn't take that answer (error \(code)). Try again from here."
        }
    }
}

/// The AI asks before it calls (voice/offers.py on the Mac): a notification such as "About “Planned a Saturday
/// hike”: free for a quick call at 9:00 PM today?" with three buttons, which also show on the lock screen and
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

        enum CodingKeys: String, CodingKey { // as voice/offers.py Offer.view() names them
            case id, state, topic, kind, source
            case suggestedEpoch = "suggested_epoch"
            case callAtEpoch = "call_epoch"
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
    private(set) var answering: Set<String> = [] // offers whose answer is on its way; their buttons are off
    @ObservationIgnored private let transport: any HTTPTransport
    @ObservationIgnored private let noteStatus: (Int) -> Void

    init(transport: any HTTPTransport = URLSession.shared,
         noteStatus: @escaping (Int) -> Void = { Server.shared.noteStatus($0) }) {
        self.transport = transport
        self.noteStatus = noteStatus
        super.init()
    }

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

    func refresh() async {
        await PushTokens.shared.deliverIfNeeded(.alert) // the server may have been unreachable at launch
        guard let request = Server.request("/offers"),
              let (data, response) = try? await transport.send(request) else { return }
        noteStatus(response.httpStatus)
        guard response.httpStatus == 200 else { return }
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

    /// Sends the answer, and says under the list what became of it. Nil when an answer to this offer is already
    /// on its way (a double tap): one answer per offer at a time.
    @discardableResult
    func answer(_ offerID: String, _ choice: Choice) async -> OfferAnswer? {
        guard answering.insert(offerID).inserted else { return nil }
        defer { answering.remove(offerID) } // after the refresh, so the row does not come back for a moment
        guard let request = Server.request("/offers/\(offerID)", method: "POST", json: ["choice": choice.rawValue])
        else { return .unreachable }
        let response = try? await transport.send(request)
        let result = OfferAnswer(status: response?.1.httpStatus)
        if result == .signedOut { noteStatus(401) }
        notice = result.notice
        await refresh()
        return result
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
            if let result = await answer(offerID, choice), result != .accepted { await tellAnswerFailed() }
        } else {
            await refresh() // the notification itself was tapped: the app opens on the offer list
        }
    }
}

/// Plain push registration lands on the app delegate; the rest lives in OfferCenter.
final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        #if DEBUG
        guard VoiceDemo.requested() == nil else { return true } // a pretend call: no notifications, no server
        #endif
        OfferCenter.shared.setUp() // before launch ends, so a button tapped while the app was closed is delivered
        // Every kind of launch, also one for a VoIP push or a notification button, which never reach the
        // foreground: the server must not act on stale switches (contract section 1).
        Server.shared.appLaunched()
        return true
    }

    /// iOS's token for plain notifications (not the VoIP one).
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { await PushTokens.shared.received(deviceToken, kind: .alert) }
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
                    OfferRow(offer: offer, isAnswering: offers.answering.contains(offer.id)) { choice in
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
    let isAnswering: Bool // the answer is on its way: the buttons wait for it
    let answer: (OfferCenter.Choice) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(offer.title)
                .font(.headline)
                .lineLimit(2)
            Text(offer.status)
                .font(.subheadline)
                .foregroundStyle(.secondary)
            HStack(spacing: 8) {
                switch offer.state {
                case "asked":
                    if let at = offer.suggestedEpoch {
                        Button("At \(OfferCenter.Offer.time(at))") { answer(.later) }.buttonStyle(.borderedProminent)
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
            .disabled(isAnswering)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 12))
    }
}

extension OfferCenter.Offer {
    /// The line under the offer's title in the app's list.
    var status: String {
        switch state {
        case "asked":
            return suggestedEpoch.map { "Bee Call asks: a quick call \(Self.day($0)) at \(Self.time($0))?" }
                ?? "Bee Call asks for a quick call"
        case "booked":
            let who = source == "call" ? "You asked for a call back" : "You said yes"
            return callAtEpoch.map { "\(who): \(Self.day($0)) at \(Self.time($0))" } ?? who
        default:
            return "Calling you now…"
        }
    }

    static func time(_ epoch: Int) -> String {
        Date(timeIntervalSince1970: TimeInterval(epoch)).formatted(date: .omitted, time: .shortened)
    }

    private static func day(_ epoch: Int) -> String {
        let date = Date(timeIntervalSince1970: TimeInterval(epoch))
        return Calendar.current.isDateInToday(date) ? "today" : Calendar.current.isDateInTomorrow(date) ? "tomorrow"
            : date.formatted(.dateTime.weekday(.wide))
    }
}
