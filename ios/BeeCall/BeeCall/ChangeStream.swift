import Foundation

/// The token server's GET /changes (contract section 3), modelled on the ring stream: Server-Sent Events whose one
/// event, `changed`, says the resource at a path (/calls, /offers or /calls/{call_id}) now has a version. On connect
/// the server sends one for the list, the offers and each call that is live or ended in the last ten minutes, so a
/// reconnect brings the app up to date with no event ids. The app keeps the stream open while it is on screen or in
/// a call, and fetches a changed resource again if it holds it (CallStore, OfferCenter).
enum ChangeStream {
    /// The server answered, but not with a change stream.
    struct BadStatus: Error {
        let code: Int
    }

    /// GET /changes with the sign-in. The server sends a keepalive every 15 s, so 40 s without a line is a dead
    /// stream.
    static func request(server: ServerClient) -> URLRequest? {
        guard var request = server.request("/changes", timeout: 40) else { return nil }
        request.setValue("text/event-stream", forHTTPHeaderField: "Accept")
        return request
    }

    /// Opens the stream and reads its changes until it ends. `onOpen` runs once the server has accepted it.
    static func read(server: ServerClient, onOpen: () -> Void, onChange: (Change) -> Void) async throws {
        guard let request = request(server: server) else { throw URLError(.badURL) } // no address in this build
        let (bytes, response) = try await URLSession.shared.bytes(for: request)
        let code = response.httpStatus
        guard code == 200 else {
            _ = server.read(code, Data(), to: request) // a 401: the sign-in ended
            throw BadStatus(code: code)
        }
        onOpen()
        var parser = Parser()
        for try await line in bytes.lines {
            if let change = parser.read(line) { onChange(change) }
        }
    }

    /// The `changed` events among a stream's lines. `lines` drops the blank line that ends an event, so an event
    /// is handled at its data line (the server sends one per event); lines starting with ":" are keepalives.
    struct Parser {
        private var event = ""

        /// The change a line completes, if it is the data line of a `changed` event that can be read.
        mutating func read(_ line: String) -> Change? {
            if line.hasPrefix("event:") {
                event = value(of: line, field: "event:")
            } else if line.hasPrefix("data:") {
                defer { event = "" }
                guard event == "changed" else { return nil }
                return try? Contract.decode(Change.self, from: Data(value(of: line, field: "data:").utf8))
            }
            return nil
        }

        private func value(of line: String, field: String) -> String {
            let rest = line.dropFirst(field.count)
            return String(rest.hasPrefix(" ") ? rest.dropFirst() : rest)
        }
    }

    /// Keeps the stream open until the task is cancelled, opening it again after a pause when it closes: 1 s,
    /// doubling up to 10 s, and 1 s again once a stream was accepted.
    static func keepOpen(server: ServerClient, onChange: (Change) -> Void) async {
        var delay = 1
        while !Task.isCancelled {
            try? await read(server: server, onOpen: { delay = 1 }, onChange: onChange)
            guard !Task.isCancelled else { return }
            try? await Task.sleep(for: .seconds(delay))
            delay = min(delay * 2, 10)
        }
    }
}
