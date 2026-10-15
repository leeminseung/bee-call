import Foundation

/// The way to design D's endpoints (contract/app-contract.md). Until the token server serves them, a Debug build
/// answers them from the contract's own example files (ContractExamples), so the screens are fed through the same
/// stores and decoding as they will be by the network; launched with `-realServer YES`, it asks the server. A
/// Release build always asks the server. Moving Debug builds to the network is this one switch.
enum ContractServer {
    static let client: ServerClient = {
        #if DEBUG
        if !UserDefaults.standard.bool(forKey: "realServer") { return ServerClient(transport: ContractExamples()) }
        #endif
        return ServerClient()
    }()
}

#if DEBUG
/// Debug builds only: answers D's requests with the files in contract/examples, which the Debug build bundles
/// (the Release build leaves them out). It holds no content of its own. The person's settings are me.json, kept in
/// memory so a language or voice picked shows, with the contract's rule that a new language without a voice goes
/// back to that language's default. Paths without an example answer 404, as the server does for what it lacks.
final class ContractExamples: HTTPTransport {
    private let bundle: Bundle
    private let live: Bool // the MacBook call while it is on (call-view-live.json) instead of after (call-view.json)
    private var me: [String: Any]?

    init(bundle: Bundle = .main, live: Bool = false) {
        self.bundle = bundle
        self.live = live
    }

    func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        let url = request.url ?? URL(fileURLWithPath: "/")
        let method = request.httpMethod ?? "GET"
        let path = url.path
        let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
        let reply: (Int, Data)
        switch (method, path) {
        case ("GET", "/calls"):
            reply = query.contains { $0.name == "before" } ? (400, Data("The examples have one page.".utf8))
                : versioned(example("calls"), for: request)
        case ("GET", _) where path.hasPrefix("/calls/") && path.split(separator: "/").count == 2:
            reply = view(String(path.dropFirst("/calls/".count))).map { versioned($0, for: request) } ?? (404, Data())
        case ("GET", "/offers"):
            reply = versioned(example("offers"), for: request)
        case ("POST", _) where path.hasPrefix("/offers/"):
            reply = answer(offer: String(path.dropFirst("/offers/".count)))
        case ("GET", "/voices"):
            let voices = example("voices")
            let language = query.first { $0.name == "language" }?.value
            reply = object(voices)?["language"] as? String == language ? (200, voices) : (404, Data())
        case ("GET", "/me"):
            reply = (200, (try? JSONSerialization.data(withJSONObject: person())) ?? Data())
        case ("PUT", "/settings"):
            reply = change(settings: request.httpBody)
        default:
            reply = (404, Data())
        }
        let response = HTTPURLResponse(url: url, statusCode: reply.0, httpVersion: "HTTP/1.1",
                                       headerFields: ["Content-Type": "application/json"])!
        return (reply.1, response)
    }

    // MARK: - The files

    private func example(_ name: String) -> Data {
        bundle.url(forResource: name, withExtension: "json").flatMap { try? Data(contentsOf: $0) } ?? Data()
    }

    private func object(_ data: Data) -> [String: Any]? {
        (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    /// The view of a call: the example whose call has this id, the live one only when asked for.
    private func view(_ callID: String) -> Data? {
        let names = ["call-view", "call-view-live", "call-view-ai"]
        let views = names.map { example($0) }.filter { data in
            let call = object(data)?["call"] as? [String: Any]
            return call?["call_id"] as? String == callID
        }
        return views.first { data in
            let state = (object(data)?["call"] as? [String: Any])?["state"] as? String
            return (state == "live") == live
        } ?? views.first
    }

    /// 304 when the request already holds this body's version (If-None-Match), as the server answers.
    private func versioned(_ body: Data, for request: URLRequest) -> (Int, Data) {
        guard let version = object(body)?["version"] as? String,
              request.value(forHTTPHeaderField: "If-None-Match") == "\"\(version)\"" else { return (200, body) }
        return (304, Data())
    }

    private func answer(offer id: String) -> (Int, Data) {
        let body = example("offer-answer")
        let offer = object(body)?["offer"] as? [String: Any]
        return offer?["id"] as? String == id ? (200, body) : (404, Data())
    }

    // MARK: - The person's settings

    private func person() -> [String: Any] {
        if let me { return me }
        let loaded = object(example("me")) ?? [:]
        me = loaded
        return loaded
    }

    private func change(settings body: Data?) -> (Int, Data) {
        var person = person()
        guard var settings = person["settings"] as? [String: Any],
              let changes = body.flatMap({ object($0) }) else { return (400, Data("Not a settings object.".utf8)) }
        if let language = changes["language"] as? String, language != settings["language"] as? String {
            settings["language"] = language
            // A new language without a voice in the same request goes to that language's default.
            if changes["voice"] == nil, (settings["voice"] as? String)?.hasPrefix(language + "-") != true {
                settings["voice"] = NSNull()
            }
        }
        if let voice = changes["voice"] { settings["voice"] = voice }
        person["settings"] = settings
        me = person
        let answer = (try? JSONSerialization.data(withJSONObject: ["settings": settings])) ?? Data()
        return (200, answer)
    }
}
#endif
