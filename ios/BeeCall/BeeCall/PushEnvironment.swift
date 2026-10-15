import Foundation

/// Which of Apple's push services this build's tokens belong to, as the server is told with each token (review
/// finding 12). The signing profile decides it, not the build configuration: Xcode installs Debug and Release
/// builds on an iPhone signed for development, so their tokens are development ones, while TestFlight and the App
/// Store sign for production and leave no profile in the app. So it is read from the app's embedded profile, and
/// no profile means production. The Simulator gets its pushes from the development service.
nonisolated enum PushEnvironment {
    static var current: String {
        #if targetEnvironment(simulator)
        return "development"
        #else
        return ofApp()
        #endif
    }

    /// What the app's own embedded profile grants (compiled for the Simulator too, so its tests reach it).
    static func ofApp(_ bundle: Bundle = .main) -> String {
        of(profile: bundle.url(forResource: "embedded", withExtension: "mobileprovision")
            .flatMap { try? Data(contentsOf: $0) })
    }

    /// The aps-environment a provisioning profile grants: its property list sits inside a signed envelope.
    static func of(profile: Data?) -> String {
        guard let profile, let start = profile.range(of: Data("<?xml".utf8)),
              let end = profile.range(of: Data("</plist>".utf8), in: start.lowerBound..<profile.endIndex),
              let plist = try? PropertyListSerialization.propertyList(from: profile[start.lowerBound..<end.upperBound],
                                                                      format: nil) as? [String: Any],
              let entitlements = plist["Entitlements"] as? [String: Any],
              let environment = entitlements["aps-environment"] as? String else { return "production" }
        return environment
    }
}
