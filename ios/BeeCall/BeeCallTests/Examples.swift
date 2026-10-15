import Foundation
import XCTest
@testable import BeeCall

/// The contract's example files (contract/examples), as the tests read them. The server's tests regenerate these
/// files from real code, so a test takes its expectations (versions, ids, counts, defaults, texts) from the files
/// instead of copying their values, and asserts the behaviour around them.
@MainActor
enum Examples {
    nonisolated static let folder = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().appending(path: "contract/examples")

    static func data(_ name: String) throws -> Data {
        try Data(contentsOf: folder.appending(path: "\(name).json"))
    }

    static func text(_ name: String) throws -> String {
        String(decoding: try data(name), as: UTF8.self)
    }

    /// The file as plain JSON, to compare what the app decoded with what the file says.
    static func object(_ name: String) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: data(name)) as? [String: Any], name)
    }

    static func decode<Body: Decodable>(_ type: Body.Type, _ name: String) throws -> Body {
        try Contract.decode(type, from: data(name))
    }

    /// A body made from an example with some fields changed: the server's answer in a test.
    static func changed(_ name: String, _ change: (inout [String: Any]) throws -> Void) throws -> String {
        var body = try object(name)
        try change(&body)
        return String(decoding: try JSONSerialization.data(withJSONObject: body), as: UTF8.self)
    }

    /// A version other than `version`, as the server would announce after a change.
    static func otherVersion(than version: String) -> String {
        version == "0123456789abcdef" ? "fedcba9876543210" : "0123456789abcdef"
    }
}
