import SwiftUI

struct CallScreen: View {
    let calls: CallManager
    @AppStorage("tokenServerURL") private var tokenServerURL = "http://localhost:8787/token"
    @Environment(\.scenePhase) private var scenePhase
    @State private var showsCallRecord = false

    var body: some View {
        VStack(spacing: 16) {
            Text("Bee Call")
                .font(.largeTitle.bold())
            VStack(spacing: 4) {
                Text(calls.status)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                if !calls.isInCall && !calls.ringStatus.isEmpty {
                    Text(calls.ringsWhenClosed ? "\(calls.ringStatus), also while the app is closed" : calls.ringStatus)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
            }

            captions

            VStack(alignment: .leading, spacing: 4) {
                Text("Token server")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                TextField("http://<Mac IP>:8787/token", text: $tokenServerURL)
                    .textFieldStyle(.roundedBorder)
                    .keyboardType(.URL)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .disabled(calls.isInCall)
            }

            Button {
                showsCallRecord = true
            } label: {
                Label("Call record", systemImage: "list.bullet.rectangle")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.bordered)
            .disabled(callRecordURL == nil)

            Button {
                Task {
                    if calls.isInCall {
                        await calls.endCall()
                    } else {
                        await calls.startCall(tokenServerURL: tokenServerURL)
                    }
                }
            } label: {
                Text(calls.isRinging ? "Decline" : calls.isInCall ? "End" : "Call")
                    .font(.title3.bold())
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .tint(calls.isInCall ? .red : .green)
            .disabled(calls.isEnding)
        }
        .padding()
        .sheet(isPresented: $showsCallRecord) {
            if let callRecordURL {
                CallRecordSheet(url: callRecordURL) { earlierCall in
                    guard !calls.isInCall else { return } // one call at a time; the record stays open
                    showsCallRecord = false
                    Task { await calls.startCall(tokenServerURL: tokenServerURL, continuing: earlierCall) }
                }
            }
        }
        // A new id cancels the running listener and starts the next one.
        .task(id: ringSource) {
            await calls.listenForRings(tokenServerURL: ringSource)
        }
        .task {
            await calls.askForMicrophone()
        }
    }

    /// Where to listen for the AI's calls: the token server while the app is in the foreground, else
    /// nowhere. Inactive still counts as foreground; the incoming-call banner makes the app inactive.
    private var ringSource: String {
        scenePhase == .background ? "" : tokenServerURL
    }

    private var callRecordURL: URL? {
        CallRecordSheet.url(tokenServerURL: tokenServerURL)
    }

    private var captions: some View {
        ScrollView {
            VStack(spacing: 12) {
                ForEach(calls.liveKit.captions) { line in
                    VStack(alignment: line.isMine ? .trailing : .leading, spacing: 2) {
                        Text(line.isMine ? "You" : "AI")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text(line.text)
                            .multilineTextAlignment(line.isMine ? .trailing : .leading)
                    }
                    .frame(maxWidth: .infinity, alignment: line.isMine ? .trailing : .leading)
                }
            }
            .padding()
        }
        .defaultScrollAnchor(.bottom)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.secondarySystemBackground), in: .rect(cornerRadius: 12))
        .overlay {
            if calls.liveKit.captions.isEmpty {
                Text("Captions of both sides appear here during a call.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding()
            }
        }
    }
}
