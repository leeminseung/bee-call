import XCTest
@testable import BeeCall

/// Debug builds answer design D's requests from the contract's example files until the server serves them
/// (ContractExamples). These tests hold it to the files and to the server's answers it stands in for.
final class ContractExamplesTests: XCTestCase {
    @MainActor private func get(_ path: String, from examples: ContractExamples = ContractExamples(),
                                 holding version: String? = nil) async throws -> (Int, Data) {
        var request = URLRequest(url: URL(string: "http://bee-call.test" + path)!)
        if let version { request.setValue("\"\(version)\"", forHTTPHeaderField: "If-None-Match") }
        let (data, response) = try await examples.send(request)
        return (response.httpStatus, data)
    }

    /// The Debug build carries the files themselves, the same bytes as contract/examples.
    @MainActor func testTheDebugBuildCarriesTheContractsExamples() throws {
        for name in ["calls", "call-view", "call-view-live", "call-view-ai", "offers", "voices", "me"] {
            let bundled = try XCTUnwrap(Bundle.main.url(forResource: name, withExtension: "json"), name)
            XCTAssertEqual(try Data(contentsOf: bundled), try ContractTests.example(name), name)
        }
    }

    @MainActor func testTheCallListAndEachViewComeFromTheirFiles() async throws {
        let (status, list) = try await get("/calls")
        XCTAssertEqual(status, 200)
        XCTAssertEqual(try Contract.decode(CallList.self, from: list).calls.count, 6)
        let (ended, after) = try await get("/calls/call-minseung-20261015-012301")
        XCTAssertEqual(ended, 200)
        XCTAssertEqual(try Contract.decode(CallView.self, from: after).call.state, .ended)
        let (_, during) = try await get("/calls/call-minseung-20261015-012301", from: ContractExamples(live: true))
        XCTAssertEqual(try Contract.decode(CallView.self, from: during).call.state, .live)
        let (_, ai) = try await get("/calls/call-minseung-20261014-180158")
        XCTAssertNotNil(try Contract.decode(CallView.self, from: ai).whyCalling)
        let (missing, _) = try await get("/calls/call-minseung-20261015-142209")
        XCTAssertEqual(missing, 404, "a call without an example view")
    }

    @MainActor func testAKnownVersionIsAnswered304() async throws {
        let (status, body) = try await get("/calls", holding: "f46f5990ebfadcab")
        XCTAssertEqual(status, 304)
        XCTAssertTrue(body.isEmpty)
        let (fresh, _) = try await get("/calls", holding: "0000000000000000")
        XCTAssertEqual(fresh, 200)
    }

    @MainActor func testVoicesOffersAndThePerson() async throws {
        let (korean, voices) = try await get("/voices?language=ko")
        XCTAssertEqual(korean, 200)
        XCTAssertEqual(try Contract.decode(VoiceList.self, from: voices).language, .ko)
        let (english, _) = try await get("/voices?language=en")
        XCTAssertEqual(english, 404, "the contract has no English example yet")
        let (_, offers) = try await get("/offers")
        XCTAssertEqual(try Contract.decode(OpenOffers.self, from: offers).offers.count, 2)
        let (_, me) = try await get("/me")
        XCTAssertEqual(try Contract.decode(Me.self, from: me).settings.voice, "ko-harin")
    }

    /// The examples are written for 2026-10-15 in Seoul. Another day moves their times by whole days, so "오늘" and
    /// "어제" read as they would; their clock times stay.
    @MainActor func testTheExamplesTimesMoveToToday() async throws {
        let nextDay = Date(timeIntervalSince1970: 1_792_129_500) // 2026-10-16 14:45 in Seoul
        let (_, body) = try await get("/calls", from: ContractExamples(now: nextDay))
        let list = try Contract.decode(CallList.self, from: body)
        XCTAssertEqual(list.calls.first?.call.startedAt, ZonedTime("2026-10-16T14:22:13+09:00"))
        let (_, view) = try await get("/calls/call-minseung-20261015-012301", from: ContractExamples(now: nextDay))
        XCTAssertEqual(try Contract.decode(CallView.self, from: view).prep?.lookedUp.last, .day(CalendarDay(year: 2026, month: 10, day: 16)))
    }

    /// The live frames are 3:41 into the MacBook call, now: the call began 3:41 ago, and the list and the offers are
    /// as they were then, without what came after.
    @MainActor func testTheLiveFramesAreThreeMinutesIntoTheCall() async throws {
        let now = Date(timeIntervalSince1970: 1_792_065_000)
        let examples = ContractExamples(live: true, now: now)
        let (_, view) = try await get("/calls/call-minseung-20261015-012301", from: examples)
        let started = try Contract.decode(CallView.self, from: view).call.startedAt
        XCTAssertEqual(started.date, now.addingTimeInterval(-221))
        XCTAssertEqual(started.zone.secondsFromGMT(), 9 * 3600)
        let (_, list) = try await get("/calls", from: examples)
        XCTAssertEqual(try Contract.decode(CallList.self, from: list).calls.map(\.call.callID).first,
                       "call-minseung-20261015-012301", "the calls after it are not there yet")
        let (_, offers) = try await get("/offers", from: examples)
        XCTAssertEqual(try Contract.decode(OpenOffers.self, from: offers).offers, [])
    }

    /// A pick shows: the settings change in memory, and a new language without a voice goes to its default.
    @MainActor func testSettingsChangesAreKept() async throws {
        let examples = ContractExamples()
        func put(_ body: [String: Any]) async throws -> PersonSettings {
            var request = URLRequest(url: URL(string: "http://bee-call.test/settings")!)
            request.httpMethod = "PUT"
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            let (data, response) = try await examples.send(request)
            XCTAssertEqual(response.httpStatus, 200)
            return try Contract.decode(SettingsAnswer.self, from: data).settings
        }
        let voice = try await put(["voice": "ko-daon"])
        XCTAssertEqual(voice.voice, "ko-daon")
        let english = try await put(["language": "en"])
        XCTAssertEqual(english.language, .en)
        XCTAssertNil(english.voice, "the new language's default")
        let (_, me) = try await get("/me", from: examples)
        XCTAssertEqual(try Contract.decode(Me.self, from: me).settings.language, .en)
    }
}
