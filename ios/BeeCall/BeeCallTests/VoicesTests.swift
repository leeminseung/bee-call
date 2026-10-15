import XCTest
@testable import BeeCall

/// GET /voices?language= (contract section 5): one language's voices, the default first, for the first run's
/// second step and the settings sheet; each with a sample of about five seconds that needs no sign-in.
final class VoicesTests: XCTestCase {
    @MainActor private func voicesBody() throws -> String {
        String(decoding: try ContractTests.example("voices"), as: UTF8.self)
    }

    @MainActor func testALanguagesVoicesComeInTheServersOrder() async throws {
        let server = FakeServer()
        server.replies["GET /voices"] = .status(200, try voicesBody())
        let catalog = VoiceCatalog(server: server.client())
        await catalog.load(.ko)
        XCTAssertEqual(server.requests.last?.url?.query, "language=ko")
        XCTAssertEqual(catalog.voices[.ko]?.map(\.id), ["ko-harin", "ko-seojun", "ko-daon", "ko-narae", "ko-doyoon"])
        XCTAssertEqual(catalog.voices[.ko]?.first?.isDefault, true)
        await catalog.load(.ko)
        XCTAssertEqual(server.requests.count, 1, "a language's voices are asked for once")
    }

    /// Any number from 1 to 12 (the contract's change of 2026-10-15): a list of one reads like any other.
    @MainActor func testAnyNumberOfVoicesIsAList() async throws {
        let server = FakeServer()
        let one = """
        {"language": "en", "voices": [{"id": "en-zara", "name": "Zara", "character": {"ko": "따뜻함", "en": "Warm"},
         "sample_url": "/voices/samples/en-zara-1a2b3c4d.mp3", "default": true}]}
        """
        server.replies["GET /voices"] = .status(200, one)
        let catalog = VoiceCatalog(server: server.client())
        await catalog.load(.en)
        XCTAssertEqual(catalog.voices[.en]?.map(\.name), ["Zara"])
    }

    @MainActor func testVoicesThatCannotBeLoadedSaySoAndAreAskedForAgain() async throws {
        let server = FakeServer()
        server.replies["GET /voices"] = .status(404)
        let catalog = VoiceCatalog(server: server.client())
        await catalog.load(.en)
        XCTAssertNil(catalog.voices[.en])
        XCTAssertTrue(catalog.failed.contains(.en))
        server.replies["GET /voices"] = .status(200, try voicesBody().replacingOccurrences(of: "\"language\": \"ko\"",
                                                                                 with: "\"language\": \"en\""))
        await catalog.load(.en)
        XCTAssertEqual(catalog.voices[.en]?.count, 5)
        XCTAssertFalse(catalog.failed.contains(.en))
    }

    /// The sample is a path on the token server, which AVPlayer streams without the sign-in.
    @MainActor func testTheSampleComesFromTheServersAddress() throws {
        let voice = try XCTUnwrap(try Contract.decode(VoiceList.self, from: ContractTests.example("voices")).voices.first)
        XCTAssertEqual(SamplePlayer.url(of: voice)?.absoluteString, Server.api + "/voices/samples/ko-harin-3f9c2a1b.mp3")
    }
}
