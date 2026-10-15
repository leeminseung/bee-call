import Foundation

/// What the call record's web view lets in (review finding 8). The page shows files from background jobs, and a
/// link in one could take the web view anywhere, inside the app and with no address bar. So the web view stays on
/// the records server's own pages, another site opens in Safari when the person taps a link to it, and only the
/// records page itself can ask the app to continue a call.
struct RecordPagePolicy {
    enum Navigation: Equatable {
        case stay // a page of the records server
        case openInSafari // another web address
        case refuse // anything that is not a web address
    }

    let records: URL

    func navigation(to url: URL) -> Navigation {
        if let origin = Origin(url), origin == Origin(records) { return .stay }
        return ["http", "https"].contains(url.scheme?.lowercased()) ? .openInSafari : .refuse
    }

    /// What to do with a load (round 2's review): the records server's own pages stay; another site opens in
    /// Safari only for a link the person tapped in the page itself; a frame inside a page, such as an embed in a
    /// job's file, or a page's own redirect, is stopped quietly, so nothing sends the person out of the app
    /// without a tap.
    func decision(for url: URL, tapped: Bool, mainFrame: Bool) -> Navigation {
        switch navigation(to: url) {
        case .stay: .stay
        case .openInSafari where tapped && mainFrame: .openInSafari
        case .openInSafari, .refuse: .refuse
        }
    }

    /// "Continue this call" starts a call, so it counts only from the main frame of a page on the records server:
    /// not from a frame inside a page, nor from a page anywhere else. WebKit gives a default port as 0.
    func acceptsMessage(fromMainFrame isMainFrame: Bool, scheme: String, host: String, port: Int) -> Bool {
        guard isMainFrame, let origin = Origin(scheme: scheme, host: host, port: port == 0 ? nil : port) else {
            return false
        }
        return origin == Origin(records)
    }

    /// Scheme, host and port, with a default port written or not.
    private struct Origin: Equatable {
        let scheme: String
        let host: String
        let port: Int

        init?(scheme: String, host: String, port: Int?) {
            let scheme = scheme.lowercased()
            guard let standard = ["http": 80, "https": 443][scheme], !host.isEmpty else { return nil }
            self.scheme = scheme
            self.host = host.lowercased()
            self.port = port ?? standard
        }

        init?(_ url: URL) {
            guard let scheme = url.scheme, let host = url.host else { return nil }
            self.init(scheme: scheme, host: host, port: url.port)
        }
    }
}
