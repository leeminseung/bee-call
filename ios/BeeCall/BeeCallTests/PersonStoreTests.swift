import XCTest
@testable import BeeCall

/// The call language, the voice and the models belong to the app after D (contract section 6): read from GET /me,
/// and sent with PUT /settings when the person picks them, never again from the Settings app. The person and the
/// server's answers are the contract's example files, with only the fields a test is about changed.
final class PersonStoreTests: XCTestCase {
    /// The server's answer to a PUT: settings.json with these fields.
    @MainActor private func answer(_ fields: [String: Any]) throws -> String {
        try Examples.changed("settings") { body in
            var settings = body["settings"] as? [String: Any] ?? [:]
            settings.merge(fields) { $1 }
            body["settings"] = settings
        }
    }

    /// A person whose call language is `language`, as GET /me answers.
    @MainActor private func me(language: String) throws -> String {
        try Examples.changed("me") { body in
            var settings = body["settings"] as? [String: Any] ?? [:]
            settings["language"] = language
            body["settings"] = settings
        }
    }

    @MainActor func testThePersonIsReadAndTheAppsLanguageFollows() async throws {
        let server = FakeServer(), store = freshDefaults(self), me = try Examples.decode(Me.self, "me")
        server.replies["GET /me"] = .status(200, try Examples.text("me"))
        let person = PersonStore(server: server.client(), store: store)
        await person.refresh()
        XCTAssertEqual(person.settings, me.settings)
        XCTAssertEqual(store.string(forKey: "language"), me.settings.language.rawValue,
                       "the screens' words follow the call language")
        XCTAssertEqual(person.beeLine(korean: true), me.settings.bee ? "Bee 연결됨"
                       : me.beeConnecting ? "Bee 앱에서 승인을 기다리는 중" : nil)
    }

    /// A new language without a voice: the server goes back to that language's default (voice null), and the app
    /// takes the server's answer.
    @MainActor func testPickingALanguageSendsItAndTakesTheServersAnswer() async throws {
        let server = FakeServer(), store = freshDefaults(self)
        server.replies["GET /me"] = .status(200, try me(language: "ko"))
        server.replies["PUT /settings"] = .status(200, try answer(["language": "en", "voice": NSNull()]))
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
        let voice = try XCTUnwrap(try Examples.decode(VoiceList.self, "voices").voices.last?.id)
        server.replies["GET /me"] = .status(200, try Examples.text("me"))
        server.replies["PUT /settings"] = .status(200, try answer(["voice": voice]))
        let person = PersonStore(server: server.client(), store: freshDefaults(self))
        await person.refresh()
        let saved = await person.choose(voice: voice)
        XCTAssertTrue(saved)
        XCTAssertEqual(try server.body(of: "PUT /settings") as NSDictionary, ["voice": voice] as NSDictionary)
        XCTAssertEqual(person.settings?.voice, voice)
    }

    /// The call and worker models, picked in the settings sheet from GET /models (section 6), by their catalog ids.
    @MainActor func testPickingAModelSendsItsId() async throws {
        let server = FakeServer()
        let call = try XCTUnwrap(try Examples.decode(ModelList.self, "models-call").models.last?.id)
        let worker = try XCTUnwrap(try Examples.decode(ModelList.self, "models-worker").models.last?.id)
        server.replies["GET /me"] = .status(200, try Examples.text("me"))
        server.replies["PUT /settings"] = .status(200, try answer(["call_model": call]))
        let person = PersonStore(server: server.client(), store: freshDefaults(self))
        await person.refresh()
        let saved = await person.choose(model: call, for: .call)
        XCTAssertTrue(saved)
        XCTAssertEqual(try server.body(of: "PUT /settings") as NSDictionary, ["call_model": call] as NSDictionary)
        XCTAssertEqual(person.settings?.callModel, call)
        server.replies["PUT /settings"] = .status(200, try answer(["worker_model": worker]))
        _ = await person.choose(model: worker, for: .worker)
        XCTAssertEqual(try server.body(of: "PUT /settings") as NSDictionary, ["worker_model": worker] as NSDictionary)
        XCTAssertEqual(person.settings?.workerModel, worker)
    }

    /// Noise cancellation, a switch in the settings sheet (contract section 6): flipped, it is sent, and the
    /// server's answer is what shows. It applies from the next call; the server reads it when a call starts.
    @MainActor func testFlippingNoiseCancellationSendsIt() async throws {
        let server = FakeServer()
        let held = try Examples.decode(Me.self, "me").settings.noiseCancellation
        server.replies["GET /me"] = .status(200, try Examples.text("me"))
        server.replies["PUT /settings"] = .status(200, try answer(["noise_cancellation": !held]))
        let person = PersonStore(server: server.client(), store: freshDefaults(self))
        await person.refresh()
        XCTAssertEqual(person.settings?.noiseCancellation, held)
        let saved = await person.choose(noiseCancellation: !held)
        XCTAssertTrue(saved)
        XCTAssertEqual(try server.body(of: "PUT /settings") as NSDictionary, ["noise_cancellation": !held] as NSDictionary)
        XCTAssertEqual(person.settings?.noiseCancellation, !held)
    }

    @MainActor func testARefusedFlipKeepsTheSwitchAndSaysWhy() async throws {
        let server = FakeServer()
        let held = try Examples.decode(Me.self, "me").settings.noiseCancellation
        server.replies["GET /me"] = .status(200, try Examples.text("me"))
        server.replies["PUT /settings"] = .status(503, "")
        let person = PersonStore(server: server.client(), store: freshDefaults(self))
        await person.refresh()
        let saved = await person.choose(noiseCancellation: !held)
        XCTAssertFalse(saved)
        XCTAssertEqual(person.settings?.noiseCancellation, held, "what the server holds")
        XCTAssertNotNil(person.problem)
    }

    /// A pick the server refuses keeps what was there, on the screen too, and says why.
    @MainActor func testARefusedPickKeepsWhatWasThereAndSaysSo() async throws {
        let server = FakeServer(), store = freshDefaults(self)
        server.replies["GET /me"] = .status(200, try me(language: "ko"))
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
        server.replies["PUT /settings"] = .status(200, try answer(["language": "en", "voice": NSNull()]))
        let person = PersonStore(server: server.client(), store: store)
        let saved = await person.choose(language: .en)
        XCTAssertTrue(saved)
        XCTAssertEqual(person.settings?.language, .en)
        XCTAssertEqual(store.string(forKey: "language"), "en")
    }

    /// A GET /me answered after a pick, but read before it, does not undo the pick.
    @MainActor func testAnOlderReadDoesNotUndoAPick() async throws {
        let server = FakeServer(), store = freshDefaults(self)
        server.replies["GET /me"] = .slow(.milliseconds(300), try me(language: "ko"))
        server.replies["PUT /settings"] = .status(200, try answer(["language": "en", "voice": NSNull()]))
        let person = PersonStore(server: server.client(), store: store)
        let reading = Task { await person.refresh() }
        try await Task.sleep(for: .milliseconds(50))
        let saved = await person.choose(language: .en)
        XCTAssertTrue(saved)
        await reading.value
        XCTAssertEqual(person.settings?.language, .en)
        XCTAssertEqual(store.string(forKey: "language"), "en")
        XCTAssertEqual(person.me?.person, try Examples.decode(Me.self, "me").person, "the rest of the read is kept")
    }
}
