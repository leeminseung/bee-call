import Network
import WebKit
import XCTest
@testable import BeeCall

/// The call record page's sign-in cookie (review finding 4). A Debug build's records address is
/// http://<Mac>:8788/; HTTPCookie treats the mere presence of the .secure key as Secure, so a cookie built
/// with `.secure: false` was never sent there and every /api/ request got a 401.
final class RecordCookieTests: XCTestCase {
    @MainActor func testTheCookieCarriesTheSignInTokenForTheRecordsHost() throws {
        let cookie = try XCTUnwrap(CallRecordSheet.signInCookie(token: "t0ken",
                                                                for: URL(string: "https://call.mslee.dev:8443/")!))
        XCTAssertEqual(cookie.name, "bee_call_session")
        XCTAssertEqual(cookie.value, "t0ken")
        XCTAssertEqual(cookie.domain, "call.mslee.dev")
        XCTAssertEqual(cookie.path, "/")
    }

    @MainActor func testAnHTTPSAddressGetsASecureCookie() throws {
        let cookie = try XCTUnwrap(CallRecordSheet.signInCookie(token: "t0ken",
                                                                for: URL(string: "https://call.mslee.dev:8443/")!))
        XCTAssertTrue(cookie.isSecure)
    }

    @MainActor func testAnHTTPAddressGetsACookieThatIsNotSecure() throws {
        let cookie = try XCTUnwrap(CallRecordSheet.signInCookie(token: "t0ken",
                                                                for: URL(string: "http://192.168.219.106:8788/")!))
        XCTAssertFalse(cookie.isSecure)
    }

    /// Scripts in the page cannot read the sign-in token.
    @MainActor func testTheCookieIsHTTPOnly() throws {
        for address in ["https://call.mslee.dev:8443/", "http://192.168.219.106:8788/"] {
            let cookie = try XCTUnwrap(CallRecordSheet.signInCookie(token: "t0ken", for: URL(string: address)!))
            XCTAssertTrue(cookie.isHTTPOnly, address)
        }
    }

    /// The platform check: a WKWebView on this Simulator sends the cookie to an http records address. The page
    /// is served on this Mac's LAN address, like a Debug build's records address: WebKit sends even a Secure
    /// cookie to 127.0.0.1 over http, so the loopback address would hide the defect.
    @MainActor func testAWebViewSendsTheCookieToAnHTTPRecordsAddress() async throws {
        let host = try XCTUnwrap(lanAddress(), "no LAN address on this Mac to serve the page on")
        let server = try await OnePageServer.start(on: host)
        defer { server.stop() }
        let url = URL(string: "http://\(host):\(server.port)/")!
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let cookie = try XCTUnwrap(CallRecordSheet.signInCookie(token: "t0ken", for: url))
        await configuration.websiteDataStore.httpCookieStore.setCookie(cookie)
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.load(URLRequest(url: url))
        await waitUntil("the page is requested", timeout: .seconds(15)) { !server.requests.isEmpty }
        let request = try XCTUnwrap(server.requests.first)
        // HTTP lines end in "\r\n", which Swift counts as one Character, so the head is split on it.
        let headers = request.components(separatedBy: "\r\n").map { $0.lowercased() }
        XCTAssertTrue(headers.contains("cookie: bee_call_session=t0ken"), "no sign-in cookie in:\n\(request)")
    }
}

/// The first IPv4 address of a Wi-Fi or Ethernet interface (en0, en1, ...), which the Simulator shares with the Mac.
private func lanAddress() -> String? {
    var first: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&first) == 0 else { return nil }
    defer { freeifaddrs(first) }
    var next = first
    while let entry = next?.pointee {
        next = entry.ifa_next
        guard String(cString: entry.ifa_name).hasPrefix("en"), let address = entry.ifa_addr,
              address.pointee.sa_family == UInt8(AF_INET) else { continue }
        var text = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        guard getnameinfo(address, socklen_t(address.pointee.sa_len), &text, socklen_t(text.count), nil, 0,
                          NI_NUMERICHOST) == 0 else { continue }
        return String(cString: text)
    }
    return nil
}

/// A web server on one address that answers every request with a small page and keeps the request's head.
@MainActor
private final class OnePageServer {
    private let listener: NWListener
    private(set) var requests: [String] = []

    private init(listener: NWListener) {
        self.listener = listener
    }

    var port: UInt16 { listener.port?.rawValue ?? 0 }

    static func start(on host: String) async throws -> OnePageServer {
        let parameters = NWParameters.tcp
        parameters.requiredLocalEndpoint = .hostPort(host: NWEndpoint.Host(host), port: .any)
        let server = OnePageServer(listener: try NWListener(using: parameters))
        server.listener.newConnectionHandler = { connection in
            MainActor.assumeIsolated { server.answer(connection) }
        }
        await withCheckedContinuation { (ready: CheckedContinuation<Void, Never>) in
            var resumed = false
            server.listener.stateUpdateHandler = { state in
                MainActor.assumeIsolated {
                    guard !resumed else { return }
                    switch state {
                    case .ready, .failed, .cancelled:
                        resumed = true
                        ready.resume()
                    default:
                        break
                    }
                }
            }
            server.listener.start(queue: .main)
        }
        return server
    }

    func stop() {
        listener.cancel()
    }

    private func answer(_ connection: NWConnection) {
        connection.start(queue: .main)
        connection.receive(minimumIncompleteLength: 1, maximumLength: 65_536) { data, _, _, _ in
            MainActor.assumeIsolated {
                self.requests.append(String(decoding: data ?? Data(), as: UTF8.self))
            }
            let page = "<!doctype html><title>ok</title>ok"
            let reply = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: \(page.utf8.count)\r\n"
                + "Connection: close\r\n\r\n\(page)"
            connection.send(content: Data(reply.utf8), completion: .contentProcessed { _ in connection.cancel() })
        }
    }
}
