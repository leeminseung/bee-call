import Foundation
import LiveKit
import Observation

/// One caption line, as transcribed by the voice server.
struct Caption: Identifiable {
    let id: String
    let isMine: Bool
    var text: String
    var sentAt: Date
}

/// The LiveKit side of a call: room token, room, microphone, captions.
@Observable
final class LiveKitClient: NSObject {
    /// The voice server sets this attribute to "answered" when the AI picks up. Until then a call the user
    /// started hears ringing from the voice server while the AI reads the user's Bee data.
    nonisolated static let stateAttribute = "bee_call.state"

    private(set) var captions: [Caption] = []
    @ObservationIgnored var onUnexpectedDisconnect: (() -> Void)?
    @ObservationIgnored var onAgentAnswered: (() -> Void)?
    @ObservationIgnored private var room: Room?
    @ObservationIgnored private var microphoneOn = true // a mute that arrives before the mic is published

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
    func connect(tokenServerURL: String, answering roomName: String? = nil,
                 continuing earlierCall: String? = nil) async throws -> String {
        captions = []
        let access = try await fetchAccess(from: tokenServerURL, answering: roomName, continuing: earlierCall)
        try Task.checkCancellation() // the call ended while we fetched the token
        return try await connect(access: access)
    }

    private func connect(access: Access) async throws -> String {
        let room = Room()
        room.add(delegate: self)
        self.room = room // set first, so disconnect() can stop this attempt at any point
        // The voice server sends both sides' transcripts on this topic. An AI line arrives as one
        // stream in chunks; a user line is re-sent in full as a new stream each time it improves.
        try await room.registerTextStreamHandler(for: "lk.transcription") { [weak self, weak room] reader, sender in
            let segment = reader.info.attributes["lk.segment_id"] ?? reader.info.id
            var text = ""
            for try await chunk in reader {
                text += chunk
                guard let self, let room else { return }
                await self.show(text, segment: segment, from: sender, sentAt: reader.info.timestamp, in: room)
            }
        }
        try Task.checkCancellation() // the call ended while we got the room ready
        try await room.connect(url: access.url, token: access.token)
        try await room.localParticipant.setMicrophone(enabled: microphoneOn)
        if room.remoteParticipants.values.contains(where: { $0.attributes[Self.stateAttribute] == "answered" }) {
            onAgentAnswered?() // the AI picked up before we finished joining (its own call, already answered)
        }
        return access.room
    }

    func setMicrophone(enabled: Bool) async throws {
        microphoneOn = enabled
        try await room?.localParticipant.setMicrophone(enabled: enabled)
    }

    func disconnect() async {
        microphoneOn = true // the next call starts unmuted
        guard let room else { return }
        self.room = nil
        await room.disconnect()
    }

    private func show(_ text: String, segment: String, from sender: Participant.Identity, sentAt: Date, in room: Room) {
        guard room === self.room else { return } // a late line from an earlier call
        let id = "\(sender.stringValue)/\(segment)"
        if let index = captions.firstIndex(where: { $0.id == id }) {
            guard sentAt >= captions[index].sentAt else { return } // an older version of this line
            captions[index].text = text
            captions[index].sentAt = sentAt
        } else {
            let isMine = sender == room.localParticipant.identity
            captions.append(Caption(id: id, isMine: isMine, text: text, sentAt: sentAt))
            if captions.count > 30 { captions.removeFirst() }
        }
    }

    // MARK: - Token server

    private struct Access: Decodable {
        let url: String
        let token: String
        let room: String
    }

    /// The token server answered 410 to an answer request: the AI stopped ringing and closed the room.
    struct CallAlreadyEnded: Error {}

    private struct TokenServerError: LocalizedError {
        let errorDescription: String?
    }

    /// A pass for a new call: the voice server is dispatched into the new room when the app joins.
    /// With `answering`, a pass into the room of the AI's call instead; the voice server is already there.
    /// With `continuing`, the new call picks up from that earlier call (its record id, like 2026-10-14/0055).
    private func fetchAccess(from address: String, answering roomName: String?,
                             continuing earlierCall: String? = nil) async throws -> Access {
        let address = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var parts = URLComponents(string: address), parts.scheme != nil else {
            throw TokenServerError(errorDescription: "The token server address is not a URL.")
        }
        var query = [URLQueryItem(name: "identity", value: "user")]
        if let roomName {
            query += [URLQueryItem(name: "room", value: roomName), URLQueryItem(name: "answer", value: "1")]
        } else if let earlierCall {
            query.append(URLQueryItem(name: "continue", value: earlierCall))
        }
        parts.queryItems = (parts.queryItems ?? []) + query
        guard let url = parts.url else {
            throw TokenServerError(errorDescription: "The token server address is not a URL.")
        }
        let (data, response) = try await http.data(for: URLRequest(url: url, timeoutInterval: 10))
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        if code == 410, roomName != nil {
            throw CallAlreadyEnded()
        }
        guard code == 200 else {
            throw TokenServerError(errorDescription: "The token server answered \(code).")
        }
        return try JSONDecoder().decode(Access.self, from: data)
    }
}

extension LiveKitClient: RoomDelegate {
    nonisolated func room(_ room: Room, didDisconnectWithError error: LiveKitError?) {
        Task { @MainActor in
            guard room === self.room else { return } // we left on purpose
            self.room = nil
            self.onUnexpectedDisconnect?()
        }
    }

    nonisolated func room(_ room: Room, participant: Participant, didUpdateAttributes attributes: [String: String]) {
        guard participant is RemoteParticipant, participant.attributes[Self.stateAttribute] == "answered" else { return }
        Task { @MainActor in
            guard room === self.room else { return } // a late update from an earlier call
            self.onAgentAnswered?()
        }
    }
}
