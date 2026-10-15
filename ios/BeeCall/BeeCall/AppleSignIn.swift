import CryptoKit
import Foundation
import Observation

/// Sign in with Apple against the server (voice/token_server.py). The server hands out a one-time value
/// (GET /auth/nonce); Apple puts its SHA-256 into the identity token, and the server checks the value it gets
/// back with that token (POST /auth/apple).
@Observable
final class AppleSignIn {
    enum Outcome: Equatable {
        case signedIn(token: String, person: String)
        case waiting(code: String) // the server's operator has not approved this person yet
        case cancelled
        case failed(String)
    }

    @ObservationIgnored private var value: String?
    @ObservationIgnored private let transport: any HTTPTransport

    init(transport: any HTTPTransport = URLSession.shared) {
        self.transport = transport
    }

    /// Fetches a one-time value; false when the server could not give one.
    func prepare() async -> Bool {
        guard let request = Server.request("/auth/nonce"),
              let (data, response) = try? await transport.send(request), response.httpStatus == 200,
              let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            value = nil
            return false
        }
        value = body["nonce"] as? String
        return true
    }

    /// For Apple's request: the value's SHA-256, which Apple puts into the identity token.
    func hashForRequest() -> String {
        SHA256.hash(data: Data((value ?? "").utf8)).map { String(format: "%02x", $0) }.joined()
    }

    /// Apple answered: with its identity token, or without one (`cancelled` when the person closed Apple's sheet).
    func finish(identityToken: String?, cancelled: Bool) async -> Outcome {
        guard let identityToken, let value else {
            return cancelled ? .cancelled : .failed("Sign in with Apple did not finish.")
        }
        guard let request = Server.request("/auth/apple", method: "POST",
                                           json: ["identity_token": identityToken, "nonce": value]),
              let (data, response) = try? await transport.send(request) else {
            return .failed("The server cannot be reached right now.")
        }
        let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        switch response.httpStatus {
        case 200:
            if let token = body["token"] as? String, let person = body["person"] as? String {
                return .signedIn(token: token, person: person)
            }
        case 403:
            if let code = body["waiting"] as? String { return .waiting(code: code) }
        default:
            break
        }
        return .failed("The server did not accept the sign-in.")
    }
}
