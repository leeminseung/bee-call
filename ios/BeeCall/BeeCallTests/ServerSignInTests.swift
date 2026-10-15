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

    /// Review finding 17: the result of saving the token was ignored, so a sign-in this iPhone could not keep
    /// showed the person signed in, until the next request signed them out with "Your sign-in ended".
    @MainActor func testASignInThisIPhoneCannotKeepIsNotShownAsSignedIn() async {
        let store = freshDefaults(self)
        let server = Server(keychain: keychain(.none, save: errSecInteractionNotAllowed), store: store)
        await server.complete(.signedIn(token: "t", person: "matt"))
        XCTAssertEqual(server.state, .signedOut)
        XCTAssertEqual(server.problem, "This iPhone could not keep your sign-in (Keychain error -25308). Try again.")
        XCTAssertNil(store.string(forKey: "person"))
    }
}
