import Foundation
import Observation

/// The signed-in person as the server holds them (contract section 6): GET /me, and the two settings the app owns
/// after D, the call language and the voice, sent with PUT /settings when the person picks them (never again from
/// the Settings app). The screens' words follow the call language, which is kept in the defaults ("language") for
/// them. Through ContractServer, like the rest of design D.
@Observable
final class PersonStore {
    private(set) var me: Me?
    /// The last pick the server did not take, in words.
    private(set) var problem: String?
    @ObservationIgnored private let server: ServerClient
    @ObservationIgnored private let store: UserDefaults

    init(server: ServerClient = ContractServer.client, store: UserDefaults = .standard) {
        self.server = server
        self.store = store
    }

    func refresh() async {
        guard case .ok(200, let data) = await server.send("/me"),
              let me = try? Contract.decode(Me.self, from: data) else { return }
        self.me = me
        follow(me.settings.language)
    }

    /// The line under the name in the home's header: Bee connected, or its approval waiting in the Bee app.
    func beeLine(korean: Bool) -> String? {
        guard let me else { return nil }
        if me.settings.bee { return korean ? "Bee 연결됨" : "Bee connected" }
        if me.beeConnecting { return korean ? "Bee 앱에서 승인을 기다리는 중" : "Waiting for approval in the Bee app" }
        return nil
    }

    /// Sends the call language; the server answers with the settings it keeps (a new language without a voice
    /// goes back to that language's default). False when it was not saved.
    func choose(language: CallLanguage) async -> Bool {
        await change(["language": language.rawValue])
    }

    func choose(voice: String) async -> Bool {
        await change(["voice": voice])
    }

    private func change(_ fields: [String: Any]) async -> Bool {
        switch await server.send("/settings", method: "PUT", json: fields) {
        case .ok(200, let data):
            guard let answer = try? Contract.decode(SettingsAnswer.self, from: data) else {
                problem = "Bee Call's server sent something this app cannot read."
                return false
            }
            if let me {
                self.me = Me(person: me.person, settings: answer.settings, spentTodayUSD: me.spentTodayUSD,
                             beeConnecting: me.beeConnecting)
            }
            follow(answer.settings.language)
            problem = nil
            return true
        case .refused(_, let data):
            let reason = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            problem = reason.isEmpty ? "Bee Call's server refused it." : reason
            return false
        case .signedOut:
            problem = "Your sign-in ended."
            return false
        case .unreachable, .ok:
            problem = "Bee Call's server cannot be reached."
            return false
        }
    }

    /// The screens' words follow the call language.
    private func follow(_ language: CallLanguage) {
        guard language != .unrecognized else { return }
        store.set(language.rawValue, forKey: "language")
    }
}
