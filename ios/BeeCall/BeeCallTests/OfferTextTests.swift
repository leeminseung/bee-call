import XCTest
@testable import BeeCall

/// The line under an offer's title in the app's list.
final class OfferTextTests: XCTestCase {
    /// Noon today, so "today" holds whenever the test runs.
    private let noon = Int(Calendar.current.startOfDay(for: .now).timeIntervalSince1970) + 12 * 3600

    @MainActor private func offer(_ state: String, suggested: Int? = nil, callAt: Int? = nil,
                                  source: String = "bee") -> OfferCenter.Offer {
        OfferCenter.Offer(id: "20261015-2100-1", state: state, topic: "Saturday hike", kind: "conversation",
                          source: source, suggestedEpoch: suggested, callAtEpoch: callAt)
    }

    @MainActor func testAQuestionWithASuggestedTime() {
        XCTAssertEqual(offer("asked", suggested: noon).status,
                       "Bee Call asks: a 5-minute call today at \(OfferCenter.Offer.time(noon))?")
    }

    @MainActor func testAQuestionWithoutATime() {
        XCTAssertEqual(offer("asked").status, "Bee Call asks for a 5-minute call")
    }

    @MainActor func testABookedCall() {
        XCTAssertEqual(offer("booked", callAt: noon).status, "You said yes: today at \(OfferCenter.Offer.time(noon))")
        XCTAssertEqual(offer("booked", callAt: noon, source: "call").status,
                       "You asked for a call back: today at \(OfferCenter.Offer.time(noon))")
    }

    @MainActor func testACallOnItsWay() {
        XCTAssertEqual(offer("calling").status, "Calling you now…")
    }
}
