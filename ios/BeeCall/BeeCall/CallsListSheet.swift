import SwiftUI

/// "통화 목록" (scratch/2026-10-15-ui-mockups/calls-list.html): the home's list button opens every call by day, newest
/// first, in a sheet. A call that is over opens its record in the same sheet, with the way back at the top; the
/// person's call that is on closes the sheet and goes back to the call; a missed or declined call has no record and
/// does not open. The bottom row loads the next older page when it shows. The data is the home's, GET /calls through
/// CallStore, so nothing is fetched twice.
struct CallsListSheet: View {
    let source: any ConversationSource
    /// "이 통화 이어서 하기" in a record waits while another call is on.
    let canContinue: Bool
    let backToCall: () -> Void
    let continueCall: (String) -> Void
    var startAtEnd = false // Debug demo: the list scrolled to its foot
    @Environment(\.dismiss) private var dismiss
    @AppStorage("language") private var language = "en"
    @State private var files = FilePreview() // a job's file, opened over this sheet
    @State private var path: [String] // the record open in the sheet, if any

    init(source: any ConversationSource, canContinue: Bool, backToCall: @escaping () -> Void,
         continueCall: @escaping (String) -> Void, startAtEnd: Bool = false, startOn record: String? = nil) {
        self.source = source
        self.canContinue = canContinue
        self.backToCall = backToCall
        self.continueCall = continueCall
        self.startAtEnd = startAtEnd
        _path = State(initialValue: record.map { [$0] } ?? [])
    }

    private var korean: Bool { language == "ko" }
    private var words: CallsListWords { CallsListWords(korean: korean) }

    var body: some View {
        NavigationStack(path: $path) {
            content
                .navigationTitle(words.title)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { dismiss() } label: {
                            Image(systemName: "xmark")
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(BeeStyle.ink)
                        }
                        .accessibilityLabel(korean ? "닫기" : "Close")
                    }
                }
                .navigationDestination(for: String.self) { callID in
                    CallRecord(callID: callID, source: source, canContinue: canContinue) { continueCall(callID) }
                }
        }
        .tint(BeeStyle.ink)
        .filePreviews(files)
        .presentationDragIndicator(.visible)
    }

    @ViewBuilder
    private var content: some View {
        switch CallsList.Content(source.listed, loaded: source.listLoaded, older: source.olderPages,
                                 problem: source.problem, korean: korean, now: .now) {
        case .loading:
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed: // the first page could not be had (review of the calls list, finding 3)
            Button { Task { await source.reloadList() } } label: {
                Text(words.couldNotLoad)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(BeeStyle.ink)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .contentShape(.rect)
            }
            .buttonStyle(.plain)
        case .empty:
            ContentUnavailableView {
                Label(words.emptyTitle, systemImage: "phone")
            } description: {
                Text(words.emptyText)
            }
            .foregroundStyle(BeeStyle.ink)
        case .days(let days):
            ScrollViewReader { proxy in
                List {
                    ForEach(days) { day in
                        Section {
                            ForEach(day.calls) { entry in row(entry) }
                        } header: {
                            DayHeader(day: day)
                        }
                    }
                    Section {
                        foot(hasCalls: !days.isEmpty)
                            .id("foot")
                    }
                    .listRowBackground(Color.clear)
                }
                .listStyle(.insetGrouped)
                .headerProminence(.increased)
                .onAppear { if startAtEnd { proxy.scrollTo("foot", anchor: .bottom) } }
            }
        }
    }

    @ViewBuilder
    private func row(_ entry: CallEntry) -> some View {
        let row = CallsList.Row(entry, korean: korean, ownCall: source.ownCall)
        switch row.opens {
        case .record:
            NavigationLink(value: entry.id) { CallRow(row: row) }
        case .call:
            Button(action: backToCall) {
                HStack(spacing: 8) {
                    CallRow(row: row)
                    Image(systemName: "chevron.right")
                        .font(.footnote.weight(.semibold))
                        .foregroundStyle(Color(.tertiaryLabel))
                }
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityHint(words.backToCall)
        case .nothing:
            CallRow(row: row)
        }
    }

    /// The bottom row: the next older page loads when it shows.
    @ViewBuilder
    private func foot(hasCalls: Bool) -> some View {
        switch source.olderPages {
        case .more, .loading:
            HStack(spacing: 9) {
                ProgressView()
                Text(words.loadingMore)
            }
            .font(.subheadline.weight(.medium))
            .foregroundStyle(BeeStyle.secondaryInk)
            .frame(maxWidth: .infinity, minHeight: 44)
            // When it shows, and again after a page if it still shows: the next older page.
            .task(id: source.listed.count) { if source.olderPages == .more { await source.loadOlder() } }
        case .failed:
            Button { Task { await source.loadOlder() } } label: {
                Text(words.couldNotLoad)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(BeeStyle.ink)
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
            .buttonStyle(.plain)
        case .done:
            if hasCalls {
                Text(words.allLoaded)
                    .font(.subheadline)
                    .foregroundStyle(BeeStyle.secondaryInk)
                    .frame(maxWidth: .infinity, minHeight: 44)
            }
        }
    }
}

/// A day's heading: its name, and an older day's weekday quieter beside it.
struct DayHeader: View {
    let day: CallsList.Day

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(day.title)
                .font(.title3.bold())
                .foregroundStyle(BeeStyle.ink)
            if let weekday = day.weekday {
                Text(weekday)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(BeeStyle.secondaryInk)
            }
        }
        .textCase(nil)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// One call in the list: who placed it (or the small orb for the call that is on), its title, "오후 2:22 · 내가 건
/// 전화 · 6분 12초" with its state word, and what it made.
struct CallRow: View {
    let row: CallsList.Row

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            icon
                .frame(width: 26, height: 22)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 2) {
                Text(row.title)
                    .font(.callout.weight(row.quietTitle ? .regular : .semibold))
                    .foregroundStyle(row.quietTitle ? BeeStyle.secondaryInk : BeeStyle.ink)
                    .lineLimit(1)
                line
                if !row.pills.isEmpty {
                    HStack(spacing: 4) {
                        ForEach(row.pills) { ResultPill(pill: $0) }
                    }
                    .padding(.top, 3)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var icon: some View {
        switch row.icon {
        case .live:
            OrbView(look: OrbLook(.rest, lastSide: nil, reduceMotion: true), diameter: 20)
        case .outgoing:
            Image(systemName: "phone.arrow.up.right").foregroundStyle(BeeStyle.secondaryInk)
        case .incoming:
            Image(systemName: "phone.arrow.down.left").foregroundStyle(BeeStyle.secondaryInk)
        case .missed:
            Image(systemName: "phone.arrow.down.left").foregroundStyle(BeeStyle.redText)
        }
    }

    @ViewBuilder
    private var line: some View {
        let base = Text(row.line).foregroundStyle(BeeStyle.secondaryInk)
        switch row.state?.look {
        case .livePill:
            HStack(spacing: 4) {
                base + Text(" ·").foregroundStyle(BeeStyle.secondaryInk)
                LivePill(text: row.state?.text ?? "")
            }
            .font(.footnote)
        case .red:
            (base + Text(" · ").foregroundStyle(BeeStyle.secondaryInk)
                + Text(row.state?.text ?? "").fontWeight(.semibold).foregroundStyle(BeeStyle.redText))
                .font(.footnote)
        case .quiet:
            (base + Text(" · " + (row.state?.text ?? "")).foregroundStyle(BeeStyle.secondaryInk))
                .font(.footnote)
        case nil:
            base.font(.footnote)
        }
    }
}

/// The yellow "통화 중" pill of the call that is on.
struct LivePill: View {
    let text: String

    var body: some View {
        HStack(spacing: 4) {
            Circle().fill(BeeStyle.onYellow).frame(width: 6, height: 6)
            Text(text)
        }
        .font(.caption.weight(.bold))
        .foregroundStyle(BeeStyle.onYellow)
        .padding(.leading, 6)
        .padding(.trailing, 8)
        .frame(height: 20)
        .background(BeeStyle.yellow, in: .capsule)
    }
}

/// What a call made, as a small pill: "보고서", "표", "차트", "Bee 할 일 1".
struct ResultPill: View {
    let pill: CallsList.Pill

    private var symbol: String {
        switch pill.kind {
        case .report: "doc.text"
        case .table: "tablecells"
        case .chart: "chart.bar"
        case .beeTodo: "checklist"
        case .beeFact: "text.bubble"
        }
    }

    var body: some View {
        HStack(spacing: 3) {
            Image(systemName: symbol)
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(BeeStyle.resultPillIcon)
            Text(pill.text)
                .font(.caption.weight(.semibold))
                .foregroundStyle(BeeStyle.ink)
        }
        .padding(.leading, 6)
        .padding(.trailing, 7)
        .frame(height: 22)
        .background(BeeStyle.resultPill, in: .capsule)
    }
}

/// The calls list's words, in the person's call language.
struct CallsListWords {
    let korean: Bool

    var title: String { korean ? "통화 목록" : "Calls" }
    var open: String { korean ? "통화 목록" : "Calls" } // the home's list button
    var emptyTitle: String { korean ? "아직 통화가 없어요" : "No calls yet" }
    var emptyText: String { korean ? "Bee Call과 통화하면 날짜별로 여기에 모여요." : "Your calls with Bee Call gather here by day." }
    var loadingMore: String { korean ? "더 불러오는 중" : "Loading more" }
    var couldNotLoad: String { korean ? "불러오지 못했어요 · 다시 시도" : "Couldn't load · Try again" }
    var allLoaded: String { korean ? "처음 통화까지 다 봤어요" : "That's every call" }
    var backToCall: String { korean ? "통화로 돌아가요" : "Goes back to the call" }
}
