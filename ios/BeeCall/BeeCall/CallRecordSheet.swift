import SwiftUI
import WebKit

/// The session screen from the server (voice/session_server.py): the live or latest call, and the list
/// of earlier calls. Only a web page, so it can stay open during a call; CallKit keeps the audio.
/// An ended call's record has "Continue this call", which the page sends here with the record's id.
/// The page asks the server for the signed-in person's calls with the sign-in cookie set before it loads.
struct CallRecordSheet: View {
    let url: URL
    let onContinue: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var failed = false
    @State private var attempt = 0


    var body: some View {
        NavigationStack {
            SessionPage(url: url, attempt: attempt, failed: $failed, onContinue: onContinue)
                .overlay {
                    if failed {
                        ContentUnavailableView {
                            Label("Call record unavailable", systemImage: "desktopcomputer")
                        } description: {
                            Text("The call record could not be loaded from the server.")
                        } actions: {
                            Button("Try again") {
                                failed = false
                                attempt += 1
                            }
                        }
                        .background(Color(.systemBackground))
                    }
                }
                .navigationTitle("Call record")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Close") { dismiss() }
                    }
                }
        }
    }

    /// The sign-in cookie the page's requests carry (voice/session_server.py reads bee_call_session). HttpOnly, so
    /// scripts in the page cannot read the token.
    static func signInCookie(token: String, for url: URL) -> HTTPCookie? {
        guard let host = url.host else { return nil }
        var properties: [HTTPCookiePropertyKey: Any] = [.name: "bee_call_session", .value: token, .domain: host,
                                                        .path: "/", HTTPCookiePropertyKey("HttpOnly"): "TRUE"]
        // HTTPCookie reads the mere presence of .secure as Secure, whatever its value, and a Secure cookie never
        // goes to a Debug build's http://<Mac>:8788/. So the key is there for https only.
        if url.scheme == "https" {
            properties[.secure] = "TRUE"
        }
        return HTTPCookie(properties: properties)
    }
}

/// WKWebView with the session screen. Loads again when `attempt` changes.
struct SessionPage: UIViewRepresentable {
    static let bridge = "beeCall" // window.webkit.messageHandlers.beeCall in web/app.js
    let url: URL
    let attempt: Int
    @Binding var failed: Bool
    let onContinue: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(policy: RecordPagePolicy(records: url), failed: $failed, onContinue: onContinue)
    }

    func makeUIView(context: Context) -> WKWebView {
        Self.webView(context.coordinator)
    }

    /// The web view, with the coordinator as its delegates and the page's bridge.
    static func webView(_ coordinator: Coordinator) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(coordinator, name: bridge)
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = coordinator
        view.uiDelegate = coordinator
        view.allowsBackForwardNavigationGestures = true // back from a file opened with Raw
        return view
    }

    static func dismantleUIView(_ view: WKWebView, coordinator: Coordinator) {
        // The content controller keeps its handler alive; let the coordinator go with the sheet.
        view.configuration.userContentController.removeScriptMessageHandler(forName: bridge)
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        guard context.coordinator.loadedAttempt != attempt else { return }
        context.coordinator.loadedAttempt = attempt
        let request = URLRequest(url: url, timeoutInterval: 10)
        guard let token = Server.savedToken(), let cookie = CallRecordSheet.signInCookie(token: token, for: url) else {
            view.load(request)
            return
        }
        view.configuration.websiteDataStore.httpCookieStore.setCookie(cookie) { view.load(request) }
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        let policy: RecordPagePolicy
        let failed: Binding<Bool>
        let onContinue: (String) -> Void
        let openOutside: (URL) -> Void // Safari, in the app
        var loadedAttempt: Int?

        init(policy: RecordPagePolicy, failed: Binding<Bool>, onContinue: @escaping (String) -> Void,
             openOutside: @escaping (URL) -> Void = { UIApplication.shared.open($0) }) {
            self.policy = policy
            self.failed = failed
            self.onContinue = onContinue
            self.openOutside = openOutside
        }

        /// "Continue this call": {"continue": "2026-10-14/0055"}, from the records page itself (RecordPagePolicy).
        /// Only an id of that shape is passed on.
        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            let frame = message.frameInfo, origin = frame.securityOrigin
            guard policy.acceptsMessage(fromMainFrame: frame.isMainFrame, scheme: origin.protocol, host: origin.host,
                                        port: origin.port),
                  let body = message.body as? [String: Any], let id = body["continue"] as? String,
                  id.wholeMatch(of: /\d{4}-\d{2}-\d{2}\/\d{4}(-\d+)?/) != nil else { return }
            onContinue(id)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: any Error) {
            // A load replaced by the next one also ends here; that is not a failure.
            guard (error as? URLError)?.code != .cancelled else { return }
            failed.wrappedValue = true
        }

        /// Every load, the page's own and a link's: the records server's pages stay here, other sites open in
        /// Safari, and anything else goes nowhere (RecordPagePolicy).
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping @MainActor (WKNavigationActionPolicy) -> Void) {
            guard let url = navigationAction.request.url else { return decisionHandler(.cancel) }
            switch policy.navigation(to: url) {
            case .stay:
                decisionHandler(.allow)
            case .openInSafari:
                openOutside(url)
                decisionHandler(.cancel)
            case .refuse:
                decisionHandler(.cancel)
            }
        }

        // The page opens a file's raw view in a new window (target=_blank). Show it in this one; the load goes
        // through the policy above like any other.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            webView.load(navigationAction.request)
            return nil
        }
    }
}
