import Foundation

/// A call the AI placed and is waiting for the phone to answer.
struct Ring {
    let room: String
    let topic: String? // Bee's short summary of the conversation the call is about
    let expiresAt: Date // after this the voice server has given up and closed the room
}

/// The token server's GET /ring, a Server-Sent Events stream: first the calls placed in the last
/// minute, then each new one. This is how an open app hears that the AI is calling; a VoIP push
/// carries the same ring to a closed or locked one (CallManager).
enum RingStream {
    /// The server answered, but not with a ring stream (for example a token server without /ring).
    struct BadStatus: Error {
        let code: Int
    }

    /// The ring a VoIP push carries: the same fields as a ring event.
    static func ring(fromPush payload: [AnyHashable: Any]) -> Ring? {
        guard JSONSerialization.isValidJSONObject(payload),
              let data = try? JSONSerialization.data(withJSONObject: payload) else { return nil }
        return parse(String(decoding: data, as: UTF8.self))
    }

    /// Reads ring events until the stream ends. `onOpen` runs once the server has accepted the stream.
    static func read(server: ServerClient = ServerClient(), onOpen: () -> Void, onRing: (Ring) -> Void) async throws {
        // The server sends a keepalive every 15 s, so 40 s of silence means the connection is gone.
        guard var request = server.request("/ring", timeout: 40) else { throw URLError(.badURL) } // no address
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else {
            _ = server.read(code, Data(), to: request) // a 401: the sign-in ended
            throw BadStatus(code: code)
        }
        onOpen()
        var event = ""
        // `lines` drops the blank line that ends an event, so an event is handled at its data line
        // (the token server sends one per event). Lines starting with ":" are keepalives.
        for try await line in bytes.lines {
            if line.hasPrefix("event:") {
                event = value(of: line, field: "event:")
            } else if line.hasPrefix("data:") {
                if event == "ring", let ring = parse(value(of: line, field: "data:")) {
                    onRing(ring)
                }
                event = ""
            }
        }
    }

    private static func value(of line: String, field: String) -> String {
        let rest = line.dropFirst(field.count)
        return String(rest.hasPrefix(" ") ? rest.dropFirst() : rest)
    }

    private struct Line: Decodable {
        let room: String
        let topic: String?
        let expiresAt: String
        let expiresEpoch: Double? // the same moment as seconds since 1970
    }

    private static func parse(_ json: String) -> Ring? {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let line = try? decoder.decode(Line.self, from: Data(json.utf8)),
              let expiresAt = line.expiresEpoch.map(Date.init(timeIntervalSince1970:))
                ?? ISO8601DateFormatter().date(from: line.expiresAt) else { return nil }
        let topic = line.topic?.trimmingCharacters(in: .whitespacesAndNewlines)
        return Ring(room: line.room, topic: topic?.isEmpty == false ? topic : nil, expiresAt: expiresAt)
    }
}
