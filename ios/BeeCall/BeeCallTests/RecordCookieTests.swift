import XCTest
@testable import BeeCall

/// The call record page's sign-in cookie (review finding 4).
final class RecordCookieTests: XCTestCase {
    @MainActor func testTheCookieCarriesTheSignInTokenForTheRecordsHost() throws {
        let cookie = try XCTUnwrap(CallRecordSheet.signInCookie(token: "t0ken",
                                                                for: URL(string: "https://call.mslee.dev:8443/")!))
        XCTAssertEqual(cookie.name, "bee_call_session")
        XCTAssertEqual(cookie.value, "t0ken")
        XCTAssertEqual(cookie.domain, "call.mslee.dev")
        XCTAssertEqual(cookie.path, "/")
    }

    @MainActor func testAnHTTPSAddressGetsASecureCookie() throws {
        let cookie = try XCTUnwrap(CallRecordSheet.signInCookie(token: "t0ken",
                                                                for: URL(string: "https://call.mslee.dev:8443/")!))
        XCTAssertTrue(cookie.isSecure)
    }
}
