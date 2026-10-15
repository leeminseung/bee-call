import Security
import XCTest
@testable import BeeCall

/// Who is signed in on this iPhone: the server's token in the Keychain, and the person's name beside it.
final class ServerSignInTests: XCTestCase {
    @MainActor private func keychain(_ read: SignInKeychain.Read, save: OSStatus = errSecSuccess) -> SignInKeychain {
        SignInKeychain(read: { read }, save: { _ in save }, delete: {})
    }

    @MainActor func testASavedTokenMeansSignedIn() {
        let store = freshDefaults(self)
        store.set("matt", forKey: "person")
        XCTAssertEqual(Server(keychain: keychain(.token("t")), store: store).state, .signedIn(person: "matt"))
        XCTAssertEqual(Server(keychain: keychain(.none), store: store).state, .signedOut)
    }

    @MainActor func testASignInWaitingForApprovalOrRefusedSaysSo() async {
        let server = Server(keychain: keychain(.none), store: freshDefaults(self))
        await server.complete(.waiting(code: "ABCD"))
        XCTAssertEqual(server.state, .waiting(code: "ABCD"))
        XCTAssertNil(server.problem)
        await server.complete(.failed("The server did not accept the sign-in."))
        XCTAssertEqual(server.problem, "The server did not accept the sign-in.")
    }
}
