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
        _preview = State(initialValue: VoicePreview(samples: SamplePlayer(inCall: { calls.isInCall })))
    }

    var body: some View {
        Group {
            // A call that comes during the first run gets the home and its call screen; the first run comes back after.
            if firstRunDone || calls.isInCall {
                ConversationRoot(calls: calls, source: data, offers: data.offers, status: status, problem: problem,
                                 person: data.person, catalog: catalog,
                                 samples: SamplePlayer(inCall: { calls.isInCall }))
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
        .onChange(of: calls.isInCall) { _, inCall in if inCall { preview.stop() } } // the first run's sample
        .task(id: scenePhase == .active) {
            guard scenePhase == .active else { return }
            await data.person.refresh() // the first run starts on the server's language
        }
    }

    private var ringSource: String {
        scenePhase == .background ? "" : Server.api
    }

    /// The line under the call button, and Bee's state when it needs the person (connecting, not connected,
    /// turning off), which the old one-screen app showed too. "Bee 연결됨" and the approval waiting are in the
    /// header already.
    private var status: String {
        let korean = language == "ko"
        let ready = HomeStatus.line(korean: korean, callStatus: calls.statusLine, ringStatus: calls.ringStatus,
                                    voipRegistered: PushTokens.shared.voipRegistered)
        let bee = server.bee.status
        let beeNote = ["", "Bee connected", "Waiting for your approval in the Bee app"].contains(bee) ? ""
            : MessageWords.words(bee, korean: korean)
        return [ready, beeNote].filter { !$0.isEmpty }.joined(separator: "\n")
    }

    /// What did not reach the server or did not start, in red under the status line, in the call language.
    private var problem: String? {
        (server.settings.problem ?? calls.phoneRecordingProblem ?? data.offers.notice ?? data.problem)
            .map { MessageWords.words($0, korean: language == "ko") }
    }
}
