import SwiftUI

/// The app once signed in (design D): the first run once, then the conversation home (ConversationRoot), with
/// everything the earlier one-screen app kept running: the ring stream while the app is on screen, the microphone
/// prompt, the person's settings kept fresh, and the lines under the call button. The in-app web record sheet is
/// gone: a call's record is its sheet in the conversation, and the web page stays for the Mac.
struct AppRoot: View {
    let calls: CallManager
    @State private var data: ThreadData
    @State private var catalog = VoiceCatalog()
    @State private var preview: VoicePreview
    @AppStorage(FirstRun.doneKey) private var firstRunDone = false
    @AppStorage("language") private var language = "en"
    @Environment(\.scenePhase) private var scenePhase
    private let server = Server.shared

    init(calls: CallManager) {
        self.calls = calls
        _data = State(initialValue: ThreadData(calls: calls))
        _preview = State(initialValue: VoicePreview(samples: SamplePlayer(callIsOn: { calls.callIsOn })))
    }

    var body: some View {
        Group {
            if firstRunDone {
                ConversationRoot(calls: calls, source: data, offers: data.offers, status: status, problem: problem,
                                 person: data.person, catalog: catalog,
                                 samples: SamplePlayer(callIsOn: { calls.callIsOn }))
            } else {
                FirstRunView(person: data.person, catalog: catalog, preview: preview) {}
            }
        }
        // A new id cancels the running listener and starts the next one: the app went to the background, or a
        // Debug build's typed address changed. Inactive still counts: the incoming-call banner makes the app so.
        .task(id: ringSource) {
            guard !ringSource.isEmpty else { return }
            await calls.listenForRings()
        }
        .task { await calls.askForMicrophone() }
        .task(id: scenePhase == .active) {
            guard scenePhase == .active else { return }
            await data.person.refresh() // the first run starts on the server's language
        }
    }

    private var ringSource: String {
        scenePhase == .background ? "" : Server.api
    }

    private var status: String {
        HomeStatus.line(korean: language == "ko", callStatus: calls.statusLine, ringStatus: calls.ringStatus,
                        voipRegistered: PushTokens.shared.voipRegistered)
    }

    /// What did not reach the server or did not start, in red under the status line.
    private var problem: String? {
        server.settings.problem ?? calls.phoneRecordingProblem ?? data.offers.notice ?? data.problem
    }
}
