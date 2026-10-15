import XCTest
@testable import BeeCall

/// The call and worker models are picked in the app's settings sheet from GET /models?role= (contract section 5b):
/// the server's catalog names them, so a new model or provider needs no app update.
final class ModelsTests: XCTestCase {
    @MainActor private func text(_ name: String) throws -> String {
        String(decoding: try ContractTests.example(name), as: UTF8.self)
    }

    @MainActor func testEachRolesModelsComeFromTheServer() async throws {
        let server = FakeServer()
        server.replies["GET /models"] = .status(200, try text("models-worker"))
        let catalog = ModelCatalog(server: server.client())
        await catalog.load(.worker)
        XCTAssertEqual(server.requests.last?.url?.query, "role=worker")
        XCTAssertEqual(catalog.models[.worker]?.map(\.id), ["sonnet", "glm"])
        await catalog.load(.worker)
        XCTAssertEqual(server.requests.count, 1, "a role's models are asked for once")
        server.replies["GET /models"] = .status(404)
        await catalog.load(.call)
        XCTAssertTrue(catalog.failed.contains(.call))
    }

    /// The model shown as chosen: the stored id when the catalog has it, else the role's default (a stored id the
    /// catalog no longer has reads as the default, section 6).
    @MainActor func testTheChosenModelIsTheStoredOneElseTheDefault() throws {
        let models = try Contract.decode(ModelList.self, from: ContractTests.example("models-call")).models
        XCTAssertEqual(ModelChoice.effective(stored: "sonnet", in: models)?.id, "sonnet")
        XCTAssertEqual(ModelChoice.effective(stored: "glm", in: models)?.id, "haiku")
        XCTAssertNil(ModelChoice.effective(stored: "haiku", in: []))
    }
}
