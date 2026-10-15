import XCTest
@testable import BeeCall

/// GET /voices?language= (contract section 5): one language's voices, the default first, for the first run's
/// second step and the settings sheet; each with a sample of about five seconds that needs no sign-in.
final class VoicesTests: XCTestCase {
    @MainActor func testALanguagesVoicesComeInTheServersOrder() async throws {
        let server = FakeServer()
        server.replies["GET /voices"] = .status(200, try Examples.text("voices"))
        let catalog = VoiceCatalog(server: server.client())
        await catalog.load(.ko)
        XCTAssertEqual(server.requests.last?.url?.query, "language=ko")
        XCTAssertEqual(catalog.voices[.ko], try Examples.decode(VoiceList.self, "voices").voices, "in the server's order")
        XCTAssertEqual(catalog.voices[.ko]?.first?.isDefault, true, "the default first")
        await catalog.load(.ko)
        XCTAssertEqual(server.requests.count, 1, "a language's voices are asked for once")
    }

    /// Any number from 1 to 12 (the contract's change of 2026-10-15): a list of one reads like any other.
    @MainActor func testAnyNumberOfVoicesIsAList() async throws {
        let server = FakeServer()
        let first = try XCTUnwrap(try Examples.decode(VoiceList.self, "voices-en").voices.first)
        server.replies["GET /voices"] = .status(200, try Examples.changed("voices-en") { body in
            body["voices"] = (body["voices"] as? [Any])?.prefix(1).map { $0 } // the default alone
        })
        let catalog = VoiceCatalog(server: server.client())
        await catalog.load(.en)
        XCTAssertEqual(catalog.voices[.en], [first])
    }

    @MainActor func testVoicesThatCannotBeLoadedSaySoAndAreAskedForAgain() async throws {
        let server = FakeServer()
        server.replies["GET /voices"] = .status(404)
        let catalog = VoiceCatalog(server: server.client())
        await catalog.load(.en)
        XCTAssertNil(catalog.voices[.en])
        XCTAssertTrue(catalog.failed.contains(.en))
        server.replies["GET /voices"] = .status(200, try Examples.text("voices-en"))
        await catalog.load(.en)
        XCTAssertEqual(catalog.voices[.en], try Examples.decode(VoiceList.self, "voices-en").voices)
        XCTAssertFalse(catalog.failed.contains(.en))
    }

    /// The sample is a path on the token server, which AVPlayer streams without the sign-in.
    @MainActor func testTheSampleComesFromTheServersAddress() throws {
        let voice = try XCTUnwrap(try Examples.decode(VoiceList.self, "voices").voices.first)
        XCTAssertTrue(voice.sampleURL.hasPrefix("/"), "a path on the token server")
        XCTAssertEqual(SamplePlayer.url(of: voice)?.absoluteString, Server.api + voice.sampleURL)
    }

    /// Second review of the D wiring, finding 3: a sample must not take the audio session while Bee Call's call rings
    /// either, just before CallKit activates it for the call.
    @MainActor func testNoSampleWhileACallIsOnOrRinging() throws {
        let voice = try XCTUnwrap(try Examples.decode(VoiceList.self, "voices").voices.first)
        var inCall = true
        let player = SamplePlayer(inCall: { inCall })
        player.play(voice)
        XCTAssertFalse(player.isPlaying)
        inCall = false
        player.play(voice)
        XCTAssertTrue(player.isPlaying)
        player.stop()
        XCTAssertFalse(player.isPlaying)
    }

    /// A language a later server adds has no list here: it says so instead of waiting for ever.
    @MainActor func testAnUnknownLanguageSaysItsVoicesCannotBeLoaded() async {
        let catalog = VoiceCatalog(server: FakeServer().client())
        await catalog.load(.unrecognized)
        XCTAssertTrue(catalog.failed.contains(.unrecognized))
    }
}
