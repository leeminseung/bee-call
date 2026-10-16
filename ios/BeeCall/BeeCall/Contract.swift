import Foundation

/// The bodies the token server sends for design D, one type per schema of contract/app-contract.md (contract/schemas).
/// The server builds the call view and the app only draws it, so these types hold no rules of their own beyond
/// decoding. Field names are the contract's, in snake_case on the wire. A value one of the contract's closed lists
/// does not have yet decodes as `unrecognized` instead of failing, and is skipped where it would be drawn, so a
/// later server can add one without breaking this build (section 0, Enumerations).
enum Contract {
    static func decode<Body: Decodable>(_ type: Body.Type, from data: Data) throws -> Body {
        try JSONDecoder().decode(type, from: data)
    }
}

// MARK: - Shared values

/// A moment as the server writes it, "2026-10-15T01:23:05+09:00", to the second and with the UTC offset of the
/// person's time zone, which the app keeps to show times on the person's own clock (section 0, Times).
nonisolated struct ZonedTime: Decodable, Hashable, Comparable {
    let date: Date
    let zone: TimeZone

    init?(_ text: String) {
        guard let match = text.wholeMatch(of: /(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})([+-])(\d{2}):(\d{2})/),
              let hours = Int(match.8), let minutes = Int(match.9),
              let zone = TimeZone(secondsFromGMT: (hours * 3600 + minutes * 60) * (match.7 == "-" ? -1 : 1))
        else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let parts = DateComponents(year: Int(match.1), month: Int(match.2), day: Int(match.3), hour: Int(match.4),
                                   minute: Int(match.5), second: Int(match.6))
        guard let date = calendar.date(from: parts) else { return nil }
        self.date = date
        self.zone = zone
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        let text = try container.decode(String.self)
        guard let time = ZonedTime(text) else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Not a time with its offset: \(text)")
        }
        self = time
    }

    /// The calendar of the person's clock.
    var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        return calendar
    }

    static func < (lhs: ZonedTime, rhs: ZonedTime) -> Bool {
        lhs.date < rhs.date
    }
}

/// A calendar day, "2026-10-15".
nonisolated struct CalendarDay: Decodable, Hashable {
    let year: Int
    let month: Int
    let day: Int

    init(year: Int, month: Int, day: Int) {
        self.year = year
        self.month = month
        self.day = day
    }

    init?(_ text: String) {
        guard let match = text.wholeMatch(of: /(\d{4})-(\d{2})-(\d{2})/), let year = Int(match.1),
              let month = Int(match.2), let day = Int(match.3) else { return nil }
        self.init(year: year, month: month, day: day)
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        let text = try container.decode(String.self)
        guard let day = CalendarDay(text) else {
            throw DecodingError.dataCorruptedError(in: container, debugDescription: "Not a calendar day: \(text)")
        }
        self = day
    }
}

/// A field the contract always sends, whose value may be null (section 0, Bodies: "Every key a schema requires is
/// always present. A value that does not apply is null, never left out."). Decoding needs the key, so a renamed or
/// missing field fails instead of reading as null, as `call_at_epoch` once read as no time at all.
@propertyWrapper
nonisolated struct Present<Value: Decodable & Hashable>: Decodable, Hashable {
    var wrappedValue: Value?

    init(wrappedValue: Value?) {
        self.wrappedValue = wrappedValue
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.singleValueContainer()
        wrappedValue = container.decodeNil() ? nil : try container.decode(Value.self)
    }
}

/// A value from one of the contract's closed lists. One this build does not know decodes as `unrecognized`.
nonisolated protocol ContractValue: RawRepresentable<String>, Decodable, Hashable {
    static var unrecognized: Self { get }
}

extension ContractValue {
    nonisolated init(from decoder: any Decoder) throws {
        self = Self(rawValue: try decoder.singleValueContainer().decode(String.self)) ?? .unrecognized
    }
}

/// Who placed a call, and who said a piece of speech.
nonisolated enum Speaker: String, ContractValue {
    case person, ai, unrecognized
}

// MARK: - The call list and the call view (sections 1 and 2)

nonisolated enum TitleSource: String, ContractValue {
    case summary, topic, firstSentence = "first_sentence", none, unrecognized
}

nonisolated enum CallState: String, ContractValue {
    case live, ended, dropped, missed, declined, unrecognized
}

/// What a call's finished jobs made, and how many of its Bee writes are in Bee now.
nonisolated struct Marks: Decodable, Hashable {
    let report: Bool
    let table: Bool
    let chart: Bool
    let beeWrites: Int

    enum CodingKeys: String, CodingKey {
        case report, table, chart
        case beeWrites = "bee_writes"
    }
}

/// A call as the list and the view both describe it (section 1).
nonisolated struct CallSummary: Decodable, Hashable, Identifiable {
    let callID: String
    let startedAt: ZonedTime
    let startedBy: Speaker
    @Present var topic: String?
    @Present var title: String?
    let titleSource: TitleSource
    let state: CallState
    @Present var durationS: Int?
    @Present var continues: String?
    let marks: Marks

    var id: String { callID }

    enum CodingKeys: String, CodingKey {
        case callID = "call_id"
        case startedAt = "started_at"
        case startedBy = "started_by"
        case topic, title
        case titleSource = "title_source"
        case state
        case durationS = "duration_s"
        case continues, marks
    }
}

/// A call in the list: its summary, and its jobs and Bee writes for the folded section.
nonisolated struct CallEntry: Decodable, Hashable, Identifiable {
    let call: CallSummary
    let results: [CallItem]

    var id: String { call.callID }

    init(call: CallSummary, results: [CallItem]) {
        self.call = call
        self.results = results
    }

    init(from decoder: any Decoder) throws {
        call = try CallSummary(from: decoder) // one flat object: the summary's fields and `results`
        results = try decoder.container(keyedBy: CodingKeys.self).decode([CallItem].self, forKey: .results)
    }

    private enum CodingKeys: String, CodingKey {
        case results
    }
}

/// GET /calls: newest first.
nonisolated struct CallList: Decodable, Hashable {
    let calls: [CallEntry]
    let more: Bool
    @Present var before: String?
    let version: String
}

/// How many caption lines of each speaker the view's items already hold (section 3).
nonisolated struct CaptionCounts: Decodable, Hashable {
    let person: Int
    let ai: Int
}

/// GET /calls/{call_id}: one call as its thread.
nonisolated struct CallView: Decodable, Hashable {
    let call: CallSummary
    @Present var whyCalling: WhyCalling?
    @Present var prep: Prep?
    let items: [CallItem]
    let captionsIncluded: CaptionCounts
    let version: String

    /// The items this build can draw, in the server's order.
    var drawnItems: [CallItem] {
        items.filter(\.isDrawn)
    }

    enum CodingKeys: String, CodingKey {
        case call
        case whyCalling = "why_calling"
        case prep, items
        case captionsIncluded = "captions_included"
        case version
    }
}

/// One item of a call's thread (2.4).
nonisolated enum CallItem: Decodable, Hashable, Identifiable {
    case speech(Speech)
    case job(Job)
    case beeWrite(BeeWrite)
    case unrecognized(type: String, id: String)

    var id: String {
        switch self {
        case .speech(let speech): speech.id
        case .job(let job): job.id
        case .beeWrite(let write): write.id
        case .unrecognized(let type, let id): "\(type)/\(id)"
        }
    }

    /// Whether this build can draw it: a known kind with known values where the drawing depends on them.
    var isDrawn: Bool {
        switch self {
        case .speech(let speech): speech.speaker != .unrecognized
        case .job(let job): job.state != .unrecognized
        case .beeWrite(let write): write.state != .unrecognized && write.kind != .unrecognized
        case .unrecognized: false
        }
    }

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: Kind.self)
        switch try container.decode(String.self, forKey: .type) {
        case "speech": self = .speech(try Speech(from: decoder))
        case "job": self = .job(try Job(from: decoder))
        case "bee_write": self = .beeWrite(try BeeWrite(from: decoder))
        case let other: self = .unrecognized(type: other, id: (try? container.decode(String.self, forKey: .id)) ?? "")
        }
    }

    private enum Kind: String, CodingKey {
        case type, id
    }
}

/// One bubble: a run of one speaker's speech.
nonisolated struct Speech: Decodable, Hashable {
    let id: String
    let at: ZonedTime
    let speaker: Speaker
    let text: String
    let interrupted: Bool
}

nonisolated enum JobState: String, ContractValue {
    case running, done, failed, unrecognized
}

nonisolated enum JobFailure: String, ContractValue {
    case callEnded = "call_ended", error, unrecognized
}

/// A background job: running with its newest step, or finished with what it found and made.
nonisolated struct Job: Decodable, Hashable {
    let id: String
    let at: ZonedTime
    let title: String
    let state: JobState
    let startedAt: ZonedTime
    @Present var durationS: Int?
    @Present var step: String?
    @Present var summary: String?
    let files: [JobFile]
    @Present var failure: JobFailure?

    enum CodingKeys: String, CodingKey {
        case id, at, title, state
        case startedAt = "started_at"
        case durationS = "duration_s"
        case step, summary, files, failure
    }
}

nonisolated enum FileKind: String, ContractValue {
    case report, table, chart, other, unrecognized
}

/// A file a job made. `url`, `pageURL`, `imageURL` and `imageURLDark` are paths on the token server, loaded with the
/// sign-in. A chart has both images: `imageURLDark` is the same chart drawn for dark mode, null for a chart drawn
/// before dark charts existed. A report has `pageURL`, the report rendered as a readable page with its own light and
/// dark styles, null for a report from before pages existed.
nonisolated struct JobFile: Decodable, Hashable {
    let name: String
    let kind: FileKind
    let url: String
    @Present var pageURL: String?
    @Present var title: String?
    @Present var imageURL: String?
    @Present var imageURLDark: String?

    enum CodingKeys: String, CodingKey {
        case name, kind, url, title
        case pageURL = "page_url"
        case imageURL = "image_url"
        case imageURLDark = "image_url_dark"
    }
}

nonisolated enum BeeWriteKind: String, ContractValue {
    case todoCreate = "todo_create", todoAccept = "todo_accept", factConfirm = "fact_confirm",
         factUpdate = "fact_update", unrecognized
}

nonisolated enum BeeWriteState: String, ContractValue {
    case written, undone, unknown, failed, unrecognized // `unknown` is the contract's: Bee may or may not have it
}

/// A fact or to-do written to Bee after the person's spoken yes.
nonisolated struct BeeWrite: Decodable, Hashable {
    let id: String
    let at: ZonedTime
    let kind: BeeWriteKind
    @Present var text: String?
    let state: BeeWriteState
    let willRetry: Bool
    let undoable: Bool

    enum CodingKeys: String, CodingKey {
        case id, at, kind, text, state
        case willRetry = "will_retry"
        case undoable
    }
}

/// The card of a call the AI placed (2.2).
nonisolated struct WhyCalling: Decodable, Hashable {
    @Present var reason: String?
    @Present var conversation: BeeConversation?
}

/// The Bee conversation behind an AI call: Bee's short summary and its last lines, oldest first.
nonisolated struct BeeConversation: Decodable, Hashable {
    @Present var summary: String?
    let quotes: [Quote]
}

nonisolated enum QuoteSpeaker: String, ContractValue {
    case person, other, unrecognized
}

nonisolated struct Quote: Decodable, Hashable {
    let speaker: QuoteSpeaker
    let text: String
    @Present var at: ZonedTime?
}

/// "통화 전에 Bee에서 본 것" (2.3).
nonisolated struct Prep: Decodable, Hashable {
    @Present var note: String?
    let lookedUp: [LookedUp]
    let fromBee: Bool

    enum CodingKeys: String, CodingKey {
        case note
        case lookedUp = "looked_up"
        case fromBee = "from_bee"
    }
}

/// One thing the prep read: a Bee conversation, a day's summary, or a search.
nonisolated enum LookedUp: Decodable, Hashable {
    case conversation(summary: String?, at: ZonedTime?)
    case day(CalendarDay)
    case search(query: String)
    case unrecognized(type: String)

    init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: Keys.self)
        switch try container.decode(String.self, forKey: .type) {
        case "conversation":
            self = .conversation(summary: try container.decode(String?.self, forKey: .summary),
                                 at: try container.decode(ZonedTime?.self, forKey: .at))
        case "day": self = .day(try container.decode(CalendarDay.self, forKey: .date))
        case "search": self = .search(query: try container.decode(String.self, forKey: .query))
        case let other: self = .unrecognized(type: other)
        }
    }

    private enum Keys: String, CodingKey {
        case type, summary, at, date, query
    }
}

// MARK: - Live updates (section 3)

/// One `changed` event of GET /changes: the resource at `path` now has `version`.
nonisolated struct Change: Decodable, Hashable {
    let path: String
    let version: String
}

// MARK: - Offers (section 4)

nonisolated enum OfferState: String, ContractValue {
    case asked, booked, calling, declined, unrecognized
}

/// A call the AI asked about, or one booked for a time. `topic` is never null: the server writes the line its
/// notification uses when the offer has none, so the app keeps no wording of its own.
nonisolated struct Offer: Decodable, Hashable, Identifiable {
    let id: String
    let state: OfferState
    let topic: String
    let createdAt: ZonedTime
    @Present var suggestedAt: ZonedTime?
    @Present var callAt: ZonedTime?

    enum CodingKeys: String, CodingKey {
        case id, state, topic
        case createdAt = "created_at"
        case suggestedAt = "suggested_at"
        case callAt = "call_at"
    }
}

/// GET /offers: the open offers, in the order they were made.
nonisolated struct OpenOffers: Decodable, Hashable {
    let offers: [Offer]
    let version: String
}

/// POST /offers/{offer_id}: the offer after the answer.
nonisolated struct OfferAnswerBody: Decodable, Hashable {
    let offer: Offer
}

// MARK: - Voices and settings (sections 5 and 6)

/// How a voice sounds, in both languages, so the app's words can differ from the call's.
nonisolated struct VoiceCharacter: Decodable, Hashable {
    let ko: String
    let en: String
}

/// A voice of the catalog. `sampleURL` is a path on the token server that needs no sign-in.
nonisolated struct Voice: Decodable, Hashable, Identifiable {
    let id: String
    let name: String
    let character: VoiceCharacter
    let sampleURL: String
    let isDefault: Bool

    enum CodingKeys: String, CodingKey {
        case id, name, character
        case sampleURL = "sample_url"
        case isDefault = "default"
    }
}

/// GET /voices?language=: one language's voices, the default first.
nonisolated struct VoiceList: Decodable, Hashable {
    let language: CallLanguage
    let voices: [Voice]
}

// MARK: - Models (section 5b)

nonisolated enum ModelRole: String, ContractValue {
    case call, worker, unrecognized
}

/// One short line on a model for its role, in both languages.
nonisolated struct ModelDescription: Decodable, Hashable {
    let ko: String
    let en: String
}

/// A model of the server's catalog, as the settings sheet lists it. `id` is the catalog's (haiku, sonnet, glm),
/// never a provider's own model id.
nonisolated struct CatalogModel: Decodable, Hashable, Identifiable {
    let id: String
    let name: String
    let maker: String
    let description: ModelDescription
    let isDefault: Bool

    enum CodingKeys: String, CodingKey {
        case id, name, maker, description
        case isDefault = "default"
    }
}

/// GET /models?role=: one role's choices, 1 to 12, the default first.
nonisolated struct ModelList: Decodable, Hashable {
    let role: ModelRole
    let models: [CatalogModel]
}

/// The person's settings as the server holds them.
nonisolated struct PersonSettings: Decodable, Hashable {
    let language: CallLanguage
    @Present var voice: String? // null: the language's default voice
    let callModel: String // a catalog id of GET /models?role=call
    let workerModel: String // a catalog id of GET /models?role=worker
    /// Noise cancellation on the person's audio in their calls, default on; a call keeps the value it started with.
    let noiseCancellation: Bool
    let callTime: String
    let recordOnServer: Bool
    let recordOnPhone: Bool
    let timeZone: String
    let bee: Bool
    let dailyBudgetUSD: Double

    enum CodingKeys: String, CodingKey {
        case language, voice
        case callModel = "call_model"
        case workerModel = "worker_model"
        case noiseCancellation = "noise_cancellation"
        case callTime = "call_time"
        case recordOnServer = "record_on_server"
        case recordOnPhone = "record_on_phone"
        case timeZone = "time_zone"
        case bee
        case dailyBudgetUSD = "daily_budget_usd"
    }
}

/// GET /me.
nonisolated struct Me: Decodable, Hashable {
    let person: String
    let settings: PersonSettings
    let spentTodayUSD: Double
    let beeConnecting: Bool

    enum CodingKeys: String, CodingKey {
        case person, settings
        case spentTodayUSD = "spent_today_usd"
        case beeConnecting = "bee_connecting"
    }
}

/// PUT /settings: the settings after the change.
nonisolated struct SettingsAnswer: Decodable, Hashable {
    let settings: PersonSettings
}

// MARK: - Endpoints D keeps (section 7)

/// A ring of GET /ring and of a VoIP push. D reads these three fields only. Its topic may be left out: the ring
/// path stays lenient, since a push the app cannot read is still reported to CallKit, but cannot ring.
nonisolated struct RingEvent: Decodable, Hashable {
    let room: String
    let expiresAt: ZonedTime
    let topic: String?

    enum CodingKeys: String, CodingKey {
        case room
        case expiresAt = "expires_at"
        case topic
    }
}

/// GET /token: the pass into a call's room.
nonisolated struct CallPass: Decodable, Hashable {
    let url: String
    let token: String
    let room: String
    let identity: String
}

/// GET /auth/nonce.
nonisolated struct Nonce: Decodable, Hashable {
    let nonce: String
}

/// POST /auth/apple: the server's own sign-in token.
nonisolated struct AppleSignInAnswer: Decodable, Hashable {
    let token: String
    let person: String
}

/// POST /auth/apple answered 403: the code the operator approves.
nonisolated struct Waiting: Decodable, Hashable {
    let waiting: String
}

/// POST /bee/connect: the page where the person approves Bee.
nonisolated struct BeeConnectLink: Decodable, Hashable {
    let link: String
}
