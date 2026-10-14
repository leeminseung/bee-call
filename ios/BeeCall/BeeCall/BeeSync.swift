import Foundation
import Observation
import UIKit

/// What to do so that Bee matches "Use Bee" in the Settings app.
enum BeeStep: Equatable {
    /// Ask the server for Bee's approval link (POST /bee/connect) and open it.
    case connect
    /// Cancel a pending approval, or log Bee out (POST /bee/disconnect).
    case disconnect
    /// Nothing to send; show this status.
    case show(String)

    static let notConnected =
        "Bee is not connected. To try again, turn Use Bee off, come back to Bee Call, and turn it on again."

    /// The whole decision, from "Use Bee" (`wanted`), the server's state (`connected`, and `connecting` while an
    /// approval is pending) and whether the switch changed since the app last acted on it. Off stops Bee in
    /// either state, so a "no" given while an approval is pending is honoured. Bee's approval page opens only
    /// on the first foreground after the switch changed, so a lapsed approval does not open it again and again.
    static func next(wanted: Bool, connected: Bool, connecting: Bool, switchChanged: Bool) -> BeeStep {
        switch (wanted, connected, connecting) {
        case (false, true, _), (false, false, true): .disconnect
        case (false, false, false): .show("")
        case (true, true, _): .show("Bee connected")
        case (true, false, true): .show("Waiting for your approval in the Bee app")
        case (true, false, false): switchChanged ? .connect : .show(notConnected)
        }
    }
}

/// Brings Bee in line with "Use Bee", on coming to the foreground and after signing in. The server says in
/// GET /me whether Bee is connected, or an approval is pending.
@Observable
final class BeeSync {
    private(set) var status = "" // shown under the call status when the person uses Bee
    @ObservationIgnored private let store: UserDefaults
    @ObservationIgnored private let transport: any HTTPTransport
    @ObservationIgnored private let open: (URL) async -> Void
    @ObservationIgnored private let noteStatus: (Int) -> Void
    /// The sync still running, if any: syncs run one at a time, and each sees what the one before did.
    @ObservationIgnored private var running: Task<Void, Never>?
    /// The value of "Use Bee" the app last acted on, kept across launches: the switch changed when it differs.
    private static let handledKey = "use_bee_handled"

    init(store: UserDefaults = .standard, transport: any HTTPTransport = URLSession.shared,
         open: @escaping (URL) async -> Void = { _ = await UIApplication.shared.open($0) },
         noteStatus: @escaping (Int) -> Void) {
        self.store = store
        self.transport = transport
        self.open = open
        self.noteStatus = noteStatus
    }

    func clearStatus() {
        status = ""
    }

    /// After a sign-in, which may be another person's: Bee's page may open again for the switch as it is.
    func forgetHandledSwitch() {
        store.removeObject(forKey: Self.handledKey)
    }

    func sync() async {
        let previous = running
        let next = Task {
            await previous?.value
            await syncOnce()
        }
        running = next
        await next.value
    }

    private func syncOnce() async {
        let wanted = AppSettings(store).useBee
        guard let request = Server.request("/me"), let (data, response) = try? await transport.send(request) else {
            return // unreachable: the next foreground tries again
        }
        noteStatus(response.httpStatus)
        guard response.httpStatus == 200,
              let me = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let settings = me["settings"] as? [String: Any] else { return }
        let handled = store.object(forKey: Self.handledKey) as? Bool
        let step = BeeStep.next(wanted: wanted, connected: settings["bee"] as? Bool ?? false,
                                connecting: me["bee_connecting"] as? Bool ?? false, switchChanged: handled != wanted)
        switch step {
        case .show(let text):
            status = text
            store.set(wanted, forKey: Self.handledKey)
        case .connect:
            status = "Connecting Bee: approve Bee Call in the Bee app"
            guard let link = await approvalLink() else {
                status = "Bee could not be connected right now" // the next foreground tries again
                return
            }
            store.set(true, forKey: Self.handledKey)
            await open(link) // Bee's own approval page
        case .disconnect:
            status = "Turning Bee off…"
            if await disconnect() {
                status = ""
                store.set(false, forKey: Self.handledKey)
            } else {
                status = "Bee could not be turned off right now. Bee Call tries again when you come back."
            }
        }
    }

    private func approvalLink() async -> URL? {
        guard let request = Server.request("/bee/connect", method: "POST", timeout: 40),
              let (body, response) = try? await transport.send(request) else { return nil }
        noteStatus(response.httpStatus)
        guard response.httpStatus == 200,
              let link = (try? JSONSerialization.jsonObject(with: body) as? [String: Any])?["link"] as? String
        else { return nil }
        return URL(string: link)
    }

    /// True once the server confirmed (204): Bee is logged out and any pending approval is cancelled.
    private func disconnect() async -> Bool {
        guard let request = Server.request("/bee/disconnect", method: "POST"),
              let (_, response) = try? await transport.send(request) else { return false }
        noteStatus(response.httpStatus)
        return response.httpStatus == 204
    }
}
