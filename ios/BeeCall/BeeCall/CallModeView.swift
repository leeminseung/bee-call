import SwiftUI

/// A background job as the call screen's pill shows it (design D, frames 2 and 5): running, with a short name
/// and how long it has run, or done during this call, when the pill turns yellow and leads to the result card in
/// the conversation. The server's call view will say which jobs a call has; until then a stand-in feeds it.
struct JobPill: Equatable, Identifiable {
    let id: String
    let label: String // short, such as "가격 비교"
    let startedAt: Date
    var arrived: Bool
}

/// Design D's call screen (frame 2): the orb that follows the live voice, the speaking side's line large and the
/// other side's last line small, the job pill, and round mute, speaker and end buttons. "대화 보기" (or a swipe
/// down) goes to the conversation, where the call goes on under a call bar.
struct CallModeView: View {
    let calls: CallManager
    var job: JobPill?
    let showConversation: () -> Void
    @AppStorage("language") private var language = "en"
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var lastSide: VoiceSide?

    private var words: CallWords { CallWords(korean: language == "ko") }

    var body: some View {
        let indicator = calls.liveKit.voices.indicator
        let lines = CallLines(captions: calls.liveKit.captions, speaking: indicator.side)
        VStack(spacing: 0) {
            header
            Spacer(minLength: 24)
            OrbView(look: OrbLook(indicator, lastSide: lastSide ?? lines.currentSide, reduceMotion: reduceMotion),
                    diameter: 172)
            Spacer(minLength: 24)
            lineStack(lines)
                .frame(minHeight: 150, alignment: .top)
            Spacer(minLength: 16)
            if let job {
                JobPillView(job: job, words: words, open: showConversation)
                    .padding(.bottom, 22)
            }
            controls
                .padding(.bottom, 12)
        }
        .padding(.horizontal, 20)
        .background(BeeStyle.background.ignoresSafeArea())
        .onChange(of: indicator.side) { _, side in
            if let side { lastSide = side }
        }
    }

    private var header: some View {
        ZStack {
            VStack(spacing: 2) {
                Text("Bee Call")
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(BeeStyle.ink)
                if let state = words.state(reconnecting: calls.isReconnecting, aiIsThere: calls.aiIsThere,
                                           answered: calls.ownRoom != nil && calls.connectedAt != nil) {
                    Text(state) // until the AI is there, and while the line comes back
                        .font(.footnote)
                        .foregroundStyle(BeeStyle.secondaryInk)
                } else {
                    TimelineView(.periodic(from: .now, by: 1)) { clock in
                        Text(CallClock.text(since: calls.connectedAt, now: clock.date))
                            .font(.footnote.monospacedDigit())
                            .foregroundStyle(BeeStyle.secondaryInk)
                    }
                }
                if let problem = calls.phoneRecordingProblem { // "Save on this iPhone" is on, but nothing is saved
                    Text(MessageWords.words(problem, korean: language == "ko"))
                        .font(.caption)
                        .foregroundStyle(BeeStyle.redText)
                        .multilineTextAlignment(.center)
                        .padding(.top, 2)
                }
            }
            HStack {
                Button(action: showConversation) {
                    Label(words.showConversation, systemImage: "chevron.down")
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(BeeStyle.ink)
                        .padding(.horizontal, 16)
                        .frame(minHeight: 44)
                        .beeGlass(in: .capsule)
                        .overlay(alignment: .topTrailing) {
                            if job?.arrived == true { // a result waits in the conversation
                                Circle().fill(BeeStyle.yellow).frame(width: 9, height: 9).offset(x: 2, y: 2)
                            }
                        }
                }
                .buttonStyle(.plain)
                Spacer()
            }
        }
        .padding(.top, 8)
    }

    @ViewBuilder
    private func lineStack(_ lines: CallLines) -> some View {
        VStack(spacing: 14) {
            if let current = lines.current {
                ArrivingText(text: current.text, arriving: current.arriving)
                    .font(.title2.bold())
                    .foregroundStyle(BeeStyle.ink)
                    .multilineTextAlignment(.center)
                    .lineLimit(4)
                    .minimumScaleFactor(0.8)
            }
            if let other = lines.other {
                (Text(Image(systemName: "circle.fill")).font(.system(size: 7))
                    .foregroundStyle(other.isMine ? BeeStyle.personBlue : BeeStyle.yellow)
                 + Text(" ")
                 + Text(other.isMine ? words.me : "Bee Call").bold()
                 + Text(" " + other.text))
                    .font(.subheadline)
                    .foregroundStyle(BeeStyle.secondaryInk)
                    .multilineTextAlignment(.center)
                    .lineLimit(2)
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.2), value: lines.current?.text)
    }

    private var controls: some View {
        HStack(alignment: .top) {
            CallControl(label: words.mute, symbol: "mic.slash.fill", on: calls.isMuted, tint: nil) {
                Task { await calls.setMuted(!calls.isMuted) }
            }
            Spacer()
            CallControl(label: words.speaker, symbol: "speaker.wave.2.fill", on: calls.isSpeakerOn, tint: nil) {
                calls.setSpeaker(!calls.isSpeakerOn)
            }
            Spacer()
            CallControl(label: words.end, symbol: "xmark", on: false, tint: BeeStyle.red) {
                Task { await calls.endCall() }
            }
            .disabled(calls.isEnding)
        }
        .padding(.horizontal, 24)
    }
}

/// The orb: a sphere lit from the upper left, Bee yellow for the AI and blue for the person, with a halo that
/// grows with the voice.
struct OrbView: View {
    let look: OrbLook
    let diameter: CGFloat

    var body: some View {
        let colors = look.side == .ai
            ? [Color(white: 1), Color(red: 1, green: 0.92, blue: 0.42), BeeStyle.yellow,
               Color(red: 0.95, green: 0.72, blue: 0.02)]
            : [Color(white: 1), Color(red: 0.62, green: 0.80, blue: 1), Color(red: 0.25, green: 0.56, blue: 1),
               Color(red: 0.04, green: 0.37, blue: 0.85)]
        ZStack {
            Circle()
                .fill(colors[2])
                .blur(radius: diameter * 0.12)
                .opacity(0.15 + 0.35 * look.glow)
                .scaleEffect(1.1)
            Circle()
                .fill(RadialGradient(colors: colors, center: UnitPoint(x: 0.36, y: 0.3), startRadius: 0,
                                     endRadius: diameter * 0.78))
                .shadow(color: colors[3].opacity(0.25), radius: 10, y: 6)
        }
        .frame(width: diameter, height: diameter)
        .scaleEffect(look.scale)
        .animation(look.animates ? .easeOut(duration: 0.12) : nil, value: look)
        .accessibilityHidden(true)
    }
}

/// A round call button with its name under it, as on design D's call screen: grey, inverted while on, or a
/// colour of its own (the red end).
struct CallControl: View {
    let label: String
    let symbol: String
    let on: Bool
    let tint: Color?
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 8) {
                Image(systemName: symbol)
                    .font(.title2.weight(.semibold))
                    .foregroundStyle(tint != nil ? .white : on ? BeeStyle.background : BeeStyle.ink)
                    .frame(width: 66, height: 66)
                    .background(tint ?? (on ? BeeStyle.ink : BeeStyle.controlFill), in: .circle)
                Text(label)
                    .font(.footnote)
                    .foregroundStyle(BeeStyle.secondaryInk)
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
        .accessibilityAddTraits(on ? .isSelected : [])
    }
}

/// The job pill: grey while the job runs, with how long it has run; yellow once its result has arrived, leading
/// to the result card in the conversation.
struct JobPillView: View {
    let job: JobPill
    let words: CallWords
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(spacing: 8) {
                if job.arrived {
                    Image(systemName: "checkmark")
                        .font(.subheadline.weight(.bold))
                    Text(words.resultArrived)
                } else {
                    ProgressView()
                        .controlSize(.small)
                    TimelineView(.periodic(from: .now, by: 1)) { clock in
                        HStack(spacing: 0) { // a long job title is cut, its clock never
                            Text(job.label)
                                .lineLimit(1)
                            Text(words.runningFor(CallClock.text(since: job.startedAt, now: clock.date)))
                                .monospacedDigit()
                                .fixedSize()
                        }
                    }
                }
                Image(systemName: "chevron.right")
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(job.arrived ? BeeStyle.onYellow : BeeStyle.secondaryInk)
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(job.arrived ? BeeStyle.onYellow : BeeStyle.ink)
            .padding(.horizontal, 18)
            .frame(minHeight: 44)
            .background(job.arrived ? BeeStyle.yellow : BeeStyle.controlFill, in: .capsule)
        }
        .buttonStyle(.plain)
        .sensoryFeedback(.success, trigger: job.arrived) { _, arrived in arrived } // one short tap on arrival
    }
}

/// Design D's words on the call screen, in the person's call language.
struct CallWords {
    let korean: Bool

    var showConversation: String { korean ? "대화 보기" : "Conversation" }
    var me: String { korean ? "나" : "You" }
    var mute: String { korean ? "음소거" : "Mute" }
    var speaker: String { korean ? "스피커" : "Speaker" }
    var end: String { korean ? "끝내기" : "End" }
    var resultArrived: String { korean ? "결과 도착 · 대화에서 보기" : "Result ready · See it" }

    /// Under the name until the AI is there, and while LiveKit gets a dropped line back; nil once the clock runs.
    /// `answered`: Bee Call's own call, answered, whose AI is already in the room.
    func state(reconnecting: Bool, aiIsThere: Bool, answered: Bool) -> String? {
        if reconnecting { return korean ? "다시 연결하는 중…" : "Reconnecting…" }
        if aiIsThere { return nil }
        if answered { return korean ? "연결하는 중…" : "Connecting…" }
        return korean ? "전화 거는 중…" : "Calling…"
    }

    /// After a running job's title on the pill: " 중 · 1:07".
    func runningFor(_ clock: String) -> String {
        korean ? " 중 · \(clock)" : " · \(clock)"
    }
}
