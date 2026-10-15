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
        XCTAssertEqual(person.settings?.voice, "ko-harin")
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
        XCTAssertEqual(person.settings?.language, .en)
        XCTAssertNil(person.settings?.voice)
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
        XCTAssertEqual(person.settings?.voice, "ko-daon")
    }

    /// The call and worker models, picked in the settings sheet from GET /models (section 6).
    @MainActor func testPickingAModelSendsItsId() async throws {
        let server = FakeServer()
        server.replies["GET /me"] = .status(200, try text("me"))
        server.replies["PUT /settings"] = .status(200, try text("settings")
            .replacingOccurrences(of: "\"call_model\": \"haiku\"", with: "\"call_model\": \"sonnet\""))
        let person = PersonStore(server: server.client(), store: freshDefaults(self))
        await person.refresh()
        let saved = await person.choose(model: "sonnet", for: .call)
        XCTAssertTrue(saved)
        XCTAssertEqual(try server.body(of: "PUT /settings") as NSDictionary, ["call_model": "sonnet"] as NSDictionary)
        XCTAssertEqual(person.settings?.callModel, "sonnet")
        _ = await person.choose(model: "glm", for: .worker)
        XCTAssertEqual(try server.body(of: "PUT /settings") as NSDictionary, ["worker_model": "glm"] as NSDictionary)
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
        XCTAssertEqual(person.settings?.language, .ko)
        XCTAssertEqual(store.string(forKey: "language"), "ko")
        XCTAssertNotNil(person.problem)
    }

    // MARK: - Second review of the D wiring, finding 4

    /// The server's answer to a pick is kept even when GET /me has not come yet.
    @MainActor func testAPickIsKeptBeforeThePersonIsRead() async throws {
        let server = FakeServer(), store = freshDefaults(self)
        server.replies["PUT /settings"] = .status(200, try text("settings")
            .replacingOccurrences(of: "\"language\": \"ko\"", with: "\"language\": \"en\""))
        let person = PersonStore(server: server.client(), store: store)
        let saved = await person.choose(language: .en)
        XCTAssertTrue(saved)
        XCTAssertEqual(person.settings?.language, .en)
        XCTAssertEqual(store.string(forKey: "language"), "en")
    }

    /// A GET /me answered after a pick, but read before it, does not undo the pick.
    @MainActor func testAnOlderReadDoesNotUndoAPick() async throws {
        let server = FakeServer(), store = freshDefaults(self)
        server.replies["GET /me"] = .slow(.milliseconds(300), try text("me")) // language ko
        server.replies["PUT /settings"] = .status(200, try text("settings")
            .replacingOccurrences(of: "\"language\": \"ko\"", with: "\"language\": \"en\""))
        let person = PersonStore(server: server.client(), store: store)
        let reading = Task { await person.refresh() }
        try await Task.sleep(for: .milliseconds(50))
        let saved = await person.choose(language: .en)
        XCTAssertTrue(saved)
        await reading.value
        XCTAssertEqual(person.settings?.language, .en)
        XCTAssertEqual(store.string(forKey: "language"), "en")
        XCTAssertEqual(person.me?.person, "minseung", "the rest of the read is kept")
    }
}
