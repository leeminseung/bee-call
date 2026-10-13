import Foundation

/// A call the AI placed and is waiting for the phone to answer.
struct Ring {
    let room: String
    let topic: String? // Bee's short summary of the conversation the call is about
    let expiresAt: Date // after this the voice server has given up and closed the room
}

/// The token server's GET /ring, a Server-Sent Events stream: first the calls placed in the last
/// minute, then each new one. Until VoIP push is set up (it needs the paid Apple Developer account),
/// this is how an open app hears that the AI is calling.
enum RingStream {
    /// The server answered, but not with a ring stream (for example a token server without /ring).
    struct BadStatus: Error {
        let code: Int
    }

    /// `/ring` on the token server's scheme, host and port. Nil if the address is empty or not a URL.
    static func url(tokenServerURL: String) -> URL? {
        let address = tokenServerURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard var parts = URLComponents(string: address), parts.scheme == "http" || parts.scheme == "https",
              parts.host?.isEmpty == false else { return nil }
        parts.path = "/ring"
        parts.query = nil
        parts.fragment = nil
        return parts.url
    }

    /// Reads ring events until the stream ends. `onOpen` runs once the server has accepted the stream.
    static func read(from url: URL, onOpen: () -> Void, onRing: (Ring) -> Void) async throws {
        // The server sends a keepalive every 15 s, so 40 s of silence means the connection is gone.
        var request = URLRequest(url: url, timeoutInterval: 40)
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        let code = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard code == 200 else { throw BadStatus(code: code) }
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
    }

    private static func parse(_ json: String) -> Ring? {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        guard let line = try? decoder.decode(Line.self, from: Data(json.utf8)),
              let expiresAt = macClock.date(from: line.expiresAt) else { return nil }
        let topic = line.topic?.trimmingCharacters(in: .whitespacesAndNewlines)
        return Ring(room: line.room, topic: topic?.isEmpty == false ? topic : nil, expiresAt: expiresAt)
    }

    /// The Mac writes its local time with no zone. The phone is in the same zone, so read it as local time.
    private static let macClock: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return formatter
    }()
}
