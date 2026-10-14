import Foundation

/// The Bee Call page in the iOS Settings app (Settings.bundle/Root.plist), read as one value.
struct AppSettings: Equatable {
    /// Each switch's value until the person changes it: the DefaultValue entries of Settings.bundle/Root.plist.
    static let defaults: [String: Any] = ["language": "en", "call_time": "21:00", "record_on_server": true,
                                          "record_on_phone": false, "use_bee": false]

    var language: String
    var callTime: String
    var recordOnServer: Bool
    var recordOnPhone: Bool
    var useBee: Bool

    /// Reads the switches; a key the person never set has its default, also before register(defaults:) ran.
    init(_ store: UserDefaults) {
        func text(_ key: String) -> String { store.string(forKey: key) ?? Self.defaults[key] as? String ?? "" }
        func flag(_ key: String) -> Bool {
            store.object(forKey: key) == nil ? Self.defaults[key] as? Bool ?? false : store.bool(forKey: key)
        }
        language = text("language")
        callTime = text("call_time")
        recordOnServer = flag("record_on_server")
        recordOnPhone = flag("record_on_phone")
        useBee = flag("use_bee")
    }

    /// The body of PUT /settings. "Use Bee" is not in it: Bee is connected with POST /bee/connect.
    var serverFields: [String: Any] {
        var fields: [String: Any] = ["language": language, "record_on_server": recordOnServer,
                                     "record_on_phone": recordOnPhone]
        // A typed time the server would refuse is left out, so it does not hold back the other settings.
        let time = callTime.trimmingCharacters(in: .whitespaces)
        if time.wholeMatch(of: /([01]\d|2[0-3]):[0-5]\d/) != nil {
            fields["call_time"] = time
        }
        return fields
    }
}

/// Sends the Settings-app switches to the server (PUT /settings).
final class SettingsSync {
    private let store: UserDefaults
    private let transport: any HTTPTransport
    private let isSignedIn: () -> Bool
    private let noteStatus: (Int) -> Void

    init(store: UserDefaults = .standard, transport: any HTTPTransport = URLSession.shared,
         isSignedIn: @escaping () -> Bool, noteStatus: @escaping (Int) -> Void) {
        self.store = store
        self.transport = transport
        self.isSignedIn = isSignedIn
        self.noteStatus = noteStatus
    }

    func send() async {
        guard isSignedIn(),
              let request = Server.request("/settings", method: "PUT", json: AppSettings(store).serverFields),
              let (_, response) = try? await transport.send(request) else { return }
        noteStatus(response.httpStatus)
    }
}
