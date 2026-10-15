import SwiftUI

/// The conversation: every section in time order, the newest at the bottom (design D). A call that is over is
/// folded to its results until "대화 펼치기"; the call that is on shows everything, with a running job at the
/// very bottom like a typing indicator. `jump` scrolls to a card (a result that arrived during the call).
struct ConversationThread: View {
    let sections: [ThreadSection]
    let words: ConversationWords
    var glowing: String?
    @Binding var jump: String?
    let answer: (String, OfferCenter.Choice) -> Void
    let openCall: (ConversationCall) -> Void
    var topInset: CGFloat = 0
    var bottomInset: CGFloat = 0
    @State private var expanded: Set<String> = []

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 10) {
                    ForEach(sections) { section in
                        switch section {
                        case .call(let call): callSection(call)
                        case .offer(let offer): offerSection(offer)
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
    private func callSection(_ call: ConversationCall) -> some View {
        let isExpanded = expanded.contains(call.id)
        VStack(spacing: 10) {
            Button { if !call.isLive { openCall(call) } } label: {
                Stamp(text: CallInfo.line(call, korean: words.korean, now: .now, calendar: .current), live: call.isLive,
                      opens: !call.isLive)
            }
            .buttonStyle(.plain)
            .padding(.top, 8)
            if !call.isLive {
                Text(call.title)
                    .font(call.entries.isEmpty ? .subheadline : .subheadline.weight(.semibold))
                    .foregroundStyle(call.entries.isEmpty ? Color.secondary : BeeStyle.ink)
                    .multilineTextAlignment(.center)
                    .padding(.top, -6)
                if call.canUnfold {
                    Button {
                        withAnimation(.easeInOut(duration: 0.25)) {
                            if isExpanded { expanded.remove(call.id) } else { expanded.insert(call.id) }
                        }
                    } label: {
                        Label(isExpanded ? words.fold : words.unfold,
                              systemImage: isExpanded ? "chevron.up" : "chevron.down")
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
            }
            ForEach(ordered(call, expanded: isExpanded)) { entry in
                entryView(entry, in: call, expanded: isExpanded)
                    .id(entry.id)
            }
        }
    }

    /// The entries to show, with a running job of the call that is on moved to the very bottom.
    private func ordered(_ call: ConversationCall, expanded: Bool) -> [ConversationEntry] {
        let shown = call.shown(expanded: expanded)
        guard call.isLive else { return shown }
        let running = shown.filter { if case .job(let job) = $0 { !job.isDone } else { false } }
        return shown.filter { entry in !running.contains { $0.id == entry.id } } + running
    }

    @ViewBuilder
    private func entryView(_ entry: ConversationEntry, in call: ConversationCall, expanded: Bool) -> some View {
        switch entry {
        case .turn(let turn):
            TurnBubble(turn: turn)
        case .job(let job):
            if !call.isLive && !expanded {
                JobCompactRow(job: job, korean: words.korean)
            } else {
                JobCardView(job: job, words: words, glowing: glowing == job.id)
            }
        case .beeWrite(let write):
            HStack {
                BeeWriteView(write: write, live: call.isLive, words: words)
                Spacer(minLength: 0)
            }
        }
    }

    @ViewBuilder
    private func offerSection(_ offer: OfferCard) -> some View {
        VStack(spacing: 10) {
            Stamp(text: CallInfo.day(offer.askedAt, korean: words.korean, now: .now, calendar: .current) + " "
                  + CallInfo.time(offer.askedAt, korean: words.korean, calendar: .current) + " · " + words.asks)
                .padding(.top, 8)
            OfferCardView(offer: offer, words: words) { answer(offer.id, $0) }
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
    var glowing: String?
    @Binding var jump: String?
    let showCallScreen: () -> Void
    let openSettings: () -> Void
    let openCall: (ConversationCall) -> Void
    let startCall: () -> Void
    var zoom: Namespace.ID? // the call screen grows out of the call bar or the call button (ConversationRoot)
    @AppStorage("language") private var language = "en"

    private var words: ConversationWords { ConversationWords(korean: language == "ko") }
    private var booked: [OfferCenter.Offer] { offers.open.filter { $0.state == "booked" } }

    var body: some View {
        ZStack {
            BeeStyle.background.ignoresSafeArea()
            ConversationThread(sections: source.sections, words: words, glowing: glowing, jump: $jump,
                               answer: { id, choice in Task { await offers.answer(id, choice) } },
                               openCall: openCall,
                               topInset: calls.isInCall ? 64 : (booked.isEmpty ? 70 : 140),
                               bottomInset: calls.isInCall ? 110 : 170)
            VStack(spacing: 8) {
                VStack(spacing: 8) {
                    if calls.isInCall {
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
                if calls.isInCall {
                    smallControls
                } else {
                    callButton
                }
            }
            .padding(.horizontal, 12)
        }
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
                    if let bee = source.beeLine {
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
