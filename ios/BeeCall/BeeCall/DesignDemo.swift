#if DEBUG
import SwiftUI

/// Debug builds only: launched with `-designD <frame>`, the app shows one frame of design D
/// (scratch/2026-10-15-ui-mockups/d-chat-and-call.html) with the design's own content (brief-common.md), to compare
/// with the mockup and take screenshots before the server's call view exists. Nothing goes to a server or to
/// CallKit: the offers come from OfferDemo's stand-in, the calls from DemoConversation.
enum DesignDemo: String {
    case home // frame 1
    case call // frame 2: the call screen
    case conversation // frame 3: the conversation during the call
    case live // frame 4: a pretend call, with the call screen and the conversation to go between

    static func requested(in store: UserDefaults = .standard) -> DesignDemo? {
        store.string(forKey: "designD").flatMap(DesignDemo.init(rawValue:))
    }

    /// The call in the design: the AI asking, after the person's question.
    static let callLines: [(mine: Bool, text: String)] = [
        (false, "안녕하세요, 민승 님. 오늘 밤엔 무슨 이야기 할까요?"),
        (true, "맥북 에어랑 프로 중에 코딩용으로 고민 중이에요. 한국 가격 비교해 주고, 저한테 뭐가 맞는지 알려 주세요."),
        (false, "지금 애플 한국 가격표를 보고 있어요. 주로 어떤 코딩을 하세요?"),
    ]
}

/// The design's conversation (brief-common.md): yesterday's and today's calls, an ask, and in a call frame the
/// call that is on.
@Observable
final class DemoConversation: ConversationSource {
    var sections: [ThreadSection]
    var beeLine: String? = "Bee 연결됨"

    init(live: Bool, now: Date = .now) {
        let calendar = Calendar.current
        func today(_ hour: Int, _ minute: Int) -> Date {
            calendar.date(bySettingHour: hour, minute: minute, second: 0, of: now) ?? now
        }
        func yesterday(_ hour: Int, _ minute: Int) -> Date {
            calendar.date(byAdding: .day, value: -1, to: today(hour, minute)) ?? now
        }
        let silent = ConversationCall(id: "2026-10-14/2145", title: "대화 없음", startedAt: yesterday(21, 45),
                                      starter: .person, length: 16, entries: [])
        let macbookTurns: [ConversationEntry] = [
            .turn(Turn(id: "m1", mine: false, text: "안녕하세요, 민승 님. 오늘 밤엔 무슨 이야기 할까요?")),
            .beeWrite(BeeWriteCard(id: "m-bee", kind: "할 일", text: "토요일 촬영 장소 정하기", added: true)),
            .turn(Turn(id: "m2", mine: true,
                       text: "맥북 에어랑 프로 중에 코딩용으로 고민 중이에요. 한국 가격 비교해 주고, 저한테 뭐가 맞는지 알려 주세요.")),
            .turn(Turn(id: "m3", mine: false, text: "지금 애플 한국 가격표를 보고 있어요. 주로 어떤 코딩을 하세요?")),
            .turn(Turn(id: "m4", mine: true, text: "주로 파이썬이고 머신러닝도 조금 해요. 도서관이나 카페에서 일할 때가 많아요")),
        ]
        if live {
            var call = ConversationCall(id: "2026-10-15/0123", title: "맥북 에어와 프로, 코딩용으로 뭐가 나을까",
                                        startedAt: today(1, 23), starter: .person, length: nil, entries: macbookTurns)
            call.entries.append(.job(JobCard(id: "job-1", title: "맥북 에어와 프로 한국 가격 비교", short: "가격 비교",
                                             startedAt: now.addingTimeInterval(-70), finishedAt: nil,
                                             step: "애플 한국 가격표 읽는 중", result: nil, files: [])))
            sections = [.call(Self.rain(yesterday(12, 26))), .call(Self.memo(yesterday(18, 2))), .call(silent), .call(call)]
            return
        }
        let macbook = ConversationCall(
            id: "2026-10-15/0123", title: "맥북 에어와 프로, 코딩용으로 뭐가 나을까", startedAt: today(1, 23),
            starter: .person, length: 460,
            entries: macbookTurns + [.job(Self.priceJob(started: today(1, 25)))])
        let library = ConversationCall(
            id: "2026-10-15/1422", title: "도서관 가는 길, 오늘 할 일 정리", startedAt: today(14, 22), starter: .person,
            length: 372, entries: [
                .turn(Turn(id: "l1", mine: false, text: "도서관 가는 길이시네요. 오늘 할 일부터 정리해 볼까요?")),
                .turn(Turn(id: "l2", mine: true, text: "응, 데모 영상 대본도 다듬어야 해.")),
                .beeWrite(BeeWriteCard(id: "l-bee", kind: "할 일", text: "데모 영상 대본 다듬기", added: true)),
            ])
        let rain = Self.rain(yesterday(12, 26)), memo = Self.memo(yesterday(18, 2))
        let ask = OfferCard(id: "20261015-144100-demo", askedAt: today(14, 41), topic: "오늘 회의에서 정한 데모 촬영 일정",
                            question: "오늘 저녁 8:30에 잠깐 통화할까요?", suggestedTime: "8:30")
        sections = [.call(rain), .call(memo), .call(silent), .call(macbook), .call(library), .offer(ask)]
    }

    static func rain(_ at: Date) -> ConversationCall {
        ConversationCall(id: "2026-10-14/1226", title: "이번 주 비 소식, 토요일 북한산 등산은 괜찮을지", startedAt: at,
                         starter: .person, length: 60, entries: [
                            .turn(Turn(id: "r1", mine: true, text: "이번 주 토요일 북한산 등산 괜찮을까?")),
                         ])
    }

    static func memo(_ at: Date) -> ConversationCall {
        ConversationCall(id: "2026-10-14/1802", title: "새 음성 메모: 성현이와 토요일 촬영 이야기", startedAt: at,
                         starter: .beeCall, length: 124, entries: [
                            .turn(Turn(id: "v1", mine: false, text: "성현 님과 토요일 촬영 이야기를 하셨네요.")),
                         ])
    }

    /// The job of the MacBook call, finished, with its chart and files.
    static func priceJob(started: Date) -> JobCard {
        JobCard(id: "job-1", title: "코딩용 맥북 에어와 프로 한국 가격 비교", startedAt: started,
                finishedAt: started.addingTimeInterval(87), step: nil,
                result: "애플 한국 기준 에어는 1,590,000원, 프로는 2,390,000원부터예요. 파이썬과 가벼운 머신러닝을 밖에서 한다면 "
                    + "배터리와 무게에서 에어가 낫고, 프로는 노트북에서 모델을 학습시킬 때만 값을 해요.",
                files: ["보고서.md", "비교표.csv"],
                chart: JobChart(title: "한국 가격(원), 에어와 프로, 세 가지 구성", series: ["에어", "프로"],
                                groups: [.init(name: "기본", values: [1_590_000, 2_390_000]),
                                         .init(name: "중간", values: [1_890_000, 2_690_000]),
                                         .init(name: "최상", values: [2_190_000, 2_990_000])]))
    }
}

/// One frame of design D, with the design's content.
struct DesignDemoView: View {
    let demo: DesignDemo
    let calls: CallManager
    @State private var offers = OfferCenter(server: ServerClient(transport: OfferDemo.StandIn(
        language: "ko", topic: "도서관에서 정리한 할 일 다시 보기")))
    @State private var jump: String?

    var body: some View {
        switch demo {
        case .home:
            HomeView(source: DemoConversation(live: false), calls: calls, offers: offers,
                     status: "준비됨 · 앱을 닫아도 전화가 와요", jump: $jump, showCallScreen: {}, openSettings: {},
                     openCall: { _ in }, startCall: {})
                .task { await offers.refresh() }
        case .call:
            CallModeView(calls: calls,
                         job: JobPill(id: "1", label: "가격 비교", startedAt: .now.addingTimeInterval(-67), arrived: false),
                         showConversation: {})
                .task { calls.playDesignDemo(lines: DesignDemo.callLines, voice: .ai, connectedAgo: 218) }
        case .conversation:
            HomeView(source: DemoConversation(live: true), calls: calls, offers: offers, status: "", jump: $jump,
                     showCallScreen: {}, openSettings: {}, openCall: { _ in }, startCall: {})
                .task { calls.playDesignDemo(lines: DesignDemo.callLines, voice: .person, connectedAgo: 221) }
        case .live:
            ConversationRoot(calls: calls, source: DemoConversation(live: true), offers: offers, status: "")
                .task { calls.playDesignDemo(lines: DesignDemo.callLines, voice: .both, connectedAgo: 218) }
        }
    }
}
#endif
