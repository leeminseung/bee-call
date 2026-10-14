import Foundation
import XCTest
@testable import BeeCall

/// Answers the app's requests instead of the server, and keeps them for the test to read.
@MainActor
final class FakeServer: HTTPTransport {
    /// What happens to a request: its status code and body, or an error such as an unreachable server.
    enum Reply {
        case status(Int, String = "")
        case json(Int, Any)
        case unreachable
        /// Never answers, like a server that has stopped responding; the request ends only when its task is cancelled.
        case hang
    }

    private(set) var requests: [URLRequest] = []
    /// The reply per "METHOD /path"; anything else gets a 204.
    var replies: [String: Reply] = [:]
    /// Runs when a request arrives, before it is answered (for tests that look at the app mid-request).
    var onRequest: ((URLRequest) -> Void)?

    func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        requests.append(request)
        onRequest?(request)
        let key = "\(request.httpMethod ?? "GET") \(request.url?.path ?? "")"
        let reply = replies[key] ?? .status(204)
        func response(_ code: Int) -> HTTPURLResponse {
            HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: "HTTP/1.1", headerFields: nil)!
        }
        switch reply {
        case .status(let code, let body):
            return (Data(body.utf8), response(code))
        case .json(let code, let object):
            return (try JSONSerialization.data(withJSONObject: object), response(code))
        case .unreachable:
            throw URLError(.notConnectedToInternet)
        case .hang:
            while !Task.isCancelled { try? await Task.sleep(for: .milliseconds(20)) }
            throw URLError(.cancelled)
        }
    }

    /// "METHOD /path" of every request so far.
    var sent: [String] {
        requests.map { "\($0.httpMethod ?? "GET") \($0.url?.path ?? "")" }
    }

    /// The JSON body of the last request to "METHOD /path".
    func body(of key: String) throws -> [String: Any] {
        let request = try XCTUnwrap(requests.last { "\($0.httpMethod ?? "GET") \($0.url?.path ?? "")" == key },
                                    "no request \(key)")
        let data = try XCTUnwrap(request.httpBody, "\(key) has no body")
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }
}

/// A UserDefaults of its own for one test, removed when the test ends.
@MainActor
func freshDefaults(_ test: XCTestCase) -> UserDefaults {
    let name = "BeeCallTests-\(UUID().uuidString)"
    let store = UserDefaults(suiteName: name)!
    test.addTeardownBlock { UserDefaults().removePersistentDomain(forName: name) }
    return store
}
