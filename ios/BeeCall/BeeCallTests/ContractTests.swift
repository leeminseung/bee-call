import XCTest
@testable import BeeCall

/// The app's side of the D contract (contract/app-contract.md): one Codable type per schema, which decodes every
/// example file the contract keeps. The server's tests regenerate those files from real code, so these tests break
/// when the two sides drift apart.
final class ContractTests: XCTestCase {
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
        let files = try FileManager.default.contentsOfDirectory(at: Examples.folder, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "json" }
        let names = files.map { $0.deletingPathExtension().lastPathComponent }
        for schema in types.keys {
            XCTAssertTrue(names.contains { $0 == schema || $0.hasPrefix(schema + "-") }, "\(schema) has an example")
        }
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

    // MARK: - What the types read, against the files themselves

    /// Every field of every call in the list, as the file has it (the server regenerates the file, so the values
    /// come from it), and the kinds of calls D draws differently are all there.
    @MainActor func testTheCallListReadsWhatTheFileSays() throws {
        let raw = try Examples.object("calls"), list = try Examples.decode(CallList.self, "calls")
        let entries = try XCTUnwrap(raw["calls"] as? [[String: Any]])
        XCTAssertEqual(list.calls.count, entries.count)
        XCTAssertEqual(list.more, raw["more"] as? Bool)
        XCTAssertEqual(list.before, raw["before"] as? String)
        XCTAssertEqual(list.version, raw["version"] as? String)
        for (entry, file) in zip(list.calls, entries) {
            try assertCall(entry.call, readsAs: file)
            XCTAssertEqual(entry.results.map(\.id), (file["results"] as? [[String: Any]])?.compactMap { $0["id"] as? String })
        }
        XCTAssertTrue(list.calls.contains { $0.call.titleSource == .none && $0.call.title == nil }, "a call with no conversation")
        XCTAssertTrue(list.calls.contains { $0.call.state == .missed && $0.call.durationS == nil }, "a missed call")
        XCTAssertTrue(list.calls.contains { $0.call.startedBy == .ai && $0.call.topic != nil }, "Bee Call's own call")
        XCTAssertTrue(list.calls.contains { $0.results.contains { if case .job = $0 { true } else { false } } }, "a job")
    }

    /// A call summary, field by field, as its file has it.
    @MainActor private func assertCall(_ call: CallSummary, readsAs file: [String: Any],
                                       file path: StaticString = #filePath, line: UInt = #line) throws {
        XCTAssertEqual(call.callID, file["call_id"] as? String, file: path, line: line)
        XCTAssertEqual(call.startedAt, (file["started_at"] as? String).flatMap(ZonedTime.init), file: path, line: line)
        XCTAssertEqual(call.startedBy.rawValue, file["started_by"] as? String, file: path, line: line)
        XCTAssertEqual(call.titleSource.rawValue, file["title_source"] as? String, file: path, line: line)
        XCTAssertEqual(call.state.rawValue, file["state"] as? String, file: path, line: line)
        XCTAssertEqual(call.title, file["title"] as? String, file: path, line: line)
        XCTAssertEqual(call.topic, file["topic"] as? String, file: path, line: line)
        XCTAssertEqual(call.durationS, file["duration_s"] as? Int, file: path, line: line)
        XCTAssertEqual(call.continues, file["continues"] as? String, file: path, line: line)
        let marks = try XCTUnwrap(file["marks"] as? [String: Any], file: path, line: line)
        XCTAssertEqual(call.marks, Marks(report: marks["report"] as? Bool ?? false, table: marks["table"] as? Bool ?? false,
                                         chart: marks["chart"] as? Bool ?? false, beeWrites: marks["bee_writes"] as? Int ?? -1),
                       file: path, line: line)
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

    /// Every item of the three views, in the file's order, field by field; and what the contract says of each view:
    /// the live call's running job comes last, only Bee Call's own call says why it called.
    @MainActor func testTheCallViewsReadTheirItemsInTheFilesOrder() throws {
        for name in ["call-view", "call-view-live", "call-view-ai"] {
            let raw = try Examples.object(name), view = try Examples.decode(CallView.self, name)
            let items = try XCTUnwrap(raw["items"] as? [[String: Any]])
            XCTAssertEqual(view.items.map(\.id), items.compactMap { $0["id"] as? String }, name)
            XCTAssertEqual(view.drawnItems.count, items.count, "every item of \(name) is one this build draws")
            XCTAssertEqual(view.version, raw["version"] as? String)
            let counts = try XCTUnwrap(raw["captions_included"] as? [String: Any])
            XCTAssertEqual(view.captionsIncluded, CaptionCounts(person: counts["person"] as? Int ?? -1, ai: counts["ai"] as? Int ?? -1))
            try assertCall(view.call, readsAs: try XCTUnwrap(raw["call"] as? [String: Any]))
            XCTAssertEqual(view.whyCalling == nil, raw["why_calling"] is NSNull, name)
            XCTAssertEqual(view.whyCalling != nil, view.call.startedBy == .ai, "only Bee Call's own call says why")
            if let why = view.whyCalling, let rawWhy = raw["why_calling"] as? [String: Any] {
                XCTAssertEqual(why.reason, rawWhy["reason"] as? String)
                let conversation = rawWhy["conversation"] as? [String: Any]
                let quotes = conversation?["quotes"] as? [[String: Any]] ?? []
                XCTAssertEqual(why.conversation?.summary, conversation?["summary"] as? String)
                XCTAssertEqual(why.conversation?.quotes.map(\.speaker.rawValue) ?? [], quotes.compactMap { $0["speaker"] as? String })
                XCTAssertEqual(why.conversation?.quotes.map(\.text) ?? [], quotes.compactMap { $0["text"] as? String })
                XCTAssertEqual(why.conversation?.quotes.map(\.at) ?? [], quotes.map { ($0["at"] as? String).flatMap(ZonedTime.init) })
            }
            XCTAssertEqual(view.prep == nil, raw["prep"] is NSNull, name)
            if let prep = view.prep, let rawPrep = raw["prep"] as? [String: Any] {
                XCTAssertEqual(prep.note, rawPrep["note"] as? String)
                XCTAssertEqual(prep.fromBee, rawPrep["from_bee"] as? Bool)
                let looked = rawPrep["looked_up"] as? [[String: Any]] ?? []
                XCTAssertEqual(prep.lookedUp, looked.map { entry -> LookedUp in
                    let type = entry["type"] as? String ?? ""
                    return switch type {
                    case "conversation": .conversation(summary: entry["summary"] as? String,
                                                       at: (entry["at"] as? String).flatMap(ZonedTime.init))
                    case "day": (entry["date"] as? String).flatMap(CalendarDay.init).map(LookedUp.day) ?? .unrecognized(type: type)
                    case "search": .search(query: entry["query"] as? String ?? "")
                    default: .unrecognized(type: type)
                    }
                }, name)
            }
            for (item, file) in zip(view.items, items) {
                switch item {
                case .speech(let speech):
                    XCTAssertEqual(file["type"] as? String, "speech")
                    XCTAssertEqual(speech.speaker.rawValue, file["speaker"] as? String)
                    XCTAssertEqual(speech.text, file["text"] as? String)
                    XCTAssertEqual(speech.interrupted, file["interrupted"] as? Bool)
                case .job(let job):
                    XCTAssertEqual(file["type"] as? String, "job")
                    XCTAssertEqual(job.state.rawValue, file["state"] as? String)
                    XCTAssertEqual(job.title, file["title"] as? String)
                    XCTAssertEqual(job.durationS, file["duration_s"] as? Int)
                    XCTAssertEqual(job.step, file["step"] as? String)
                    XCTAssertEqual(job.summary, file["summary"] as? String)
                    XCTAssertEqual(job.failure?.rawValue, file["failure"] as? String)
                    let files = file["files"] as? [[String: Any]] ?? []
                    XCTAssertEqual(job.files.map(\.url), files.compactMap { $0["url"] as? String })
                    XCTAssertEqual(job.files.map(\.kind.rawValue), files.compactMap { $0["kind"] as? String })
                    XCTAssertEqual(job.files.map(\.imageURL), files.map { $0["image_url"] as? String })
                    XCTAssertEqual(job.files.map(\.imageURLDark), files.map { $0["image_url_dark"] as? String })
                    XCTAssertEqual(job.files.map(\.pageURL), files.map { $0["page_url"] as? String })
                case .beeWrite(let write):
                    XCTAssertEqual(file["type"] as? String, "bee_write")
                    XCTAssertEqual(write.kind.rawValue, file["kind"] as? String)
                    XCTAssertEqual(write.state.rawValue, file["state"] as? String)
                    XCTAssertEqual(write.text, file["text"] as? String)
                    XCTAssertEqual(write.undoable, file["undoable"] as? Bool)
                    XCTAssertEqual(write.willRetry, file["will_retry"] as? Bool)
                case .unrecognized(let type, _):
                    XCTFail("\(name) has an item of a type this build does not know: \(type)")
                }
            }
            let running = view.items.filter { if case .job(let job) = $0 { job.state == .running } else { false } }
            if let last = running.last { XCTAssertEqual(view.items.last, last, "a running job comes last (2.5)") }
            XCTAssertEqual(!running.isEmpty, view.call.state == .live, "\(name): a running job only while the call is on")
        }
    }

    /// The offers, the voices, the person and a ring, as their files say; and what the contract says of them: one
    /// default voice and it comes first, an asked offer has a suggested time and no call time yet.
    @MainActor func testOffersVoicesPersonAndRingReadWhatTheFilesSay() throws {
        let rawOffers = try XCTUnwrap(Examples.object("offers")["offers"] as? [[String: Any]])
        let offers = try Examples.decode(OpenOffers.self, "offers").offers
        XCTAssertEqual(offers.map(\.id), rawOffers.compactMap { $0["id"] as? String })
        XCTAssertEqual(offers.map(\.state.rawValue), rawOffers.compactMap { $0["state"] as? String })
        XCTAssertEqual(offers.map(\.topic), rawOffers.compactMap { $0["topic"] as? String })
        XCTAssertEqual(offers.map(\.callAt), rawOffers.map { ($0["call_at"] as? String).flatMap(ZonedTime.init) })
        XCTAssertEqual(offers.map(\.suggestedAt), rawOffers.map { ($0["suggested_at"] as? String).flatMap(ZonedTime.init) })
        for offer in offers where offer.state == .asked {
            XCTAssertNotNil(offer.suggestedAt)
            XCTAssertNil(offer.callAt)
        }
        for name in ["voices", "voices-en"] {
            let raw = try Examples.object(name), list = try Examples.decode(VoiceList.self, name)
            let rawVoices = try XCTUnwrap(raw["voices"] as? [[String: Any]])
            XCTAssertEqual(list.language.rawValue, raw["language"] as? String)
            XCTAssertEqual(list.voices.map(\.id), rawVoices.compactMap { $0["id"] as? String })
            XCTAssertEqual(list.voices.map(\.sampleURL), rawVoices.compactMap { $0["sample_url"] as? String })
            XCTAssertEqual(list.voices.map(\.character.ko), rawVoices.compactMap { ($0["character"] as? [String: Any])?["ko"] as? String })
            XCTAssertEqual(list.voices.filter(\.isDefault).count, 1, "\(name): exactly one default")
            XCTAssertEqual(list.voices.first?.isDefault, true, "\(name): the default first")
            XCTAssertTrue((1...12).contains(list.voices.count))
        }
        let rawSettings = try XCTUnwrap(Examples.object("me")["settings"] as? [String: Any])
        let me = try Examples.decode(Me.self, "me")
        XCTAssertEqual(me.settings.language.rawValue, rawSettings["language"] as? String)
        XCTAssertEqual(me.settings.voice, rawSettings["voice"] as? String)
        XCTAssertEqual(me.settings.callModel, rawSettings["call_model"] as? String)
        XCTAssertEqual(me.settings.workerModel, rawSettings["worker_model"] as? String)
        XCTAssertEqual(me.settings.noiseCancellation, rawSettings["noise_cancellation"] as? Bool)
        XCTAssertEqual(me.settings.bee, rawSettings["bee"] as? Bool)
        XCTAssertEqual(me.beeConnecting, try Examples.object("me")["bee_connecting"] as? Bool)
        let ring = try Examples.decode(RingEvent.self, "ring"), rawRing = try Examples.object("ring")
        XCTAssertEqual(ring.expiresAt, (rawRing["expires_at"] as? String).flatMap(ZonedTime.init))
        XCTAssertEqual(ring.room, rawRing["room"] as? String)
    }

    /// GET /models (section 5b): each role's choices as the file has them, one default, first; a role may list one.
    @MainActor func testTheModelListsReadWhatTheFilesSay() throws {
        for (name, role) in [("models-call", ModelRole.call), ("models-worker", .worker)] {
            let raw = try Examples.object(name), list = try Examples.decode(ModelList.self, name)
            let models = try XCTUnwrap(raw["models"] as? [[String: Any]])
            XCTAssertEqual(list.role, role)
            XCTAssertEqual(list.models.map(\.id), models.compactMap { $0["id"] as? String })
            XCTAssertEqual(list.models.map(\.name), models.compactMap { $0["name"] as? String })
            XCTAssertEqual(list.models.map(\.maker), models.compactMap { $0["maker"] as? String })
            XCTAssertEqual(list.models.map(\.description.en),
                           models.compactMap { ($0["description"] as? [String: Any])?["en"] as? String })
            XCTAssertEqual(list.models.filter(\.isDefault).count, 1, "\(name): exactly one default")
            XCTAssertEqual(list.models.first?.isDefault, true, "\(name): the default first")
        }
    }

    // MARK: - A later server

    /// A value a later server adds to a closed list decodes instead of failing, and the item it marks is skipped.
    @MainActor func testUnknownValuesDecodeAndUnknownItemsAreSkipped() throws {
        var body = try Examples.object("call-view")
        var items = try XCTUnwrap(body["items"] as? [[String: Any]])
        items.append(["type": "photo", "id": "photo-1", "at": "2026-10-15T01:31:00+09:00", "url": "/x"])
        let speech = try XCTUnwrap(items.firstIndex { $0["type"] as? String == "speech" })
        items[speech]["speaker"] = "narrator"
        body["items"] = items
        var call = try XCTUnwrap(body["call"] as? [String: Any])
        call["state"] = "paused"
        body["call"] = call
        let view = try Contract.decode(CallView.self, from: JSONSerialization.data(withJSONObject: body))
        XCTAssertEqual(view.call.state, .unrecognized)
        XCTAssertEqual(view.items.count, items.count)
        XCTAssertEqual(view.items.last, .unrecognized(type: "photo", id: "photo-1"))
        XCTAssertEqual(view.drawnItems.count, items.count - 2,
                       "the unknown item and the speech of an unknown speaker are not drawn")
    }

    /// Section 0: every key a schema requires is always present, null when it does not apply. A required key that
    /// is missing (a renamed field, as `call_at_epoch` once was) fails instead of reading as null (second review of
    /// the D wiring, finding 8).
    @MainActor func testAMissingNullableKeyFails() throws {
        func without(_ name: String, _ remove: (inout [String: Any]) -> Void) throws -> Data {
            var body = try Examples.object(name)
            remove(&body)
            return try JSONSerialization.data(withJSONObject: body)
        }
        XCTAssertThrowsError(try Contract.decode(CallView.self, from: without("call-view") { body in
            var call = body["call"] as? [String: Any] ?? [:]
            call["continue"] = call.removeValue(forKey: "continues") // renamed
            body["call"] = call
        }))
        XCTAssertThrowsError(try Contract.decode(CallView.self, from: without("call-view") { body in
            var items = body["items"] as? [[String: Any]] ?? []
            if let job = items.firstIndex(where: { $0["type"] as? String == "job" }) { items[job].removeValue(forKey: "failure") }
            body["items"] = items
        }))
        XCTAssertThrowsError(try Contract.decode(CallView.self, from: without("call-view") { $0.removeValue(forKey: "why_calling") }))
        XCTAssertThrowsError(try Contract.decode(CallView.self, from: without("call-view") { body in
            var items = body["items"] as? [[String: Any]] ?? []
            guard let job = items.firstIndex(where: { ($0["files"] as? [[String: Any]])?.isEmpty == false }) else { return }
            var files = items[job]["files"] as? [[String: Any]] ?? []
            files[0].removeValue(forKey: "image_url_dark") // a server from before dark charts
            items[job]["files"] = files
            body["items"] = items
        }), "image_url_dark is required, null when there is none")
        XCTAssertThrowsError(try Contract.decode(CallView.self, from: without("call-view") { body in
            var items = body["items"] as? [[String: Any]] ?? []
            guard let job = items.firstIndex(where: { ($0["files"] as? [[String: Any]])?.isEmpty == false }) else { return }
            var files = items[job]["files"] as? [[String: Any]] ?? []
            files[0].removeValue(forKey: "page_url") // a server from before report pages
            items[job]["files"] = files
            body["items"] = items
        }), "page_url is required, null when there is none")
        XCTAssertThrowsError(try Contract.decode(OpenOffers.self, from: without("offers") { body in
            var offers = body["offers"] as? [[String: Any]] ?? []
            offers[0].removeValue(forKey: "suggested_at")
            body["offers"] = offers
        }))
        XCTAssertThrowsError(try Contract.decode(Me.self, from: without("me") { body in
            var settings = body["settings"] as? [String: Any] ?? [:]
            settings.removeValue(forKey: "voice")
            body["settings"] = settings
        }))
        XCTAssertThrowsError(try Contract.decode(Me.self, from: without("me") { body in
            var settings = body["settings"] as? [String: Any] ?? [:]
            settings.removeValue(forKey: "noise_cancellation") // a server from before the switch
            body["settings"] = settings
        }), "noise_cancellation is required")
        XCTAssertThrowsError(try Contract.decode(CallList.self, from: without("calls") { $0.removeValue(forKey: "before") }))
    }

    @MainActor func testABodyThatBreaksTheContractFails() throws {
        var body = try Examples.object("calls")
        var calls = try XCTUnwrap(body["calls"] as? [[String: Any]])
        calls[0]["started_at"] = "2026-10-15T14:22:13" // no offset
        body["calls"] = calls
        XCTAssertThrowsError(try Contract.decode(CallList.self, from: JSONSerialization.data(withJSONObject: body)))
    }
}
