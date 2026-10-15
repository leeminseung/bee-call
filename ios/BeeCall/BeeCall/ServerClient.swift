import Foundation

/// What the token server answered a request, read once by ServerClient. Each caller turns it into its own words.
enum ServerAnswer: Equatable {
    case ok(Int, Data) // a 2xx, with its body
    case signedOut // a 401: the sign-in has ended, and the person is signed out already
    case refused(Int, Data) // any other answer, such as 400, 404, 409, 410, 429 or a proxy's 502, with its body
    case unreachable // no answer at all, or no server address in this build
}

/// The one way to the token server (voice/token_server.py). Every request is built from the build's address and
/// carries the sign-in token when there is one, and every answer is read here, once: a 401 means the sign-in has
/// ended (it expired, or the person was removed), so the person is signed out (review finding 5).
struct ServerClient {
    var transport: any HTTPTransport = URLSession.shared
    /// The sign-in token, when the Keychain gives one.
    var token: () -> String? = { Server.savedToken() }
    /// What an ended sign-in does: Server.shared signs the person out.
    var signInEnded: () -> Void = { Server.shared.signInEnded() }

    /// A request to `path` on the token server. Nil when this build has no server address.
    func request(_ path: String, method: String = "GET", json: Any? = nil, timeout: TimeInterval = 10) -> URLRequest? {
        guard !Server.api.isEmpty, let url = URL(string: Server.api + path) else { return nil }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        authorize(&request)
        if let json {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try? JSONSerialization.data(withJSONObject: json)
        }
        return request
    }

    /// Adds the sign-in token to a request built elsewhere (the call's pass, the ring stream).
    func authorize(_ request: inout URLRequest) {
        if let token = token() {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
    }

    func send(_ path: String, method: String = "GET", json: Any? = nil, timeout: TimeInterval = 10) async
        -> ServerAnswer {
        guard let request = request(path, method: method, json: json, timeout: timeout) else { return .unreachable }
        return await send(request)
    }

    func send(_ request: URLRequest) async -> ServerAnswer {
        guard let (body, response) = try? await transport.send(request) else { return .unreachable }
        return read(response.httpStatus, body, to: request)
    }

    /// The one reading of an answer, also for answers that come some other way (the ring stream's).
    func read(_ status: Int, _ body: Data, to request: URLRequest) -> ServerAnswer {
        switch status {
        case 200..<300:
            return .ok(status, body)
        case 401:
            signInEnded()
            return .signedOut
        default:
            return .refused(status, body)
        }
    }
}
