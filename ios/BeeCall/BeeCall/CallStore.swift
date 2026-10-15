import Foundation
import Observation

/// The only place that fetches the call list (GET /calls) and the call views (GET /calls/{call_id}), contract
/// sections 1 to 3. It keeps the last body of each with its version and asks again with If-None-Match, so an
/// unchanged answer is a 304 with no body. What /changes announces is fetched only when the app holds that
/// resource; a view is asked again each time it is shown, and everything held each time /changes opens, so nothing
/// held outlives what the server says (a call can end while the stream is closed).
@Observable
final class CallStore {
    private(set) var list: CallList? // the first page
    /// Every call a page gave, by id, the newest word on each: a call a new one pushes off the first page stays,
    /// and none shows twice (second review of the D wiring, finding 6).
    private var known: [String: CallEntry] = [:]
    private(set) var views: [String: CallView] = [:]
    private(set) var missing: Set<String> = [] // views the server has no record for (404), until it does
    private(set) var problem: String? // the last fetch that did not come through, in words
    private var olderLoaded = false // an older page has come
    private var olderCursor: String? // the next older page after the last one loaded
    @ObservationIgnored private var loadingOlder = false // a second tap waits for the page on its way
    @ObservationIgnored private let fetches = OneFetchAtATime<String>() // by path: the list, and each view
    @ObservationIgnored private let server: ServerClient

    init(server: ServerClient = ContractServer.client) {
        self.server = server
    }

    /// The calls, newest first.
    var calls: [CallEntry] {
        known.values.sorted { ($0.call.startedAt.date, $0.call.callID) > ($1.call.startedAt.date, $1.call.callID) }
    }

    /// Older calls exist that are not loaded.
    var hasOlder: Bool {
        olderLoaded ? olderCursor != nil : list?.more == true
    }

    // MARK: - Fetching

    /// Fetches the first page. One fetch at a time: one asked for while another is on its way waits for it and
    /// then asks again, so an older answer never lands after a newer one.
    func refreshList() async {
        await fetches.run("/calls") { await self.fetchList() }
    }

    /// The first page replaces what is held in its span: a held call newer than the page's oldest that the page
    /// does not list is gone, such as a call listed for a moment before the server deleted its record (hung up
    /// before the first words, BC-027). Calls older than the page stay, on their older pages, unless the page is
    /// the whole list.
    private func fetchList() async {
        guard let body: CallList = await fetch("/calls", holding: list?.version) else { return }
        list = body
        let listed = Set(body.calls.map(\.id)), oldest = body.calls.map(\.call.startedAt.date).min()
        known = known.filter { id, entry in
            if listed.contains(id) { return true }
            guard body.more, let oldest else { return false } // the page is the whole list
            return entry.call.startedAt.date <= oldest // on an older page
        }
        for entry in body.calls { known[entry.id] = entry }
    }

    /// Everything held, asked again with its version (a 304 costs nothing): the first page and every view.
    /// /changes announces on connect only the calls live or ended in the last ten minutes, so a view held from a
    /// call that ended while the stream was closed for longer would otherwise keep showing it live (fresh-context
    /// review of round1 7c1361a, finding 1).
    func refreshHeld() async {
        await withTaskGroup(of: Void.self) { group in
            group.addTask { await self.refreshList() }
            for callID in views.keys { group.addTask { await self.refreshView(callID) } }
        }
    }

    func loadOlder() async {
        guard !loadingOlder, let before = olderLoaded ? olderCursor : list?.before else { return }
        loadingOlder = true
        defer { loadingOlder = false }
        var query = URLComponents()
        query.queryItems = [URLQueryItem(name: "before", value: before)]
        guard let page: CallList = await fetch("/calls?" + (query.percentEncodedQuery ?? ""), holding: nil) else {
            return
        }
        for entry in page.calls where known[entry.id] == nil { known[entry.id] = entry } // the first page is newer
        olderCursor = page.more ? page.before : nil
        olderLoaded = true
    }

    /// Fetches a call's view. One fetch per view at a time: one asked for while another is on its way waits for
    /// it and then asks again, so an older answer never lands after a newer one.
    func refreshView(_ callID: String) async {
        await fetches.run(Self.viewPath(callID)) { await self.fetchView(callID) }
    }

    private func fetchView(_ callID: String) async {
        let path = Self.viewPath(callID)
        switch await answer(path, holding: views[callID]?.version) {
        case .body(let data):
            guard let view = read(CallView.self, data) else { return }
            views[callID] = view
            missing.remove(callID)
        case .notFound:
            views[callID] = nil // a record the server deleted (BC-027) is not kept
            missing.insert(callID)
        case .unchanged, .failed:
            break
        }
    }

    /// A call's section is shown (expanded, opened as a sheet, or the call that is on): fetched, or asked again
    /// with the version held, so a view held from before (a call that ended while the app was away) is not shown
    /// as it was (fresh-context review of round1 7c1361a, finding 1).
    func show(_ callID: String) async {
        await refreshView(callID)
    }

    /// GET /changes said the resource at this path now has this version.
    func changed(_ change: Change) async {
        if change.path == "/calls" {
            if list?.version != change.version { await refreshList() }
        } else if change.path.hasPrefix("/calls/") {
            let callID = String(change.path.dropFirst("/calls/".count))
            if let view = views[callID] {
                if view.version != change.version { await refreshView(callID) }
            } else if missing.contains(callID) {
                await refreshView(callID) // a record that has begun since the 404
            } // else not held: fetched when it is shown
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
