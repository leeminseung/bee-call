import AVFoundation
import CallKit
import LiveKit
import Observation
import PushKit
import UIKit

/// Runs each call through CallKit, so iOS treats it as a phone call:
/// call-priority audio, AirPods and lock-screen controls, the green call indicator.
@Observable
final class CallManager: NSObject {
    private(set) var status = "Ready" // what the call is doing
    private(set) var isReconnecting = false // LiveKit is getting a dropped connection back
    /// The line the call screen shows: "Reconnecting…" while LiveKit reconnects, then the call's status again.
    var statusLine: String { isReconnecting ? "Reconnecting…" : status }
    private(set) var ringStatus = "" // whether the app can hear the AI's calls; empty while not listening
    private(set) var isInCall = false
    private(set) var isRinging = false // the AI's call, until it is answered or ends
    private(set) var isEnding = false
    let liveKit: LiveKitClient // the call screen shows its captions and voice levels

    /// The call's room: `liveKit` in the app, a stand-in in tests.
    @ObservationIgnored private let room: any CallRoom
    /// Before an answered call joins: the Settings-app switches go to the server (contract section 1).
    @ObservationIgnored private let beforeAnswering: () async -> Void

    @ObservationIgnored private let callKit: any CallKitReports
    @ObservationIgnored private let calls: any CallKitRequests
    @ObservationIgnored private let microphone: () async -> Bool
    @ObservationIgnored private var callID: UUID?
    @ObservationIgnored private var tokenServerURL = ""
    @ObservationIgnored private var earlierCall: String? // the record id of the call a new call continues
    @ObservationIgnored private var connecting: Task<Void, Never>?
    // Room → CallKit call, for every AI call rung. The ring stream replays the last minute on each reconnect.
    @ObservationIgnored private var rungRooms: [String: UUID] = [:]
    @ObservationIgnored private var ringTimeout: Task<Void, Never>?
    // VoIP pushes ring the AI's calls while the app is closed or the phone is locked.
    @ObservationIgnored private let pushRegistry = PKPushRegistry(queue: .main)
    // Each call waits for the AI: a call the user starts rings until it picks up (it reads Bee first, at most
    // about 8 s), and an answered call until it is there.
    @ObservationIgnored private var pickUpTimeout: Task<Void, Never>?
    @ObservationIgnored private var pickedUp = false
    @ObservationIgnored private let pickUpWait: Duration
    @ObservationIgnored private let decline = CallDecline(transport: URLSession.shared,
                                                          noteStatus: { Server.shared.noteStatus($0) })
    /// The name of every call, both ways, on CallKit's call screen and in the Phone app's Recents.
    private static let handle = CXHandle(type: .generic, value: "Bee Call")

    /// CallKit's provider and controller, the microphone prompt, the LiveKit room and the settings sent before an
    /// answer, unless a test passes stand-ins. `pickUpWait`: how long a call waits for the AI to pick up; the
    /// voice server picks up within about 8 s, so this catches a stuck one.
    init(callKit: (any CallKitReports)? = nil, calls: (any CallKitRequests)? = nil,
         microphone: @escaping () async -> Bool = { await LiveKitSDK.ensureDeviceAccess(for: [.audio]) },
         room: (any CallRoom)? = nil,
         beforeAnswering: @escaping () async -> Void = { await Server.shared.settings.sendBeforeAnswering() },
         pickUpWait: Duration = .seconds(30)) {
        let provider: CXProvider? = callKit == nil ? Self.provider() : nil
        self.callKit = callKit ?? provider!
        self.calls = calls ?? CXCallController()
        self.microphone = microphone
        let liveKit = LiveKitClient()
        self.liveKit = liveKit
        self.room = room ?? liveKit
        self.beforeAnswering = beforeAnswering
        self.pickUpWait = pickUpWait
        super.init()
        provider?.setDelegate(self, queue: nil) // nil = main queue

        // CallKit is the only owner of the audio session. The SDK must not configure the session,
        // and its audio engine may run only between didActivate and didDeactivate below
        // (LiveKit Swift SDK README, "Integration with CallKit").
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = false
        try? AudioManager.shared.setEngineAvailability(.none)

        self.room.onLinkChange = { [weak self] link in
            self?.linkChanged(link)
        }
        pushRegistry.delegate = self
        pushRegistry.desiredPushTypes = [.voIP]
    }

    private static func provider() -> CXProvider {
        let configuration = CXProviderConfiguration()
        configuration.supportsVideo = false
        configuration.maximumCallGroups = 1
        configuration.maximumCallsPerCallGroup = 1
        configuration.supportedHandleTypes = [.generic]
        return CXProvider(configuration: configuration)
    }

    /// Asks for the microphone while the app is on screen. A call rung by a VoIP push cannot show the prompt.
    func askForMicrophone() async {
        #if DEBUG
        guard VoiceDemo.requested() == nil else { return } // a pretend call needs no microphone
        #endif
        _ = await microphone()
    }

    /// Calls Bee Call. With `continuing`, the new call picks up from that earlier call (its record id).
    func startCall(tokenServerURL: String, continuing earlierCall: String? = nil) async {
        guard !isInCall else { return }
        isInCall = true
        self.earlierCall = earlierCall
        status = earlierCall == nil ? "Calling…" : "Calling to continue…"
        // Ask now, while the app is on screen. Inside the CallKit audio window the SDK
        // cannot show the prompt, and turning the engine on fails without permission.
        guard await microphone() else {
            finish("Microphone access is off. Turn it on in Settings.")
            return
        }
        let id = UUID()
        callID = id
        self.tokenServerURL = tokenServerURL
        let start = CXStartCallAction(call: id, handle: Self.handle)
        do {
            try await calls.request(CXTransaction(action: start))
        } catch {
            finish("Could not start the call: \(error.localizedDescription)")
        }
    }

    func endCall() async {
        guard let id = callID, !isEnding else { return }
        isEnding = true
        do {
            try await calls.request(CXTransaction(action: CXEndCallAction(call: id)))
        } catch {
            // CallKit no longer knows this call, so clean up here.
            connecting?.cancel()
            await room.disconnect()
            finish("Call ended")
        }
    }

    // MARK: - The AI's calls

    /// Keeps the token server's ring stream open until this task is cancelled (the app left the
    /// foreground, or the address changed), reconnecting after a drop.
    func listenForRings(tokenServerURL: String) async {
        ringStatus = ""
        guard let url = RingStream.url(tokenServerURL: tokenServerURL) else { return }
        try? await Task.sleep(for: .milliseconds(500)) // the address may still be being typed
        var delay = 1
        while !Task.isCancelled {
            var problem = "The ring stream closed."
            do {
                try await RingStream.read(from: url, onOpen: {
                    ringStatus = "Waiting for calls"
                    delay = 1
                    Task { await PushTokens.shared.deliver(.voip) } // the address may be new
                }, onRing: {
                    ringPhone(for: $0)
                })
            } catch let error as RingStream.BadStatus {
                Server.shared.noteStatus(error.code) // 401: the sign-in ended
                problem = "The token server answered \(error.code) to the ring stream."
            } catch {
                problem = "Can't reach the token server for calls."
            }
            guard !Task.isCancelled else { return }
            ringStatus = "\(problem) Retrying…"
            try? await Task.sleep(for: .seconds(delay))
            delay = min(delay * 2, 10)
        }
    }

    /// The ring stream's ring: rings the phone through CallKit, if the one gate lets it.
    func ringPhone(for ring: Ring) {
        guard let id = beginRinging(ring) else { return }
        Task { await reportIncoming(id, ring) }
    }

    /// The one way into ringing, for the ring stream and a VoIP push alike: a ring that has not expired, for a
    /// room that has not rung yet, while no other call is on and the app knows its server. Returns the new
    /// call's id, or nil when the ring must not ring.
    private func beginRinging(_ ring: Ring) -> UUID? {
        guard Self.answerBy(ring) > .now, rungRooms[ring.room] == nil, !isInCall, !Server.tokenURL.isEmpty else {
            return nil
        }
        let id = UUID()
        rungRooms[ring.room] = id
        callID = id
        tokenServerURL = Server.tokenURL
        isInCall = true
        isRinging = true
        status = ring.topic.map { "Bee Call is calling about: \($0)" } ?? "Bee Call is calling…"
        return id
    }

    /// What CallKit shows for the AI's call: Bee Call, and what it is about.
    private func callUpdate(topic: String?) -> CXCallUpdate {
        let update = CXCallUpdate()
        update.remoteHandle = Self.handle
        update.localizedCallerName = callerName(topic: topic)
        update.hasVideo = false
        return update
    }

    private func reportIncoming(_ id: UUID, _ ring: Ring) async {
        // Same reason as in startCall: once CallKit owns the audio, the prompt cannot appear.
        let micAllowed = await microphone()
        guard callID == id else { return } // ended while we asked
        guard micAllowed else {
            finish("Microphone access is off. Turn it on in Settings.")
            return
        }
        guard Self.answerBy(ring) > .now else {
            finish("Missed a call from Bee Call")
            return
        }
        let update = callUpdate(topic: ring.topic)
        do {
            try await withCheckedThrowingContinuation { (reported: CheckedContinuation<Void, Error>) in
                callKit.reportIncoming(id, update) { error in
                    if let error { reported.resume(throwing: error) } else { reported.resume() }
                }
            }
        } catch {
            guard callID == id else { return }
            if (error as? CXErrorCodeIncomingCallError)?.code == .callUUIDAlreadyExists {
                stopRingingAtExpiry(id, ring) // a VoIP push for this call rang it while we asked for the mic
                return
            }
            finish(Self.ringFailure(error))
            return
        }
        guard callID == id else {
            callKit.reportEnded(id, reason: .remoteEnded) // ended while CallKit set it up
            return
        }
        stopRingingAtExpiry(id, ring)
    }

    /// What to show when iOS would not ring the AI's call.
    nonisolated private static func ringFailure(_ error: Error) -> String {
        switch (error as? CXErrorCodeIncomingCallError)?.code {
        case .filteredByDoNotDisturb: "Missed a call from Bee Call: Do Not Disturb or a Focus is on"
        case .filteredByBlockList: "Missed a call from Bee Call: it is blocked on this phone"
        default: "Could not ring: \(error.localizedDescription)"
        }
    }

    private func stopRingingAtExpiry(_ id: UUID, _ ring: Ring) {
        ringTimeout = Task {
            try? await Task.sleep(for: .seconds(Self.answerBy(ring).timeIntervalSinceNow))
            guard !Task.isCancelled, callID == id, isRinging else { return }
            hangUp(.unanswered, "Missed a call from Bee Call")
        }
    }

    // MARK: - VoIP push

    /// iOS ends an app that does not report a call to CallKit for every VoIP push, and may stop its pushes, so
    /// this reports one at once, before anything else: also for a ring that expired, rang already, or cannot ring.
    func reportPushedCall(_ ring: Ring?, completion: @escaping () -> Void) {
        let done = Handed(completion)
        let update = callUpdate(topic: ring?.topic)
        if let ring, let rung = rungRooms[ring.room], callID == rung {
            // The ring stream rang this call (or is about to): reporting it again changes nothing on screen,
            // and if this report wins the race the stream's own report gets callUUIDAlreadyExists.
            callKit.reportIncoming(rung, update) { _ in done.call() }
            return
        }
        guard let ring, let id = beginRinging(ring) else {
            // Unreadable, already rung and over, expired, during another call, or no token server set.
            let id = UUID()
            callKit.reportIncoming(id, update) { [weak self] _ in
                Task { @MainActor in self?.callKit.reportEnded(id, reason: .unanswered) }
                done.call()
            }
            return
        }
        callKit.reportIncoming(id, update) { [weak self] error in
            let failure = error.map(Self.ringFailure)
            Task { @MainActor in
                guard let self, self.callID == id else { return }
                if let failure {
                    self.finish(failure)
                } else {
                    self.stopRingingAtExpiry(id, ring)
                }
            }
            done.call()
        }
    }

    /// Stops ringing a little before expires_at, when the voice server gives up and closes the room.
    /// Answering still needs the settings (SettingsSync.answerWait at most), a pass and a LiveKit join (about
    /// 5 s), and a join that lands after the room closed would open a new, empty room with no AI in it.
    static func answerBy(_ ring: Ring) -> Date {
        ring.expiresAt.addingTimeInterval(-5 - TimeInterval(SettingsSync.answerWait.components.seconds))
    }

    /// CallKit shows the caller on one line, so a long topic is cut at a word.
    private func callerName(topic: String?) -> String {
        guard let topic else { return "Bee Call" }
        guard topic.count > 30 else { return "Bee Call · \(topic)" }
        let cut = topic.prefix(30)
        return "Bee Call · \(cut[..<(cut.lastIndex(of: " ") ?? cut.endIndex)])…"
    }

    // MARK: - Both directions

    /// Gets a pass and joins the room: a new room for a call the user starts,
    /// or with `answering`, the room of the AI's call.
    private func join(_ id: UUID, answering roomName: String?) {
        let url = tokenServerURL
        pickedUp = false
        room.onAgentAnswered = { [weak self] in self?.agentPickedUp(id) }
        connecting = Task {
            do {
                if roomName != nil {
                    // The voice server reads "Save on the server" when the phone joins, so the switches go first;
                    // a slow network holds the call at most SettingsSync.answerWait (contract section 1).
                    await beforeAnswering()
                    try Task.checkCancellation() // the call ended while the settings went out
                }
                _ = try await room.connect(tokenServerURL: url, answering: roomName,
                                           continuing: roomName == nil ? earlierCall : nil)
                try Task.checkCancellation() // the call ended while we were connecting
                // Both ways, the call screen says "Connected" once the AI is there (agentPickedUp), which may
                // already be so. Until then a call the person starts rings, and an answered call waits: a join
                // that lands after the AI gave up opens an empty room, which would sit "Connected" in silence
                // (review finding 14). If the AI never comes, the call is given up.
                guard !pickedUp else { return }
                if roomName == nil { status = "Ringing…" }
                pickUpTimeout = Task {
                    try? await Task.sleep(for: pickUpWait)
                    guard !Task.isCancelled, callID == id, !pickedUp else { return }
                    if roomName == nil {
                        hangUp(.failed, "Bee Call didn't pick up")
                    } else {
                        hangUp(.remoteEnded, "That call already ended")
                    }
                }
            } catch {
                guard !Task.isCancelled else { return } // whoever ended the call cleans up
                if error is LiveKitClient.CallAlreadyEnded {
                    hangUp(.remoteEnded, "That call already ended")
                } else {
                    hangUp(.failed, "Call failed: \(error.localizedDescription)")
                }
            }
        }
    }

    /// The voice server marked the call answered: the AI is there. A call the person started is now connected
    /// for CallKit too; CallKit counted the AI's own call as connected when it was answered.
    private func agentPickedUp(_ id: UUID) {
        guard callID == id, !pickedUp, !isEnding else { return }
        pickedUp = true
        pickUpTimeout?.cancel()
        pickUpTimeout = nil
        if !rungRooms.values.contains(id) { callKit.reportConnected(id) }
        status = "Connected"
    }

    private func linkChanged(_ link: CallLink) {
        switch link {
        case .reconnecting:
            isReconnecting = true
        case .reconnected:
            isReconnecting = false
        case .lost(let error):
            isReconnecting = false
            let ending = CallEnding.afterDisconnect(error)
            hangUp(ending.reason, ending.message)
        }
    }

    /// Ends the call from our side. CallKit sends no end action for these, so we clean up here.
    private func hangUp(_ reason: CXCallEndedReason, _ message: String) {
        guard let id = callID, !isEnding else { return }
        isEnding = true
        callKit.reportEnded(id, reason: reason)
        connecting?.cancel()
        Task {
            await room.disconnect()
            finish(message)
        }
    }

    private func finish(_ message: String) {
        connecting?.cancel() // a connect still in flight must not touch the next call
        connecting = nil
        ringTimeout?.cancel()
        ringTimeout = nil
        pickUpTimeout?.cancel()
        pickUpTimeout = nil
        room.onAgentAnswered = nil
        pickedUp = false
        callID = nil
        earlierCall = nil
        isInCall = false
        isRinging = false
        isEnding = false
        isReconnecting = false
        status = message
    }
}

/// PushKit's completion handler, passed into CallKit's callback; calling it from any queue is fine. Nonisolated
/// against the project's main-actor default, which would make reading `call` from CallKit's queue an error in a
/// stricter Swift (review finding 9).
nonisolated private struct Handed: @unchecked Sendable {
    let call: () -> Void
    init(_ call: @escaping () -> Void) { self.call = call }
}

extension CallManager: PKPushRegistryDelegate {
    func pushRegistry(_ registry: PKPushRegistry, didUpdate pushCredentials: PKPushCredentials, for type: PKPushType) {
        let token = pushCredentials.token
        Task { await PushTokens.shared.received(token, kind: .voip) }
    }

    func pushRegistry(_ registry: PKPushRegistry, didInvalidatePushTokenFor type: PKPushType) {
        PushTokens.shared.invalidated(.voip)
    }

    func pushRegistry(_ registry: PKPushRegistry, didReceiveIncomingPushWith payload: PKPushPayload,
                      for type: PKPushType, completion: @escaping () -> Void) {
        reportPushedCall(RingStream.ring(fromPush: payload.dictionaryPayload), completion: completion)
    }
}

extension CallManager: CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) {
        try? AudioManager.shared.setEngineAvailability(.none)
        connecting?.cancel()
        Task {
            await room.disconnect()
            finish("iOS reset the call")
        }
    }

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) { perform(action) }
    func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) { perform(action) }
    func provider(_ provider: CXProvider, perform action: CXEndCallAction) { perform(action) }
    func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) { perform(action) }

    // What CallKit asks of a call, apart from the provider that asks it, so tests can ask it too.

    func perform(_ action: CXStartCallAction) {
        action.fulfill() // CallKit activates the audio session after this.
        callKit.reportStartedConnecting(action.callUUID)
        join(action.callUUID, answering: nil)
    }

    func perform(_ action: CXAnswerCallAction) {
        guard isRinging, action.callUUID == callID,
              let room = rungRooms.first(where: { $0.value == action.callUUID })?.key else {
            action.fail()
            return
        }
        isRinging = false
        ringTimeout?.cancel() // answered in time; if the AI gave up anyway, the token server says 410
        ringTimeout = nil
        action.fulfill() // CallKit activates the audio session after this.
        status = "Answering…"
        join(action.callUUID, answering: room)
    }

    func perform(_ action: CXEndCallAction) {
        // Also reached when iOS ends the call (lock screen, AirPods, Dynamic Island).
        let declined = isRinging // the AI's call, ended before it was answered
        if declined, let room = rungRooms.first(where: { $0.value == action.callUUID })?.key {
            // Not awaited: CallKit's end goes on at once. Declined from the lock screen, the app runs only for the
            // call, so it asks iOS for the time to finish the request after the call is gone.
            let awake = UIApplication.shared.beginBackgroundTask(withName: "decline")
            Task {
                await decline.send(room: room)
                UIApplication.shared.endBackgroundTask(awake)
            }
        }
        isEnding = true
        connecting?.cancel()
        Task {
            await room.disconnect()
            action.fulfill()
            finish(declined ? "Declined" : "Call ended")
        }
    }

    func perform(_ action: CXSetMutedCallAction) {
        Task {
            do {
                try await room.setMicrophone(enabled: !action.isMuted)
                action.fulfill()
            } catch {
                action.fail()
            }
        }
    }

    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        do {
            // LiveKit's CallKit snippet also passes .mixWithOthers. SDK 2.17.0 dropped that option
            // from its own call setup because it causes echo, so it is left out here.
            // .allowBluetoothHFP lets AirPods carry the microphone as well as the AI's voice.
            try audioSession.setCategory(.playAndRecord, mode: .voiceChat, options: [.allowBluetoothHFP])
            try AudioManager.shared.setEngineAvailability(.default)
        } catch {
            hangUp(.failed, "Audio could not start: \(error.localizedDescription)")
        }
    }

    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        try? AudioManager.shared.setEngineAvailability(.none)
    }
}

#if DEBUG
extension CallManager {
    /// Debug builds only (VoiceDemo.swift): a pretend call, so the call screen and its voice indicator can be
    /// seen without a server. Nothing goes to CallKit, no room is joined, nothing is recorded.
    func playVoiceDemo(_ demo: VoiceDemo) {
        isInCall = true
        status = "Connected"
        liveKit.playVoiceDemo(demo)
    }
}
#endif
