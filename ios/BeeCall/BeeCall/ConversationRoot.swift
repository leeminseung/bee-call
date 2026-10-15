import SwiftUI

/// Design D's app around a call (frame 4): the conversation, with the call screen over it while a call is on. A
/// call starts on the call screen and keeps the view the person last picked (CallPresentation). With iOS 18's
/// zoom transition the call screen grows out of the call bar (or the call button) and shrinks back into it, the
/// way a photo goes back to its thumbnail; a swipe down or "대화 보기" goes to the conversation, and the call goes
/// on under the call bar.
struct ConversationRoot: View {
    let calls: CallManager
    let source: any ConversationSource
    let offers: OfferCenter
    let status: String
    var problem: String? // in red under the status line: what did not reach the server or did not start
    let person: PersonStore
    let catalog: VoiceCatalog
    @State private var presentation: CallPresentation
    @State private var preview: VoicePreview
    @State private var settingsOpen = false // the gear's sheet (frame 8)
    @State private var opened: OpenedCall? // a call that is over, its record open as a sheet
    @State private var continuing: String? // the call to continue once its sheet is down
    /// A sheet is up, from its opening until its dismissal has finished: the call screen waits for it, since one
    /// view presents one thing at a time (review of 2026-10-15, finding 6).
    @State private var sheetUp = false
    @State private var notice = ResultNotice()
    @State private var files = FilePreview() // a job's file open in Quick Look
    @State private var jump: String?
    @Environment(\.scenePhase) private var scenePhase
    @Namespace private var zoom

    init(calls: CallManager, source: any ConversationSource, offers: OfferCenter, status: String,
         problem: String? = nil, person: PersonStore, catalog: VoiceCatalog, samples: any VoiceSamples,
         presentation: CallPresentation = CallPresentation()) {
        self.calls = calls
        self.source = source
        self.offers = offers
        self.status = status
        self.problem = problem
        self.person = person
        self.catalog = catalog
        _presentation = State(initialValue: presentation)
        _preview = State(initialValue: VoicePreview(samples: samples))
    }

    var body: some View {
        HomeView(source: source, calls: calls, offers: offers, status: status, problem: problem, glowing: notice.glowing,
                 jump: $jump,
                 showCallScreen: { presentation.show(.callScreen) },
                 openSettings: {
                     sheetUp = true
                     settingsOpen = true
                 },
                 openCall: {
                     sheetUp = true
                     opened = OpenedCall(id: $0)
                 },
                 startCall: { Task { await calls.startCall() } }, zoom: zoom)
            .fullScreenCover(isPresented: Binding(get: { presentation.showsCallScreen(inCall: calls.callIsOn) && !sheetUp },
                                                  set: { if !$0 { presentation.show(.conversation) } })) {
                CallModeView(calls: calls, job: pill, showConversation: { presentation.show(.conversation) })
                    .zoomTransition("call", in: zoom)
                    // The call's own view, for the pill and "결과 도착", also while the call screen covers the
                    // conversation, whose live section asks for it too.
                    .task(id: source.ownCall) { if let id = source.ownCall { await source.show(id) } }
            }
            .filePreviews(files, onDismiss: { sheetUp = false })
            .onChange(of: files.url) { _, url in if url != nil { sheetUp = true } }
            .sheet(isPresented: $settingsOpen, onDismiss: { sheetUp = false }) {
                SettingsSheet(person: person, catalog: catalog, preview: preview)
            }
            .sheet(item: $opened, onDismiss: continueOpenedCall) { call in
                CallSheet(callID: call.id, source: source, canContinue: !calls.isInCall) {
                    continuing = call.id
                    opened = nil
                }
            }
            .onChange(of: calls.callIsOn, initial: true) { _, on in
                presentation.callChanged(inCall: on)
                if on { // the call screen goes over everything: what is still up is closed first
                    settingsOpen = false
                    opened = nil
                    files.url = nil
                    offers.keepBooked() // the cancel question's alert
                    preview.stop()
                } else {
                    notice.callEnded()
                }
            }
            .onChange(of: OwnJobs(call: source.ownCall, done: doneJobs), initial: true) { _, jobs in
                notice.update(call: jobs.call, done: jobs.done,
                              callScreenUp: presentation.showsCallScreen(inCall: calls.callIsOn))
            }
            .onChange(of: presentation.mode) { _, mode in
                if mode == .conversation, let id = notice.openedConversation() { jump = id }
            }
            .task(id: notice.glowing) { // a result card lights up for about two seconds
                guard let id = notice.glowing else { return }
                try? await Task.sleep(for: .seconds(2))
                notice.glowEnded(id)
            }
            // Up to date while on screen or in a call (contract section 3), and again on coming back.
            .onChange(of: scenePhase != .background || calls.isInCall, initial: true) { _, on in source.keepFresh(on) }
    }

    /// "이 통화 이어서 하기": the call starts once the sheet is down, so the call screen is not asked for while the
    /// sheet is still going away.
    private func continueOpenedCall() {
        sheetUp = false
        guard let id = continuing else { return }
        continuing = nil
        Task { await calls.startCall(continuing: id) }
    }

    private var ownJobs: [Job] {
        guard let id = source.ownCall, let view = source.view(id) else { return [] }
        return view.drawnItems.compactMap { if case .job(let job) = $0 { job } else { nil } }
    }

    private var doneJobs: [String] {
        ownJobs.filter { $0.state == .done }.map(\.id)
    }

    /// The call screen's pill: a result waiting in the conversation, else the newest running job.
    private var pill: JobPill? {
        let jobs = ownJobs
        if let id = notice.unseen, let job = jobs.first(where: { $0.id == id }) {
            return JobPill(id: job.id, label: job.title, startedAt: job.startedAt.date, arrived: true)
        }
        return jobs.last { $0.state == .running }.map {
            JobPill(id: $0.id, label: $0.title, startedAt: $0.startedAt.date, arrived: false)
        }
    }
}

/// A call whose record is open as a sheet.
struct OpenedCall: Identifiable, Hashable {
    let id: String
}

/// The own call's finished jobs, watched together with the call they belong to.
private struct OwnJobs: Equatable {
    let call: String?
    let done: [String]
}

extension View {
    /// Where design D's zoom transition starts and ends (iOS 18 and later; earlier, the plain cover).
    @ViewBuilder
    func zoomSource(_ id: String, in namespace: Namespace.ID?) -> some View {
        if #available(iOS 18, *), let namespace {
            matchedTransitionSource(id: id, in: namespace)
        } else {
            self
        }
    }

    @ViewBuilder
    func zoomTransition(_ id: String, in namespace: Namespace.ID) -> some View {
        if #available(iOS 18, *) {
            navigationTransition(.zoom(sourceID: id, in: namespace))
        } else {
            self
        }
    }
}
