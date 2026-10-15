import CallKit

/// What CallManager tells CallKit about its calls: CXProvider in the app, a recorder in tests.
protocol CallKitReports: AnyObject {
    func reportIncoming(_ call: UUID, _ update: CXCallUpdate, completion: @escaping @Sendable (Error?) -> Void)
    func reportEnded(_ call: UUID, reason: CXCallEndedReason)
    func reportStartedConnecting(_ call: UUID)
    func reportConnected(_ call: UUID)
}

/// What CallManager asks CallKit for: to start or end a call. CXCallController in the app.
protocol CallKitRequests: AnyObject {
    func request(_ transaction: CXTransaction) async throws
}

extension CXProvider: CallKitReports {
    func reportIncoming(_ call: UUID, _ update: CXCallUpdate, completion: @escaping @Sendable (Error?) -> Void) {
        reportNewIncomingCall(with: call, update: update, completion: completion)
    }

    func reportEnded(_ call: UUID, reason: CXCallEndedReason) {
        reportCall(with: call, endedAt: nil, reason: reason)
    }

    func reportStartedConnecting(_ call: UUID) {
        reportOutgoingCall(with: call, startedConnectingAt: nil)
    }

    func reportConnected(_ call: UUID) {
        reportOutgoingCall(with: call, connectedAt: nil)
    }
}

extension CXCallController: CallKitRequests {}
