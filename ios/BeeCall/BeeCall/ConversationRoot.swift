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
    @State private var presentation: CallPresentation
    @State private var notice = ResultNotice()
    @State private var jump: String?
    @Namespace private var zoom

    init(calls: CallManager, source: any ConversationSource, offers: OfferCenter, status: String,
         presentation: CallPresentation = CallPresentation()) {
        self.calls = calls
        self.source = source
        self.offers = offers
        self.status = status
        _presentation = State(initialValue: presentation)
    }

    var body: some View {
        HomeView(source: source, calls: calls, offers: offers, status: status, glowing: notice.glowing, jump: $jump,
                 showCallScreen: { presentation.show(.callScreen) }, openSettings: {}, openCall: { _ in },
                 startCall: { Task { await calls.startCall() } }, zoom: zoom)
            .fullScreenCover(isPresented: Binding(get: { presentation.showsCallScreen(inCall: calls.isInCall) },
                                                  set: { if !$0 { presentation.show(.conversation) } })) {
                CallModeView(calls: calls, job: pill, showConversation: { presentation.show(.conversation) })
                    .zoomTransition("call", in: zoom)
            }
            .onChange(of: calls.isInCall, initial: true) { _, inCall in
                presentation.callChanged(inCall: inCall)
                if !inCall { notice.callEnded() }
            }
            .onChange(of: liveCall?.finishedJobs ?? [], initial: true) { _, done in
                guard liveCall != nil else { return }
                notice.update(done: done, callScreenUp: presentation.showsCallScreen(inCall: calls.isInCall))
            }
            .onChange(of: presentation.mode) { _, mode in
                if mode == .conversation, let id = notice.openedConversation() { jump = id }
            }
            .task(id: notice.glowing) { // a result card lights up for about two seconds
                guard let id = notice.glowing else { return }
                try? await Task.sleep(for: .seconds(2))
                notice.glowEnded(id)
            }
    }

    private var liveCall: ConversationCall? {
        for case .call(let call) in source.sections.reversed() where call.isLive { return call }
        return nil
    }

    /// The call screen's pill: a result waiting in the conversation, else the newest running job.
    private var pill: JobPill? {
        guard let call = liveCall else { return nil }
        let jobs = call.entries.compactMap { if case .job(let job) = $0 { job } else { nil } }
        if let id = notice.unseen, let job = jobs.first(where: { $0.id == id }) {
            return JobPill(id: job.id, label: job.short ?? job.title, startedAt: job.startedAt, arrived: true)
        }
        return jobs.last { !$0.isDone }.map {
            JobPill(id: $0.id, label: $0.short ?? $0.title, startedAt: $0.startedAt, arrived: false)
        }
    }
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
