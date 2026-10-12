import SwiftUI

@main
struct BeeCallApp: App {
    // Created at launch so CallKit and the audio settings are in place before the first call.
    @State private var calls = CallManager()

    var body: some Scene {
        WindowGroup {
            CallScreen(calls: calls)
        }
    }
}
