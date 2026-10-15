#if DEBUG
import SwiftUI

/// Debug builds only: launched with `-designD <frame>`, the app shows one frame of design D
/// (scratch/2026-10-15-ui-mockups/d-chat-and-call.html) with the design's own content, to compare with the
/// mockup and take screenshots before the server's call view exists. Nothing goes to a server or to CallKit.
enum DesignDemo: String {
    case call // frame 2: the call screen

    static func requested(in store: UserDefaults = .standard) -> DesignDemo? {
        store.string(forKey: "designD").flatMap(DesignDemo.init(rawValue:))
    }

    /// The call in the design (brief-common.md): the AI asking, after the person's question.
    static let callLines: [(mine: Bool, text: String)] = [
        (false, "안녕하세요, 민승 님. 오늘 밤엔 무슨 이야기 할까요?"),
        (true, "맥북 에어랑 프로 중에 코딩용으로 고민 중이에요. 한국 가격 비교해 주고, 저한테 뭐가 맞는지 알려 주세요."),
        (false, "지금 애플 한국 가격표를 보고 있어요. 주로 어떤 코딩을 하세요?"),
    ]
}

/// One frame of design D, with the design's content.
struct DesignDemoView: View {
    let demo: DesignDemo
    let calls: CallManager

    var body: some View {
        switch demo {
        case .call:
            CallModeView(calls: calls,
                         job: JobPill(id: "1", label: "가격 비교", startedAt: .now.addingTimeInterval(-67), arrived: false),
                         showConversation: {})
                .task { calls.playDesignDemo(lines: DesignDemo.callLines, voice: .ai, connectedAgo: 218) }
        }
    }
}
#endif
