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
                #if DEBUG
                if let design = DesignDemo.requested() {
                    DesignDemoView(demo: design, calls: calls)
                } else {
                    root
                }
                #else
                root
                #endif
            }
            // The switches live in the Settings app. SettingsSync sends them at launch and on every change; coming
            // back to the app sends them again too, and brings Bee in line with "Use Bee".
            .task(id: scenePhase) {
                guard scenePhase == .active else { return }
                server.readSignInAgain() // the Keychain may have been locked at launch
                await server.syncSettings()
            }
            // A Bee Call entry tapped in the Phone app's Recents: a new call, as the Call button makes.
            .onContinueUserActivity(RecentsCall.activityType) { activity in
                guard RecentsCall.startsCall(activity, signedIn: server.isSignedIn, inCall: calls.isInCall)
                else { return }
                Task { await calls.startCall() }
            }
            #if DEBUG
            .task {
                if let demo = VoiceDemo.requested() { calls.playVoiceDemo(demo) }
            }
            #endif
        }
    }

    /// Design D's app once signed in (AppRoot), else the sign-in screen. In a Debug build the voice demo shows the
    /// call screen with its orb on synthetic levels (VoiceDemo.swift).
    @ViewBuilder private var root: some View {
        #if DEBUG
        if VoiceDemo.requested() != nil {
            CallModeView(calls: calls, job: nil, showConversation: {})
        } else {
            signedInOrNot
        }
        #else
        signedInOrNot
        #endif
    }

    @ViewBuilder private var signedInOrNot: some View {
        if server.isSignedIn {
            AppRoot(calls: calls)
        } else {
            SignInView(server: server)
        }
    }
}
