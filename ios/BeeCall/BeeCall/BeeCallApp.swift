import SwiftUI

@main
struct BeeCallApp: App {
    // Notification registration and the offer buttons (OfferCenter.swift) start with the app delegate.
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    // Created at launch so CallKit and the audio settings are in place before the first call.
    @State private var calls = CallManager()

    var body: some Scene {
        WindowGroup {
            CallScreen(calls: calls)
        }
    }
}
