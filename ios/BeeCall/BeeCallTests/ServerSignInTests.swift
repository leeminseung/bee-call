import Security
import UIKit
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

    /// Review finding 17: the signed-in state was read once, at launch. A launch before the first unlock after a
    /// restart, such as one for a VoIP push, cannot read the Keychain, and left the sign-in screen up for good.
    @MainActor func testASignInUnreadableAtLaunchIsReadAgainWhenTheKeychainOpens() {
        let store = freshDefaults(self)
        store.set("matt", forKey: "person")
        var read = SignInKeychain.Read.unreadable(errSecInteractionNotAllowed)
        let server = Server(keychain: SignInKeychain(read: { read }, save: { _ in errSecSuccess }, delete: {}),
                            store: store)
        XCTAssertEqual(server.state, .signedOut)
        read = .token("t")
        NotificationCenter.default.post(name: UIApplication.protectedDataDidBecomeAvailableNotification, object: nil)
        XCTAssertEqual(server.state, .signedIn(person: "matt"))
    }

    @MainActor func testComingToTheFrontReadsTheSignInAgainButKeepsAnEndedOneEnded() {
        var read = SignInKeychain.Read.unreadable(errSecInteractionNotAllowed)
        let server = Server(keychain: SignInKeychain(read: { read }, save: { _ in errSecSuccess }, delete: {}),
                            store: freshDefaults(self))
        read = .none // signed out meanwhile, or never signed in
        server.readSignInAgain()
        XCTAssertEqual(server.state, .signedOut)
        read = .token("t")
        server.readSignInAgain()
        XCTAssertEqual(server.state, .signedIn(person: ""))
    }

    /// Review of 2026-10-15, finding 3: after a tap took the value, the loop slept up to 15 s before fetching the
    /// next one, so the button stayed off after the person closed Apple's sheet or the server refused.
    @MainActor func testTheNextValueIsReadySoonAfterATapTookOne() async {
        let nonces = FakeServer()
        nonces.replies["GET /auth/nonce"] = .json(200, ["nonce": "n"])
        let server = Server(keychain: keychain(.none), store: freshDefaults(self),
                            signIn: AppleSignIn(server: nonces.client(token: nil)))
        let preparing = Task { await server.prepareSignIn() }
        defer { preparing.cancel() }
        await waitUntil("the first value is ready") { server.signIn.isReady }
        XCTAssertNotNil(server.signIn.takeForRequest())
        XCTAssertFalse(server.signIn.isReady)
        await waitUntil("the next value is ready", timeout: .seconds(2)) { server.signIn.isReady }
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
