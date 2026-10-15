import Foundation

/// What the call record's web view lets in (review finding 8). The page shows files from background jobs, and a
/// link in one could take the web view anywhere, inside the app and with no address bar. So the web view stays on
/// the records server's own pages, and any other web address opens in Safari.
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
