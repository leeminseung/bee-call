import AuthenticationServices
import Foundation
import Observation
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
    @ObservationIgnored let signIn: AppleSignIn
    @ObservationIgnored let bee = BeeSync()
    @ObservationIgnored let settings = SettingsSync(tokenReadable: { Server.savedToken() != nil })
    @ObservationIgnored private let keychain: SignInKeychain
    @ObservationIgnored private let store: UserDefaults // keeps who is signed in ("person")
    @ObservationIgnored private var keychainOpened: (any NSObjectProtocol)?

    /// The system Keychain and defaults, unless a test passes stand-ins.
    init(keychain: SignInKeychain = .system, store: UserDefaults = .standard, signIn: AppleSignIn = AppleSignIn()) {
        self.keychain = keychain
        self.store = store
        self.signIn = signIn
        if case .token = keychain.read() {
            state = .signedIn(person: store.string(forKey: "person") ?? "")
        } else {
            state = .signedOut
        }
        keychainOpened = NotificationCenter.default.addObserver(
            forName: UIApplication.protectedDataDidBecomeAvailableNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.readSignInAgain() }
        }
    }

    isolated deinit {
        if let keychainOpened { NotificationCenter.default.removeObserver(keychainOpened) }
    }

    // MARK: - Address

    /// "<scheme>://<host>:<port>" of the token server: the one address every request is built from
    /// (ServerClient). Empty when the build has none.
    nonisolated static var api: String {
        #if DEBUG
        if let typed = UserDefaults.standard.string(forKey: typedAddressKey), let base = origin(of: typed) {
            return base
        }
        #endif
        return origin(of: Bundle.main.object(forInfoDictionaryKey: "BeeCallServer") as? String ?? "") ?? ""
    }

    /// Where a Debug build keeps the address typed on its first screen.
    nonisolated static let typedAddressKey = "tokenServerURL"

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

    /// The token server answered 401 to a request that carried the sign-in (ServerClient, which every request
    /// goes through): the sign-in has ended, because it expired or the person was removed.
    func signInEnded() {
        guard case .signedIn = state else { return }
        forget()
        problem = "Your sign-in ended. Sign in again."
    }

    // MARK: - Signing in

    private static let unreachable = "The server cannot be reached right now."

    /// While the sign-in screen shows, keeps a one-time value ready for the button: fetches one whenever none is
    /// ready (none yet, the last one went to Apple, or it was kept too long), every 5 s while the server cannot
    /// be reached. A ready value is looked at twice a second, so a tap that took it is followed by a new one at
    /// once and the button is not left off after Apple's sheet closes.
    func prepareSignIn() async {
        while !Task.isCancelled {
            signIn.dropIfOld()
            if signIn.isReady {
                try? await Task.sleep(for: .milliseconds(500))
            } else if await signIn.prepare() {
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
        await complete(signIn.finish(identityToken: identityToken, cancelled: cancelled))
    }

    /// The Keychain is locked from a restart until the first unlock, so a launch then (for a VoIP push) cannot read
    /// the sign-in and starts signed out. It is read again when the Keychain opens and when the app comes to the
    /// front (review finding 17); a sign-in that ended has no token left, and stays ended.
    func readSignInAgain() {
        guard case .signedOut = state, case .token = keychain.read() else { return }
        state = .signedIn(person: store.string(forKey: "person") ?? "")
    }

    /// What the sign-in came to.
    func complete(_ outcome: AppleSignIn.Outcome) async {
        switch outcome {
        case .signedIn(let token, let person):
            let saved = keychain.save(token)
            guard saved == errSecSuccess else { // signed in on the server, but every request would go without it
                problem = "This iPhone could not keep your sign-in (Keychain error \(saved)). Try again."
                return
            }
            store.set(person, forKey: "person")
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

    private func forget() {
        keychain.delete()
        store.removeObject(forKey: "person")
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

    /// The sign-in token every request carries, when the Keychain gives one (SignInKeychain.swift).
    nonisolated static func savedToken() -> String? {
        if case .token(let token) = SystemKeychain.read() { token } else { nil }
    }
}
