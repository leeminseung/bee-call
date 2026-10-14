import Foundation

/// How a request reaches the server: URLSession in the app, a stand-in that answers like the server in tests.
protocol HTTPTransport {
    func send(_ request: URLRequest) async throws -> (Data, URLResponse)
}

extension URLSession: HTTPTransport {
    func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        try await data(for: request)
    }
}

extension URLResponse {
    /// The HTTP status code, or 0 for a response that is not HTTP.
    var httpStatus: Int { (self as? HTTPURLResponse)?.statusCode ?? 0 }
}
