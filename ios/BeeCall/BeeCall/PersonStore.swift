import Foundation
import Observation

/// The signed-in person as the server holds them (contract section 6): GET /me, and the settings the app owns after
/// D (the call language, the voice, the call and worker models, noise cancellation), sent with PUT /settings when the
/// person picks them (never again from the Settings app). The screens' words follow the call language, which is kept in the defaults ("language") for
/// them. Through ContractServer, like the rest of design D.
@Observable
final class PersonStore {
    private(set) var me: Me?
    /// The settings as the server last said them: from GET /me, or from its answer to a pick, which is kept even
    /// before GET /me has come (second review of the D wiring, finding 4).
    private(set) var settings: PersonSettings?
    /// The last pick the server did not take, in words.
    private(set) var problem: String?
    @ObservationIgnored private let server: ServerClient
    @ObservationIgnored private let store: UserDefaults
    @ObservationIgnored private var picks = 0 // picks sent so far: a read begun before one does not undo it

    init(server: ServerClient = ContractServer.client, store: UserDefaults = .standard) {
        self.server = server
        self.store = store
    }

    func refresh() async {
        let picksBefore = picks
        guard case .ok(200, let data) = await server.send("/me"),
              let me = try? Contract.decode(Me.self, from: data) else { return }
        self.me = me
        guard picks == picksBefore else { return } // a pick went out meanwhile: its answer is newer
        settings = me.settings
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

    /// Sends the model picked for a role, by its catalog id (GET /models).
    func choose(model id: String, for role: ModelRole) async -> Bool {
        await change([role == .worker ? "worker_model" : "call_model": id])
    }

    /// Noise cancellation on the person's audio, a switch in the settings sheet; the server reads it when a call
    /// starts, so it applies from the next call.
    func choose(noiseCancellation on: Bool) async -> Bool {
        await change(["noise_cancellation": on])
    }

    private func change(_ fields: [String: Any]) async -> Bool {
        picks += 1
        switch await server.send("/settings", method: "PUT", json: fields) {
        case .ok(200, let data):
            guard let answer = try? Contract.decode(SettingsAnswer.self, from: data) else {
                problem = "Bee Call's server sent something this app cannot read."
                return false
            }
            settings = answer.settings
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
