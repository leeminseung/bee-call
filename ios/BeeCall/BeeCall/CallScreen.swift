import SwiftUI

struct CallScreen: View {
    let calls: CallManager
    @AppStorage("tokenServerURL") private var tokenServerURL = "http://localhost:8787/token"

    var body: some View {
        VStack(spacing: 16) {
            Text("Bee Call")
                .font(.largeTitle.bold())
            Text(calls.status)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)

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
                Task {
                    if calls.isInCall {
                        await calls.endCall()
                    } else {
                        await calls.startCall(tokenServerURL: tokenServerURL)
                    }
                }
            } label: {
                Text(calls.isInCall ? "End" : "Call")
                    .font(.title3.bold())
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .tint(calls.isInCall ? .red : .green)
            .disabled(calls.isEnding)
        }
        .padding()
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
