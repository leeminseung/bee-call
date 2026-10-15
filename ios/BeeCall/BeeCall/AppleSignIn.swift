import CryptoKit
import Foundation
import Observation

/// Sign in with Apple against the server (voice/token_server.py). The server hands out a one-time value
/// (GET /auth/nonce); Apple puts its SHA-256 into the identity token, and the server checks the value it gets
/// back with that token (POST /auth/apple). So each attempt takes one value at the tap and sends that same value
/// to the server, and the button waits while no value is ready.
@Observable
final class AppleSignIn {
    enum Outcome: Equatable {
        case signedIn(token: String, person: String)
        case waiting(code: String) // the server's operator has not approved this person yet
        case cancelled
        case failed(String)
    }

    /// A value is waiting for the button; the button waits until one is.
    private(set) var isReady = false
    @ObservationIgnored private var ready: String? {
        didSet { isReady = ready != nil }
    }
    @ObservationIgnored private var taken: String? // the value Apple was given, for the server
    @ObservationIgnored private var readyAt: Date? // when the ready value came
    @ObservationIgnored private let server: ServerClient
    @ObservationIgnored private let clock: () -> Date

    /// The server forgets a value after 10 minutes (voice/apple_signin.py). One kept 8 minutes is let go, which
    /// leaves time for Apple's sheet, so the first tap after a long wait for approval does not fail.
    static let lifetime: TimeInterval = 8 * 60

    init(server: ServerClient = ServerClient(), clock: @escaping () -> Date = Date.init) {
        self.server = server
        self.clock = clock
    }

    /// Lets go of a value kept too long: the button then waits for a new one.
    func dropIfOld() {
        guard ready != nil, let readyAt, clock().timeIntervalSince(readyAt) >= Self.lifetime else { return }
        ready = nil
    }

    /// Fetches a value for the next attempt; false when the server could not give one.
    func prepare() async -> Bool {
        guard case .ok(200, let data) = await server.send("/auth/nonce"),
              let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let value = body["nonce"] as? String else { return false }
        ready = value
        readyAt = clock()
        return true
    }

    /// At the tap: the value's SHA-256 for Apple's request. The value is taken: the same one goes to the server
    /// with Apple's answer, and the next attempt needs a new one. Nil when no value is ready.
    func takeForRequest() -> String? {
        guard let value = ready else { return nil }
        ready = nil
        taken = value
        return SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// Apple answered: with its identity token, or without one (`cancelled` when the person closed Apple's sheet).
    func finish(identityToken: String?, cancelled: Bool) async -> Outcome {
        let value = taken
        taken = nil
        guard let identityToken, let value else {
            return cancelled ? .cancelled : .failed("Sign in with Apple did not finish.")
        }
        let answer = await server.send("/auth/apple", method: "POST",
                                       json: ["identity_token": identityToken, "nonce": value])
        switch answer {
        case .unreachable:
            return .failed("The server cannot be reached right now.")
        case .ok(200, let data):
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            if let token = body["token"] as? String, let person = body["person"] as? String {
                return .signedIn(token: token, person: person)
            }
        case .refused(403, let data):
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            if let code = body["waiting"] as? String { return .waiting(code: code) }
        default:
            break
        }
        return .failed("The server did not accept the sign-in.")
    }
}
