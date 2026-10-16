@preconcurrency import UserNotifications
import XCTest
@testable import BeeCall

/// Build 4's crash (the user's phone, 2026-10-16): a tapped notification without an offer made the delegate's
/// compiler-made completion run on a background thread, and UIKit asserts that a response's completion handler runs
/// on the main thread, so every tapped notification closed the app. iOS calls the Objective-C completion-handler form,
/// so these tests call that form too, from a background thread, and check where the handler runs.
final class NotificationDelegateTests: XCTestCase {
    /// A notification as iOS delivers it, made with the classes' own (unpublished) constructors, as no public one
    /// exists.
    @MainActor private func notification(_ userInfo: [AnyHashable: Any]) throws -> UNNotification {
        let content = UNMutableNotificationContent()
        content.title = "Bee Call"
        content.userInfo = userInfo
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        let made = (UNNotification.self as AnyObject).perform(NSSelectorFromString("notificationWithRequest:date:"),
                                                               with: request, with: Date())
        return try XCTUnwrap(made?.takeUnretainedValue() as? UNNotification)
    }

    @MainActor private func response(_ action: String, _ userInfo: [AnyHashable: Any]) throws -> UNNotificationResponse {
        let made = (UNNotificationResponse.self as AnyObject).perform(
            NSSelectorFromString("responseWithNotification:actionIdentifier:"), with: try notification(userInfo),
            with: action)
        return try XCTUnwrap(made?.takeUnretainedValue() as? UNNotificationResponse)
    }

    /// What a handler saw, read from any thread.
    private final class Seen: @unchecked Sendable {
        private let lock = NSLock()
        private var values: [String: Bool] = [:]

        subscript(key: String) -> Bool? {
            get { lock.withLock { values[key] } }
            set { lock.withLock { values[key] = newValue } }
        }
    }

    /// A plain alert (no offer) tapped: the handler runs on the main thread.
    @MainActor func testATappedNotificationsHandlerRunsOnTheMainThread() async throws {
        nonisolated(unsafe) let delegate: any UNUserNotificationCenterDelegate = OfferCenter(server: FakeServer().client())
        nonisolated(unsafe) let response = try response(UNNotificationDefaultActionIdentifier, ["kind": "summary"])
        let seen = Seen(), ran = expectation(description: "the handler ran")
        DispatchQueue.global(qos: .userInitiated).async {
            delegate.userNotificationCenter?(.current(), didReceive: response) {
                seen["main"] = Thread.isMainThread
                ran.fulfill()
            }
        }
        await fulfillment(of: [ran], timeout: 5)
        XCTAssertEqual(seen["main"], true, "UIKit asserts that a response's handler runs on the main thread")
    }

    /// An offer's button: the answer goes to the server first, then the handler runs, on the main thread.
    @MainActor func testAnOffersButtonAnswersThenRunsTheHandlerOnTheMainThread() async throws {
        let server = FakeServer(), seen = Seen(), offer = "20261015-144102-a91f"
        server.replies["POST /offers/\(offer)"] = .status(200, "{}")
        server.onRequest = { request in if request.httpMethod == "POST" { seen["answered"] = true } }
        nonisolated(unsafe) let delegate: any UNUserNotificationCenterDelegate = OfferCenter(server: server.client())
        nonisolated(unsafe) let response = try response(OfferCenter.Choice.later.rawValue, ["offer": offer])
        let ran = expectation(description: "the handler ran")
        DispatchQueue.global(qos: .userInitiated).async {
            delegate.userNotificationCenter?(.current(), didReceive: response) {
                seen["main"] = Thread.isMainThread
                seen["answeredFirst"] = seen["answered"] ?? false
                ran.fulfill()
            }
        }
        await fulfillment(of: [ran], timeout: 5)
        XCTAssertEqual(seen["main"], true, "UIKit asserts that a response's handler runs on the main thread")
        XCTAssertEqual(seen["answeredFirst"], true, "iOS keeps the app running until the handler: the answer goes first")
    }

    /// A notification arriving with the app open is shown, and the handler runs on the main thread.
    @MainActor func testANotificationWhileOpenIsShownFromTheMainThread() async throws {
        nonisolated(unsafe) let delegate: any UNUserNotificationCenterDelegate = OfferCenter(server: FakeServer().client())
        nonisolated(unsafe) let notification = try notification(["offer": "20261015-144102-a91f"])
        let seen = Seen(), ran = expectation(description: "the handler ran")
        DispatchQueue.global(qos: .userInitiated).async {
            delegate.userNotificationCenter?(.current(), willPresent: notification) { options in
                seen["main"] = Thread.isMainThread
                seen["shown"] = options == [.banner, .list, .sound]
                ran.fulfill()
            }
        }
        await fulfillment(of: [ran], timeout: 5)
        XCTAssertEqual(seen["main"], true)
        XCTAssertEqual(seen["shown"], true)
    }
}
