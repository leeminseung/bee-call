import SwiftUI

@main
struct BeeCallApp: App {
    // Notification registration and the offer buttons (OfferCenter.swift) start with the app delegate.
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    // Created at launch so CallKit and the audio settings are in place before the first call.
    @State private var calls = CallManager()
    @Environment(\.scenePhase) private var scenePhase
    private let server = Server.shared

    init() {
        // The Bee Call page in the iOS Settings app (Settings.bundle) shows these until the person changes them.
        UserDefaults.standard.register(defaults: AppSettings.defaults)
    }

    var body: some Scene {
        WindowGroup {
            Group {
                if case .signedIn = server.state {
                    CallScreen(calls: calls)
                } else {
                    SignInView(server: server)
                }
            }
            // The switches live in the Settings app, so the server learns them whenever the app comes back.
            .task(id: scenePhase) {
                if scenePhase == .active { await server.syncSettings() }
            }
        }
    }
}
