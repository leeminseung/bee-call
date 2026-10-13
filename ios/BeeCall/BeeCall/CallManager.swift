import AVFoundation
import CallKit
import LiveKit
import Observation

/// Runs each call through CallKit, so iOS treats it as a phone call:
/// call-priority audio, AirPods and lock-screen controls, the green call indicator.
@Observable
final class CallManager: NSObject {
    private(set) var status = "Ready"
    private(set) var ringStatus = "" // whether the app can hear the AI's calls; empty while not listening
    private(set) var isInCall = false
    private(set) var isRinging = false // the AI's call, until it is answered or ends
    private(set) var isEnding = false
    let liveKit = LiveKitClient()

    @ObservationIgnored private let provider: CXProvider
    @ObservationIgnored private let controller = CXCallController()
    @ObservationIgnored private var callID: UUID?
    @ObservationIgnored private var tokenServerURL = ""
    @ObservationIgnored private var connecting: Task<Void, Never>?
    // Room → CallKit call, for every AI call rung. The ring stream replays the last minute on each reconnect.
    @ObservationIgnored private var rungRooms: [String: UUID] = [:]
    @ObservationIgnored private var ringTimeout: Task<Void, Never>?

    override init() {
        let configuration = CXProviderConfiguration()
        configuration.supportsVideo = false
        configuration.maximumCallGroups = 1
        configuration.maximumCallsPerCallGroup = 1
        configuration.supportedHandleTypes = [.generic]
        provider = CXProvider(configuration: configuration)
        super.init()
        provider.setDelegate(self, queue: nil) // nil = main queue

        // CallKit is the only owner of the audio session. The SDK must not configure the session,
        // and its audio engine may run only between didActivate and didDeactivate below
        // (LiveKit Swift SDK README, "Integration with CallKit").
        AudioManager.shared.audioSession.isAutomaticConfigurationEnabled = false
        try? AudioManager.shared.setEngineAvailability(.none)

        liveKit.onUnexpectedDisconnect = { [weak self] in
            self?.hangUp(.remoteEnded, "The call dropped")
        }
    }

    func startCall(tokenServerURL: String) async {
        guard !isInCall else { return }
        isInCall = true
        status = "Calling…"
        // Ask now, while the app is on screen. Inside the CallKit audio window the SDK
        // cannot show the prompt, and turning the engine on fails without permission.
        guard await LiveKitSDK.ensureDeviceAccess(for: [.audio]) else {
            finish("Microphone access is off. Turn it on in Settings.")
            return
        }
        let id = UUID()
        callID = id
        self.tokenServerURL = tokenServerURL
        let start = CXStartCallAction(call: id, handle: CXHandle(type: .generic, value: "AI"))
        do {
            try await controller.request(CXTransaction(action: start))
        } catch {
            finish("Could not start the call: \(error.localizedDescription)")
        }
    }

    func endCall() async {
        guard let id = callID, !isEnding else { return }
        isEnding = true
        do {
            try await controller.request(CXTransaction(action: CXEndCallAction(call: id)))
        } catch {
            // CallKit no longer knows this call, so clean up here.
            connecting?.cancel()
            await liveKit.disconnect()
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
                }, onRing: {
                    ringPhone(for: $0, tokenServerURL: tokenServerURL)
                })
            } catch let error as RingStream.BadStatus {
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

    /// Rings the phone through CallKit for the AI's call, unless the ring has expired,
    /// this room rang already, or another call is on.
    private func ringPhone(for ring: Ring, tokenServerURL: String) {
        guard answerBy(ring) > .now, rungRooms[ring.room] == nil, !isInCall else { return }
        let id = UUID()
        rungRooms[ring.room] = id
        callID = id
        self.tokenServerURL = tokenServerURL
        isInCall = true
        isRinging = true
        status = ring.topic.map { "Bee Call is calling about: \($0)" } ?? "Bee Call is calling…"
        Task { await reportIncoming(id, ring) }
    }

    private func reportIncoming(_ id: UUID, _ ring: Ring) async {
        // Same reason as in startCall: once CallKit owns the audio, the prompt cannot appear.
        let micAllowed = await LiveKitSDK.ensureDeviceAccess(for: [.audio])
        guard callID == id else { return } // ended while we asked
        guard micAllowed else {
            finish("Microphone access is off. Turn it on in Settings.")
            return
        }
        guard answerBy(ring) > .now else {
            finish("Missed a call from Bee Call")
            return
        }
        let update = CXCallUpdate()
        update.remoteHandle = CXHandle(type: .generic, value: "Bee Call")
        update.localizedCallerName = callerName(topic: ring.topic)
        update.hasVideo = false
        do {
            try await provider.reportNewIncomingCall(with: id, update: update)
        } catch {
            guard callID == id else { return }
            if (error as? CXErrorCodeIncomingCallError)?.code == .filteredByDoNotDisturb {
                finish("Missed a call from Bee Call: Do Not Disturb or a Focus is on")
            } else {
                finish("Could not ring: \(error.localizedDescription)")
            }
            return
        }
        guard callID == id else {
            provider.reportCall(with: id, endedAt: nil, reason: .remoteEnded) // ended while CallKit set it up
            return
        }
        ringTimeout = Task {
            try? await Task.sleep(for: .seconds(answerBy(ring).timeIntervalSinceNow))
            guard !Task.isCancelled, callID == id, isRinging else { return }
            hangUp(.unanswered, "Missed a call from Bee Call")
        }
    }

    /// Stops ringing a little before expires_at, when the voice server gives up and closes the room.
    /// Answering still needs a pass and a LiveKit join, and a join that lands after the room closed
    /// would open a new, empty room with no AI in it.
    private func answerBy(_ ring: Ring) -> Date {
        ring.expiresAt.addingTimeInterval(-5)
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
        connecting = Task {
            do {
                let room = try await liveKit.connect(tokenServerURL: url, answering: roomName)
                try Task.checkCancellation() // the call ended while we were connecting
                // CallKit counts an answered call as connected once the answer action is fulfilled.
                if roomName == nil {
                    provider.reportOutgoingCall(with: id, connectedAt: nil)
                }
                status = "Connected · \(room)"
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

    /// Ends the call from our side. CallKit sends no end action for these, so we clean up here.
    private func hangUp(_ reason: CXCallEndedReason, _ message: String) {
        guard let id = callID, !isEnding else { return }
        isEnding = true
        provider.reportCall(with: id, endedAt: nil, reason: reason)
        connecting?.cancel()
        Task {
            await liveKit.disconnect()
            finish(message)
        }
    }

    private func finish(_ message: String) {
        connecting?.cancel() // a connect still in flight must not touch the next call
        connecting = nil
        ringTimeout?.cancel()
        ringTimeout = nil
        callID = nil
        isInCall = false
        isRinging = false
        isEnding = false
        status = message
    }
}

extension CallManager: CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) {
        try? AudioManager.shared.setEngineAvailability(.none)
        connecting?.cancel()
        Task {
            await liveKit.disconnect()
            finish("iOS reset the call")
        }
    }

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        action.fulfill() // CallKit activates the audio session after this.
        provider.reportOutgoingCall(with: action.callUUID, startedConnectingAt: nil)
        join(action.callUUID, answering: nil)
    }

    func provider(_ provider: CXProvider, perform action: CXAnswerCallAction) {
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

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        // Also reached when iOS ends the call (lock screen, AirPods, Dynamic Island).
        let declined = isRinging // the AI's call, ended before it was answered
        isEnding = true
        connecting?.cancel()
        Task {
            await liveKit.disconnect()
            action.fulfill()
            finish(declined ? "Declined" : "Call ended")
        }
    }

    func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        Task {
            do {
                try await liveKit.setMicrophone(enabled: !action.isMuted)
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
