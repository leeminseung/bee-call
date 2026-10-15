import Foundation
import Observation

/// The signed-in person as the server holds them (contract section 6): GET /me, for the call language and voice
/// the app owns, and for "Bee 연결됨" in the home's header. Through ContractServer, like the rest of design D.
@Observable
final class PersonStore {
    private(set) var me: Me?
    @ObservationIgnored private let server: ServerClient

    init(server: ServerClient = ContractServer.client) {
        self.server = server
    }

    func refresh() async {
        guard case .ok(200, let data) = await server.send("/me"),
              let me = try? Contract.decode(Me.self, from: data) else { return }
        self.me = me
    }

    /// The line under the name in the home's header: Bee connected, or its approval waiting in the Bee app.
    func beeLine(korean: Bool) -> String? {
        guard let me else { return nil }
        if me.settings.bee { return korean ? "Bee 연결됨" : "Bee connected" }
        if me.beeConnecting { return korean ? "Bee 앱에서 승인을 기다리는 중" : "Waiting for approval in the Bee app" }
        return nil
    }
}
