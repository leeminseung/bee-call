import SwiftUI
import WebKit

/// The session screen from the Mac (voice/session_server.py): the live or latest call, and the list
/// of earlier calls. Only a web page, so it can stay open during a call; CallKit keeps the audio.
struct CallRecordSheet: View {
    let url: URL
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
            SessionPage(url: url, attempt: attempt, failed: $failed)
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
    let url: URL
    let attempt: Int
    @Binding var failed: Bool

    func makeCoordinator() -> Coordinator {
        Coordinator(failed: $failed)
    }

    func makeUIView(context: Context) -> WKWebView {
        let view = WKWebView()
        view.navigationDelegate = context.coordinator
        view.uiDelegate = context.coordinator
        view.allowsBackForwardNavigationGestures = true // back from a file opened with Raw
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {
        guard context.coordinator.loadedAttempt != attempt else { return }
        context.coordinator.loadedAttempt = attempt
        view.load(URLRequest(url: url, timeoutInterval: 10))
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        let failed: Binding<Bool>
        var loadedAttempt: Int?

        init(failed: Binding<Bool>) {
            self.failed = failed
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
