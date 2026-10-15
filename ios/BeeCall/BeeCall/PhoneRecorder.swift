import AVFAudio
import Foundation
import LiveKit

/// Records a call on this iPhone when "Save call audio on this iPhone" is on in the Settings app: the person's
/// microphone and the AI's voice mixed into one file under Documents/Calls, which the Files app shows under
/// On My iPhone > Bee Call. The recording never goes to the server; the server's own copy follows the other
/// switch, "Save call audio on the server".
final class PhoneRecorder {
    private var recorder: AudioMixRecorder?
    private var sources: [(source: AudioMixRecorderSource, local: Bool)] = []
    private let store: UserDefaults // where the Settings app keeps the switch
    private let folder: URL? // where the recordings go; nil: Documents/Calls

    init(store: UserDefaults = .standard, folder: URL? = nil) {
        self.store = store
        self.folder = folder
    }

    // Speech at 16 kHz in AAC: about 1 to 2 MB for ten minutes.
    private static let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatMPEG4AAC,
        AVSampleRateKey: 16000,
        AVNumberOfChannelsKey: 1,
    ]

    /// Starts recording if the switch is on. A failure leaves the call alone; it comes back in words, nil when
    /// there was none.
    func startIfWanted() -> String? {
        guard store.bool(forKey: "record_on_phone"), recorder == nil else { return nil }
        do {
            let folder = try folder ?? FileManager.default.url(for: .documentDirectory, in: .userDomainMask,
                                                               appropriateFor: nil, create: true)
                .appendingPathComponent("Calls", isDirectory: true)
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            let name = Self.fileName.string(from: Date())
            let recorder = try AudioMixRecorder(filePath: folder.appendingPathComponent("\(name).m4a"),
                                                audioSettings: Self.settings)
            try recorder.start()
            let mine = recorder.addSource(), theirs = recorder.addSource()
            AudioManager.shared.add(localAudioRenderer: mine) // the microphone, as sent
            AudioManager.shared.add(remoteAudioRenderer: theirs) // the AI's voice, as heard
            sources = [(mine, true), (theirs, false)]
            self.recorder = recorder
            return nil
        } catch {
            print("phone recording did not start: \(error)")
            return "Recording on this iPhone did not start: \(error.localizedDescription)"
        }
    }

    func stop() {
        for (source, local) in sources {
            if local {
                AudioManager.shared.remove(localAudioRenderer: source)
            } else {
                AudioManager.shared.remove(remoteAudioRenderer: source)
            }
        }
        sources = []
        recorder?.stop()
        recorder = nil
    }

    private static let fileName: DateFormatter = {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.dateFormat = "yyyy-MM-dd HHmm ss"
        return formatter
    }()
}
