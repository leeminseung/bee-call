import Foundation
import LiveKit
import Observation

/// The LiveKit side of a call: room token, room, microphone, captions.
@Observable
final class LiveKitClient: NSObject {
    /// The voice server sets this attribute to "answered" when the AI picks up. Until then a call the user
    /// started hears ringing from the voice server while the AI reads the user's Bee data.
    nonisolated static let stateAttribute = "bee_call.state"
    /// The voice server changes this attribute after its own room reconnects (contract section 3, rule 6).
    nonisolated static let captionsAttribute = "bee_call.captions"

    private(set) var captionList = CaptionList()
    var captions: [Caption] { captionList.captions }
    /// Whether the caption numbers still line up with the server's counts, so the thread may draw caption lines.
    private(set) var numbering = CaptionNumbering()
    @ObservationIgnored private var voiceServers: Set<String> = [] // the voice servers in the room, by identity
    /// The room's connection dropped, came back, or ended without the app asking.
    @ObservationIgnored var onLinkChange: ((CallLink) -> Void)?
    @ObservationIgnored var onAgentAnswered: (() -> Void)?
    @ObservationIgnored private var room: Room?
    @ObservationIgnored private var microphoneOn = true // a mute that arrives before the mic is published
    @ObservationIgnored private let phoneRecorder = PhoneRecorder() // "Save call audio on this iPhone"
    @ObservationIgnored private let server = ServerClient()
    /// Both voices' live levels, for the indicator around the call screen's main button.
    let voices = VoiceActivity()

    // On a fresh install, the first request to the Mac's LAN address makes iOS ask for Local Network
    // access. Waiting for connectivity lets that first call go through once the user taps Allow.
    @ObservationIgnored private let http: URLSession = {
        let configuration = URLSessionConfiguration.default
        configuration.waitsForConnectivity = true
        configuration.timeoutIntervalForResource = 30
        return URLSession(configuration: configuration)
    }()

    /// Joins a room and publishes the microphone. Returns the room name. A new room for a call the user
    /// starts; with `answering`, the room of the AI's call. Only the pass from the token server differs.
    func connect(answering roomName: String? = nil, continuing earlierCall: String? = nil) async throws -> String {
        captionList = CaptionList()
        numbering = CaptionNumbering()
        voiceServers = []
        voices.aiHasAnswered = roomName != nil // the AI's own call has no ringing: the first sound is its voice
        let access = try await fetchAccess(answering: roomName, continuing: earlierCall)
        try Task.checkCancellation() // the call ended while we fetched the token
        return try await connect(access: access)
    }

    private func connect(access: CallPass) async throws -> String {
        let room = Room()
        room.add(delegate: self)
        self.room = room // set first, so disconnect() can stop this attempt at any point
        // The voice server sends both sides' transcripts on this topic. An AI line arrives as one
        // stream in chunks; a user line is re-sent in full as a new stream each time it improves.
        try await room.registerTextStreamHandler(for: "lk.transcription") { [weak self, weak room] reader, sender in
            let segment = reader.info.attributes["lk.segment_id"] ?? reader.info.id
            let final = reader.info.attributes["lk.transcription_final"] == "true"
            var text = ""
            for try await chunk in reader {
                text += chunk
                guard let self, let room else { return }
                await self.show(text, segment: segment, from: sender, sentAt: reader.info.timestamp, in: room)
            }
            guard let self, let room else { return }
            await self.streamEnded(segment: segment, from: sender, final: final, in: room)
        }
        try Task.checkCancellation() // the call ended while we got the room ready
        try await room.connect(url: access.url, token: access.token)
        try await room.localParticipant.setMicrophone(enabled: microphoneOn)
        // The call may have ended during those waits; then its voice levels must not start (review finding 15).
        guard room === self.room else { throw CancellationError() }
        voices.microphoneOn = microphoneOn
        voices.start()
        for participant in room.remoteParticipants.values { voiceServerJoined(participant) } // there before us
        if room.remoteParticipants.values.contains(where: { $0.attributes[Self.stateAttribute] == "answered" }) {
            agentAnswered() // the AI picked up before we finished joining (its own call, already answered)
        }
        return access.room
    }

    /// The voice server marked the call answered: the AI's voice now counts for the indicator, and the call
    /// manager learns that the AI picked up.
    private func agentAnswered() {
        voices.aiHasAnswered = true
        onAgentAnswered?()
    }

    /// The recording on this iPhone, once the call has joined; it stops when the call leaves its room.
    func recordOnPhoneIfWanted() -> String? {
        phoneRecorder.startIfWanted()
    }

    func setMicrophone(enabled: Bool) async throws {
        microphoneOn = enabled
        voices.microphoneOn = enabled
        try await room?.localParticipant.setMicrophone(enabled: enabled)
    }

    func disconnect() async {
        microphoneOn = true // the next call starts unmuted
        captionList = CaptionList() // the next call's screen starts without this call's words
        phoneRecorder.stop()
        voices.stop()
        guard let room else { return }
        self.room = nil
        await room.disconnect()
    }

    /// A voice server in the room, met once per identity: when the phone joins, or when it arrives later.
    private func voiceServerJoined(_ participant: Participant) {
        guard participant.kind == .agent, voiceServers.insert(participant.identity?.stringValue ?? "").inserted
        else { return }
        numbering.voiceServerJoined(captions: participant.attributes[Self.captionsAttribute])
    }

    private func show(_ text: String, segment: String, from sender: Participant.Identity, sentAt: Date, in room: Room) {
        guard room === self.room else { return } // a late line from an earlier call
        let isMine = sender == room.localParticipant.identity
        captionList.show(text, line: "\(sender.stringValue)/\(segment)", isMine: isMine, sentAt: sentAt)
    }

    /// A line's stream has ended: the AI's line is complete, a person's line once its final version has come.
    private func streamEnded(segment: String, from sender: Participant.Identity, final: Bool, in room: Room) {
        guard room === self.room else { return }
        captionList.streamEnded(line: "\(sender.stringValue)/\(segment)", final: final)
    }

    // MARK: - Token server

    /// Where the pass comes from on the token server: GET /token, for a new call, an answer, or a continuation.
    static func passPath(answering roomName: String?, continuing earlierCall: String?) -> String {
        var query = URLComponents()
        if let roomName {
            query.queryItems = [URLQueryItem(name: "room", value: roomName), URLQueryItem(name: "answer", value: "1")]
        } else if let earlierCall {
            query.queryItems = [URLQueryItem(name: "continue", value: earlierCall)]
        }
        return "/token" + (query.percentEncodedQuery.map { "?" + $0 } ?? "")
    }

    /// The token server answered 410 to an answer request: the AI stopped ringing and closed the room.
    struct CallAlreadyEnded: Error {}

    private struct TokenServerError: LocalizedError {
        let errorDescription: String?
    }

    /// A pass for a new call: the voice server is dispatched into the new room when the app joins.
    /// With `answering`, a pass into the room of the AI's call instead; the voice server is already there.
    /// With `continuing`, the new call picks up from that earlier call, by its call id (contract section 7).
    private func fetchAccess(answering roomName: String?, continuing earlierCall: String? = nil) async throws
        -> CallPass {
        guard let request = server.request(Self.passPath(answering: roomName, continuing: earlierCall)) else {
            throw TokenServerError(errorDescription: "This build has no server address.")
        }
        let (data, response) = try await http.data(for: request)
        switch server.read(response.httpStatus, data, to: request) {
        case .ok(200, let body):
            return try Contract.decode(CallPass.self, from: body)
        case .refused(410, _) where roomName != nil:
            throw CallAlreadyEnded()
        case .refused(429, let body): // today's budget is used up; the server says how much
            throw TokenServerError(errorDescription: String(decoding: body, as: UTF8.self)
                .trimmingCharacters(in: .whitespacesAndNewlines))
        default:
            throw TokenServerError(errorDescription: "The token server answered \(response.httpStatus).")
        }
    }
}

extension LiveKitClient: RoomDelegate {
    /// Every reconnect, quick or full. (roomIsReconnecting comes only for a full one, LiveKit's last attempt.)
    nonisolated func room(_ room: Room, didStartReconnectWithMode reconnectMode: ReconnectMode) {
        Task { @MainActor in
            guard room === self.room else { return } // an earlier call's room
            self.numbering.roomReconnecting() // lines sent meanwhile are lost to the phone
            self.onLinkChange?(.reconnecting)
        }
    }

    nonisolated func room(_ room: Room, participantDidConnect participant: RemoteParticipant) {
        Task { @MainActor in
            guard room === self.room else { return }
            self.voiceServerJoined(participant)
        }
    }

    nonisolated func room(_ room: Room, participantDidDisconnect participant: RemoteParticipant) {
        Task { @MainActor in
            guard room === self.room, participant.kind == .agent,
                  self.voiceServers.remove(participant.identity?.stringValue ?? "") != nil else { return }
            self.numbering.voiceServerLeft()
        }
    }

    nonisolated func room(_ room: Room, didCompleteReconnectWithMode reconnectMode: ReconnectMode) {
        Task { @MainActor in
            guard room === self.room else { return } // also an earlier call's room, or one that failed to come back
            self.onLinkChange?(.reconnected)
        }
    }

    nonisolated func room(_ room: Room, didDisconnectWithError error: LiveKitError?) {
        Task { @MainActor in
            guard room === self.room else { return } // we left on purpose
            self.room = nil
            self.phoneRecorder.stop()
            self.voices.stop()
            self.onLinkChange?(.lost(error))
        }
    }

    nonisolated func room(_ room: Room, participant: Participant, didUpdateAttributes attributes: [String: String]) {
        guard participant is RemoteParticipant else { return }
        let answered = participant.attributes[Self.stateAttribute] == "answered"
        let captions = participant.attributes[Self.captionsAttribute], isVoiceServer = participant.kind == .agent
        Task { @MainActor in
            guard room === self.room else { return } // a late update from an earlier call
            if isVoiceServer { self.numbering.captionsMark(captions) }
            if answered { self.agentAnswered() }
        }
    }
}

#if DEBUG
extension LiveKitClient {
    /// Debug builds only (VoiceDemo.swift): two caption bubbles and synthetic voice levels; no room is joined.
    func playVoiceDemo(_ demo: VoiceDemo) {
        captionList = CaptionList()
        captionList.show("Hi, it's Bee Call. Is now still good to pick Saturday's trail?", line: "ai/demo", isMine: false,
                         sentAt: .now)
        captionList.streamEnded(line: "ai/demo", final: true)
        captionList.show("Yes, now is fine. Dana says her knee is better,", line: "me/demo", isMine: true, sentAt: .now)
        voices.playDemo(demo)
    }

    /// Debug builds only (DesignDemo.swift): one more line, as if just said in full.
    func demoSay(_ text: String, mine: Bool) {
        let id = "\(mine ? "me" : "ai")/demo-\(UUID().uuidString)"
        captionList.show(text, line: id, isMine: mine, sentAt: .now)
        captionList.streamEnded(line: id, final: true)
    }

    /// Debug builds only (DesignDemo.swift): these caption lines, said in full, and synthetic voice levels; no room
    /// is joined. With `held`, they are numbered as the last lines the call view holds, and `growing` follows them,
    /// still arriving.
    func playDemo(lines: [(mine: Bool, text: String)], voice: VoiceDemo, held: CaptionCounts? = nil,
                  growing: CaptionLine? = nil) {
        captionList = held.map { CaptionList(numberedAfter: CaptionCounts(person: $0.person - 1, ai: $0.ai - 1)) }
            ?? CaptionList()
        for (index, line) in lines.enumerated() {
            let id = "\(line.mine ? "me" : "ai")/demo\(index)"
            captionList.show(line.text, line: id, isMine: line.mine, sentAt: .now)
            captionList.streamEnded(line: id, final: true)
        }
        if let growing {
            captionList.show(growing.text, line: "me/demo-growing", isMine: growing.speaker == .person, sentAt: .now)
        }
        voices.playDemo(voice)
    }
}
#endif
