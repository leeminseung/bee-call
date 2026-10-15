import XCTest
@testable import BeeCall

/// The line under an offer's title in the app's list, while Bee Call asks or calls (booked calls: BookedCallTests).
final class OfferTextTests: XCTestCase {
    /// Noon today, so "today" holds whenever the test runs.
    private let noon = Int(Calendar.current.startOfDay(for: .now).timeIntervalSince1970) + 12 * 3600

    @MainActor private func offer(_ state: String, suggested: Int? = nil) -> OfferCenter.Offer {
        OfferCenter.Offer(id: "20261015-2100-1", state: state, topic: "Saturday hike", kind: "conversation",
                          source: "bee", suggestedEpoch: suggested, callAtEpoch: nil)
    }

    /// No promised length: the server's notification says "a quick call" too (the owner's decision of 2026-10-15).
    @MainActor func testAQuestionWithASuggestedTime() {
        XCTAssertEqual(offer("asked", suggested: noon).status,
                       "Bee Call asks: a quick call today at \(OfferCenter.Offer.time(noon))?")
    }

    @MainActor func testAQuestionWithoutATime() {
        XCTAssertEqual(offer("asked").status, "Bee Call asks for a quick call")
    }

    @MainActor func testACallOnItsWay() {
        XCTAssertEqual(offer("calling").status, "Calling you now…")
    }
}
