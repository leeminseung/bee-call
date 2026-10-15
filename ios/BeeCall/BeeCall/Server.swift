import AuthenticationServices
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
    @ObservationIgnored let signIn = AppleSignIn()
    @ObservationIgnored let bee = BeeSync()
    @ObservationIgnored let settings = SettingsSync(tokenReadable: { Server.savedToken() != nil })

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
    /// port 8788, with ?debug=1, the only address on which the page offers its debug events (web/app.js).
    nonisolated static var recordsURL: URL? {
        #if DEBUG
        if let host = URLComponents(string: api)?.host {
            return URL(string: "http://\(host):8788/?debug=1")
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

    /// Every kind of launch, also one for a VoIP push or a notification button, which never reach the foreground:
    /// the switches go to the server, and "Use Bee" turned off is carried out (contract section 1).
    func appLaunched() {
        settings.send()
        guard Self.savedToken() != nil else { return }
        Task { await bee.sync(openingApprovalPage: false) }
    }

    var isSignedIn: Bool {
        if case .signedIn = state { true } else { false }
    }

    /// The token server answered 401 (ServerClient, which every request goes through): the sign-in has ended,
    /// because it expired or the person was removed.
    func signInEnded() {
        guard case .signedIn = state else { return }
        forget()
        problem = "Your sign-in ended. Sign in again."
    }

    // MARK: - Signing in

    private static let unreachable = "The server cannot be reached right now."

    /// While the sign-in screen shows and no one-time value is ready for the button (none yet, or the last one
    /// went to Apple), fetches one, every 5 s while the server cannot be reached.
    func prepareSignIn() async {
        while !signIn.isReady, !Task.isCancelled {
            if await signIn.prepare() {
                if problem == Self.unreachable { problem = nil }
            } else {
                problem = Self.unreachable
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    func finish(_ result: Result<ASAuthorization, any Error>) async {
        var identityToken: String?
        var cancelled = false
        switch result {
        case .success(let authorization):
            if let credential = authorization.credential as? ASAuthorizationAppleIDCredential,
               let data = credential.identityToken {
                identityToken = String(data: data, encoding: .utf8)
            }
        case .failure(let error):
            cancelled = (error as? ASAuthorizationError)?.code == .canceled
        }
        switch await signIn.finish(identityToken: identityToken, cancelled: cancelled) {
        case .signedIn(let token, let person):
            Self.save(token: token)
            UserDefaults.standard.set(person, forKey: "person")
            state = .signedIn(person: person)
            problem = nil
            await afterSignIn()
        case .waiting(let code):
            state = .waiting(code: code)
            problem = nil
        case .cancelled:
            break
        case .failed(let message):
            problem = message
        }
    }

    func signOut() async {
        _ = await ServerClient().send("/auth/signout", method: "POST")
        forget()
    }

    private func forget() {
        Self.deleteToken()
        UserDefaults.standard.removeObject(forKey: "person")
        state = .signedOut
        bee.clearStatus()
    }

    /// Push tokens and settings belong to the person, so they go to the server again after a sign-in.
    private func afterSignIn() async {
        bee.forgetHandledSwitch()
        await PushTokens.shared.signedIn()
        await syncSettings()
    }

    // MARK: - Settings (the Bee Call page in the iOS Settings app, Settings.bundle)

    /// Sends the switches from the Settings app to the server, and connects or disconnects Bee to match "Use Bee".
    func syncSettings() async {
        guard case .signedIn = state else { return }
        await settings.send().value
        await bee.sync()
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
