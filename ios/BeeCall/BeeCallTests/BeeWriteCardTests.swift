import XCTest
@testable import BeeCall

/// The Bee write card (contract 2.4, bee_write): each kind says in a few words what became of it in Bee, in both
/// languages; a write that cannot be undone has no "빼 줘" hint; and a kind a later server adds reads as a plain
/// "Bee에 저장했어요" instead of disappearing.
final class BeeWriteCardTests: XCTestCase {
    /// A write as a call's view gives it, of this kind.
    @MainActor private func write(_ kind: String, state: String = "written", undoable: Bool = true) throws -> BeeWrite {
        let body: [String: Any] = ["type": "bee_write", "id": "write-1", "at": "2026-10-15T01:25:00+09:00", "kind": kind,
                                   "text": "장보기", "state": state, "will_retry": false, "undoable": undoable]
        return try Contract.decode(BeeWrite.self, from: JSONSerialization.data(withJSONObject: body))
    }

    @MainActor func testEveryKindDecodesToItself() throws {
        let kinds: [(String, BeeWriteKind)] = [
            ("todo_create", .todoCreate), ("todo_accept", .todoAccept), ("todo_complete", .todoComplete),
            ("todo_dismiss", .todoDismiss), ("fact_create", .factCreate), ("fact_confirm", .factConfirm),
            ("fact_update", .factUpdate), ("fact_delete", .factDelete), ("fact_merge", .unrecognized),
        ]
        for (raw, kind) in kinds {
            XCTAssertEqual(try write(raw).kind, kind, raw)
        }
    }

    /// The card's line during the call, what the write did.
    @MainActor func testEachKindSaysWhatItDidInBothLanguages() {
        let ko = ConversationWords(korean: true), en = ConversationWords(korean: false)
        let lines: [(BeeWriteKind, String, String)] = [
            (.todoCreate, "Bee 할 일에 넣었어요", "Added to Bee: To-do"),
            (.todoAccept, "Bee 할 일에 넣었어요", "Added to Bee: To-do"),
            (.factConfirm, "Bee 사실에 넣었어요", "Added to Bee: Fact"),
            (.factUpdate, "Bee 사실에 넣었어요", "Added to Bee: Fact"),
            (.factCreate, "Bee에 기억해 뒀어요", "Saved to Bee"),
            (.todoComplete, "Bee 할 일을 끝냈어요", "Marked done in Bee"),
            (.todoDismiss, "Bee 제안을 뺐어요", "Removed Bee's suggestion"),
            (.factDelete, "Bee에서 지웠어요", "Deleted from Bee"),
            (.unrecognized, "Bee에 저장했어요", "Saved to Bee"),
        ]
        for (kind, korean, english) in lines {
            XCTAssertEqual(ko.done(kind), korean, "\(kind)")
            XCTAssertEqual(en.done(kind), english, "\(kind)")
        }
    }

    /// After the call, what became of a write that went in: 넣음, or what the kind did.
    @MainActor func testAfterTheCallEachKindSaysWhatBecameOfIt() throws {
        let ko = ConversationWords(korean: true), en = ConversationWords(korean: false)
        let states: [(String, String, String)] = [
            ("todo_create", "넣음", "Added"), ("fact_confirm", "넣음", "Added"), ("fact_create", "넣음", "Added"),
            ("todo_complete", "끝냄", "Done"), ("todo_dismiss", "뺌", "Removed"), ("fact_delete", "지움", "Deleted"),
            ("fact_merge", "저장함", "Saved"),
        ]
        for (kind, korean, english) in states {
            XCTAssertEqual(ko.state(try write(kind)), korean, kind)
            XCTAssertEqual(en.state(try write(kind)), english, kind)
        }
        XCTAssertEqual(ko.state(try write("todo_complete", state: "undone")), "되돌림", "a completion taken back")
        XCTAssertEqual(ko.state(try write("todo_create", state: "undone")), "뺌")
    }

    /// After the call the card reads "할 일: 장보기"; an unknown kind without words still says something.
    @MainActor func testTheLineAfterTheCall() throws {
        let words = ConversationWords(korean: true)
        XCTAssertEqual(words.title(try write("todo_complete")), "할 일: 장보기")
        XCTAssertEqual(words.title(try write("fact_merge")), "장보기")
        let bare = try Contract.decode(BeeWrite.self, from: JSONSerialization.data(withJSONObject: [
            "type": "bee_write", "id": "write-2", "at": "2026-10-15T01:25:00+09:00", "kind": "fact_merge", "text": NSNull(),
            "state": "written", "will_retry": false, "undoable": false,
        ]))
        XCTAssertEqual(words.title(bare), "Bee에 저장했어요")
    }

    /// "빼려면 '빼 줘'라고 말하세요" only on a write that can be undone, while the call is on.
    @MainActor func testOnlyAWriteThatCanBeUndoneHasTheHint() throws {
        let words = ConversationWords(korean: true)
        XCTAssertEqual(BeeWriteView.hint(try write("todo_create"), live: true, words: words), "빼려면 '빼 줘'라고 말하세요")
        XCTAssertNil(BeeWriteView.hint(try write("todo_dismiss", undoable: false), live: true, words: words))
        XCTAssertNil(BeeWriteView.hint(try write("fact_delete", undoable: false), live: true, words: words))
        XCTAssertNil(BeeWriteView.hint(try write("todo_create"), live: false, words: words), "after the call")
    }

    /// A kind a later server adds is drawn with the generic line; an unknown state still is not.
    @MainActor func testAnUnknownKindIsDrawnAndAnUnknownStateIsNot() throws {
        XCTAssertTrue(CallItem.beeWrite(try write("fact_merge")).isDrawn)
        XCTAssertFalse(CallItem.beeWrite(try write("todo_create", state: "pending")).isDrawn)
    }
}
