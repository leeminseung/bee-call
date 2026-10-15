import SwiftUI
import UIKit

/// Design D, frame 8: the home's gear opens this sheet. "목소리" and "언어" open the first run's own lists; the last
/// row opens the Bee Call page of the iOS Settings app, which keeps the other switches (evening call time, saving
/// call audio, Bee, the call model). Language belongs to the app: once design D is the app, the Settings app's
/// page drops its language row, so one place sets it.
struct SettingsSheet: View {
    let catalog: any VoiceCatalog
    let preview: VoicePreview
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @AppStorage("language") private var language = "en"
    @AppStorage(FirstRun.voiceKey) private var voice = ""

    private var korean: Bool { language == "ko" }
    private var words: SettingsWords { SettingsWords(korean: korean) }
    private var listWords: FirstRunWords { FirstRunWords(korean: korean) }
    private var callLanguage: CallLanguage { CallLanguage(rawValue: language) ?? .en }
    private var voices: [CallVoice] { catalog.voices(for: callLanguage) }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    NavigationLink {
                        voiceList
                    } label: {
                        LabeledContent(words.voice, value: words.voiceValue(VoiceChoice.effective(stored: voice, in: voices)))
                    }
                    NavigationLink {
                        languageList
                    } label: {
                        LabeledContent(words.language, value: callLanguage.name)
                    }
                } header: {
                    Text(words.calls)
                }
                Section {
                    Button {
                        if let page = SettingsWords.settingsPage { openURL(page) }
                    } label: {
                        HStack {
                            Text(words.openSettings)
                                .foregroundStyle(BeeStyle.ink)
                            Spacer()
                            Image(systemName: "arrow.up.forward.app")
                                .foregroundStyle(.secondary)
                        }
                        .contentShape(.rect)
                    }
                } footer: {
                    Text(words.footer)
                }
            }
            .navigationTitle(words.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button { dismiss() } label: {
                        Image(systemName: "xmark")
                            .font(.subheadline.weight(.semibold))
                            .foregroundStyle(BeeStyle.ink)
                    }
                    .accessibilityLabel(words.close)
                }
            }
        }
        .tint(BeeStyle.ink)
        .presentationDragIndicator(.visible)
    }

    private var voiceList: some View {
        List {
            Section {
                VoiceRows(voices: voices, picked: $voice, preview: preview, words: listWords)
            } header: {
                Text(listWords.voices)
            }
        }
        .navigationTitle(words.voice)
        .onDisappear { preview.stop() }
    }

    private var languageList: some View {
        List {
            Section {
                LanguageRows(language: $language)
            }
        }
        .navigationTitle(words.language)
    }
}

/// The settings sheet's words, in the call language.
struct SettingsWords {
    let korean: Bool

    /// The app's own page in the iOS Settings app, where Settings.bundle's switches are.
    static let settingsPage = URL(string: UIApplication.openSettingsURLString)

    var title: String { korean ? "설정" : "Settings" }
    var calls: String { korean ? "통화" : "Calls" }
    var voice: String { korean ? "목소리" : "Voice" }
    var language: String { korean ? "언어" : "Language" }
    var openSettings: String { korean ? "iOS 설정 열기" : "Open iOS Settings" }
    var close: String { korean ? "닫기" : "Close" }
    var footer: String {
        korean ? "저녁 전화 시간, 통화 녹음 저장, Bee 사용 같은 나머지 스위치는 iOS 설정 앱의 Bee Call 페이지에 있어요."
            : "The other switches, such as the evening call time, saving call audio and using Bee, are on the Bee Call "
                + "page of the iOS Settings app."
    }

    /// The voice row's value: "하린 · 친근함"; nothing while there are no voices.
    func voiceValue(_ voice: CallVoice?) -> String {
        voice.map { "\($0.name) · \($0.manner)" } ?? ""
    }
}
