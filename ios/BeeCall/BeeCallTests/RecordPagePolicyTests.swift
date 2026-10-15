import SwiftUI
import UIKit
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

    /// Safari only for a link the person taps in the page itself (round 2's review): a frame inside a page, such
    /// as an embed in a job's file, or a page's own redirect, never sends the person out of the app.
    @MainActor func testOnlyATappedLinkInThePageItselfOpensSafari() {
        let elsewhere = URL(string: "https://example.org/")!
        XCTAssertEqual(records.decision(for: elsewhere, tapped: true, mainFrame: true), .openInSafari)
        XCTAssertEqual(records.decision(for: elsewhere, tapped: false, mainFrame: true), .refuse) // a redirect
        XCTAssertEqual(records.decision(for: elsewhere, tapped: true, mainFrame: false), .refuse) // inside a frame
        let own = URL(string: "https://call.mslee.dev:8443/api/files/x")!
        XCTAssertEqual(records.decision(for: own, tapped: false, mainFrame: false), .stay)
        XCTAssertEqual(records.decision(for: URL(string: "about:blank")!, tapped: true, mainFrame: true), .refuse)
    }

    /// The web view itself (with addresses under .invalid, so nothing leaves this Mac): a tapped link to another
    /// site opens in Safari.
    @MainActor func testTheWebViewHandsATappedLinkToSafari() async throws {
        let records = URL(string: "https://records.invalid:8443/")!
        var opened: [URL] = []
        let coordinator = SessionPage.Coordinator(policy: RecordPagePolicy(records: records), failed: .constant(false),
                                                  onContinue: { _ in }, openOutside: { opened.append($0) })
        let view = SessionPage.webView(coordinator)
        view.loadHTMLString("""
            <!doctype html><a id="out" href="https://elsewhere.invalid/page">elsewhere</a>
            <script>document.getElementById("out").click()</script>
            """, baseURL: records)
        await waitUntil("the other site is handed on", timeout: .seconds(10)) { !opened.isEmpty }
        XCTAssertEqual(opened, [URL(string: "https://elsewhere.invalid/page")!])
        XCTAssertEqual(view.url?.host, "records.invalid")
    }

    /// The web view itself, on screen so that it loads frames: a frame from another site and a page's own
    /// redirect send nobody to Safari, and the page stays.
    @MainActor func testTheWebViewSendsNoFrameOrRedirectToSafari() async throws {
        let records = URL(string: "https://records.invalid:8443/")!
        var opened: [URL] = []
        var asked: [URL] = []
        let coordinator = SessionPage.Coordinator(policy: RecordPagePolicy(records: records), failed: .constant(false),
                                                  onContinue: { _ in }, openOutside: { opened.append($0) })
        coordinator.onDecision = { asked.append($0) }
        let view = SessionPage.webView(coordinator)
        let window = try XCTUnwrap((UIApplication.shared.connectedScenes.first as? UIWindowScene)?.windows.first)
        view.frame = CGRect(x: 0, y: 0, width: 300, height: 300)
        window.addSubview(view)
        defer { view.removeFromSuperview() }
        view.loadHTMLString("""
            <!doctype html><iframe src="https://ads.invalid/frame"></iframe>
            <script>location.href = "https://elsewhere.invalid/redirect"</script>
            """, baseURL: records)
        await waitUntil("both loads are decided", timeout: .seconds(10)) {
            asked.contains(URL(string: "https://ads.invalid/frame")!)
                && asked.contains(URL(string: "https://elsewhere.invalid/redirect")!)
        }
        XCTAssertEqual(opened, [])
        XCTAssertEqual(view.url?.host, "records.invalid")
    }
}
