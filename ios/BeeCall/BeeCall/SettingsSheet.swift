import SwiftUI
import UIKit

/// Design D, frame 8: the home's gear opens this sheet. "목소리" and "언어" open the first run's own lists, "통화
/// 모델" and "일꾼 모델" the server's model lists (GET /models), and each pick goes to the server at once (PUT
/// /settings); the last row opens the Bee Call page of the iOS Settings app, which keeps the other switches (evening
/// call time, saving call audio, Bee). The language, the voice and the models belong to the app (contract section 6).
struct SettingsSheet: View {
    let person: PersonStore
    let catalog: VoiceCatalog
    let preview: VoicePreview
    @State private var models = ModelCatalog() // GET /models, through ContractServer
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @AppStorage("language") private var appLanguage = "en"
    @State private var picked: CallLanguage?
    @State private var pickedVoice: String?
    @State private var pickedModels: [ModelRole: String] = [:] // tapped, before the server's answer
    #if DEBUG
    @AppStorage(Server.typedAddressKey) private var typedAddress = "" // a Debug build's token server, typed
    #endif

    private var language: CallLanguage {
        let held = person.settings?.language
        return picked ?? (held == .unrecognized ? nil : held) ?? CallLanguage(rawValue: appLanguage) ?? .en
    }

    private var korean: Bool { language == .ko }
    private var words: SettingsWords { SettingsWords(korean: korean) }
    private var listWords: FirstRunWords { FirstRunWords(korean: korean) }
    private var voice: Voice? {
        VoiceChoice.effective(stored: pickedVoice ?? person.settings?.voice, in: catalog.voices[language] ?? [])
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    NavigationLink {
                        voiceList
                    } label: {
                        LabeledContent(words.voice, value: words.voiceValue(voice))
                    }
                    NavigationLink {
                        languageList
                    } label: {
                        LabeledContent(words.language, value: language.name)
                    }
                    ForEach([ModelRole.call, .worker], id: \.self) { role in
                        NavigationLink {
                            modelList(role)
                        } label: {
                            LabeledContent(words.model(role), value: chosen(role)?.name ?? "")
                        }
                    }
                } header: {
                    Text(words.calls)
                } footer: {
                    ProblemLine(problem: person.problem, words: listWords)
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
                #if DEBUG
                Section {
                    TextField("http://<Mac IP>:8787", text: $typedAddress)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                } header: {
                    Text("Token server (development)")
                } footer: {
                    Text("Debug builds only: the Mac's address changes with the Wi-Fi. Design D's calls, offers and voices "
                         + "come from the contract's examples unless the app is launched with -realServer YES.")
                }
                #endif
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
        .task(id: language) { await catalog.load(language) }
        .task {
            await models.load(.call)
            await models.load(.worker)
        }
    }

    /// The model shown as chosen for a role: the pick, the server's, or the role's default.
    private func chosen(_ role: ModelRole) -> CatalogModel? {
        let stored = pickedModels[role] ?? (role == .worker ? person.settings?.workerModel : person.settings?.callModel)
        return ModelChoice.effective(stored: stored, in: models.models[role] ?? [])
    }

    private func modelList(_ role: ModelRole) -> some View {
        List {
            Section {
                if let list = models.models[role] {
                    ForEach(list) { model in
                        let isChosen = model.id == chosen(role)?.id
                        Button {
                            pickedModels[role] = model.id
                            Task {
                                if await !person.choose(model: model.id, for: role) { pickedModels[role] = nil }
                            }
                        } label: {
                            HStack {
                                VStack(alignment: .leading, spacing: 1) {
                                    Text(model.name)
                                        .font(.body)
                                        .foregroundStyle(BeeStyle.ink)
                                    Text("\(model.maker) · \(korean ? model.description.ko : model.description.en)")
                                        .font(.footnote)
                                        .foregroundStyle(.secondary)
                                }
                                Spacer()
                                if isChosen {
                                    Image(systemName: "checkmark")
                                        .font(.body.weight(.semibold))
                                        .foregroundStyle(BeeStyle.ink)
                                }
                            }
                            .contentShape(.rect)
                        }
                        .accessibilityAddTraits(isChosen ? .isSelected : [])
                    }
                } else if models.failed.contains(role) {
                    HStack {
                        Text(words.modelsFailed)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button(listWords.tryAgain) { Task { await models.load(role) } }
                            .buttonStyle(.borderless)
                    }
                } else {
                    ProgressView()
                        .frame(maxWidth: .infinity)
                }
            } footer: {
                ProblemLine(problem: person.problem, words: listWords)
            }
        }
        .navigationTitle(words.model(role))
        .task { await models.load(role) }
    }

    private var voiceList: some View {
        List {
            Section {
                VoicesSection(language: language, catalog: catalog, picked: pickedVoice ?? person.settings?.voice,
                          preview: preview, words: listWords) { voice in
                    pickedVoice = voice.id
                    Task { if await !person.choose(voice: voice.id) { pickedVoice = nil } }
                }
            } header: {
                Text(listWords.voices(language))
            } footer: {
                ProblemLine(problem: person.problem, words: listWords)
            }
        }
        .navigationTitle(words.voice)
        .onDisappear { preview.stop() }
    }

    private var languageList: some View {
        List {
            Section {
                LanguageRows(selected: language) { language in
                    picked = language
                    pickedVoice = nil
                    appLanguage = language.rawValue
                    Task {
                        guard await !person.choose(language: language) else { return }
                        picked = nil
                        appLanguage = self.language.rawValue
                    }
                }
            } footer: {
                ProblemLine(problem: person.problem, words: listWords)
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
    var callModel: String { korean ? "통화 모델" : "Call model" }
    var workerModel: String { korean ? "일꾼 모델" : "Worker model" }
    func model(_ role: ModelRole) -> String { role == .worker ? workerModel : callModel }
    var modelsFailed: String { korean ? "모델 목록을 불러오지 못했어요." : "The models could not be loaded." }
    var openSettings: String { korean ? "iOS 설정 열기" : "Open iOS Settings" }
    var close: String { korean ? "닫기" : "Close" }
    var footer: String {
        korean ? "저녁 전화 시간, 통화 녹음 저장, Bee 사용 같은 나머지 스위치는 iOS 설정 앱의 Bee Call 페이지에 있어요."
            : "The other switches, such as the evening call time, saving call audio and using Bee, are on the Bee Call "
                + "page of the iOS Settings app."
    }

    /// The voice row's value: the voice's own name and how it sounds, in the app's language; nothing while there
    /// are no voices.
    func voiceValue(_ voice: Voice?) -> String {
        voice.map { "\($0.name) · \(korean ? $0.character.ko : $0.character.en)" } ?? ""
    }
}
