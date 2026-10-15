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
    @State private var presentation = CallPresentation()
    @State private var jump: String?
    @Namespace private var zoom

    var body: some View {
        HomeView(source: source, calls: calls, offers: offers, status: status, jump: $jump,
                 showCallScreen: { presentation.show(.callScreen) }, openSettings: {}, openCall: { _ in },
                 startCall: { Task { await calls.startCall() } }, zoom: zoom)
            .fullScreenCover(isPresented: Binding(get: { presentation.showsCallScreen(inCall: calls.isInCall) },
                                                  set: { if !$0 { presentation.show(.conversation) } })) {
                CallModeView(calls: calls, job: liveJob, showConversation: { presentation.show(.conversation) })
                    .zoomTransition("call", in: zoom)
            }
            .onChange(of: calls.isInCall, initial: true) { _, inCall in presentation.callChanged(inCall: inCall) }
    }

    /// The newest job of the call that is on, for the call screen's pill.
    private var liveJob: JobPill? {
        for case .call(let call) in source.sections.reversed() where call.isLive {
            for case .job(let job) in call.entries.reversed() {
                return JobPill(id: job.id, label: job.short ?? job.title, startedAt: job.startedAt, arrived: job.isDone)
            }
        }
        return nil
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
