import XCTest
@testable import BeeCall

/// Where the app opens the call record page (contract section 8).
final class RecordAddressTests: XCTestCase {
    /// The page shows its debug toggle only with ?debug=1, which only a Debug build adds. Tests run a Debug build.
    @MainActor func testADebugBuildOpensTheRecordWithItsDebugToggle() throws {
        let url = try XCTUnwrap(Server.recordsURL, "a Debug build has a records address next to its server")
        let parts = try XCTUnwrap(URLComponents(url: url, resolvingAgainstBaseURL: false))
        XCTAssertEqual(parts.queryItems, [URLQueryItem(name: "debug", value: "1")])
        XCTAssertEqual(parts.port, 8788)
        XCTAssertEqual(parts.path, "/")
    }
}
