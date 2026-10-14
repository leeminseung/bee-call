import Foundation

/// The person declined the AI's ringing call: POST /calls/decline (contract section 2). The voice server stops
/// waiting for the phone, records the call as declined rather than missed, and /ring never rings it again.
struct CallDecline {
    let transport: any HTTPTransport
    let noteStatus: (Int) -> Void

    /// The call's CallKit end never waits for this. A decline that does not arrive only means the server waits
    /// out the ring and records a missed call, as before.
    func send(room: String) async {
        guard let request = Server.request("/calls/decline", method: "POST", json: ["room": room]),
              let (_, response) = try? await transport.send(request) else { return }
        noteStatus(response.httpStatus)
    }
}
