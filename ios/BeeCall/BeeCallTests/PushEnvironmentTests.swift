import XCTest
@testable import BeeCall

/// Which of Apple's push services this build's tokens belong to, as the server is told with each token (review
/// finding 12): the signing profile decides it, not the build configuration.
final class PushEnvironmentTests: XCTestCase {
    /// A provisioning profile as the app carries it: the profile's property list inside a signed envelope.
    private func profile(apsEnvironment: String?) -> Data {
        var entitlements: [String: Any] = ["application-identifier": "TEAM.com.leeminseung.beecall"]
        entitlements["aps-environment"] = apsEnvironment
        let plist = try! PropertyListSerialization.data(fromPropertyList: ["Name": "Bee Call", "Entitlements": entitlements],
                                                        format: .xml, options: 0)
        return Data([0x30, 0x82, 0x4E, 0x1F, 0x06, 0x09]) + plist + Data([0xA0, 0x82, 0x0D, 0x00, 0x30])
    }

    /// Xcode installs Debug and Release builds on an iPhone signed for development: a Release build so installed
    /// used to tell the server "production", and the AI's calls never rang it while closed.
    @MainActor func testABuildSignedForDevelopmentGetsDevelopmentPushes() {
        XCTAssertEqual(PushEnvironment.of(profile: profile(apsEnvironment: "development")), "development")
    }

    @MainActor func testAnAdHocProfileGetsProductionPushes() {
        XCTAssertEqual(PushEnvironment.of(profile: profile(apsEnvironment: "production")), "production")
    }

    /// TestFlight and App Store builds carry no profile.
    @MainActor func testNoProfileMeansProduction() {
        XCTAssertEqual(PushEnvironment.of(profile: nil), "production")
        XCTAssertEqual(PushEnvironment.of(profile: Data("not a profile".utf8)), "production")
        XCTAssertEqual(PushEnvironment.of(profile: profile(apsEnvironment: nil)), "production")
        XCTAssertEqual(PushEnvironment.ofApp(), "production") // this unsigned Simulator build carries none
    }
}
