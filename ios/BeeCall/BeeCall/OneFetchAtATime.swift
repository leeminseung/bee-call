import Foundation

/// One fetch of a resource at a time. A fetch asked for while one is on its way waits for it, and then the resource
/// is fetched once more, so the body kept is never older than the newest ask: two answers cannot land out of order
/// and leave the older one (fresh-context review of round1 7c1361a, finding 2: each /changes event fetched on its
/// own). A caller returns once a fetch that began after its ask has come back. A fetch must not ask for its own
/// resource: it would wait for itself.
final class OneFetchAtATime<Key: Hashable> {
    private var running: [Key: Task<Void, Never>] = [:]
    private var again: Set<Key> = []

    func run(_ key: Key, _ fetch: @escaping () async -> Void) async {
        if let fetching = running[key] {
            again.insert(key) // the fetch on its way began before this ask: one more after it
            await fetching.value
            return
        }
        let task = Task {
            repeat {
                again.remove(key)
                await fetch()
            } while again.contains(key)
            running[key] = nil // with nothing in between, so no ask can come after the last check and be missed
        }
        running[key] = task
        await task.value
    }
}
