import XCTest
@testable import BeeCall

/// The call language and the voice belong to the app after D (contract section 6): read from GET /me, and sent
/// with PUT /settings when the person picks them, never again from the Settings app.
final class PersonStoreTests: XCTestCase {
    @MainActor private func text(_ name: String) throws -> String {
        String(decoding: try ContractTests.example(name), as: UTF8.self)
    }

    @MainActor func testThePersonIsReadAndTheAppsLanguageFollows() async throws {
        let server = FakeServer(), store = freshDefaults(self)
        server.replies["GET /me"] = .status(200, try text("me"))
        let person = PersonStore(server: server.client(), store: store)
        await person.refresh()
        XCTAssertEqual(person.me?.settings.voice, "ko-harin")
        XCTAssertEqual(store.string(forKey: "language"), "ko", "the screens' words follow the call language")
        XCTAssertEqual(person.beeLine(korean: true), "Bee 연결됨")
    }

    /// A new language without a voice: the server goes back to that language's default (voice null), and the app
    /// takes the server's answer.
    @MainActor func testPickingALanguageSendsItAndTakesTheServersAnswer() async throws {
        let server = FakeServer(), store = freshDefaults(self)
        server.replies["GET /me"] = .status(200, try text("me"))
        server.replies["PUT /settings"] = .status(200, try text("settings")
            .replacingOccurrences(of: "\"language\": \"ko\"", with: "\"language\": \"en\"")
            .replacingOccurrences(of: "\"voice\": \"ko-daon\"", with: "\"voice\": null"))
        let person = PersonStore(server: server.client(), store: store)
        await person.refresh()
        let saved = await person.choose(language: .en)
        XCTAssertTrue(saved)
        XCTAssertEqual(try server.body(of: "PUT /settings") as NSDictionary, ["language": "en"] as NSDictionary)
        XCTAssertEqual(person.me?.settings.language, .en)
        XCTAssertNil(person.me?.settings.voice)
        XCTAssertEqual(store.string(forKey: "language"), "en")
    }

    @MainActor func testPickingAVoiceSendsIt() async throws {
        let server = FakeServer()
        server.replies["GET /me"] = .status(200, try text("me"))
        server.replies["PUT /settings"] = .status(200, try text("settings"))
        let person = PersonStore(server: server.client(), store: freshDefaults(self))
        await person.refresh()
        let saved = await person.choose(voice: "ko-daon")
        XCTAssertTrue(saved)
        XCTAssertEqual(try server.body(of: "PUT /settings") as NSDictionary, ["voice": "ko-daon"] as NSDictionary)
        XCTAssertEqual(person.me?.settings.voice, "ko-daon")
    }

    /// A pick the server refuses keeps what was there, on the screen too, and says why.
    @MainActor func testARefusedPickKeepsWhatWasThereAndSaysSo() async throws {
        let server = FakeServer(), store = freshDefaults(self)
        server.replies["GET /me"] = .status(200, try text("me"))
        server.replies["PUT /settings"] = .status(400, "voice is not one of en's voices")
        let person = PersonStore(server: server.client(), store: store)
        await person.refresh()
        let saved = await person.choose(language: .en)
        XCTAssertFalse(saved)
        XCTAssertEqual(person.me?.settings.language, .ko)
        XCTAssertEqual(store.string(forKey: "language"), "ko")
        XCTAssertNotNil(person.problem)
    }
}
