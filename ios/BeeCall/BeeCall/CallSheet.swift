import SwiftUI

/// A call's part of the conversation as a sheet (design D, frame 6), opened from the line above a call that is
/// over: its title and how it went, what Bee Call read in Bee before the call, then the same bubbles and cards as
/// the conversation, all unfolded, the call's end marked, and the round yellow "이 통화 이어서 하기" where the call
/// button is on the home screen. Scrolled down, the title moves up into the bar. In the app this sheet takes the
/// place of the web record page, which stays for the Mac.
struct CallSheet: View {
    let call: ConversationCall
    let continueCall: () -> Void
    var startAtEnd = false // Debug demo: the sheet scrolled to its end
    @Environment(\.dismiss) private var dismiss
    @AppStorage("language") private var language = "en"
    @State private var titleUp = false

    private var korean: Bool { language == "ko" }
    private var words: ConversationWords { ConversationWords(korean: korean) }

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 10) {
                        header
                        if let prep = call.prep {
                            PrepCard(prep: prep, korean: korean)
                                .padding(.top, 6)
                        }
                        ForEach(call.entries) { entry in
                            switch entry {
                            case .turn(let turn): TurnBubble(turn: turn)
                            case .job(let job): JobCardView(job: job, words: words)
                            case .beeWrite(let write): BeeWriteView(write: write, live: false, words: words)
                            }
                        }
                        Stamp(text: CallInfo.endMark(call, korean: korean))
                            .padding(.top, 8)
                    }
                    .padding(.horizontal, 16)
                    .padding(.bottom, 170)
                    Color.clear.frame(height: 0).id("end") // the very end, under room for the button
                }
                .scrollIndicators(.hidden)
                .titleMovesUp($titleUp)
                .onAppear {
                    if startAtEnd { proxy.scrollTo("end", anchor: .bottom) }
                }
            }
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .principal) {
                    Text(call.title)
                        .font(.subheadline.weight(.semibold))
                        .lineLimit(1)
                        .opacity(titleUp ? 1 : 0)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button { dismiss() } label: {
                        Image(systemName: "xmark")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(BeeStyle.ink)
                    }
                    .accessibilityLabel(korean ? "닫기" : "Close")
                }
            }
            .overlay(alignment: .bottom) { continueButton }
        }
        .presentationDragIndicator(.visible)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(call.title)
                .font(.title2.bold())
                .foregroundStyle(BeeStyle.ink)
            infoLine
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityLabel(CallInfo.sheetLine(call, korean: korean, now: .now, calendar: .current))
        }
        .padding(.top, 4)
    }

    /// One text, so a long English line wraps instead of being cut; the check is drawn, not read out.
    private var infoLine: Text {
        let line = CallInfo.line(call, korean: korean, now: .now, calendar: .current)
        guard let ending = call.ending else { return Text(line) }
        return Text("\(line) · \(Image(systemName: "checkmark")) \(ending)")
    }

    private var continueButton: some View {
        VStack(spacing: 6) {
            Button(action: continueCall) {
                Image(systemName: "phone.fill")
                    .font(.system(size: 30, weight: .semibold))
                    .foregroundStyle(BeeStyle.onYellow)
                    .frame(width: 78, height: 78)
                    .background(BeeStyle.yellow, in: .circle)
                    .background(Circle().fill(BeeStyle.yellow.opacity(0.22)).frame(width: 98, height: 98))
            }
            .buttonStyle(.plain)
            Text(korean ? "이 통화 이어서 하기" : "Continue this call")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(BeeStyle.ink)
        }
        .padding(.top, 30)
        .padding(.bottom, 8)
        .frame(maxWidth: .infinity)
        .background(LinearGradient(colors: [BeeStyle.background.opacity(0), BeeStyle.background],
                                   startPoint: .top, endPoint: UnitPoint(x: 0.5, y: 0.35)).ignoresSafeArea())
    }
}

/// What Bee Call read in Bee before the call: its summary, and what it looked up.
struct PrepCard: View {
    let prep: CallPrep
    let korean: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Eyebrow(symbol: "eye", text: korean ? "통화 전에 Bee에서 본 것" : "Read in Bee before the call")
            Text(prep.summary)
                .font(.subheadline)
                .foregroundStyle(BeeStyle.ink)
                .padding(.top, 6)
            if !prep.lookedUp.isEmpty {
                Divider().padding(.vertical, 10)
                Text(korean ? "찾아본 것" : "Looked up")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                ForEach(prep.lookedUp, id: \.text) { lookup in
                    Label(lookup.text, systemImage: lookup.kind == .conversation ? "bubble.left.and.bubble.right"
                          : "calendar")
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
}

extension View {
    /// Whether the sheet's title has scrolled up under the bar (iOS 18; earlier it never moves up).
    @ViewBuilder
    func titleMovesUp(_ up: Binding<Bool>) -> some View {
        if #available(iOS 18, *) {
            onScrollGeometryChange(for: Bool.self) { $0.contentOffset.y > 60 } action: { _, moved in up.wrappedValue = moved }
        } else {
            self
        }
    }
}
