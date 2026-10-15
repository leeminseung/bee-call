import Foundation
import Observation

/// The only place that fetches the call list (GET /calls) and the call views (GET /calls/{call_id}), contract
/// sections 1 to 3. It keeps the last body of each with its version and asks again with If-None-Match, so an
/// unchanged answer is a 304 with no body. What /changes announces is fetched only when the app holds that
/// resource; otherwise the version is noted, and the resource is fetched when it is shown.
@Observable
final class CallStore {
    private(set) var list: CallList? // the first page
    private(set) var older: [CallEntry] = [] // the older pages loaded, newest first
    private(set) var views: [String: CallView] = [:]
    private(set) var missing: Set<String> = [] // views the server has no record for (404), until it does
    private(set) var problem: String? // the last fetch that did not come through, in words
    private var olderLoaded = false // an older page has come
    private var olderCursor: String? // the next older page after the last one loaded
    @ObservationIgnored private var announced: [String: String] = [:] // path → the version /changes said
    @ObservationIgnored private let server: ServerClient

    init(server: ServerClient = ContractServer.client) {
        self.server = server
    }

    /// The calls, newest first.
    var calls: [CallEntry] {
        (list?.calls ?? []) + older
    }

    /// Older calls exist that are not loaded.
    var hasOlder: Bool {
        olderLoaded ? olderCursor != nil : list?.more == true
    }

    // MARK: - Fetching

    func refreshList() async {
        guard let body: CallList = await fetch("/calls", holding: list?.version) else { return }
        list = body
        announced["/calls"] = body.version
    }

    func loadOlder() async {
        guard let before = olderLoaded ? olderCursor : list?.before else { return }
        var query = URLComponents()
        query.queryItems = [URLQueryItem(name: "before", value: before)]
        guard let page: CallList = await fetch("/calls?" + (query.percentEncodedQuery ?? ""), holding: nil) else {
            return
        }
        older += page.calls
        olderCursor = page.more ? page.before : nil
        olderLoaded = true
    }

    func refreshView(_ callID: String) async {
        let path = Self.viewPath(callID)
        switch await answer(path, holding: views[callID]?.version) {
        case .body(let data):
            guard let view = read(CallView.self, data) else { return }
            views[callID] = view
            missing.remove(callID)
            announced[path] = view.version
        case .notFound:
            missing.insert(callID)
        case .unchanged, .failed:
            break
        }
    }

    /// A call's section is shown (expanded, opened as a sheet, or the call that is on): fetched if the app has
    /// no view of it, or /changes announced a newer one.
    func show(_ callID: String) async {
        let path = Self.viewPath(callID)
        guard let view = views[callID] else { return await refreshView(callID) }
        if let version = announced[path], version != view.version { await refreshView(callID) }
    }

    /// GET /changes said the resource at this path now has this version.
    func changed(_ change: Change) async {
        announced[change.path] = change.version
        if change.path == "/calls" {
            if list?.version != change.version { await refreshList() }
        } else if change.path.hasPrefix("/calls/") {
            let callID = String(change.path.dropFirst("/calls/".count))
            if let view = views[callID] {
                if view.version != change.version { await refreshView(callID) }
            } else if missing.contains(callID) {
                await refreshView(callID) // a record that has begun since the 404
            } // else not held: only noted, for when it is shown
        }
    }

    /// The person's own call in the list: the newest call whose id is its room, or the room and a dot (a voice
    /// server restarted in the room, section 1).
    static func ownCall(room: String, in calls: [CallEntry]) -> CallEntry? {
        calls.first { $0.call.callID == room || $0.call.callID.hasPrefix(room + ".") }
    }

    static func viewPath(_ callID: String) -> String {
        "/calls/" + (callID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? callID)
    }

    // MARK: - The one way to an answer

    private enum Answer {
        case body(Data)
        case unchanged // 304
        case notFound // 404
        case failed
    }

    private func fetch<Body: Decodable>(_ path: String, holding version: String?) async -> Body? {
        guard case .body(let data) = await answer(path, holding: version) else { return nil }
        return read(Body.self, data)
    }

    private func answer(_ path: String, holding version: String?) async -> Answer {
        guard var request = server.request(path) else {
            problem = "This build has no server address."
            return .failed
        }
        request.cachePolicy = .reloadIgnoringLocalCacheData
        if let version { request.setValue("\"\(version)\"", forHTTPHeaderField: "If-None-Match") }
        switch await server.send(request) {
        case .ok(200, let data):
            return .body(data)
        case .refused(304, _):
            problem = nil
            return .unchanged
        case .refused(404, _):
            problem = nil
            return .notFound
        case .ok(let code, _), .refused(let code, _):
            problem = "Bee Call's server answered \(code)."
            return .failed
        case .signedOut:
            return .failed // the sign-in screen says so
        case .unreachable:
            problem = "Bee Call's server cannot be reached."
            return .failed
        }
    }

    private func read<Body: Decodable>(_ type: Body.Type, _ data: Data) -> Body? {
        do {
            let body = try Contract.decode(type, from: data)
            problem = nil
            return body
        } catch {
            problem = "Bee Call's server sent something this app cannot read."
            return nil
        }
    }
}
