import QuickLook
import SwiftUI

/// Design D's words in the conversation, in the person's call language.
struct ConversationWords {
    let korean: Bool

    var unfold: String { korean ? "대화 펼치기" : "Show the talk" }
    var fold: String { korean ? "대화 접기" : "Hide the talk" }
    var earlier: String { korean ? "이전 통화 보기" : "Earlier calls" }
    var wroteToBee: String { korean ? "Bee에 쓴 것" : "Written to Bee" }
    var takeOutHint: String { korean ? "빼려면 '빼 줘'라고 말하세요" : "Say “take it out” to remove it" }
    var asks: String { korean ? "Bee Call이 물어봐요" : "Bee Call asks" }
    var now: String { korean ? "지금" : "Now" }
    var notNow: String { korean ? "괜찮아요" : "Not now" }
    var call: String { korean ? "전화 걸기" : "Call" }
    var inCall: String { korean ? "통화 중" : "In a call" }
    var callScreen: String { korean ? "통화 화면" : "Call screen" }
    var notRecordedYet: String { korean ? "통화 기록을 준비하는 중이에요" : "The call's record is on its way" }
    var noRecord: String { korean ? "이 통화는 기록이 없어요" : "This call has no record" }
    var loading: String { korean ? "불러오는 중" : "Loading" }

    func kind(_ kind: BeeWriteKind) -> String {
        switch kind {
        case .todoCreate, .todoAccept: korean ? "할 일" : "To-do"
        case .factConfirm, .factUpdate, .unrecognized: korean ? "사실" : "Fact"
        }
    }

    /// The line over a Bee write during the call: "Bee 할 일에 넣었어요".
    func added(_ kind: BeeWriteKind) -> String { korean ? "Bee \(self.kind(kind))에 넣었어요" : "Added to Bee: \(self.kind(kind))" }

    /// A Bee write's state, after the call or when it did not go in.
    func state(_ write: BeeWrite) -> String {
        switch write.state {
        case .written: korean ? "넣음" : "Added"
        case .undone: korean ? "뺌" : "Taken out"
        case .unknown: korean ? "Bee에서 확인해 주세요" : "Check it in Bee"
        case .failed: write.willRetry ? (korean ? "통화 뒤에 다시 넣어 볼게요" : "Trying again after the call")
            : (korean ? "넣지 못함" : "Not added")
        case .unrecognized: ""
        }
    }

    func working(for clock: String) -> String { korean ? "작업 중 · \(clock)째" : "Working · \(clock)" }
    func finished(justNow: Bool, took clock: String) -> String {
        korean ? "\(justNow ? "방금 끝남" : "작업 끝") · \(clock) 걸림" : "\(justNow ? "Just finished" : "Done") · took \(clock)"
    }
    var didNotFinish: String { korean ? "끝내지 못한 작업" : "Didn't finish" }
    func failure(_ failure: JobFailure?) -> String {
        failure == .callEnded ? (korean ? "통화가 먼저 끝났어요" : "The call ended first")
            : (korean ? "문제가 생겨 멈췄어요" : "Something went wrong")
    }
    func step(_ text: String) -> String { korean ? "지금 \(text)" : "Now: \(text)" }

    /// A file's chip, by its kind rather than its name, which the worker keeps to ASCII.
    func file(_ file: JobFile) -> String {
        switch file.kind {
        case .report: korean ? "보고서" : "Report"
        case .table: korean ? "표" : "Table"
        case .chart: korean ? "차트" : "Chart"
        case .other, .unrecognized: file.name
        }
    }

    // The cards of a call's record.
    func prepTitle(fromBee: Bool) -> String {
        fromBee ? (korean ? "통화 전에 Bee에서 본 것" : "Read in Bee before the call")
            : (korean ? "통화 전에 본 것" : "Read before the call")
    }
    var lookedUp: String { korean ? "찾아본 것" : "Looked up" }
    var whyCalled: String { korean ? "Bee Call이 전화한 이유" : "Why Bee Call called" }
    var me: String { korean ? "나" : "Me" }
    var other: String { korean ? "상대" : "Them" }
}

/// One entry of a call's section or record: a bubble, a job (small inside a folded call), or a Bee write (with
/// how to take it back while the call is on).
struct ThreadEntryView: View {
    let entry: CallThread.Entry
    let words: ConversationWords
    var live = false
    var folded = false
    var glowing: String?

    var body: some View {
        switch entry {
        case .bubble(_, let speaker, let text):
            SpeechBubble(speaker: speaker, text: text)
        case .item(.speech(let speech)):
            SpeechBubble(speaker: speech.speaker, text: speech.text)
        case .item(.job(let job)):
            if folded {
                JobCompactRow(job: job, words: words)
            } else {
                JobCardView(job: job, words: words, glowing: glowing == job.id)
            }
        case .item(.beeWrite(let write)):
            HStack {
                BeeWriteView(write: write, live: live, words: words)
                Spacer(minLength: 0)
            }
        case .item(.unrecognized):
            EmptyView()
        }
    }
}

// MARK: - Bubbles

/// A run of speech as a bubble: the AI's light yellow on the left, the person's blue on the right, with a tail.
struct SpeechBubble: View {
    let speaker: Speaker
    let text: String

    private var mine: Bool { speaker == .person }

    var body: some View {
        HStack {
            if mine { Spacer(minLength: 60) }
            Text(text)
                .font(.body)
                .foregroundStyle(mine ? .white : BeeStyle.aiBubbleText)
                .padding(.horizontal, 13)
                .padding(.vertical, 8)
                .background(BubbleBackground(mine: mine, color: mine ? BeeStyle.personBlue : BeeStyle.aiBubble))
                .frame(maxWidth: 290, alignment: mine ? .trailing : .leading)
            if !mine { Spacer(minLength: 60) }
        }
    }
}

/// A bubble's background: a rounded body and, filled apart from it, a tail curling out of its lower outer
/// corner, as in Messages.
struct BubbleBackground: View {
    let mine: Bool
    let color: Color

    var body: some View {
        RoundedRectangle(cornerRadius: 18)
            .fill(color)
            .overlay(alignment: mine ? .bottomTrailing : .bottomLeading) {
                BubbleTail(mine: mine)
                    .fill(color)
                    .frame(width: 16, height: 18)
                    .offset(x: mine ? 5 : -5)
            }
    }
}

/// The tail: from inside the body's lower edge out to a point past its outer corner, and back along the bottom.
nonisolated struct BubbleTail: Shape {
    let mine: Bool

    func path(in rect: CGRect) -> Path {
        let w = rect.width, h = rect.height
        var path = Path()
        path.move(to: CGPoint(x: w * 0.1, y: 0))
        path.addCurve(to: CGPoint(x: w, y: h), control1: CGPoint(x: w * 0.25, y: h * 0.65),
                      control2: CGPoint(x: w * 0.6, y: h * 0.95))
        path.addCurve(to: CGPoint(x: 0, y: h * 0.72), control1: CGPoint(x: w * 0.55, y: h * 1.05),
                      control2: CGPoint(x: w * 0.2, y: h * 0.95))
        path.closeSubpath()
        guard !mine else { return path }
        return path.applying(CGAffineTransform(scaleX: -1, y: 1).translatedBy(x: -w, y: 0))
    }
}

// MARK: - Cards

/// The eyebrow line over a card: a small icon and a few grey words.
struct Eyebrow: View {
    let symbol: String?
    let text: String
    var spinning = false

    var body: some View {
        HStack(spacing: 6) {
            if spinning {
                ProgressView().controlSize(.mini)
            } else if symbol == "checkmark.circle.fill" { // a finished job: a dark check on Bee yellow
                Image(systemName: "checkmark.circle.fill")
                    .symbolRenderingMode(.palette)
                    .foregroundStyle(BeeStyle.onYellow, BeeStyle.yellow)
                    .font(.footnote.weight(.bold))
            } else if let symbol {
                Image(systemName: symbol).font(.caption.weight(.bold))
            }
            Text(text)
        }
        .font(.footnote.weight(.semibold))
        .foregroundStyle(BeeStyle.secondaryInk)
    }
}

/// A background job in the conversation: running, with how long and its newest step; finished, with what it found,
/// its chart and its files; or stopped. A result that just arrived glows yellow for a moment (frame 5).
struct JobCardView: View {
    let job: Job
    let words: ConversationWords
    var glowing = false
    @Environment(FilePreview.self) private var files: FilePreview? // where a file opens, when the screen holds one
    @State private var opened: URL? // else here

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            eyebrow
            Text(job.title)
                .font(.body.weight(.semibold))
                .foregroundStyle(BeeStyle.ink)
                .padding(.top, 3)
            switch job.state {
            case .done:
                if let summary = job.summary {
                    Text(summary)
                        .font(.subheadline)
                        .foregroundStyle(BeeStyle.ink)
                        .padding(.top, 5)
                }
            case .failed:
                Text(words.failure(job.failure))
                    .font(.subheadline)
                    .foregroundStyle(BeeStyle.secondaryInk)
                    .padding(.top, 5)
            default:
                if let step = job.step {
                    Text(words.step(step))
                        .font(.subheadline)
                        .foregroundStyle(BeeStyle.secondaryInk)
                        .padding(.top, 5)
                }
            }
            ForEach(job.files.filter { $0.kind == .chart }, id: \.url) { chart in
                VStack(alignment: .leading, spacing: 6) {
                    if let title = chart.title {
                        Text(title)
                            .font(.caption)
                            .foregroundStyle(BeeStyle.secondaryInk)
                    }
                    if let image = chart.imageURL { ServerImage(path: image) }
                }
                .padding(10)
                .background(BeeStyle.raised, in: .rect(cornerRadius: 14))
                .padding(.top, 12)
            }
            let chips = job.files.filter { $0.kind != .chart && $0.kind != .unrecognized }
            if !chips.isEmpty {
                HStack(spacing: 8) {
                    ForEach(chips, id: \.url) { file in
                        Button {
                            Task {
                                guard let url = await ServerFiles.shared.open(file) else { return }
                                if let files { files.url = url } else { opened = url }
                            }
                        } label: {
                            FileChip(label: words.file(file), kind: file.kind)
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.top, 12)
            }
        }
        .padding(.horizontal, 14)
        .padding(.top, 12)
        .padding(.bottom, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BeeStyle.card, in: .rect(cornerRadius: 20))
        .overlay {
            RoundedRectangle(cornerRadius: 20).strokeBorder(BeeStyle.yellow, lineWidth: 2.5).opacity(glowing ? 1 : 0)
        }
        .quickLookPreview($opened)
    }

    @ViewBuilder
    private var eyebrow: some View {
        switch job.state {
        case .done:
            Eyebrow(symbol: "checkmark.circle.fill",
                    text: words.finished(justNow: glowing, took: CallLine.length(job.durationS ?? 0, korean: words.korean)))
        case .failed:
            Eyebrow(symbol: "xmark.circle", text: words.didNotFinish)
        default:
            TimelineView(.periodic(from: .now, by: 1)) { clock in
                Eyebrow(symbol: nil, text: words.working(for: CallLine.length(
                    Int(clock.date.timeIntervalSince(job.startedAt.date)), korean: words.korean)), spinning: true)
            }
        }
    }
}

/// A file a job made, as a white chip named by its kind.
struct FileChip: View {
    let label: String
    let kind: FileKind

    var body: some View {
        Label(label, systemImage: kind == .table ? "tablecells" : "doc.text")
            .font(.subheadline.weight(.medium))
            .foregroundStyle(BeeStyle.ink)
            .padding(.horizontal, 12)
            .frame(minHeight: 36)
            .background(BeeStyle.raised, in: .capsule)
            .overlay(Capsule().strokeBorder(BeeStyle.separator, lineWidth: 0.5))
    }
}

/// A finished job inside a folded call: its chart small, its title, and what it made.
struct JobCompactRow: View {
    let job: Job
    let words: ConversationWords

    var body: some View {
        HStack(spacing: 12) {
            Group {
                if let chart = job.files.first(where: { $0.kind == .chart })?.imageURL {
                    ServerImage(path: chart)
                } else {
                    Image(systemName: job.state == .failed ? "xmark.circle" : "doc.text")
                        .foregroundStyle(BeeStyle.secondaryInk)
                }
            }
            .frame(width: 58, height: 46)
            .background(BeeStyle.raised, in: .rect(cornerRadius: 11))
            .overlay(RoundedRectangle(cornerRadius: 11).strokeBorder(BeeStyle.separator, lineWidth: 0.5))
            VStack(alignment: .leading, spacing: 2) {
                Text(job.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(BeeStyle.ink)
                    .lineLimit(1)
                Text(job.state == .failed ? words.didNotFinish : job.files.map(words.file).joined(separator: " · "))
                    .font(.footnote)
                    .foregroundStyle(BeeStyle.secondaryInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(BeeStyle.card, in: .rect(cornerRadius: 20))
    }
}

/// Something written to Bee: during the call with how to take it back, afterwards with what became of it.
struct BeeWriteView: View {
    let write: BeeWrite
    let live: Bool
    let words: ConversationWords

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "checklist")
                .font(.footnote.weight(.bold))
                .foregroundStyle(BeeStyle.onYellow)
                .frame(width: 30, height: 30)
                .background(BeeStyle.yellow, in: .circle)
            VStack(alignment: .leading, spacing: 1) {
                Text(live && write.state == .written ? words.added(write.kind) : words.wroteToBee)
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(BeeStyle.secondaryInk)
                Text(live && write.state == .written ? (write.text ?? words.kind(write.kind))
                     : [words.kind(write.kind), write.text].compactMap { $0 }.joined(separator: ": "))
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(BeeStyle.ink)
                if live && write.undoable {
                    Text(words.takeOutHint)
                        .font(.footnote)
                        .foregroundStyle(BeeStyle.secondaryInk)
                        .padding(.top, 1)
                }
            }
            if !(live && write.state == .written) {
                Spacer(minLength: 8)
                Label(words.state(write), systemImage: write.state == .written ? "checkmark" : "exclamationmark.circle")
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(BeeStyle.secondaryInk)
                    .padding(.top, 8)
            }
        }
        .padding(.leading, 9)
        .padding(.trailing, 14)
        .padding(.vertical, 9)
        .background(BeeStyle.card, in: .rect(cornerRadius: 20))
        .frame(maxWidth: 340, alignment: .leading)
    }
}

/// Bee Call asking for a call, with its three answers.
struct OfferCardView: View {
    let offer: Offer
    let language: String
    let answering: Bool
    let answer: (OfferCenter.Choice) -> Void

    private var words: ConversationWords { ConversationWords(korean: language == "ko") }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(offer.topic)
                .font(.body.weight(.semibold))
                .foregroundStyle(BeeStyle.ink)
            TimelineView(.everyMinute) { clock in
                Text(offer.askedLine(language: language, now: clock.date))
                    .font(.subheadline)
                    .foregroundStyle(BeeStyle.ink)
                    .padding(.top, 5)
            }
            HStack(spacing: 8) {
                if let later = offer.laterButton(language: language) {
                    answerButton(later, filled: true) { answer(.later) }
                }
                answerButton(words.now, filled: false) { answer(.now) }
                answerButton(words.notNow, filled: false) { answer(.decline) }
            }
            .disabled(answering)
            .padding(.top, 12)
        }
        .padding(.horizontal, 14)
        .padding(.top, 12)
        .padding(.bottom, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BeeStyle.card, in: .rect(cornerRadius: 20))
    }

    private func answerButton(_ title: String, filled: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(.callout.weight(.semibold))
                .foregroundStyle(filled ? BeeStyle.onYellow : BeeStyle.ink)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(filled ? BeeStyle.yellow : BeeStyle.raised, in: .capsule)
                .overlay(Capsule().strokeBorder(BeeStyle.separator, lineWidth: 0.5).opacity(filled ? 0 : 1))
        }
        .buttonStyle(.plain)
    }
}

/// The card of a call Bee Call placed (2.2): what it is about, why it called, and the Bee conversation behind it.
struct WhyCallingCard: View {
    let topic: String?
    let why: WhyCalling
    let words: ConversationWords

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Eyebrow(symbol: "phone.arrow.down.left", text: words.whyCalled)
            if let topic {
                Text(topic)
                    .font(.body.weight(.semibold))
                    .foregroundStyle(BeeStyle.ink)
                    .padding(.top, 4)
            }
            if let reason = why.reason {
                Text(reason)
                    .font(.subheadline)
                    .foregroundStyle(BeeStyle.ink)
                    .padding(.top, 5)
            }
            if let conversation = why.conversation {
                Divider().padding(.vertical, 10)
                if let summary = conversation.summary {
                    Text(summary)
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(BeeStyle.secondaryInk)
                }
                ForEach(Array(conversation.quotes.filter { $0.speaker != .unrecognized }.enumerated()), id: \.offset) { _, quote in
                    (Text((quote.speaker == .person ? words.me : words.other) + "  ").bold() + Text(quote.text))
                        .font(.subheadline)
                        .foregroundStyle(BeeStyle.ink)
                        .padding(.top, 5)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BeeStyle.card, in: .rect(cornerRadius: 20))
    }
}

/// What Bee Call read before the call (2.3): its note, and what it looked up.
struct PrepCard: View {
    let prep: Prep
    let words: ConversationWords

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Eyebrow(symbol: "eye", text: words.prepTitle(fromBee: prep.fromBee))
            if let note = prep.note {
                Text(note)
                    .font(.subheadline)
                    .foregroundStyle(BeeStyle.ink)
                    .padding(.top, 6)
            }
            let lines = prep.lookedUp.compactMap(line)
            if !lines.isEmpty {
                Divider().padding(.vertical, 10)
                Text(words.lookedUp)
                    .font(.caption)
                    .foregroundStyle(BeeStyle.secondaryInk)
                ForEach(lines, id: \.text) { line in
                    Label(line.text, systemImage: line.symbol)
                        .font(.subheadline)
                        .foregroundStyle(BeeStyle.ink)
                        .padding(.top, 6)
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(BeeStyle.card, in: .rect(cornerRadius: 20))
    }

    private func line(_ read: LookedUp) -> (text: String, symbol: String)? {
        let korean = words.korean
        switch read {
        case .conversation(let summary, let at):
            let what = summary ?? (korean ? "Bee 대화" : "A Bee conversation")
            let when = at.map { "\(CallLine.day($0, korean: korean, now: .now)) \(CallLine.time($0, korean: korean))" }
            return ([what, when].compactMap { $0 }.joined(separator: ", "), "bubble.left.and.bubble.right")
        case .day(let day):
            let today = Calendar.current.dateComponents([.year, .month, .day], from: .now)
            if today.year == day.year, today.month == day.month, today.day == day.day {
                return (korean ? "오늘 요약" : "Today's summary", "calendar")
            }
            return (korean ? "\(day.month)월 \(day.day)일 요약" : "Summary of \(day.month)/\(day.day)", "calendar")
        case .search(let query):
            return (korean ? "'\(query)' 찾아봄" : "Searched “\(query)”", "magnifyingglass")
        case .unrecognized:
            return nil
        }
    }
}

/// The grey line over a section: when, who, how long; for a call it opens the call's record.
struct Stamp: View {
    let text: String
    var live = false
    var opens = false

    var body: some View {
        HStack(spacing: 4) {
            if live {
                Circle().fill(BeeStyle.yellow).frame(width: 7, height: 7)
                    .overlay(Circle().strokeBorder(Color(red: 0.9, green: 0.77, blue: 0), lineWidth: 1))
            }
            Text(text)
            if opens {
                Image(systemName: "chevron.right").font(.system(size: 9, weight: .bold))
            }
        }
        .font(.caption)
        .foregroundStyle(BeeStyle.secondaryInk)
        .frame(maxWidth: .infinity)
    }
}

/// The booked call pinned at the top (design D, frame 1): when, about what, and "전화 취소", which asks first
/// (CancelBookedCallAlert). A call being placed now says so, and can no longer be called off.
struct BookedPin: View {
    let offer: Offer
    let language: String
    let cancel: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                TimelineView(.everyMinute) { clock in
                    Label(offer.state == .calling ? Offer.callingLine(language: language)
                          : offer.bookedLine(language: language, now: clock.date), systemImage: "clock")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(BeeStyle.ink)
                        .lineLimit(1)
                }
                Text(offer.topic)
                    .font(.footnote)
                    .foregroundStyle(BeeStyle.secondaryInk)
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
            if offer.state == .booked {
                Button(action: cancel) {
                    Text(Offer.cancelButton(language: language))
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(BeeStyle.redText)
                        .padding(.horizontal, 13)
                        .frame(height: 36)
                        .background(BeeStyle.subtleFill, in: .capsule)
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.leading, 14)
        .padding(.trailing, 9)
        .padding(.vertical, 8)
        .beeGlass(in: .rect(cornerRadius: 24))
    }
}
