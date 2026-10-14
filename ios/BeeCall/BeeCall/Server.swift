import AuthenticationServices
import CryptoKit
import Foundation
import Observation
import Security
import UIKit

/// The Bee Call server this build talks to, and who is signed in to it (voice/token_server.py).
///
/// The address comes with the build: Info.plist BeeCallServer and BeeCallRecords, set per build configuration
/// in the Xcode project. A Debug build talks to the Mac on the same Wi-Fi and can be pointed elsewhere with the
/// field on the first screen; a Release (TestFlight) build talks to the cloud server only.
/// Signing in uses Sign in with Apple. The server answers with its own sign-in token, which stays in the
/// Keychain, and every request carries it. A new person waits until the server's operator approves the code
/// the server gives (python -m voice.people approve).
@Observable
final class Server {
    static let shared = Server()

    enum State: Equatable {
        case signedOut
        case waiting(code: String) // signed in with Apple; Matt has not approved this person yet
        case signedIn(person: String)
    }

    private(set) var state: State
    private(set) var problem: String? // the last sign-in failure, in words
    private(set) var beeStatus = "" // shown under the call status when the person uses Bee
    @ObservationIgnored private var nonce: String?
    @ObservationIgnored let settings = SettingsSync(isSignedIn: { Server.shared.isSignedIn },
                                                    noteStatus: { Server.shared.noteStatus($0) })

    private init() {
        state = Self.savedToken() == nil ? .signedOut : .signedIn(person: UserDefaults.standard.string(forKey: "person") ?? "")
    }

    // MARK: - Address

    /// "<scheme>://<host>:<port>" of the token server.
    nonisolated static var api: String {
        #if DEBUG
        if let typed = UserDefaults.standard.string(forKey: "tokenServerURL"), let base = origin(of: typed) {
            return base
        }
        #endif
        return origin(of: Bundle.main.object(forInfoDictionaryKey: "BeeCallServer") as? String ?? "") ?? ""
    }

    /// What the rest of the app calls the token server address: its /token URL.
    nonisolated static var tokenURL: String { api.isEmpty ? "" : api + "/token" }

    /// The call record page (voice/session_server.py). In a Debug build: the same host as the token server,
    /// port 8788.
    nonisolated static var recordsURL: URL? {
        #if DEBUG
        if let host = URLComponents(string: api)?.host {
            return URL(string: "http://\(host):8788/")
        }
        #endif
        return URL(string: Bundle.main.object(forInfoDictionaryKey: "BeeCallRecords") as? String ?? "")
    }

    /// Apple's push service this build gets pushes from: Xcode builds the development one, TestFlight and
    /// App Store builds the production one.
    nonisolated static var pushEnvironment: String {
        #if DEBUG
        "development"
        #else
        "production"
        #endif
    }

    nonisolated private static func origin(of address: String) -> String? {
        guard let parts = URLComponents(string: address.trimmingCharacters(in: .whitespacesAndNewlines)),
              let scheme = parts.scheme, scheme == "http" || scheme == "https",
              let host = parts.host, !host.isEmpty else { return nil }
        return "\(scheme)://\(host)" + (parts.port.map { ":\($0)" } ?? "")
    }

    // MARK: - Requests

    /// A request to `path` on the token server, signed in.
    nonisolated static func request(_ path: String, method: String = "GET", json: Any? = nil,
                                    timeout: TimeInterval = 10) -> URLRequest? {
        guard !api.isEmpty, let url = URL(string: api + path) else { return nil }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        authorize(&request)
        if let json {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: json)
        }
        return request
    }

    nonisolated static func authorize(_ request: inout URLRequest) {
        if let token = savedToken() {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
    }

    var isSignedIn: Bool {
        if case .signedIn = state { true } else { false }
    }

    /// Every answer goes through here: a 401 means the sign-in is gone (expired, or the person was removed).
    func noteStatus(_ code: Int) {
        if code == 401, case .signedIn = state {
            forget()
            problem = "Your sign-in ended. Sign in again."
        }
    }

    // MARK: - Signing in

    /// A one-time value from the server, fetched before the Sign in with Apple button is tapped (its request
    /// is made synchronously).
    func prepare() async {
        guard let request = Self.request("/auth/nonce"),
              let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let body = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            nonce = nil
            problem = "The server cannot be reached right now."
            return
        }
        nonce = body["nonce"] as? String
        problem = nil
    }

    /// For Apple's request: the nonce's SHA-256, which Apple puts into the identity token.
    func hashedNonce() -> String {
        SHA256.hash(data: Data((nonce ?? "").utf8)).map { String(format: "%02x", $0) }.joined()
    }

    func finish(_ result: Result<ASAuthorization, any Error>) async {
        defer { Task { await prepare() } } // a nonce works once
        guard case .success(let authorization) = result,
              let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
              let tokenData = credential.identityToken, let identityToken = String(data: tokenData, encoding: .utf8),
              let nonce else {
            if case .failure(let error) = result, (error as? ASAuthorizationError)?.code == .canceled { return }
            problem = "Sign in with Apple did not finish."
            return
        }
        guard let request = Self.request("/auth/apple", method: "POST",
                                         json: ["identity_token": identityToken, "nonce": nonce]),
              let (data, response) = try? await URLSession.shared.data(for: request) else {
            problem = "The server cannot be reached right now."
            return
        }
        let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
        switch (response as? HTTPURLResponse)?.statusCode {
        case 200:
            guard let token = body["token"] as? String, let person = body["person"] as? String else { break }
            Self.save(token: token)
            UserDefaults.standard.set(person, forKey: "person")
            state = .signedIn(person: person)
            problem = nil
            await afterSignIn()
            return
        case 403:
            if let code = body["waiting"] as? String {
                state = .waiting(code: code)
                problem = nil
                return
            }
        default:
            break
        }
        problem = "The server did not accept the sign-in."
    }

    func signOut() async {
        if let request = Self.request("/auth/signout", method: "POST") {
            _ = try? await URLSession.shared.data(for: request)
        }
        forget()
    }

    private func forget() {
        Self.deleteToken()
        UserDefaults.standard.removeObject(forKey: "person")
        state = .signedOut
        beeStatus = ""
    }

    /// Push tokens and settings belong to the person, so they go to the server again after a sign-in.
    private func afterSignIn() async {
        await OfferCenter.shared.sendToken(again: true)
        await syncSettings()
    }

    // MARK: - Settings (the Bee Call page in the iOS Settings app, Settings.bundle)

    /// Sends the switches from the Settings app to the server, and connects or disconnects Bee to match "Use Bee".
    func syncSettings() async {
        guard case .signedIn = state else { return }
        await settings.send()
        await syncBee(wanted: AppSettings(.standard).useBee)
    }

    private func syncBee(wanted: Bool) async {
        guard let request = Self.request("/me"), let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let me = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let settings = me["settings"] as? [String: Any] else { return }
        let connected = settings["bee"] as? Bool ?? false
        let connecting = me["bee_connecting"] as? Bool ?? false
        if wanted && !connected && !connecting {
            beeStatus = "Connecting Bee: approve Bee Call in the Bee app"
            guard let start = Self.request("/bee/connect", method: "POST", timeout: 40),
                  let (body, answer) = try? await URLSession.shared.data(for: start),
                  (answer as? HTTPURLResponse)?.statusCode == 200,
                  let link = (try? JSONSerialization.jsonObject(with: body) as? [String: Any])?["link"] as? String,
                  let url = URL(string: link) else {
                beeStatus = "Bee could not be connected right now"
                return
            }
            await UIApplication.shared.open(url) // Bee's own approval page
        } else if !wanted && connected {
            if let stop = Self.request("/bee/disconnect", method: "POST") {
                _ = try? await URLSession.shared.data(for: stop)
            }
            beeStatus = ""
        } else {
            beeStatus = connected ? "Bee connected" : connecting ? "Waiting for your approval in the Bee app" : ""
        }
    }

    // MARK: - Keychain

    nonisolated private static let account = "bee-call-sign-in"

    nonisolated static func savedToken() -> String? {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account,
                                    kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    nonisolated private static func save(token: String) {
        deleteToken()
        let item: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account,
                                   kSecValueData as String: Data(token.utf8),
                                   // Readable after the first unlock, so a VoIP push can answer a call on a locked phone.
                                   kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        SecItemAdd(item as CFDictionary, nil)
    }

    nonisolated private static func deleteToken() {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account]
        SecItemDelete(query as CFDictionary)
    }
}
