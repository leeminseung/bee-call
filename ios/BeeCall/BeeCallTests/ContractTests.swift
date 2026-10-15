import XCTest
@testable import BeeCall

/// The app's side of the D contract (contract/app-contract.md): one Codable type per schema, which decodes every
/// example file the contract keeps. The server's tests regenerate those files from real code, so these tests break
/// when the two sides drift apart.
final class ContractTests: XCTestCase {
    /// contract/examples in this repository, read straight from the source tree.
    static let examples = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().appending(path: "contract/examples")

    static func example(_ name: String) throws -> Data {
        try Data(contentsOf: examples.appending(path: "\(name).json"))
    }

    /// Every example decodes with the type of its schema: the schema whose name is the longest prefix of the
    /// example's name, as contract/check.py pairs them. A new example without a type here fails.
    @MainActor func testEveryExampleDecodes() throws {
        let types: [String: (Data) throws -> Any] = [
            "calls": { try Contract.decode(CallList.self, from: $0) },
            "call-view": { try Contract.decode(CallView.self, from: $0) },
            "change": { try Contract.decode(Change.self, from: $0) },
            "me": { try Contract.decode(Me.self, from: $0) },
            "offers": { try Contract.decode(OpenOffers.self, from: $0) },
            "offer-answer": { try Contract.decode(OfferAnswerBody.self, from: $0) },
            "settings": { try Contract.decode(SettingsAnswer.self, from: $0) },
            "voices": { try Contract.decode(VoiceList.self, from: $0) },
            "models": { try Contract.decode(ModelList.self, from: $0) },
            "ring": { try Contract.decode(RingEvent.self, from: $0) },
            "token": { try Contract.decode(CallPass.self, from: $0) },
            "auth-apple": { try Contract.decode(AppleSignInAnswer.self, from: $0) },
            "auth-nonce": { try Contract.decode(Nonce.self, from: $0) },
            "auth-waiting": { try Contract.decode(Waiting.self, from: $0) },
            "bee-connect": { try Contract.decode(BeeConnectLink.self, from: $0) },
        ]
        let files = try FileManager.default.contentsOfDirectory(at: Self.examples, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
        XCTAssertGreaterThanOrEqual(files.count, 18) // one per schema, three call views and two model lists
        for file in files {
            let name = file.deletingPathExtension().lastPathComponent
            let schema = types.keys.filter { name == $0 || name.hasPrefix($0 + "-") }.max { $0.count < $1.count }
            guard let schema, let decode = types[schema] else {
                XCTFail("\(file.lastPathComponent) has no type in the app")
                continue
            }
            XCTAssertNoThrow(try decode(Data(contentsOf: file)), file.lastPathComponent)
        }
    }

    // MARK: - What the types read

    @MainActor func testTheCallListReadsEveryKindOfCall() throws {
        let list = try Contract.decode(CallList.self, from: Self.example("calls"))
        XCTAssertEqual(list.calls.count, 6)
        XCTAssertTrue(list.more)
        XCTAssertNotNil(list.before)
        XCTAssertEqual(list.version, "f46f5990ebfadcab")
        let macbook = list.calls[1]
        XCTAssertEqual(macbook.call.callID, "call-minseung-20261015-012301")
        XCTAssertEqual(macbook.call.startedBy, .person)
        XCTAssertEqual(macbook.call.titleSource, .summary)
        XCTAssertEqual(macbook.call.state, .ended)
        XCTAssertEqual(macbook.call.durationS, 460)
        XCTAssertEqual(macbook.call.marks, Marks(report: true, table: true, chart: true, beeWrites: 1))
        XCTAssertEqual(macbook.results.map(\.id), ["write-w1", "job-01"])
        XCTAssertEqual(list.calls[2].call.titleSource, .none)
        XCTAssertNil(list.calls[2].call.title)
        XCTAssertEqual(list.calls[5].call.state, .missed)
        XCTAssertNil(list.calls[5].call.durationS)
        XCTAssertEqual(list.calls[3].call.topic, "성현이와 토요일 촬영 이야기")
    }

    /// Times keep the person's UTC offset, so the app shows them on the person's clock.
    @MainActor func testTimesKeepTheirOffset() throws {
        let time = try XCTUnwrap(ZonedTime("2026-10-15T01:23:05+09:00"))
        XCTAssertEqual(time.date, Date(timeIntervalSince1970: 1_791_994_985)) // 2026-10-14T16:23:05Z
        XCTAssertEqual(time.zone.secondsFromGMT(), 9 * 3600)
        XCTAssertEqual(ZonedTime("2026-10-14T21:45:06-04:30")?.zone.secondsFromGMT(), -(4 * 3600 + 1800))
        XCTAssertNil(ZonedTime("2026-10-15T01:23:05"), "a time without its offset breaks the contract")
        XCTAssertNil(ZonedTime("2026-10-15T01:23:05Z"))
        XCTAssertNil(ZonedTime("1760459000"))
        XCTAssertEqual(CalendarDay("2026-10-15"), CalendarDay(year: 2026, month: 10, day: 15))
        XCTAssertNil(CalendarDay("2026-10-15T00:00:00+09:00"))
    }

    @MainActor func testTheCallViewReadsItsItemsInOrder() throws {
        let view = try Contract.decode(CallView.self, from: Self.example("call-view"))
        XCTAssertEqual(view.call.title, "맥북 에어와 프로, 코딩용으로 뭐가 나을까")
        XCTAssertNil(view.whyCalling)
        XCTAssertEqual(view.captionsIncluded, CaptionCounts(person: 12, ai: 11))
        let prep = try XCTUnwrap(view.prep)
        XCTAssertTrue(prep.fromBee)
        XCTAssertEqual(prep.lookedUp, [.conversation(summary: "성현 님과의 대화", at: ZonedTime("2026-10-13T18:40:00+09:00")),
                                       .day(CalendarDay(year: 2026, month: 10, day: 15))])
        XCTAssertEqual(view.items.count, 23)
        guard case .speech(let first) = view.items[0] else { return XCTFail("the first item is speech") }
        XCTAssertEqual(first.speaker, .ai)
        XCTAssertFalse(first.interrupted)
        guard case .beeWrite(let write) = view.items[4] else { return XCTFail("the fifth item is the Bee write") }
        XCTAssertEqual(write.kind, .todoCreate)
        XCTAssertEqual(write.state, .written)
        guard case .job(let job) = view.items[15] else { return XCTFail("the job sits where it finished") }
        XCTAssertEqual(job.state, .done)
        XCTAssertEqual(job.durationS, 87)
        XCTAssertEqual(job.files.map(\.kind), [.chart, .report, .table])
        XCTAssertEqual(job.files[0].imageURL, "/calls/call-minseung-20261015-012301/files/jobs/01/price-chart.png")
        XCTAssertNil(job.failure)
    }

    @MainActor func testTheLiveViewHasARunningJobAndUndoableWrite() throws {
        let view = try Contract.decode(CallView.self, from: Self.example("call-view-live"))
        XCTAssertEqual(view.call.state, .live)
        XCTAssertEqual(view.call.titleSource, .firstSentence)
        guard case .job(let job) = view.items.last else { return XCTFail("the running job comes last") }
        XCTAssertEqual(job.state, .running)
        XCTAssertEqual(job.step, "애플 한국 가격표 읽는 중")
        XCTAssertNil(job.durationS)
        guard case .beeWrite(let write) = view.items[4] else { return XCTFail("the Bee write") }
        XCTAssertTrue(write.undoable)
    }

    @MainActor func testAnAICallsViewSaysWhyItCalled() throws {
        let view = try Contract.decode(CallView.self, from: Self.example("call-view-ai"))
        let why = try XCTUnwrap(view.whyCalling)
        XCTAssertNotNil(why.reason)
        XCTAssertEqual(why.conversation?.quotes.map(\.speaker), [.other, .person])
        XCTAssertEqual(view.prep?.lookedUp.last, .search(query: "토요일 촬영 장소"))
        guard case .speech(let cut) = view.items[2] else { return XCTFail("speech") }
        XCTAssertTrue(cut.interrupted)
    }

    @MainActor func testOffersVoicesAndSettingsRead() throws {
        let offers = try Contract.decode(OpenOffers.self, from: Self.example("offers"))
        XCTAssertEqual(offers.offers.map(\.state), [.booked, .asked])
        XCTAssertEqual(offers.offers[0].callAt, ZonedTime("2026-10-15T19:00:00+09:00"))
        XCTAssertNil(offers.offers[0].suggestedAt)
        XCTAssertEqual(offers.offers[1].suggestedAt, ZonedTime("2026-10-15T20:30:00+09:00"))
        let voices = try Contract.decode(VoiceList.self, from: Self.example("voices"))
        XCTAssertEqual(voices.language, .ko)
        XCTAssertEqual(voices.voices.first?.isDefault, true)
        XCTAssertEqual(voices.voices.first?.character, VoiceCharacter(ko: "친근함", en: "Friendly"))
        let me = try Contract.decode(Me.self, from: Self.example("me"))
        XCTAssertEqual(me.settings.callModel, "haiku")
        XCTAssertEqual(me.settings.workerModel, "sonnet")
        XCTAssertEqual(me.settings.language, .ko)
        XCTAssertEqual(me.settings.voice, "ko-harin")
        XCTAssertTrue(me.settings.bee)
        XCTAssertFalse(me.beeConnecting)
        let ring = try Contract.decode(RingEvent.self, from: Self.example("ring"))
        XCTAssertEqual(ring.expiresAt, ZonedTime("2026-10-15T20:31:02+09:00"))
    }

    /// GET /models (section 5b): each role's choices, the default first, named and described by the server.
    @MainActor func testTheModelListsRead() throws {
        let call = try Contract.decode(ModelList.self, from: Self.example("models-call"))
        XCTAssertEqual(call.role, .call)
        XCTAssertEqual(call.models.map(\.id), ["haiku", "sonnet"])
        XCTAssertEqual(call.models.first?.isDefault, true)
        XCTAssertEqual(call.models.first?.name, "Claude Haiku 5.5")
        XCTAssertEqual(call.models.first?.maker, "Anthropic")
        XCTAssertEqual(call.models.first?.description, ModelDescription(ko: "가장 빠르게 대답해요", en: "Answers fastest"))
        let worker = try Contract.decode(ModelList.self, from: Self.example("models-worker"))
        XCTAssertEqual(worker.role, .worker)
        XCTAssertEqual(worker.models.map(\.id), ["sonnet", "glm"])
    }

    // MARK: - A later server

    /// A value a later server adds to a closed list decodes instead of failing, and the item it marks is skipped.
    @MainActor func testUnknownValuesDecodeAndUnknownItemsAreSkipped() throws {
        var body = try XCTUnwrap(JSONSerialization.jsonObject(with: Self.example("call-view")) as? [String: Any])
        var items = try XCTUnwrap(body["items"] as? [[String: Any]])
        items.append(["type": "photo", "id": "photo-1", "at": "2026-10-15T01:31:00+09:00", "url": "/x"])
        items[0]["speaker"] = "narrator"
        body["items"] = items
        var call = try XCTUnwrap(body["call"] as? [String: Any])
        call["state"] = "paused"
        body["call"] = call
        let view = try Contract.decode(CallView.self, from: JSONSerialization.data(withJSONObject: body))
        XCTAssertEqual(view.call.state, .unrecognized)
        XCTAssertEqual(view.items.count, 24)
        XCTAssertEqual(view.items.last, .unrecognized(type: "photo", id: "photo-1"))
        XCTAssertEqual(view.drawnItems.count, 22, "the unknown item and the speech of an unknown speaker are not drawn")
    }

    @MainActor func testABodyThatBreaksTheContractFails() throws {
        var body = try XCTUnwrap(JSONSerialization.jsonObject(with: Self.example("calls")) as? [String: Any])
        var calls = try XCTUnwrap(body["calls"] as? [[String: Any]])
        calls[0]["started_at"] = "2026-10-15T14:22:13" // no offset
        body["calls"] = calls
        XCTAssertThrowsError(try Contract.decode(CallList.self, from: JSONSerialization.data(withJSONObject: body)))
    }
}
