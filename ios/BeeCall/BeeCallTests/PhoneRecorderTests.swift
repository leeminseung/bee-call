import XCTest
@testable import BeeCall

/// "Save on this iPhone": the call's audio in Documents/Calls, which the Files app shows.
final class PhoneRecorderTests: XCTestCase {
    @MainActor private func folder() throws -> URL {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("PhoneRecorderTests-\(UUID())")
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        return folder
    }

    @MainActor func testNothingIsRecordedWithTheSwitchOff() throws {
        let store = freshDefaults(self), folder = try folder()
        let recorder = PhoneRecorder(store: store, folder: folder)
        XCTAssertNil(recorder.startIfWanted())
        XCTAssertFalse(FileManager.default.fileExists(atPath: folder.path))
    }

    @MainActor func testARecordingIsWrittenWithTheSwitchOn() throws {
        let store = freshDefaults(self), folder = try folder()
        store.set(true, forKey: "record_on_phone")
        let recorder = PhoneRecorder(store: store, folder: folder)
        XCTAssertNil(recorder.startIfWanted())
        recorder.stop()
        let files = try FileManager.default.contentsOfDirectory(atPath: folder.path)
        XCTAssertEqual(files.count, 1)
        XCTAssertTrue(files[0].hasSuffix(".m4a"), files[0])
    }

    /// The failure comes back in words, for the call screen.
    @MainActor func testARecordingThatCannotStartSaysWhy() throws {
        let store = freshDefaults(self), folder = try folder()
        store.set(true, forKey: "record_on_phone")
        try Data().write(to: folder) // a file where the recordings' folder should be: the folder cannot be made
        let problem = try XCTUnwrap(PhoneRecorder(store: store, folder: folder).startIfWanted())
        XCTAssertTrue(problem.hasPrefix("Recording on this iPhone did not start: "), problem)
    }
}
