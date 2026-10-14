import AuthenticationServices
import SwiftUI

/// The first screen until the person is signed in: Apple's own sign-in button, and while Matt has not
/// approved them yet, the code to tell him.
struct SignInView: View {
    let server: Server

    var body: some View {
        VStack(spacing: 20) {
            Spacer()
            Text("Bee Call")
                .font(.largeTitle.bold())
            Text("An AI that calls you about your day, when you want it to.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Spacer()
            if case .waiting(let code) = server.state {
                VStack(spacing: 6) {
                    Text("Waiting for approval")
                        .font(.headline)
                    Text("Send this code to the person who invited you. Sign in again once they approve it.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    Text(code)
                        .font(.title.monospaced().bold())
                        .textSelection(.enabled)
                }
            }
            if let problem = server.problem {
                Text(problem)
                    .font(.callout)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
            }
            SignInWithAppleButton(.signIn) { request in
                request.requestedScopes = [] // no name or email: the approval code says who it is
                request.nonce = server.hashedNonce()
            } onCompletion: { result in
                Task { await server.finish(result) }
            }
            .frame(height: 50)
        }
        .padding()
        .task { await server.prepare() }
    }
}
