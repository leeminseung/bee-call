import Foundation
import Security

/// The server's sign-in token in the Keychain, as Server uses it: the system Keychain in the app, a stand-in in
/// tests. A read tells "no token" apart from "could not read", and a save gives its result.
struct SignInKeychain {
    enum Read: Equatable {
        case token(String)
        case none // nothing saved: signed out
        case unreadable(OSStatus) // the Keychain did not give it, such as before the first unlock after a restart
    }

    var read: () -> Read
    var save: (String) -> OSStatus
    var delete: () -> Void

    static let system = SignInKeychain(read: SystemKeychain.read, save: SystemKeychain.save,
                                       delete: SystemKeychain.delete)
}

/// The token's Keychain item. Readable after the first unlock, so a VoIP push can answer a call on a locked phone.
nonisolated enum SystemKeychain {
    private static let account = "bee-call-sign-in"

    static func read() -> SignInKeychain.Read {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account,
                                    kSecReturnData as String: true, kSecMatchLimit as String: kSecMatchLimitOne]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        switch status {
        case errSecSuccess:
            guard let data = item as? Data, let token = String(data: data, encoding: .utf8) else { return .none }
            return .token(token)
        case errSecItemNotFound:
            return .none
        default:
            return .unreadable(status)
        }
    }

    static func save(_ token: String) -> OSStatus {
        delete()
        let item: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account,
                                   kSecValueData as String: Data(token.utf8),
                                   kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        return SecItemAdd(item as CFDictionary, nil)
    }

    static func delete() {
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account]
        SecItemDelete(query as CFDictionary)
    }
}
