#if DEBUG
import SwiftUI

/// Debug builds only: launched with `-designD <frame>`, the app shows one frame of design D
/// (scratch/2026-10-15-ui-mockups/d-chat-and-call.html) with the contract's example content (contract/examples,
/// answered by ContractExamples through the same stores as the app), to compare with the mockup and take
/// screenshots. Nothing goes to a server or to CallKit, and what a frame writes stays in defaults of its own
/// (review of 2026-10-15, finding 9).
enum DesignDemo: String {
    case home // frame 1
    case call // frame 2: the call screen
    case conversation // frame 3: the conversation during the call
    case live // frame 4: a pretend call, with the call screen and the conversation to go between
    case result // frame 5, left: a job finishes while the call screen is up
    case resultConversation // frame 5, right: a job finishes while the conversation is up
    case record // frame 6, left: a call's sheet, at its top
    case recordEnd // frame 6, right: the same sheet at its end
    case firstRun // frame 7, left: the first run's language step
    case firstRunVoice // frame 7, right: the voice step, with a sample playing
    case settings // frame 8: the gear's settings sheet
    case callModels // frame 8: the sheet's call model list
    case workerModels // frame 8: the sheet's worker model list

    static func requested(in store: UserDefaults = .standard) -> DesignDemo? {
        store.string(forKey: "designD").flatMap(DesignDemo.init(rawValue:))
    }

    /// The frames with the live example's call on (call-view-live.json).
    var isLive: Bool {
        [.call, .conversation, .live, .result, .resultConversation].contains(self)
    }

    /// The call the live frames and the record frames are in: the live example's call (call-view-live.json).
    static let liveCall: String = {
        let file = Bundle.main.url(forResource: "call-view-live", withExtension: "json")
        let view = file.flatMap { try? Data(contentsOf: $0) }.flatMap { try? Contract.decode(CallView.self, from: $0) }
        return view?.call.callID ?? ""
    }()

    /// The voice whose sample plays in frame 7's voice step: the one shown as the person's.
    static func sampleVoice(settings: PersonSettings?, in voices: [Voice]) -> Voice? {
        VoiceChoice.effective(stored: settings?.voice, in: voices)
    }

    /// The defaults a frame writes to, apart from the app's own.
    static let store = UserDefaults(suiteName: "BeeCallDesignDemo") ?? .standard

    /// The call screen's captions in the live frames: the last two speech runs of the live view, as LiveKit
    /// would have sent them.
    static func lastLines(of view: CallView?) -> [(mine: Bool, text: String)] {
        let speech = (view?.drawnItems ?? []).compactMap { if case .speech(let speech) = $0 { speech } else { nil } }
        let last = [speech.last { $0.speaker == .ai }, speech.last { $0.speaker == .person }].compactMap { $0 }
        return last.sorted { $0.at < $1.at }.map { ($0.speaker == .person, $0.text) }
    }
}

/// One frame of design D, with the contract's example content.
struct DesignDemoView: View {
    let demo: DesignDemo
    let calls: CallManager
    @State private var examples: ContractExamples
    @State private var data: ThreadData
    @State private var catalog: VoiceCatalog
    @State private var preview: VoicePreview
    @State private var presentation = CallPresentation()
    @State private var jump: String?

    init(demo: DesignDemo, calls: CallManager) {
        self.demo = demo
        self.calls = calls
        let examples = ContractExamples(live: demo.isLive)
        let server = ServerClient(transport: examples)
        _examples = State(initialValue: examples)
        _data = State(initialValue: ThreadData(calls: calls, store: CallStore(server: server),
                                               offers: OfferCenter(server: server),
                                               person: PersonStore(server: server, store: DesignDemo.store)))
        _catalog = State(initialValue: VoiceCatalog(server: server))
        _preview = State(initialValue: VoicePreview(samples: SamplePlayer(inCall: { calls.isInCall })))
    }

    var body: some View {
        frame
            .defaultAppStorage(DesignDemo.store)
    }

    @ViewBuilder
    private var frame: some View {
        switch demo {
        case .home:
            home
        case .record, .recordEnd:
            home.sheet(isPresented: .constant(true)) {
                CallSheet(callID: DesignDemo.liveCall, source: data, canContinue: true, continueCall: {},
                          startAtEnd: demo == .recordEnd)
            }
        case .settings, .callModels, .workerModels:
            home.sheet(isPresented: .constant(true)) {
                SettingsSheet(person: data.person, catalog: catalog, preview: preview,
                              startOn: demo == .callModels ? .call : demo == .workerModels ? .worker : nil)
            }
        case .firstRun, .firstRunVoice:
            FirstRunView(person: data.person, catalog: catalog, preview: preview,
                         startOn: demo == .firstRunVoice ? .voice : nil) {}
                .task {
                    await data.person.refresh()
                    guard demo == .firstRunVoice else { return }
                    let language = FirstRun.language(settings: data.person.settings, preferred: Locale.preferredLanguages)
                    await catalog.load(language)
                    try? await Task.sleep(for: .milliseconds(800))
                    let voices = catalog.voices[language] ?? []
                    if let voice = DesignDemo.sampleVoice(settings: data.person.settings, in: voices) { preview.toggle(voice) }
                }
        case .call, .conversation, .live, .result, .resultConversation:
            ConversationRoot(calls: calls, source: data, offers: data.offers, status: "", person: data.person,
                             catalog: catalog, samples: SamplePlayer(inCall: { calls.isInCall }),
                             presentation: presentation)
                .task { await playLiveCall() }
        }
    }

    private var home: some View {
        HomeView(source: data, calls: calls, offers: data.offers, status: "준비됨 · 앱을 닫아도 전화가 와요",
                 jump: $jump, showCallScreen: {}, openSettings: {}, openCall: { _ in }, startCall: {})
            .onAppear { data.keepFresh(true) }
    }

    /// The live example's call while it is on; in the result frames, its job then finishes (the view after the
    /// call, the examples' one view with the job done).
    private func playLiveCall() async {
        await data.store.refreshView(DesignDemo.liveCall)
        let view = data.store.views[DesignDemo.liveCall]
        calls.playDesignDemo(lines: DesignDemo.lastLines(of: view), voice: demo == .conversation ? .person : .ai,
                             connectedAgo: 221, room: DesignDemo.liveCall)
        if demo == .conversation || demo == .resultConversation {
            try? await Task.sleep(for: .milliseconds(300))
            presentation.show(.conversation)
        }
        guard demo == .result || demo == .resultConversation else { return }
        try? await Task.sleep(for: .seconds(5)) // time for a screenshot of the running job, too
        examples.live = false
        await data.store.refreshView(DesignDemo.liveCall)
    }
}
#endif
