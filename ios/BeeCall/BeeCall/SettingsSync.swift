import Foundation
import UIKit

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
}

/// What PUT /settings carries: the switches the server keeps, and the phone's time zone. "Use Bee" is not in
/// it: Bee is connected with POST /bee/connect (BeeSwitch.swift).
struct SettingsBody: Equatable {
    let language: String
    let callTime: String? // nil when the typed time is not one the server accepts
    let recordOnServer: Bool
    let recordOnPhone: Bool
    let timeZone: String // an IANA name such as Asia/Seoul; the server works out "the user's time" with it

    init(_ settings: AppSettings, timeZone: TimeZone) {
        language = settings.language
        let time = settings.callTime.trimmingCharacters(in: .whitespaces)
        // A typed time the server would refuse is left out, so it does not hold back the other settings.
        callTime = time.wholeMatch(of: /([01]\d|2[0-3]):[0-5]\d/) != nil ? time : nil
        recordOnServer = settings.recordOnServer
        recordOnPhone = settings.recordOnPhone
        self.timeZone = timeZone.identifier
    }

    var json: [String: Any] {
        var fields: [String: Any] = ["language": language, "record_on_server": recordOnServer,
                                     "record_on_phone": recordOnPhone, "time_zone": timeZone]
        fields["call_time"] = callTime
        return fields
    }
}

/// Sends the Settings-app switches to the server (PUT /settings), so the server never acts on stale ones
/// (contract section 1). They go out:
/// - at every launch, also one for a VoIP push or a notification button (`send()` from the app delegate), and
///   when the sign-in token becomes readable after a launch before the phone's first unlock;
/// - when the Settings app changes a switch (UserDefaults.didChangeNotification);
/// - after signing in, and when the app comes to the foreground;
/// - before an answered AI call joins its room, waiting at most `answerWait` (`sendBeforeAnswering()`), because
///   the voice server reads "Save on the server" when the phone joins.
final class SettingsSync {
    /// How long an answered call waits for its settings to reach the server before it joins anyway.
    static let answerWait: Duration = .seconds(2)

    private let store: UserDefaults
    private let transport: any HTTPTransport
    private let tokenReadable: () -> Bool
    private let noteStatus: (Int) -> Void
    private let timeZone: () -> TimeZone
    private var observers: [any NSObjectProtocol] = []
    /// The body of the last send asked for, or nil once a send failed: a change notice that finds the same body
    /// has nothing new to send.
    private var lastBody: SettingsBody?
    /// The last send in line. Sends go out one at a time, in order, so an older body never lands after a newer one.
    private var tail: Task<Void, Never>?

    init(store: UserDefaults = .standard, transport: any HTTPTransport = URLSession.shared,
         tokenReadable: @escaping () -> Bool, noteStatus: @escaping (Int) -> Void,
         timeZone: @escaping () -> TimeZone = { .current }) {
        self.store = store
        self.transport = transport
        self.tokenReadable = tokenReadable
        self.noteStatus = noteStatus
        self.timeZone = timeZone
        let center = NotificationCenter.default
        // Any defaults change posts this, the app's own too; the body comparison sorts them out.
        observers.append(center.addObserver(forName: UserDefaults.didChangeNotification, object: nil,
                                            queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.settingsMayHaveChanged() }
        })
        observers.append(center.addObserver(forName: UIApplication.protectedDataDidBecomeAvailableNotification,
                                            object: nil, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { _ = self?.send() }
        })
    }

    isolated deinit {
        observers.forEach(NotificationCenter.default.removeObserver)
    }

    /// Sends the switches as they are when the send goes out, after any send still on its way.
    @discardableResult
    func send() -> Task<Void, Never> {
        lastBody = currentBody()
        let previous = tail
        let next = Task {
            await previous?.value
            await put()
        }
        tail = next
        return next
    }

    /// Before an answered call joins: sends, and waits for the server's answer, but at most `limit`, so a slow
    /// network does not hold the call. The send carries on after that.
    func sendBeforeAnswering(within limit: Duration = SettingsSync.answerWait) async {
        let sending = send()
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            let first = FirstToFinish(continuation)
            let timer = Task {
                try? await Task.sleep(for: limit)
                first.finish()
            }
            Task {
                await sending.value
                timer.cancel()
                first.finish()
            }
        }
    }

    private func settingsMayHaveChanged() {
        guard currentBody() != lastBody else { return }
        send()
    }

    private func currentBody() -> SettingsBody {
        SettingsBody(AppSettings(store), timeZone: timeZone())
    }

    private func put() async {
        guard tokenReadable(),
              let request = Server.request("/settings", method: "PUT", json: currentBody().json) else {
            lastBody = nil
            return
        }
        guard let (_, response) = try? await transport.send(request) else {
            lastBody = nil // unreachable: the next change notice or launch sends again
            return
        }
        if !(200..<300).contains(response.httpStatus) { lastBody = nil }
        noteStatus(response.httpStatus)
    }
}

/// Resumes a continuation once, for whichever of several tasks finishes first.
private final class FirstToFinish {
    private var continuation: CheckedContinuation<Void, Never>?

    init(_ continuation: CheckedContinuation<Void, Never>) {
        self.continuation = continuation
    }

    func finish() {
        continuation?.resume()
        continuation = nil
    }
}
