import XCTest
@testable import BeeCall

/// The call and worker models are picked in the app's settings sheet from GET /models?role= (contract section 5b):
/// the server's catalog names them, so a new model or provider needs no app update.
final class ModelsTests: XCTestCase {
    @MainActor func testEachRolesModelsComeFromTheServer() async throws {
        let server = FakeServer()
        server.replies["GET /models"] = .status(200, try Examples.text("models-worker"))
        let catalog = ModelCatalog(server: server.client())
        await catalog.load(.worker)
        XCTAssertEqual(server.requests.last?.url?.query, "role=worker")
        XCTAssertEqual(catalog.models[.worker], try Examples.decode(ModelList.self, "models-worker").models)
        await catalog.load(.worker)
        XCTAssertEqual(server.requests.count, 1, "a role's models are asked for once")
        server.replies["GET /models"] = .status(404)
        await catalog.load(.call)
        XCTAssertTrue(catalog.failed.contains(.call))
    }

    /// The model shown as chosen: the stored id when the catalog has it, else the role's default (a stored id the
    /// catalog no longer has reads as the default, section 6).
    @MainActor func testTheChosenModelIsTheStoredOneElseTheDefault() throws {
        for name in ["models-call", "models-worker"] {
            let models = try Examples.decode(ModelList.self, name).models
            let standard = try XCTUnwrap(models.first(where: \.isDefault), "\(name): one default")
            for model in models {
                XCTAssertEqual(ModelChoice.effective(stored: model.id, in: models), model, "\(name): the stored one")
            }
            XCTAssertEqual(ModelChoice.effective(stored: "no-such-model", in: models), standard,
                           "\(name): an id the catalog no longer has reads as the default")
            XCTAssertEqual(ModelChoice.effective(stored: nil, in: models), standard)
        }
        XCTAssertNil(ModelChoice.effective(stored: "any-model", in: []))
        let me = try Examples.decode(Me.self, "me").settings
        for (name, stored) in [("models-call", me.callModel), ("models-worker", me.workerModel)] {
            let models = try Examples.decode(ModelList.self, name).models
            let shown = try XCTUnwrap(models.first { $0.id == stored }, "\(name): me.json's model is in the catalog")
            XCTAssertEqual(ModelChoice.effective(stored: stored, in: models), shown, "\(name): the person's stored model is shown")
        }
    }

    /// A role may list a single model (the worker lists GLM 5 alone until the Claude runner lands) or several: the
    /// chosen one is shown either way.
    @MainActor func testOneModelOrTwo() throws {
        let one = try Examples.decode(ModelList.self, "models-worker").models.prefix(1)
        XCTAssertEqual(ModelChoice.effective(stored: nil, in: Array(one))?.id, one.first?.id)
        let two = try Examples.decode(ModelList.self, "models-call").models
        XCTAssertGreaterThanOrEqual(two.count, 2, "the call list of the examples has two")
        XCTAssertEqual(ModelChoice.effective(stored: two[1].id, in: two), two[1])
    }
}
