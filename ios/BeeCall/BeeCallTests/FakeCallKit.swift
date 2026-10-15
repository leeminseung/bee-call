import CallKit
@testable import BeeCall

/// Stands in for CallKit: keeps what CallManager reports and requests, and answers a new incoming call later on
/// the main queue, as CallKit does.
@MainActor
final class FakeCallKit: CallKitReports, CallKitRequests {
    struct Incoming: Equatable {
        let call: UUID
        let name: String?
        let handle: String?
    }

    private(set) var incoming: [Incoming] = []
    private(set) var ended: [UUID: CXCallEndedReason] = [:]
    private(set) var startedConnecting: [UUID] = []
    private(set) var connected: [UUID] = []
    private(set) var updates: [UUID: [Incoming]] = [:]
    private(set) var requested: [CXAction] = []
    /// What CallKit answers to the next new incoming call, such as a Focus filtering it.
    var incomingError: Error?

    func reportIncoming(_ call: UUID, _ update: CXCallUpdate, completion: @escaping @Sendable (Error?) -> Void) {
        incoming.append(Incoming(call: call, name: update.localizedCallerName, handle: update.remoteHandle?.value))
        let error = incomingError
        DispatchQueue.main.async { completion(error) }
    }

    func reportEnded(_ call: UUID, reason: CXCallEndedReason) {
        ended[call] = reason
    }

    func reportStartedConnecting(_ call: UUID) {
        startedConnecting.append(call)
    }

    func reportConnected(_ call: UUID) {
        connected.append(call)
    }

    func request(_ transaction: CXTransaction) async throws {
        requested += transaction.actions
    }
}

/// A ring for the AI's call that expires `seconds` from now.
func ring(_ room: String = "call-matt-20261015-210000", topic: String? = "Saturday hike",
          expiringIn seconds: TimeInterval = 60) -> Ring {
    Ring(room: room, topic: topic, expiresAt: Date().addingTimeInterval(seconds))
}
