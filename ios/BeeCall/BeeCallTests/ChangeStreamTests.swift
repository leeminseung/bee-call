import XCTest
@testable import BeeCall

/// GET /changes (contract section 3): Server-Sent Events whose one event, `changed`, says a resource has a new
/// version; the app then fetches it again if it holds it.
final class ChangeStreamTests: XCTestCase {
    @MainActor private func changes(in lines: [String]) async throws -> [Change] {
        var found: [Change] = []
        let stream = AsyncStream<String> { continuation in
            lines.forEach { continuation.yield($0) }
            continuation.finish()
        }
        try await ChangeStream.read(lines: stream) { found.append($0) }
        return found
    }

    @MainActor func testEachChangedEventIsReadAndTheRestIsLeftAlone() async throws {
        let found = try await changes(in: [
            ": keepalive",
            "event: changed",
            "data: {\"path\": \"/calls\", \"version\": \"f46f5990ebfadcab\"}",
            "event: changed",
            "data:{\"path\":\"/calls/call-minseung-20261015-012301\",\"version\":\"628b815bc639280f\"}",
            "event: ring", // another stream's event is not a change
            "data: {\"path\": \"/offers\", \"version\": \"92ab364e8f439e2d\"}",
            "event: changed",
            "data: {\"path\": 3}", // unreadable: skipped
            "event: changed",
            "data: {\"path\": \"/offers\", \"version\": \"92ab364e8f439e2d\"}",
        ])
        XCTAssertEqual(found, [Change(path: "/calls", version: "f46f5990ebfadcab"),
                               Change(path: "/calls/call-minseung-20261015-012301", version: "628b815bc639280f"),
                               Change(path: "/offers", version: "92ab364e8f439e2d")])
    }

    /// The stream opens with a request that carries the sign-in and waits at most 40 s for a line: the server sends
    /// a keepalive every 15 s, so 40 s of silence is a dead stream.
    @MainActor func testTheStreamIsAskedForWithTheSignInAndATimeout() throws {
        let request = try XCTUnwrap(ChangeStream.request(server: FakeServer().client(token: "t")))
        XCTAssertEqual(request.url?.path, "/changes")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Accept"), "text/event-stream")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer t")
        XCTAssertEqual(request.timeoutInterval, 40)
    }
}
