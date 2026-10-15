import SwiftUI

/// The conversation: every section in time order, the newest at the bottom (design D). A call that is over is
/// folded to its results until "대화 펼치기"; the call that is on shows everything, with a running job at the
/// very bottom like a typing indicator. `jump` scrolls to a card (a result that arrived during the call).
struct ConversationThread: View {
    let source: any ConversationSource
    let offers: OfferCenter
    let language: String
    var glowing: String?
    @Binding var jump: String?
    let openCall: (String) -> Void
    var topInset: CGFloat = 0
    var bottomInset: CGFloat = 0
    @State private var expanded: Set<String> = []

    private var words: ConversationWords { ConversationWords(korean: language == "ko") }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(spacing: 10) { // not lazy: a lazy stack anchored at the bottom went blank when a card above grew
                    if source.hasOlder {
                        Button(words.earlier) { Task { await source.loadOlder() } }
                            .font(.footnote.weight(.semibold))
                            .foregroundStyle(BeeStyle.ink)
                            .frame(minHeight: 44)
                    }
                    ForEach(source.sections) { section in
                        switch section {
                        case .call(let entry): callSection(entry)
                        case .question(let offer): questionSection(offer)
                        case .ownCall: ownCallSection(stamp: words.inCall)
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, topInset)
                .padding(.bottom, bottomInset)
            }
            .defaultScrollAnchor(.bottom)
            .scrollIndicators(.hidden)
            .onChange(of: jump) { _, id in
                guard let id else { return }
                withAnimation(.easeInOut(duration: 0.35)) { proxy.scrollTo(id, anchor: .center) }
                jump = nil
            }
        }
    }

    @ViewBuilder
    private func callSection(_ entry: CallEntry) -> some View {
        let call = entry.call
        if call.callID == source.ownCall { // the view's summary is newer than the list's
            ownCallSection(stamp: CallLine.line(source.view(call.callID)?.call ?? call, korean: words.korean, now: .now))
        } else {
            let isExpanded = expanded.contains(call.callID) && CallThread.canUnfold(call)
            VStack(spacing: 10) {
                Button { openCall(call.callID) } label: {
                    Stamp(text: CallLine.line(call, korean: words.korean, now: .now),
                          opens: call.state == .ended || call.state == .dropped)
                }
                .buttonStyle(.plain)
                .disabled(!(call.state == .ended || call.state == .dropped))
                .padding(.top, 8)
                Text(CallLine.title(call, korean: words.korean))
                    .font(call.titleSource == .none ? .subheadline : .subheadline.weight(.semibold))
                    .foregroundStyle(call.titleSource == .none ? Color.secondary : BeeStyle.ink)
                    .multilineTextAlignment(.center)
                    .padding(.top, -6)
                if CallThread.canUnfold(call) {
                    unfoldButton(call.callID, isExpanded: isExpanded)
                }
                if isExpanded {
                    if let view = source.view(call.callID) {
                        ForEach(CallThread.expanded(view)) { item in
                            entryView(item, live: false, folded: false).id(item.id)
                        }
                    } else {
                        ProgressView().padding(8)
                    }
                } else {
                    ForEach(CallThread.folded(entry)) { item in
                        entryView(item, live: false, folded: true).id(item.id)
                    }
                }
            }
            .task(id: isExpanded) { if isExpanded { await source.show(call.callID) } }
        }
    }

    /// The call that is on: its view, the captions it does not hold yet, and the running job last.
    private func ownCallSection(stamp: String) -> some View {
        VStack(spacing: 10) {
            Stamp(text: stamp, live: true)
                .padding(.top, 8)
            ForEach(source.ownEntries) { item in
                entryView(item, live: true, folded: false).id(item.id)
            }
        }
        .task(id: source.ownCall) { if let id = source.ownCall { await source.show(id) } }
    }

    private func unfoldButton(_ callID: String, isExpanded: Bool) -> some View {
        Button {
            withAnimation(.easeInOut(duration: 0.25)) {
                if isExpanded { expanded.remove(callID) } else { expanded.insert(callID) }
            }
        } label: {
            Label(isExpanded ? words.fold : words.unfold, systemImage: isExpanded ? "chevron.up" : "chevron.down")
                .labelStyle(TrailingIconLabel())
                .font(.footnote.weight(.semibold))
                .foregroundStyle(BeeStyle.ink)
                .padding(.horizontal, 12)
                .frame(minHeight: 30)
                .background(BeeStyle.card, in: .capsule)
        }
        .buttonStyle(.plain)
        .frame(minHeight: 44)
        .padding(.vertical, -7)
    }

    private func entryView(_ entry: CallThread.Entry, live: Bool, folded: Bool) -> some View {
        ThreadEntryView(entry: entry, words: words, live: live, folded: folded, glowing: glowing)
    }

    @ViewBuilder
    private func questionSection(_ offer: Offer) -> some View {
        VStack(spacing: 10) {
            Stamp(text: CallLine.day(offer.createdAt, korean: words.korean, now: .now) + " "
                  + CallLine.time(offer.createdAt, korean: words.korean) + " · " + words.asks)
                .padding(.top, 8)
            OfferCardView(offer: offer, language: language, answering: offers.answering.contains(offer.id)) { choice in
                Task { await offers.answer(offer.id, choice) }
            }
        }
    }
}

/// A label with its icon after the text, as "대화 펼치기 ⌄".
struct TrailingIconLabel: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 4) {
            configuration.title
            configuration.icon.font(.caption2.weight(.bold))
        }
    }
}

/// Design D's home (frame 1) and the conversation during a call (frame 3): the conversation under a top bar and
/// above the bottom controls. Between calls: the name and the gear at the top, the booked call pinned under
/// them, and the round yellow call button at the bottom. During a call: the call bar at the top (its small orb
/// and clock, "통화 화면") and small round mute, speaker and end buttons at the bottom.
struct HomeView: View {
    let source: any ConversationSource
    let calls: CallManager
    let offers: OfferCenter
    let status: String
    var problem: String?
    var glowing: String?
    @Binding var jump: String?
    let showCallScreen: () -> Void
    let openSettings: () -> Void
    let openCall: (String) -> Void
    let startCall: () -> Void
    var zoom: Namespace.ID? // the call screen grows out of the call bar or the call button (ConversationRoot)
    @AppStorage("language") private var language = "en"

    private var words: ConversationWords { ConversationWords(korean: language == "ko") }
    private var booked: [Offer] { offers.open.filter { $0.state == .booked || $0.state == .calling } }

    var body: some View {
        ZStack {
            BeeStyle.background.ignoresSafeArea()
            ConversationThread(source: source, offers: offers, language: language, glowing: glowing, jump: $jump,
                               openCall: openCall, topInset: calls.callIsOn ? 64 : (booked.isEmpty ? 70 : 140),
                               bottomInset: calls.callIsOn ? 110 : 170)
            VStack(spacing: 8) {
                VStack(spacing: 8) {
                    if calls.callIsOn {
                        CallBar(calls: calls, words: words, open: showCallScreen)
                            .zoomSource("call", in: zoom)
                    } else {
                        topBar
                        ForEach(booked) { offer in
                            BookedPin(offer: offer, language: language) { offers.askToCancel(offer) }
                        }
                    }
                }
                .padding(.bottom, 6)
                .background { // the conversation fades out under the top
                    LinearGradient(stops: [.init(color: BeeStyle.background, location: 0),
                                           .init(color: BeeStyle.background, location: 0.62),
                                           .init(color: BeeStyle.background.opacity(0), location: 1)],
                                   startPoint: .top, endPoint: .bottom)
                        .padding(.horizontal, -12)
                        .padding(.bottom, -34)
                        .ignoresSafeArea(edges: .top)
                }
                Spacer()
                if calls.callIsOn {
                    smallControls
                } else if !calls.isRinging { // while Bee Call's call rings, iOS's own screen has the buttons
                    callButton
                }
            }
            .padding(.horizontal, 12)
        }
        // "전화 취소" on the pinned row asks first, with the system's alert naming the time (review of 2026-10-15,
        // finding 4: in design D the tap showed nothing).
        .modifier(CancelBookedCallAlert(offers: offers, language: language))
    }

    private var topBar: some View {
        ZStack {
            HStack(spacing: 8) {
                Image(systemName: "waveform")
                    .font(.footnote.weight(.bold))
                    .foregroundStyle(BeeStyle.onYellow)
                    .frame(width: 32, height: 32)
                    .background(BeeStyle.yellow, in: .circle)
                VStack(alignment: .leading, spacing: 0) {
                    Text("Bee Call")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(BeeStyle.ink)
                    if let bee = source.beeLine(korean: words.korean) {
                        Text(bee)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.leading, 6)
            .padding(.trailing, 16)
            .padding(.vertical, 6)
            .background(BeeStyle.background, in: .capsule)
            .shadow(color: .black.opacity(0.12), radius: 8, y: 2)
            HStack {
                Spacer()
                Button(action: openSettings) {
                    Image(systemName: "gearshape")
                        .font(.title3)
                        .foregroundStyle(BeeStyle.ink)
                        .frame(width: 44, height: 44)
                        .background(BeeStyle.background, in: .circle)
                        .shadow(color: .black.opacity(0.12), radius: 8, y: 2)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(words.korean ? "설정" : "Settings")
            }
        }
        .padding(.top, 4)
    }

    private var callButton: some View {
        VStack(spacing: 6) {
            Button(action: startCall) {
                Image(systemName: "phone.fill")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(BeeStyle.onYellow)
                    .frame(width: 78, height: 78)
                    .background(BeeStyle.yellow, in: .circle)
                    .background(Circle().fill(BeeStyle.yellow.opacity(0.22)).frame(width: 98, height: 98))
            }
            .buttonStyle(.plain)
            .zoomSource("call", in: zoom)
            .accessibilityLabel(words.call)
            Text(words.call)
                .font(.subheadline.weight(.medium))
                .foregroundStyle(BeeStyle.ink)
            Text(status)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            if let problem {
                Text(problem)
                    .font(.caption)
                    .foregroundStyle(BeeStyle.red)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, 24)
            }
        }
        .padding(.bottom, 4)
    }

    private var smallControls: some View {
        let callWords = CallWords(korean: words.korean)
        return HStack(spacing: 40) {
            SmallCallControl(label: callWords.mute, symbol: "mic.slash.fill", on: calls.isMuted, tint: nil) {
                Task { await calls.setMuted(!calls.isMuted) }
            }
            SmallCallControl(label: callWords.speaker, symbol: "speaker.wave.2.fill", on: calls.isSpeakerOn, tint: nil) {
                calls.setSpeaker(!calls.isSpeakerOn)
            }
            SmallCallControl(label: callWords.end, symbol: "xmark", on: false, tint: BeeStyle.red) {
                Task { await calls.endCall() }
            }
        }
        .padding(.top, 14)
        .padding(.bottom, 4)
        .frame(maxWidth: .infinity)
        .background(LinearGradient(colors: [BeeStyle.background.opacity(0), BeeStyle.background],
                                   startPoint: .top, endPoint: UnitPoint(x: 0.5, y: 0.2)).ignoresSafeArea())
    }
}

/// The call bar over the conversation during a call: the small orb on the live voice, the clock, and "통화 화면",
/// which brings the call screen back.
struct CallBar: View {
    let calls: CallManager
    let words: ConversationWords
    let open: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button(action: open) {
            HStack(spacing: 10) {
                OrbView(look: OrbLook(calls.liveKit.voices.indicator, lastSide: nil, reduceMotion: reduceMotion),
                        diameter: 30)
                TimelineView(.periodic(from: .now, by: 1)) { clock in
                    Text("\(words.inCall) \(CallClock.text(since: calls.connectedAt, now: clock.date))")
                        .font(.callout.weight(.semibold).monospacedDigit())
                        .foregroundStyle(BeeStyle.ink)
                }
                Spacer()
                Label(words.callScreen, systemImage: "chevron.up")
                    .labelStyle(TrailingIconLabel())
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            .padding(.leading, 8)
            .padding(.trailing, 13)
            .frame(height: 44)
            .background(.regularMaterial, in: .capsule)
            .overlay(Capsule().strokeBorder(BeeStyle.yellow, lineWidth: 1.5))
            .shadow(color: .black.opacity(0.08), radius: 8, y: 2)
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 4)
        .padding(.top, 4)
    }
}

/// The smaller round call buttons under the conversation during a call.
struct SmallCallControl: View {
    let label: String
    let symbol: String
    let on: Bool
    let tint: Color?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 5) {
                Image(systemName: symbol)
                    .font(.title3.weight(.semibold))
                    .foregroundStyle(tint != nil ? .white : on ? BeeStyle.background : BeeStyle.ink)
                    .frame(width: 52, height: 52)
                    .background(tint ?? (on ? BeeStyle.ink : BeeStyle.card), in: .circle)
                Text(label)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}
