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
    private(set) var captions: [Caption] = []
    @ObservationIgnored var onUnexpectedDisconnect: (() -> Void)?
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

    /// Joins a new room and publishes the microphone. Returns the room name.
    func connect(tokenServerURL: String) async throws -> String {
        let room = Room()
        room.add(delegate: self)
        self.room = room // set first, so disconnect() can stop this attempt at any point
        captions = []
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
        let access = try await fetchAccess(from: tokenServerURL)
        try Task.checkCancellation() // the call ended while we fetched the token
        try await room.connect(url: access.url, token: access.token)
        try await room.localParticipant.setMicrophone(enabled: microphoneOn)
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

    private struct TokenServerError: LocalizedError {
        let errorDescription: String?
    }

    private func fetchAccess(from address: String) async throws -> Access {
        let address = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var parts = URLComponents(string: address), parts.scheme != nil else {
            throw TokenServerError(errorDescription: "The token server address is not a URL.")
        }
        parts.queryItems = (parts.queryItems ?? []) + [URLQueryItem(name: "identity", value: "user")]
        guard let url = parts.url else {
            throw TokenServerError(errorDescription: "The token server address is not a URL.")
        }
        let (data, response) = try await http.data(for: URLRequest(url: url, timeoutInterval: 10))
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
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
}
