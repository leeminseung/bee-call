import Foundation
import Observation

/// GET /models?role=call|worker (contract section 5b): each role's choices from the server's one model catalog,
/// 1 to 12, the default first, for the settings sheet's "통화 모델" and "일꾼 모델". The app shows only what this
/// answer gives, so a new model or provider is a server change, never an app update. Asked for once per role, and
/// again after a failure.
@Observable
final class ModelCatalog {
    private(set) var models: [ModelRole: [CatalogModel]] = [:]
    private(set) var failed: Set<ModelRole> = []
    @ObservationIgnored private let server: ServerClient

    init(server: ServerClient = ContractServer.client) {
        self.server = server
    }

    func load(_ role: ModelRole) async {
        guard models[role] == nil, role != .unrecognized else { return }
        guard case .ok(200, let data) = await server.send("/models?role=\(role.rawValue)"),
              let list = try? Contract.decode(ModelList.self, from: data), !list.models.isEmpty else {
            failed.insert(role)
            return
        }
        models[role] = list.models
        failed.remove(role)
    }
}

enum ModelChoice {
    /// The model shown as chosen: the stored id when the catalog has it, else the role's default (a stored id the
    /// catalog no longer has reads as the default, section 6).
    static func effective(stored: String?, in models: [CatalogModel]) -> CatalogModel? {
        models.first { $0.id == stored } ?? models.first(where: \.isDefault) ?? models.first
    }
}
