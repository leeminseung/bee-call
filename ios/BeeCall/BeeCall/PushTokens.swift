import Foundation
import Observation

/// This phone's two push tokens and their delivery to the server: the alert token, for the offers that ask
/// before a call (POST /push), and the VoIP token, which rings the AI's calls on a closed or locked phone
/// (POST /voip). Both belong to the signed-in person on the server (contract section 3).
@Observable
final class PushTokens {
    static let shared = PushTokens()

    enum Kind: String {
        case alert = "/push"
        case voip = "/voip"
    }

    /// The server has this phone's VoIP token, so the AI's calls ring also while the app is closed.
    private(set) var voipRegistered = false
    @ObservationIgnored private var tokens: [Kind: String] = [:]
    @ObservationIgnored private var delivered: Set<Kind> = []
    @ObservationIgnored private let server: ServerClient
    @ObservationIgnored private let environment: String

    init(server: ServerClient = ServerClient(), environment: String = Server.pushEnvironment) {
        self.server = server
        self.environment = environment
    }

    /// A new token from iOS: kept, and sent at once.
    func received(_ token: Data, kind: Kind) async {
        tokens[kind] = token.map { String(format: "%02x", $0) }.joined()
        delivered.remove(kind)
        await deliver(kind)
    }

    /// iOS took the token back.
    func invalidated(_ kind: Kind) {
        tokens[kind] = nil
        delivered.remove(kind)
        if kind == .voip { voipRegistered = false }
    }

    /// Sends the token unless the server already has it (it may have been unreachable at launch).
    func deliverIfNeeded(_ kind: Kind) async {
        guard !delivered.contains(kind) else { return }
        await deliver(kind)
    }

    /// Sends the token now.
    func deliver(_ kind: Kind) async {
        guard let token = tokens[kind],
              let request = server.request(kind.rawValue, method: "POST",
                                           json: ["token": token, "environment": environment]) else { return }
        let accepted = (try? await server.transport.send(request))?.1.httpStatus == 204
        if accepted { delivered.insert(kind) } else { delivered.remove(kind) }
        if kind == .voip { voipRegistered = accepted }
    }

    /// After every sign-in, both tokens go again: the server gives each token to the person who sends it, so
    /// offers and AI calls follow the person now signed in on this phone (contract section 3).
    func signedIn() async {
        await deliver(.alert)
        await deliver(.voip)
    }
}
