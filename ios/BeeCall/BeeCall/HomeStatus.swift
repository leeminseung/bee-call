import Foundation

/// The line under design D's call button ("준비됨 · 앱을 닫아도 전화가 와요"): whether Bee Call's calls reach the
/// phone, from the ring stream and the VoIP registration, or what became of the last call when it went wrong, from
/// the call manager's status, until the next call.
enum HomeStatus {
    static func line(korean: Bool, callStatus: String, ringStatus: String, voipRegistered: Bool) -> String {
        if let problem = problem(callStatus, korean: korean) { return problem }
        if ringStatus.isEmpty { return "" } // not listening: no server address in this build
        if ringStatus != "Waiting for calls" { return korean ? "Bee Call 서버에 연결하는 중" : "Connecting to Bee Call" }
        if !voipRegistered { return korean ? "준비됨" : "Ready" }
        return korean ? "준비됨 · 앱을 닫아도 전화가 와요" : "Ready · calls come even with the app closed"
    }

    /// The last call's status when it says something went wrong; nil for one that went well or is on.
    private static func problem(_ status: String, korean: Bool) -> String? {
        let fine = ["Ready", "Call ended", "Declined", "Connected", "Calling…", "Calling to continue…", "Ringing…",
                    "Answering…"]
        guard !fine.contains(status), !status.hasPrefix("Bee Call is calling") else { return nil }
        guard korean else { return status }
        let known: [String: String] = [
            "Missed a call from Bee Call": "Bee Call의 전화를 받지 못했어요",
            "Missed a call from Bee Call: Do Not Disturb or a Focus is on":
                "방해 금지나 집중 모드 때문에 Bee Call의 전화를 받지 못했어요",
            "Missed a call from Bee Call: it is blocked on this phone": "이 iPhone에서 Bee Call의 전화가 차단되어 있어요",
            "Bee Call didn't pick up": "Bee Call이 전화를 받지 않았어요",
            "That call already ended": "그 통화는 이미 끝났어요",
            "Microphone access is off. Turn it on in Settings.": "마이크 접근이 꺼져 있어요. 설정에서 켜 주세요.",
            "iOS reset the call": "iOS가 통화를 끊었어요",
        ]
        if let words = known[status] { return words }
        let prefixes = ["Call failed: ": "통화가 끊겼어요: ", "Could not start the call: ": "전화를 걸지 못했어요: ",
                        "Could not ring: ": "전화가 울리지 못했어요: ", "Audio could not start: ": "소리를 켜지 못했어요: "]
        for (english, words) in prefixes where status.hasPrefix(english) {
            return words + status.dropFirst(english.count)
        }
        return status
    }
}
