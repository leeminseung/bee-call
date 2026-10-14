import Foundation
import Observation
import UIKit

/// Brings Bee in line with "Use Bee" in the Settings app: POST /bee/connect and Bee's own approval page to
/// connect, POST /bee/disconnect to stop. The server says in GET /me whether Bee is connected, or an approval
/// is pending.
@Observable
final class BeeSync {
    private(set) var status = "" // shown under the call status when the person uses Bee
    @ObservationIgnored private let store: UserDefaults
    @ObservationIgnored private let transport: any HTTPTransport
    @ObservationIgnored private let open: (URL) async -> Void
    @ObservationIgnored private let noteStatus: (Int) -> Void

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

    func sync() async {
        let wanted = AppSettings(store).useBee
        guard let request = Server.request("/me"), let (data, response) = try? await transport.send(request),
              response.httpStatus == 200,
              let me = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let settings = me["settings"] as? [String: Any] else { return }
        let connected = settings["bee"] as? Bool ?? false
        let connecting = me["bee_connecting"] as? Bool ?? false
        if wanted && !connected && !connecting {
            status = "Connecting Bee: approve Bee Call in the Bee app"
            guard let start = Server.request("/bee/connect", method: "POST", timeout: 40),
                  let (body, answer) = try? await transport.send(start),
                  answer.httpStatus == 200,
                  let link = (try? JSONSerialization.jsonObject(with: body) as? [String: Any])?["link"] as? String,
                  let url = URL(string: link) else {
                status = "Bee could not be connected right now"
                return
            }
            await open(url) // Bee's own approval page
        } else if !wanted && connected {
            if let stop = Server.request("/bee/disconnect", method: "POST") {
                _ = try? await transport.send(stop)
            }
            status = ""
        } else {
            status = connected ? "Bee connected" : connecting ? "Waiting for your approval in the Bee app" : ""
        }
    }
}
