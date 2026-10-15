import Foundation
import Observation

/// Where the home thread's content comes from, as design D's views see it.
@MainActor
protocol ConversationSource: AnyObject {
    /// Every section, oldest first (CallThread.sections), with the person's own call last while it is on.
    var sections: [CallThread.Section] { get }
    /// The person's own call while it is on: the listed call of its room, or the room before it is listed.
    var ownCall: String? { get }
    /// What the own call's section draws: its view, the captions it does not hold yet, the running job last.
    var ownEntries: [CallThread.Entry] { get }
    var hasOlder: Bool { get }
    /// The last fetch that did not come through, in words.
    var problem: String? { get }
    func beeLine(korean: Bool) -> String?
    func view(_ callID: String) -> CallView?
    /// The server has no record of the call (404): not recorded yet, or a missed call.
    func isMissing(_ callID: String) -> Bool
    /// A call's section or record is shown: its view is fetched, or asked again with the version held.
    func show(_ callID: String) async
    func loadOlder() async
    /// Keeps the content up to date while `on` (the app on screen, or in a call); off stops it.
    func keepFresh(_ on: Bool)
}

/// The home thread's content in the app: the call list and views (CallStore), the open offers (OfferCenter), the
/// person (PersonStore), and during the person's own call its room and captions (CallManager), all through
/// ContractServer: the token server, or in a Debug build the contract's examples, so the switch to the network is
/// that one place.
@Observable
final class ThreadData: ConversationSource {
    let store: CallStore
    let offers: OfferCenter
    let person: PersonStore
    let calls: CallManager
    /// Keeping the content fresh, owned here so a call screen covering the conversation does not stop it (second
    /// review of the D wiring, finding 2: it ran on the covered view).
    @ObservationIgnored private var freshness: Task<Void, Never>?
    /// GET /changes kept open until cancelled: `opened` each time the server accepts it, `changed` for each change.
    typealias Listen = @MainActor (_ opened: @escaping @MainActor () -> Void,
                                   _ changed: @escaping @MainActor (Change) -> Void) async -> Void
    @ObservationIgnored private let listen: Listen?

    /// The token server's change stream; none for the contract's examples, which never change.
    static var changeStream: Listen? {
        guard !ContractServer.usesExamples else { return nil }
        return { opened, changed in
            await ChangeStream.keepOpen(server: ContractServer.client, onOpen: opened, onChange: changed)
        }
    }

    init(calls: CallManager, store: CallStore = CallStore(), offers: OfferCenter = .shared,
         person: PersonStore = PersonStore(), listen: Listen? = ThreadData.changeStream) {
        self.calls = calls
        self.store = store
        self.offers = offers
        self.person = person
        self.listen = listen
    }

    var sections: [CallThread.Section] {
        var sections = CallThread.sections(calls: store.calls, offers: offers.open)
        if let room = calls.ownRoom, CallStore.ownCall(room: room, in: store.calls) == nil {
            if let view = store.views[room] {
                sections.append(.call(CallEntry(call: view.call, results: [])))
            } else {
                sections.append(.ownCall(room: room))
            }
        }
        return sections
    }

    var ownCall: String? {
        guard let room = calls.ownRoom else { return nil }
        return CallStore.ownCall(room: room, in: store.calls)?.call.callID ?? room
    }

    var ownEntries: [CallThread.Entry] {
        guard let id = ownCall else { return [] }
        let view = store.views[id]
        // Rule 5: captions only while the app is in that call and the call is live; rule 6: while numbers hold.
        let shown = calls.isInCall && calls.liveKit.numbering.holds && (view?.call.state ?? .live) == .live
        return CallThread.live(view: view, captions: calls.liveKit.captionList.lines, captionsShown: shown)
    }

    var hasOlder: Bool { store.hasOlder }
    var problem: String? { store.problem }

    func beeLine(korean: Bool) -> String? {
        person.beeLine(korean: korean)
    }

    func view(_ callID: String) -> CallView? {
        store.views[callID]
    }

    func isMissing(_ callID: String) -> Bool {
        store.missing.contains(callID)
    }

    func show(_ callID: String) async {
        await store.show(callID)
    }

    func loadOlder() async {
        await store.loadOlder()
    }

    func keepFresh(_ on: Bool) {
        guard on != (freshness != nil) else { return }
        freshness?.cancel()
        freshness = nil
        guard on else { return }
        freshness = Task { [weak self] in await self?.refreshAndListen() }
    }

    /// Brings the person and everything held up to date, then keeps them so through GET /changes until
    /// cancelled, asking again for everything held each time the stream opens.
    private func refreshAndListen() async {
        async let held: Void = refreshHeld()
        async let me: Void = person.refresh()
        _ = await (held, me)
        guard let listen else { return }
        await listen({ [weak self] in Task { await self?.refreshHeld() } },
                     { [weak self] change in Task { await self?.changed(change) } })
    }

    /// The list, the offers and every view held, asked again with their versions (a 304 costs nothing), so none
    /// outlives what the server says: /changes announces on connect only the calls of the last ten minutes, and a
    /// call can end while the stream is closed (fresh-context review of round1 7c1361a, finding 1).
    private func refreshHeld() async {
        async let calls: Void = store.refreshHeld()
        async let open: Void = offers.refresh()
        _ = await (calls, open)
    }

    private func changed(_ change: Change) async {
        if change.path == "/offers" {
            await offers.changed(to: change.version)
        } else {
            await store.changed(change)
        }
    }
}
