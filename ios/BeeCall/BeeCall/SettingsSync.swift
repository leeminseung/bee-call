import Foundation
import UIKit

/// The Bee Call page in the iOS Settings app (Settings.bundle/Root.plist), read as one value.
struct AppSettings: Equatable {
    /// Each switch's value until the person changes it: the DefaultValue entries of Settings.bundle/Root.plist.
    static let defaults: [String: Any] = ["language": "en", "call_model": "haiku", "call_time": "21:00",
                                          "record_on_server": true, "record_on_phone": false, "use_bee": false]

    var language: String
    var callModel: String // "haiku" (faster) or "sonnet" (smarter): the AI's model for the person's calls
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
        callModel = text("call_model")
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
    let callModel: String
    let callTime: String? // nil when the typed time is not one the server accepts
    let recordOnServer: Bool
    let recordOnPhone: Bool
    let timeZone: String // an IANA name such as Asia/Seoul; the server works out "the user's time" with it

    init(_ settings: AppSettings, timeZone: TimeZone) {
        language = settings.language
        callModel = settings.callModel
        let time = settings.callTime.trimmingCharacters(in: .whitespaces)
        // A typed time the server would refuse is left out, so it does not hold back the other settings.
        callTime = time.wholeMatch(of: /([01]\d|2[0-3]):[0-5]\d/) != nil ? time : nil
        recordOnServer = settings.recordOnServer
        recordOnPhone = settings.recordOnPhone
        self.timeZone = timeZone.identifier
    }

    /// Why the typed evening time was left out, for the person: the Settings app keeps showing it.
    static func callTimeProblem(_ settings: AppSettings, timeZone: TimeZone) -> String? {
        guard SettingsBody(settings, timeZone: timeZone).callTime == nil else { return nil }
        let typed = settings.callTime.trimmingCharacters(in: .whitespaces)
        return "The evening call time “\(typed)” was not saved: write it like 21:00 in Settings."
    }

    var json: [String: Any] {
        var fields: [String: Any] = ["language": language, "call_model": callModel, "record_on_server": recordOnServer,
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
@Observable
final class SettingsSync {
    /// How long an answered call waits for its settings to reach the server before it joins anyway.
    static let answerWait: Duration = .seconds(2)

    /// What did not reach the server, for the call screen: the server's reason when it refused the settings,
    /// or a typed evening time it would refuse (left out of every send).
    private(set) var problem: String?
    @ObservationIgnored private var refusal: String? // the server's reason for refusing the last send

    @ObservationIgnored private let store: UserDefaults
    @ObservationIgnored private let server: ServerClient
    @ObservationIgnored private let tokenReadable: () -> Bool
    @ObservationIgnored private let timeZone: () -> TimeZone
    @ObservationIgnored private var observers: [any NSObjectProtocol] = []
    /// The body of the last send asked for, or nil once a send failed: a change notice that finds the same body
    /// has nothing new to send.
    @ObservationIgnored private var lastBody: SettingsBody?
    /// The last send in line. Sends go out one at a time, in order, so an older body never lands after a newer one.
    @ObservationIgnored private var tail: Task<Void, Never>?

    init(store: UserDefaults = .standard, server: ServerClient = ServerClient(), tokenReadable: @escaping () -> Bool,
         timeZone: @escaping () -> TimeZone = { .current }) {
        self.store = store
        self.server = server
        self.tokenReadable = tokenReadable
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
        showProblem() // the typed time may have changed without changing what is sent
        guard currentBody() != lastBody else { return }
        send()
    }

    private func showProblem() {
        problem = refusal ?? SettingsBody.callTimeProblem(AppSettings(store), timeZone: timeZone())
    }

    private func currentBody() -> SettingsBody {
        SettingsBody(AppSettings(store), timeZone: timeZone())
    }

    private func put() async {
        guard tokenReadable(), let request = server.request("/settings", method: "PUT", json: currentBody().json)
        else {
            lastBody = nil
            return
        }
        switch await server.send(request) {
        case .ok:
            refusal = nil
        case .refused(400, let data):
            lastBody = nil
            let reason = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            refusal = "Your settings were not saved: \(reason.hasSuffix(".") ? String(reason.dropLast()) : reason)."
        case .refused, .signedOut, .unreachable:
            lastBody = nil // the next change notice or launch sends again
        }
        showProblem()
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
