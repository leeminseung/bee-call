import SwiftUI

/// A call's record as a sheet (design D, frame 6), opened from the line above a call that is over: the record
/// (CallRecord) with a close button. In the app this sheet takes the place of the web record page, which stays for
/// the Mac.
struct CallSheet: View {
    let callID: String
    let source: any ConversationSource
    /// "이 통화 이어서 하기" waits while another call is on (review of 2026-10-15, finding 6).
    let canContinue: Bool
    let continueCall: () -> Void
    var startAtEnd = false // Debug demo: the sheet scrolled to its end
    @Environment(\.dismiss) private var dismiss
    @AppStorage("language") private var language = "en"
    @State private var files = FilePreview() // a job's file, opened over this sheet

    var body: some View {
        NavigationStack {
            CallRecord(callID: callID, source: source, canContinue: canContinue, continueCall: continueCall,
                       startAtEnd: startAtEnd)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { dismiss() } label: {
                            Image(systemName: "xmark")
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(BeeStyle.ink)
                        }
                        .accessibilityLabel(language == "ko" ? "닫기" : "Close")
                    }
                }
        }
        .filePreviews(files)
        .presentationDragIndicator(.visible)
    }
}

/// A call's record (design D, frame 6): its title and how it went, why Bee Call called (its own calls), what it read
/// before the call, then the same bubbles and cards as the conversation, all unfolded, the call's end marked, and the
/// round yellow "이 통화 이어서 하기" where the call button is on the home screen. Scrolled down, the title moves up
/// into the bar. Shown in its own sheet (CallSheet), or pushed in the calls list.
struct CallRecord: View {
    let callID: String
    let source: any ConversationSource
    let canContinue: Bool
    let continueCall: () -> Void
    var startAtEnd = false
    @AppStorage("language") private var language = "en"
    @State private var titleUp = false

    private var korean: Bool { language == "ko" }
    private var words: ConversationWords { ConversationWords(korean: korean) }
    private var view: CallView? { source.view(callID) }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if let view {
                        content(view)
                    } else {
                        Text(source.isMissing(callID) ? words.noRecord : words.loading)
                            .font(.subheadline)
                            .foregroundStyle(BeeStyle.secondaryInk)
                            .frame(maxWidth: .infinity)
                            .padding(.top, 40)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 170)
                Color.clear.frame(height: 0).id("end") // the very end, under room for the button
            }
            .scrollIndicators(.hidden)
            .titleMovesUp($titleUp)
            .onChange(of: view != nil) { _, loaded in
                if loaded, startAtEnd { proxy.scrollTo("end", anchor: .bottom) }
            }
        }
        .background(BeeStyle.background)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                Text(view.map { CallLine.title($0.call, korean: korean) } ?? "")
                    .font(.subheadline.weight(.semibold))
                    .lineLimit(1)
                    .opacity(titleUp ? 1 : 0)
            }
        }
        .overlay(alignment: .bottom) { continueButton }
        .task { await source.show(callID) }
    }

    @ViewBuilder
    private func content(_ view: CallView) -> some View {
        header(view.call)
        if let why = view.whyCalling {
            WhyCallingCard(topic: view.call.topic, why: why, words: words)
                .padding(.top, 6)
        }
        if let prep = view.prep {
            PrepCard(prep: prep, words: words)
                .padding(.top, view.whyCalling == nil ? 6 : 0)
        }
        ForEach(CallThread.expanded(view)) { entry in
            ThreadEntryView(entry: entry, words: words)
        }
        Stamp(text: CallLine.endMark(view.call, korean: korean))
            .padding(.top, 8)
    }

    private func header(_ call: CallSummary) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(CallLine.title(call, korean: korean))
                .font(.title2.bold())
                .foregroundStyle(BeeStyle.ink)
            infoLine(call)
                .font(.caption)
                .foregroundStyle(BeeStyle.secondaryInk)
                .accessibilityLabel(call.state == .ended ? CallLine.sheetLine(call, korean: korean, now: .now)
                                    : CallLine.line(call, korean: korean, now: .now))
        }
        .padding(.top, 4)
    }

    /// One text, so a long English line wraps instead of being cut; the check is drawn, not read out.
    private func infoLine(_ call: CallSummary) -> Text {
        let line = CallLine.line(call, korean: korean, now: .now)
        guard call.state == .ended, let ending = CallLine.ending(.ended, korean: korean) else { return Text(line) }
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
                    .modifier(CallButtonHalo())
            }
            .buttonStyle(.plain)
            .disabled(!canContinue)
            .opacity(canContinue ? 1 : 0.4)
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
