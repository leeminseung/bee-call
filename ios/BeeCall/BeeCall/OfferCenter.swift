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

    init(_ answer: ServerAnswer) {
        switch answer {
        case .ok: self = .accepted
        case .signedOut: self = .signedOut
        case .refused(404, _), .refused(409, _): self = .closed
        case .refused(let code, _): self = .failed(code)
        case .unreachable: self = .unreachable
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

/// The AI asks before it calls (voice/offers.py on the server): a notification such as "About “Planned a Saturday
/// hike”: free for a quick call at 9:00 PM today?" with three buttons, which also show on the lock screen and
/// Apple Watch. A button sends the answer to the token server, even while the app is closed; the server then calls
/// at once, at the accepted time, or not at all. Offers still open are listed on the app's first screen too, so
/// a missed notification can still be answered there.
@Observable
final class OfferCenter: NSObject, UNUserNotificationCenterDelegate {
    /// D's endpoints come from ContractServer: the token server, or in a Debug build the contract's examples.
    static let shared = OfferCenter()

    enum Choice: String { case now, later, decline }

    private(set) var open: [Offer] = [] // the open offers, in the order they were made (contract section 4)
    private(set) var notice: String? // one line under the list when an answer did not go through
    private(set) var answering: Set<String> = [] // offers whose answer is on its way; their buttons are off
    @ObservationIgnored private var version: String? // of the last GET /offers body, asked again with If-None-Match
    @ObservationIgnored private let server: ServerClient

    init(server: ServerClient = ContractServer.client) {
        self.server = server
        super.init()
    }

    // The buttons. Two sets of titles: the server picks one by the person's call language.
    private static let categories: Set<UNNotificationCategory> = [
        category("CALL_OFFER", later: "At the suggested time", now: "Call me now", decline: "Not now"),
        category("CALL_OFFER_KO", later: "제안한 시간에", now: "지금 전화해 줘", decline: "괜찮아요"),
    ]

    private static func category(_ id: String, later: String, now: String, decline: String) -> UNNotificationCategory {
        // No .foreground: an answer is a message to the server, so it works from the lock screen and the watch.
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
            if granted { UIApplication.shared.registerForRemoteNotifications() }
        }
    }

    func refresh() async {
        await PushTokens.shared.deliverIfNeeded(.alert) // the server may have been unreachable at launch
        guard var request = server.request("/offers") else { return }
        request.cachePolicy = .reloadIgnoringLocalCacheData
        if let version { request.setValue("\"\(version)\"", forHTTPHeaderField: "If-None-Match") }
        guard case .ok(200, let data) = await server.send(request), // a 304 keeps what is there
              let listing = try? Contract.decode(OpenOffers.self, from: data) else { return }
        version = listing.version
        if listing.offers != open { open = listing.offers }
        if let asked = cancelling, !listing.offers.contains(where: { $0.id == asked.id && $0.state == .booked }) {
            cancelling = nil // its room is being made, or it is gone: there is nothing left to cancel
        }
        Self.clearClosed(keeping: Set(listing.offers.map(\.id)))
    }

    /// GET /changes said the offers now have this version.
    func changed(to version: String) async {
        if version != self.version { await refresh() }
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
        let result = OfferAnswer(await server.send("/offers/\(offerID)", method: "POST",
                                                   json: ["choice": choice.rawValue]))
        notice = result.notice
        await refresh()
        return result
    }

    // MARK: - Cancelling a booked call

    /// The booked call whose cancel question is on screen. A tap on "Cancel call" only asks: the system alert names
    /// the time, and nothing goes to the server before the person confirms (the user's report of 2026-10-15, a
    /// booked call called off by a tap on a button that did not say what it did).
    private(set) var cancelling: Offer?

    func askToCancel(_ offer: Offer) {
        cancelling = offer
    }

    /// The person kept the call: nothing is sent.
    func keepBooked() {
        cancelling = nil
    }

    /// The person confirmed: the server calls the booked call off ("decline", until its room exists, contract
    /// section 4). The offer comes from the alert itself, since the alert may let go of `cancelling` first.
    func confirmCancel(_ offer: Offer) async {
        cancelling = nil
        await answer(offer.id, .decline)
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
        // A pretend call or offer: no notifications, no server.
        guard VoiceDemo.requested() == nil, DesignDemo.requested() == nil else { return true }
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

/// The system alert a booked call's "전화 취소" asks first: it names the time, and nothing goes to the server
/// before the person confirms (BookedCall.swift). Attached where the booked call's row is.
struct CancelBookedCallAlert: ViewModifier {
    let offers: OfferCenter
    let language: String

    func body(content: Content) -> some View {
        content.alert(offers.cancelling?.cancelQuestion(language: language) ?? "",
                      isPresented: Binding(get: { offers.cancelling != nil }, set: { if !$0 { offers.keepBooked() } }),
                      presenting: offers.cancelling) { offer in
            Button(Offer.cancelButton(language: language), role: .destructive) {
                Task { await offers.confirmCancel(offer) }
            }
            Button(Offer.keepButton(language: language), role: .cancel) { offers.keepBooked() }
        } message: { offer in
            Text(offer.topic)
        }
    }
}
