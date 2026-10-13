import SwiftUI
import WebKit

/// The session screen from the Mac (voice/session_server.py): the live or latest call, and the list
/// of earlier calls. Only a web page, so it can stay open during a call; CallKit keeps the audio.
/// An ended call's record has "Continue this call", which the page sends here with the record's id.
struct CallRecordSheet: View {
    let url: URL
    let onContinue: (String) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var failed = false
    @State private var attempt = 0

    /// The session screen runs on the token server's Mac, on port 8788. Nil if the address has no host.
    static func url(tokenServerURL: String) -> URL? {
        let address = tokenServerURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let host = URLComponents(string: address)?.host, !host.isEmpty else { return nil }
        var page = URLComponents()
        page.scheme = "http"
        page.host = host
        page.port = 8788
        page.path = "/"
        return page.url
    }

    var body: some View {
        NavigationStack {
            SessionPage(url: url, attempt: attempt, failed: $failed, onContinue: onContinue)
                .overlay {
                    if failed {
                        ContentUnavailableView {
                            Label("Call record unavailable", systemImage: "desktopcomputer")
                        } description: {
                            Text("The session screen server is not running on the Mac. Start it with `uv run python voice/session_server.py`.")
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
}

/// WKWebView with the session screen. Loads again when `attempt` changes.
private struct SessionPage: UIViewRepresentable {
    static let bridge = "beeCall" // window.webkit.messageHandlers.beeCall in web/app.js
    let url: URL
    let attempt: Int
    @Binding var failed: Bool
    let onContinue: (String) -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(failed: $failed, onContinue: onContinue)
    }

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.userContentController.add(context.coordinator, name: Self.bridge)
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
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
        view.load(URLRequest(url: url, timeoutInterval: 10))
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
        let failed: Binding<Bool>
        let onContinue: (String) -> Void
        var loadedAttempt: Int?

        init(failed: Binding<Bool>, onContinue: @escaping (String) -> Void) {
            self.failed = failed
            self.onContinue = onContinue
        }

        /// "Continue this call": {"continue": "2026-10-14/0055"}. Only an id of that shape is passed on.
        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            guard let body = message.body as? [String: Any], let id = body["continue"] as? String,
                  id.wholeMatch(of: /\d{4}-\d{2}-\d{2}\/\d{4}(-\d+)?/) != nil else { return }
            onContinue(id)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                     withError error: any Error) {
            // A load replaced by the next one also ends here; that is not a failure.
            guard (error as? URLError)?.code != .cancelled else { return }
            failed.wrappedValue = true
        }

        // The page opens a file's raw view in a new window (target=_blank). Show it in this one.
        func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                     for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
            webView.load(navigationAction.request)
            return nil
        }
    }
}
