import XCTest
@testable import BeeCall

/// Debug builds answer design D's requests from the contract's example files until the server serves them
/// (ContractExamples). These tests hold it to the files and to the server's answers it stands in for; what to expect
/// is read from the files, which the server's tests regenerate.
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
        let names = try FileManager.default.contentsOfDirectory(at: Examples.folder, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }.map { $0.deletingPathExtension().lastPathComponent }
        XCTAssertFalse(names.isEmpty)
        for name in names {
            let bundled = try XCTUnwrap(Bundle.main.url(forResource: name, withExtension: "json"), name)
            XCTAssertEqual(try Data(contentsOf: bundled), try Examples.data(name), name)
        }
    }

    @MainActor func testTheCallListAndEachViewComeFromTheirFiles() async throws {
        let (status, list) = try await get("/calls")
        XCTAssertEqual(status, 200)
        let calls = try Contract.decode(CallList.self, from: list).calls
        XCTAssertEqual(calls.map(\.id), try Examples.decode(CallList.self, "calls").calls.map(\.id))
        let ended = try Examples.decode(CallView.self, "call-view"), ai = try Examples.decode(CallView.self, "call-view-ai")
        let (status2, after) = try await get("/calls/\(ended.call.callID)")
        XCTAssertEqual(status2, 200)
        XCTAssertEqual(try Contract.decode(CallView.self, from: after).version, ended.version)
        let (_, during) = try await get("/calls/\(ended.call.callID)", from: ContractExamples(live: true))
        XCTAssertEqual(try Contract.decode(CallView.self, from: during).version,
                       try Examples.decode(CallView.self, "call-view-live").version)
        let (_, aiView) = try await get("/calls/\(ai.call.callID)")
        XCTAssertEqual(try Contract.decode(CallView.self, from: aiView).version, ai.version)
        let withoutView = try XCTUnwrap(calls.first { ![ended.call.callID, ai.call.callID].contains($0.id) })
        let (missing, _) = try await get("/calls/\(withoutView.id)")
        XCTAssertEqual(missing, 404, "a call without an example view")
    }

    @MainActor func testAKnownVersionIsAnswered304() async throws {
        let version = try Examples.decode(CallList.self, "calls").version
        let (status, body) = try await get("/calls", holding: version)
        XCTAssertEqual(status, 304)
        XCTAssertTrue(body.isEmpty)
        let (fresh, _) = try await get("/calls", holding: Examples.otherVersion(than: version))
        XCTAssertEqual(fresh, 200)
    }

    @MainActor func testVoicesOffersAndThePerson() async throws {
        for (language, name) in [("ko", "voices"), ("en", "voices-en")] {
            let (status, voices) = try await get("/voices?language=\(language)")
            XCTAssertEqual(status, 200)
            XCTAssertEqual(try Contract.decode(VoiceList.self, from: voices).voices.map(\.id),
                           try Examples.decode(VoiceList.self, name).voices.map(\.id))
        }
        let (unknown, _) = try await get("/voices?language=fr")
        XCTAssertEqual(unknown, 404)
        let (_, offers) = try await get("/offers")
        XCTAssertEqual(try Contract.decode(OpenOffers.self, from: offers).offers.map(\.id),
                       try Examples.decode(OpenOffers.self, "offers").offers.map(\.id))
        let (_, me) = try await get("/me")
        XCTAssertEqual(try Contract.decode(Me.self, from: me).settings, try Examples.decode(Me.self, "me").settings)
        for (role, name) in [("call", "models-call"), ("worker", "models-worker")] {
            let (status, models) = try await get("/models?role=\(role)")
            XCTAssertEqual(status, 200)
            XCTAssertEqual(try Contract.decode(ModelList.self, from: models).models.map(\.id),
                           try Examples.decode(ModelList.self, name).models.map(\.id))
        }
    }

    /// The examples are written for one day in Seoul. Another day moves their times by whole days, so "오늘" and
    /// "어제" read as they would; their clock times stay.
    @MainActor func testTheExamplesTimesMoveToToday() async throws {
        let list = try Examples.decode(CallList.self, "calls"), view = try Examples.decode(CallView.self, "call-view")
        let day = try Examples.decode(CallView.self, "call-view-live").call.startedAt // the examples' day
        let nextDay = try XCTUnwrap(day.calendar.date(byAdding: .day, value: 1, to: day.date))
        let first = try XCTUnwrap(list.calls.first?.call.startedAt)
        let (_, body) = try await get("/calls", from: ContractExamples(now: nextDay))
        let moved = try XCTUnwrap(try Contract.decode(CallList.self, from: body).calls.first?.call.startedAt)
        XCTAssertEqual(moved.date, try XCTUnwrap(first.calendar.date(byAdding: .day, value: 1, to: first.date)))
        XCTAssertEqual(moved.zone, first.zone)

        func days(_ view: CallView) -> [CalendarDay] {
            (view.prep?.lookedUp ?? []).compactMap { looked -> CalendarDay? in
                if case .day(let day) = looked { day } else { nil }
            }
        }
        let original = days(view)
        XCTAssertFalse(original.isEmpty, "the example looked up a day")
        let (_, movedView) = try await get("/calls/\(view.call.callID)", from: ContractExamples(now: nextDay))
        let movedDays = days(try Contract.decode(CallView.self, from: movedView))
        XCTAssertEqual(movedDays.count, original.count)
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = .gmt
        for (movedDay, was) in zip(movedDays, original) {
            let wasDate = try XCTUnwrap(calendar.date(from: DateComponents(year: was.year, month: was.month, day: was.day)))
            let next = calendar.dateComponents([.year, .month, .day],
                                               from: try XCTUnwrap(calendar.date(byAdding: .day, value: 1, to: wasDate)))
            XCTAssertEqual(movedDay, CalendarDay(year: next.year ?? 0, month: next.month ?? 0, day: next.day ?? 0))
        }
    }

    /// The live frames are 3:41 into the live example's call, now: the call began 3:41 ago, and the list and the
    /// offers are as they were then, without what came after.
    @MainActor func testTheLiveFramesAreThreeMinutesIntoTheCall() async throws {
        let live = try Examples.decode(CallView.self, "call-view-live"), now = Date(timeIntervalSince1970: 1_792_065_000)
        let examples = ContractExamples(live: true, now: now)
        let (_, view) = try await get("/calls/\(live.call.callID)", from: examples)
        let started = try Contract.decode(CallView.self, from: view).call.startedAt
        XCTAssertEqual(started.date, now.addingTimeInterval(-221))
        XCTAssertEqual(started.zone, live.call.startedAt.zone)
        let (_, list) = try await get("/calls", from: examples)
        let calls = try Contract.decode(CallList.self, from: list).calls
        let before = try Examples.decode(CallList.self, "calls").calls.filter { $0.call.startedAt <= live.call.startedAt }
        XCTAssertEqual(calls.map(\.id), before.map(\.id), "the calls after it are not there yet")
        let (_, offers) = try await get("/offers", from: examples)
        let earlier = try Examples.decode(OpenOffers.self, "offers").offers.filter { $0.createdAt <= live.call.startedAt }
        XCTAssertEqual(try Contract.decode(OpenOffers.self, from: offers).offers.map(\.id), earlier.map(\.id))
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
        let korean = try Examples.decode(VoiceList.self, "voices").voices
        let other = try XCTUnwrap(korean.first { !$0.isDefault } ?? korean.first)
        let voice = try await put(["voice": other.id])
        XCTAssertEqual(voice.voice, other.id)
        let english = try await put(["language": "en"])
        XCTAssertEqual(english.language, .en)
        XCTAssertNil(english.voice, "the new language's default")
        let (_, me) = try await get("/me", from: examples)
        XCTAssertEqual(try Contract.decode(Me.self, from: me).settings.language, .en)
    }
}
