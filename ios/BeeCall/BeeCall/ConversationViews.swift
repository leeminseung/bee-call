import Charts
import SwiftUI

/// What the conversation is made of besides calls: Bee Call asking for a call (design D, frame 1).
struct OfferCard: Identifiable, Equatable {
    let id: String
    var askedAt: Date
    var topic: String
    var question: String // "오늘 저녁 8:30에 잠깐 통화할까요?"
    var suggestedTime: String? // "8:30", for the yellow answer
}

/// One section of the conversation, in time order: a call, or an ask.
enum ThreadSection: Identifiable, Equatable {
    case call(ConversationCall)
    case offer(OfferCard)

    var id: String {
        switch self {
        case .call(let call): "call/\(call.id)"
        case .offer(let offer): "offer/\(offer.id)"
        }
    }
}

/// Where the conversation comes from: the server's call view once its contract is settled, a stand-in until
/// then (design D's demo).
@MainActor
protocol ConversationSource: AnyObject {
    var sections: [ThreadSection] { get }
    /// "Bee 연결됨" under the name, or nothing for a person without Bee.
    var beeLine: String? { get }
}

/// Design D's words in the conversation, in the person's call language.
struct ConversationWords {
    let korean: Bool

    var unfold: String { korean ? "대화 펼치기" : "Show the talk" }
    var fold: String { korean ? "대화 접기" : "Hide the talk" }
    var wroteToBee: String { korean ? "Bee에 쓴 것" : "Written to Bee" }
    var added: String { korean ? "넣음" : "Added" }
    var removed: String { korean ? "뺌" : "Taken out" }
    var takeOutHint: String { korean ? "빼려면 '빼 줘'라고 말하세요" : "Say “take it out” to remove it" }
    var asks: String { korean ? "Bee Call이 물어봐요" : "Bee Call asks" }
    var now: String { korean ? "지금" : "Now" }
    var notNow: String { korean ? "괜찮아요" : "Not now" }
    var call: String { korean ? "전화 걸기" : "Call" }
    var inCall: String { korean ? "통화 중" : "In a call" }
    var callScreen: String { korean ? "통화 화면" : "Call screen" }

    func addedToBee(_ kind: String) -> String { korean ? "Bee \(kind)에 넣었어요" : "Added to Bee: \(kind)" }
    func at(_ time: String) -> String { korean ? "\(time)에" : "At \(time)" }
    func working(for clock: String) -> String { korean ? "작업 중 · \(clock)째" : "Working · \(clock)" }
    func finished(justNow: Bool, took clock: String) -> String {
        korean ? "\(justNow ? "방금 끝남" : "작업 끝") · \(clock) 걸림" : "\(justNow ? "Just finished" : "Done") · took \(clock)"
    }
    func step(_ text: String) -> String { korean ? "지금 \(text)" : "Now: \(text)" }
}

// MARK: - Bubbles

/// A turn as a bubble: the AI's light yellow on the left, the person's blue on the right, with a tail.
struct TurnBubble: View {
    let turn: Turn

    var body: some View {
        HStack {
            if turn.mine { Spacer(minLength: 60) }
            Text(turn.text)
                .font(.body)
                .foregroundStyle(turn.mine ? .white : BeeStyle.ink)
                .padding(.horizontal, 13)
                .padding(.vertical, 8)
                .background(BubbleBackground(mine: turn.mine, color: turn.mine ? BeeStyle.personBlue : BeeStyle.aiBubble))
                .frame(maxWidth: 290, alignment: turn.mine ? .trailing : .leading)
            if !turn.mine { Spacer(minLength: 60) }
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
        .foregroundStyle(.secondary)
    }
}

/// A background job in the conversation: running, with its newest step; or finished, with its result, chart and
/// files. A result that just arrived glows yellow for a moment (frame 5).
struct JobCardView: View {
    let job: JobCard
    let words: ConversationWords
    var glowing = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let finished = job.finishedAt {
                Eyebrow(symbol: "checkmark.circle.fill",
                        text: words.finished(justNow: glowing, took: CallInfo.length(finished.timeIntervalSince(job.startedAt),
                                                                                       korean: words.korean)))
            } else {
                TimelineView(.periodic(from: .now, by: 1)) { clock in
                    Eyebrow(symbol: nil, text: words.working(for: CallInfo.length(clock.date.timeIntervalSince(job.startedAt),
                                                                                   korean: words.korean)),
                            spinning: true)
                }
            }
            Text(job.title)
                .font(.body.weight(.semibold))
                .foregroundStyle(BeeStyle.ink)
                .padding(.top, 3)
            if let result = job.result, job.isDone {
                Text(result)
                    .font(.subheadline)
                    .foregroundStyle(BeeStyle.ink)
                    .padding(.top, 5)
            } else if let step = job.step {
                Text(words.step(step))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.top, 5)
            }
            if let chart = job.chart, job.isDone {
                JobChartView(chart: chart)
                    .padding(.top, 12)
            }
            if !job.files.isEmpty, job.isDone {
                HStack(spacing: 8) {
                    ForEach(job.files, id: \.self) { FileChip(name: $0) }
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
    }
}

/// A file a job made, as a white chip.
struct FileChip: View {
    let name: String

    var body: some View {
        Label(name, systemImage: name.hasSuffix(".csv") ? "tablecells" : "doc.text")
            .font(.subheadline.weight(.medium))
            .foregroundStyle(BeeStyle.ink)
            .padding(.horizontal, 12)
            .frame(height: 36)
            .background(BeeStyle.background, in: .capsule)
            .overlay(Capsule().strokeBorder(.black.opacity(0.08), lineWidth: 0.5))
    }
}

/// The small bar chart a job made: Bee yellow and grey bars, values on top.
struct JobChartView: View {
    let chart: JobChart

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(chart.title)
                .font(.caption)
                .foregroundStyle(.secondary)
            Chart {
                ForEach(chart.groups, id: \.name) { group in
                    ForEach(Array(group.values.enumerated()), id: \.offset) { index, value in
                        BarMark(x: .value("구성", group.name), y: .value("값", value))
                            .foregroundStyle(by: .value("계열", chart.series[index]))
                            .position(by: .value("계열", chart.series[index]))
                            .annotation(position: .top) {
                                Text(Self.short(value)).font(.system(size: 9, weight: .semibold))
                            }
                    }
                }
            }
            .chartForegroundStyleScale(domain: chart.series, range: [BeeStyle.yellow, Color(white: 0.62)])
            .chartYAxis(.hidden)
            .chartXAxis { AxisMarks { AxisValueLabel() } } // names under the groups, no grid lines
            .chartLegend(position: .bottom, alignment: .leading)
            .frame(height: 110)
        }
        .padding(10)
        .background(BeeStyle.background, in: .rect(cornerRadius: 14))
    }

    /// 1,590,000 as "159만", the design's unit.
    static func short(_ value: Double) -> String {
        value >= 10_000 ? "\(Int(value / 10_000))만" : "\(Int(value))"
    }
}

/// A finished job inside a folded call: a chart thumbnail, its title and what it made.
struct JobCompactRow: View {
    let job: JobCard
    let korean: Bool

    var body: some View {
        HStack(spacing: 12) {
            ChartThumbnail()
                .frame(width: 58, height: 46)
                .background(BeeStyle.background, in: .rect(cornerRadius: 11))
            VStack(alignment: .leading, spacing: 2) {
                Text(job.title)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(BeeStyle.ink)
                    .lineLimit(1)
                Text((job.files + (job.chart != nil ? [korean ? "차트" : "Chart"] : [])).joined(separator: " · "))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .background(BeeStyle.card, in: .rect(cornerRadius: 20))
    }
}

/// Four little bars, the thumbnail of a result with a chart.
struct ChartThumbnail: View {
    var body: some View {
        HStack(alignment: .bottom, spacing: 3) {
            ForEach(Array([0.45, 0.62, 0.55, 0.8].enumerated()), id: \.offset) { index, height in
                RoundedRectangle(cornerRadius: 1.5)
                    .fill(index.isMultiple(of: 2) ? BeeStyle.yellow : Color(white: 0.62))
                    .frame(width: 6, height: 26 * height)
            }
        }
    }
}

/// Something written to Bee: during the call with how to take it back, afterwards as a plain record.
struct BeeWriteView: View {
    let write: BeeWriteCard
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
                Text(live ? words.addedToBee(write.kind) : words.wroteToBee)
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(live ? write.text : "\(write.kind): \(write.text)")
                    .font(.callout.weight(.semibold))
                    .foregroundStyle(BeeStyle.ink)
                if live {
                    Text(words.takeOutHint)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .padding(.top, 1)
                }
            }
            if !live {
                Spacer(minLength: 8)
                Label(write.added ? words.added : words.removed, systemImage: "checkmark")
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(.secondary)
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
    let offer: OfferCard
    let words: ConversationWords
    let answer: (OfferCenter.Choice) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(offer.topic)
                .font(.body.weight(.semibold))
                .foregroundStyle(BeeStyle.ink)
            Text(offer.question)
                .font(.subheadline)
                .foregroundStyle(BeeStyle.ink)
                .padding(.top, 5)
            HStack(spacing: 8) {
                if let time = offer.suggestedTime {
                    answerButton(words.at(time), filled: true) { answer(.later) }
                }
                answerButton(words.now, filled: false) { answer(.now) }
                answerButton(words.notNow, filled: false) { answer(.decline) }
            }
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
                .background(filled ? BeeStyle.yellow : BeeStyle.background, in: .capsule)
                .overlay(Capsule().strokeBorder(.black.opacity(filled ? 0 : 0.08), lineWidth: 0.5))
        }
        .buttonStyle(.plain)
    }
}

/// The grey line over a section: when, who, how long; for a call it opens the call's sheet.
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
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity)
    }
}

/// The booked call pinned at the top (design D, frame 1): when, about what, and "전화 취소", which asks first
/// (OfferCenter's alert).
struct BookedPin: View {
    let offer: OfferCenter.Offer
    let language: String
    let cancel: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                TimelineView(.everyMinute) { clock in
                    Label(offer.bookedLine(language: language, now: clock.date), systemImage: "clock")
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(BeeStyle.ink)
                        .lineLimit(1)
                }
                Text(offer.about(language: language))
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 4)
            Button(action: cancel) {
                Text(OfferCenter.Offer.cancelButton(language: language))
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(BeeStyle.red)
                    .padding(.horizontal, 13)
                    .frame(height: 36)
                    .background(Color(.tertiarySystemFill), in: .capsule)
            }
            .buttonStyle(.plain)
        }
        .padding(.leading, 14)
        .padding(.trailing, 9)
        .padding(.vertical, 8)
        .background(BeeStyle.background, in: .rect(cornerRadius: 24))
        .shadow(color: .black.opacity(0.12), radius: 10, y: 3)
    }
}
