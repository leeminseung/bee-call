import AVFoundation
import CallKit
import LiveKit
import Observation

/// Runs each call through CallKit, so iOS treats it as a phone call:
/// call-priority audio, AirPods and lock-screen controls, the green call indicator.
@Observable
final class CallManager: NSObject {
    private(set) var status = "Ready"
    private(set) var isInCall = false
    private(set) var isEnding = false
    let liveKit = LiveKitClient()

    @ObservationIgnored private let provider: CXProvider
    @ObservationIgnored private let controller = CXCallController()
    @ObservationIgnored private var callID: UUID?
    @ObservationIgnored private var tokenServerURL = ""
    @ObservationIgnored private var connecting: Task<Void, Never>?

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
        callID = nil
        isInCall = false
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
        let url = tokenServerURL
        connecting = Task {
            do {
                let room = try await liveKit.connect(tokenServerURL: url)
                try Task.checkCancellation() // the call ended while we were connecting
                provider.reportOutgoingCall(with: action.callUUID, connectedAt: nil)
                status = "Connected · \(room)"
            } catch {
                guard !Task.isCancelled else { return } // whoever ended the call cleans up
                hangUp(.failed, "Call failed: \(error.localizedDescription)")
            }
        }
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        // Also reached when iOS ends the call (lock screen, AirPods, Dynamic Island).
        isEnding = true
        connecting?.cancel()
        Task {
            await liveKit.disconnect()
            action.fulfill()
            finish("Call ended")
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
