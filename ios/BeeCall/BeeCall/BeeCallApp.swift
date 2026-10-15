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

    @ViewBuilder private var root: some View {
        if showsCallScreen {
            CallScreen(calls: calls)
        } else {
            SignInView(server: server)
        }
    }

    /// The call screen once signed in; in a Debug build, also for the voice and offer demos (VoiceDemo.swift,
    /// OfferDemo.swift).
    private var showsCallScreen: Bool {
        #if DEBUG
        if VoiceDemo.requested() != nil || OfferDemo.requested() { return true }
        #endif
        return server.isSignedIn
    }
}
