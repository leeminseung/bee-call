import Foundation

/// The app's messages in the call language. The parts that report a problem (settings, Bee, an offer's answer, the
/// recording on this iPhone, a fetch, a pick) word it in English; design D shows them on a Korean or English home,
/// so they are put in Korean here when the call language is Korean (second review of the D wiring, finding 7). A
/// reason that comes from elsewhere (the server's own words, an error's description) stays as it came.
enum MessageWords {
    static func words(_ english: String, korean: Bool) -> String {
        guard korean else { return english }
        if let words = exact[english] { return words }
        for (prefix, words) in prefixes where english.hasPrefix(prefix) {
            return words + english.dropFirst(prefix.count)
        }
        if let typed = english.firstMatch(of: /^The evening call time “(.*)” was not saved: write it like 21:00 in Settings\.$/) {
            return "저녁 전화 시간 “\(typed.1)”을 저장하지 못했어요. 설정에서 21:00처럼 써 주세요."
        }
        if let code = english.firstMatch(of: /^Bee Call couldn't take that answer \(error (\d+)\)\. Try again from here\.$/) {
            return "Bee Call이 그 답을 받지 못했어요(오류 \(code.1)). 여기서 다시 해 주세요."
        }
        if let code = english.firstMatch(of: /^Bee Call's server answered (\d+)\.$/) {
            return "Bee Call 서버가 \(code.1)(으)로 답했어요."
        }
        return english
    }

    private static let exact: [String: String] = [
        // BeeSync
        BeeStep.notConnected: "Bee가 연결되지 않았어요. 다시 하려면 ‘Bee 사용’을 끄고 Bee Call로 돌아왔다가 다시 켜 주세요.",
        "Connecting Bee: approve Bee Call in the Bee app": "Bee 연결 중: Bee 앱에서 Bee Call을 승인해 주세요",
        "Bee could not be connected right now": "지금은 Bee를 연결하지 못했어요",
        "Turning Bee off…": "Bee를 끄는 중…",
        "Bee could not be turned off right now. Bee Call tries again when you come back.":
            "지금은 Bee를 끄지 못했어요. 다시 돌아오면 Bee Call이 또 해 볼게요.",
        // OfferAnswer
        "Your sign-in ended. Sign in again to answer.": "로그인이 끝났어요. 다시 로그인한 뒤 답해 주세요.",
        "That offer has closed. You can call Bee Call any time.": "이미 끝난 제안이에요. Bee Call에는 언제든 전화할 수 있어요.",
        "Couldn't reach Bee Call. Try again from here.": "Bee Call에 연결하지 못했어요. 여기서 다시 해 주세요.",
        // CallStore, PersonStore
        "This build has no server address.": "이 빌드에는 서버 주소가 없어요.",
        "Bee Call's server cannot be reached.": "Bee Call 서버에 연결할 수 없어요.",
        "Bee Call's server sent something this app cannot read.": "Bee Call 서버의 답을 이 앱이 읽지 못했어요.",
        "Bee Call's server refused it.": "Bee Call 서버가 받아 주지 않았어요.",
        "Your sign-in ended.": "로그인이 끝났어요.",
    ]

    private static let prefixes: [(String, String)] = [
        ("Recording on this iPhone did not start: ", "이 iPhone에서 녹음을 시작하지 못했어요: "),
        ("Your settings were not saved: ", "설정을 저장하지 못했어요: "),
    ]
}
