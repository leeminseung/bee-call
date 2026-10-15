import SwiftUI
import WebKit
import XCTest
@testable import BeeCall

/// What the call record's web view lets in (review finding 8).
final class RecordPagePolicyTests: XCTestCase {
    private let records = RecordPagePolicy(records: URL(string: "https://call.mslee.dev:8443/")!)

    // MARK: - Where it may go

    @MainActor func testTheRecordsServersOwnPagesStayInTheWebView() {
        for address in ["https://call.mslee.dev:8443/", "https://call.mslee.dev:8443/?debug=1",
                        "https://CALL.mslee.dev:8443/api/files/2026-10-14/0055/notes.md"] {
            XCTAssertEqual(records.navigation(to: URL(string: address)!), .stay, address)
        }
    }

    /// A link in a background job's file used to open any site inside the app, with no address bar.
    @MainActor func testOtherSitesOpenInSafari() {
        for address in ["https://example.org/", "https://call.mslee.dev/", "http://call.mslee.dev:8443/",
                        "https://call.mslee.dev.example.org:8443/"] {
            XCTAssertEqual(records.navigation(to: URL(string: address)!), .openInSafari, address)
        }
    }

    @MainActor func testAnythingThatIsNotAWebAddressIsRefused() {
        for address in ["about:blank", "data:text/html,hi", "file:///etc/hosts", "mailto:matt@example.org"] {
            XCTAssertEqual(records.navigation(to: URL(string: address)!), .refuse, address)
        }
    }

    /// The default port is the same whether written or not, as in a Debug build's http address.
    @MainActor func testADefaultPortIsTheSameAsNone() {
        let plain = RecordPagePolicy(records: URL(string: "https://call.mslee.dev/")!)
        XCTAssertEqual(plain.navigation(to: URL(string: "https://call.mslee.dev:443/x")!), .stay)
        let debug = RecordPagePolicy(records: URL(string: "http://192.168.219.106:8788/?debug=1")!)
        XCTAssertEqual(debug.navigation(to: URL(string: "http://192.168.219.106:8788/api/sessions")!), .stay)
        XCTAssertEqual(debug.navigation(to: URL(string: "http://192.168.219.106/")!), .openInSafari)
    }

    // MARK: - Who may continue a call

    /// "Continue this call" starts a call: it counts only from the main frame of a page on the records server.
    /// WebKit gives a default port as 0.
    @MainActor func testOnlyTheRecordsPageItselfMayContinueACall() {
        XCTAssertTrue(records.acceptsMessage(fromMainFrame: true, scheme: "https", host: "call.mslee.dev", port: 8443))
        XCTAssertFalse(records.acceptsMessage(fromMainFrame: false, scheme: "https", host: "call.mslee.dev",
                                              port: 8443)) // a frame inside the page
        XCTAssertFalse(records.acceptsMessage(fromMainFrame: true, scheme: "https", host: "example.org", port: 8443))
        XCTAssertFalse(records.acceptsMessage(fromMainFrame: true, scheme: "https", host: "call.mslee.dev", port: 0))
        XCTAssertFalse(records.acceptsMessage(fromMainFrame: true, scheme: "", host: "", port: 0)) // a sandboxed file
        let plain = RecordPagePolicy(records: URL(string: "https://call.mslee.dev/")!)
        XCTAssertTrue(plain.acceptsMessage(fromMainFrame: true, scheme: "https", host: "call.mslee.dev", port: 0))
    }

    /// The web view itself: the records page's "Continue this call" reaches the app.
    @MainActor func testTheRecordsPageContinuesACall() async throws {
        let records = URL(string: "https://records.invalid:8443/")!
        var continued: [String] = []
        let coordinator = SessionPage.Coordinator(policy: RecordPagePolicy(records: records), failed: .constant(false),
                                                  onContinue: { continued.append($0) }, openOutside: { _ in })
        let view = SessionPage.webView(coordinator)
        view.loadHTMLString("""
            <!doctype html><script>
            window.webkit.messageHandlers.beeCall.postMessage({continue: "2026-10-14/0055"});
            </script>
            """, baseURL: records)
        await waitUntil("the call is continued", timeout: .seconds(10)) { !continued.isEmpty }
        XCTAssertEqual(continued, ["2026-10-14/0055"])
    }

    /// The web view itself: a page that leaves for another site is stopped, and the site opens in Safari.
    @MainActor func testTheWebViewHandsOtherSitesToSafari() async throws {
        let records = URL(string: "https://records.invalid:8443/")! // never resolves: nothing leaves this Mac
        var opened: [URL] = []
        let coordinator = SessionPage.Coordinator(policy: RecordPagePolicy(records: records), failed: .constant(false),
                                                  onContinue: { _ in }, openOutside: { opened.append($0) })
        let view = SessionPage.webView(coordinator)
        view.loadHTMLString("<!doctype html><script>location.href = 'https://elsewhere.invalid/page'</script>",
                            baseURL: records)
        await waitUntil("the other site is handed on", timeout: .seconds(10)) { !opened.isEmpty }
        XCTAssertEqual(opened, [URL(string: "https://elsewhere.invalid/page")!])
        XCTAssertEqual(view.url?.host, "records.invalid")
    }
}
