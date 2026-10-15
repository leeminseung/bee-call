import SwiftUI

/// Design D, frame 7: the first run, in the iOS Settings look. Step 1 picks the call language (the check at the
/// end of the picked row, as in iOS's language list), step 2 the voice (the check in front, as in Accessibility's
/// voice list), with a yellow button that plays each voice for 5 seconds. Each pick goes to the server at once;
/// the screens' words follow the language picked.
struct FirstRunView: View {
    enum Step: Hashable { case voice }

    let person: PersonStore
    let catalog: VoiceCatalog
    let preview: VoicePreview
    let done: () -> Void
    @State private var path: [Step]
    @State private var picked: CallLanguage? // the language tapped, before the server's answer
    @State private var pickedVoice: String? // the voice tapped, before the server's answer
    @AppStorage("language") private var appLanguage = "en"
    @AppStorage(FirstRun.doneKey) private var finished = false

    init(person: PersonStore, catalog: VoiceCatalog, preview: VoicePreview, startOn step: Step? = nil,
         done: @escaping () -> Void) {
        self.person = person
        self.catalog = catalog
        self.preview = preview
        self.done = done
        _path = State(initialValue: step.map { [$0] } ?? [])
    }

    private var language: CallLanguage {
        picked ?? FirstRun.language(settings: person.settings, preferred: Locale.preferredLanguages)
    }

    private var words: FirstRunWords { FirstRunWords(korean: language == .ko) }

    var body: some View {
        NavigationStack(path: $path) {
            languageStep
                .navigationDestination(for: Step.self) { _ in voiceStep }
        }
        .tint(BeeStyle.ink)
    }

    private var languageStep: some View {
        List {
            Section {
                LanguageRows(selected: language, pick: pick)
            } header: {
                StepHeader(orb: true, step: words.step(1), title: words.languageTitle, lead: words.languageLead)
            } footer: {
                ProblemLine(problem: person.problem, words: words)
            }
        }
        .toolbar(.hidden, for: .navigationBar)
        .navigationTitle(words.languageBack) // the back button's name on step 2
        .safeAreaInset(edge: .bottom) {
            StepFooter(page: 0, button: words.next) {
                Task {
                    guard await languageSaved() else { return } // stays, with the reason under the list
                    path = [.voice]
                }
            }
        }
    }

    private var voiceStep: some View {
        List {
            Section {
                VoicesSection(language: language, catalog: catalog, picked: pickedVoice ?? person.settings?.voice,
                          preview: preview, words: words, pick: pick)
            } header: {
                StepHeader(orb: false, step: words.step(2), title: words.voiceTitle, lead: words.voiceLead,
                           group: words.voices(language))
            } footer: {
                ProblemLine(problem: person.problem, words: words)
            }
        }
        .navigationBarTitleDisplayMode(.inline)
        .safeAreaInset(edge: .bottom) {
            StepFooter(page: 1, button: words.start) {
                Task {
                    guard await languageSaved() else { return }
                    preview.stop()
                    finished = true
                    done()
                }
            }
        }
        .onDisappear { preview.stop() }
    }

    /// The language shown is the server's: sent now if it never was (GET /me had not come, or a pick failed), so the
    /// first run never ends with the server on another language (second review of the D wiring, finding 4).
    private func languageSaved() async -> Bool {
        let shown = language
        guard person.settings?.language != shown else { return true }
        return await person.choose(language: shown)
    }

    private func pick(_ language: CallLanguage) {
        picked = language
        pickedVoice = nil
        appLanguage = language.rawValue // the words follow at once
        Task {
            guard await !person.choose(language: language) else { return }
            picked = nil // not saved: back to what the server holds
            appLanguage = self.language.rawValue
        }
    }

    private func pick(_ voice: Voice) {
        pickedVoice = voice.id
        Task {
            if await !person.choose(voice: voice.id) { pickedVoice = nil }
        }
    }
}

/// A pick the server did not take, under the list.
struct ProblemLine: View {
    let problem: String?
    let words: FirstRunWords

    var body: some View {
        if let problem {
            Text(words.notSaved(MessageWords.words(problem, korean: words.korean)))
                .foregroundStyle(BeeStyle.red)
        }
    }
}

/// The two languages, the picked one with a check at its end.
struct LanguageRows: View {
    let selected: CallLanguage
    let pick: (CallLanguage) -> Void

    var body: some View {
        ForEach(CallLanguage.choices) { option in
            let picked = option == selected
            Button { pick(option) } label: {
                HStack {
                    VStack(alignment: .leading, spacing: 1) {
                        Text(option.name)
                            .font(.body)
                            .foregroundStyle(BeeStyle.ink)
                        Text(option.otherName)
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    if picked {
                        Image(systemName: "checkmark")
                            .font(.body.weight(.semibold))
                            .foregroundStyle(BeeStyle.ink)
                    }
                }
                .contentShape(.rect)
            }
            .accessibilityAddTraits(picked ? .isSelected : [])
        }
    }
}

/// A language's voices from GET /voices, any number of them, or why they are not there.
struct VoicesSection: View {
    let language: CallLanguage
    let catalog: VoiceCatalog
    let picked: String?
    let preview: VoicePreview
    let words: FirstRunWords
    let pick: (Voice) -> Void

    var body: some View {
        Group {
            if let voices = catalog.voices[language] {
                VoiceRows(voices: voices, picked: VoiceChoice.effective(stored: picked, in: voices)?.id,
                          preview: preview, words: words, pick: pick)
            } else if catalog.failed.contains(language) {
                HStack {
                    Text(words.voicesFailed)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button(words.tryAgain) { Task { await catalog.load(language) } }
                        .buttonStyle(.borderless)
                }
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity)
            }
        }
        .task(id: language) { await catalog.load(language) }
    }
}

/// The voices of the call language, the picked one with a check in front, each with its 5-second sample.
struct VoiceRows: View {
    let voices: [Voice]
    let picked: String?
    let preview: VoicePreview
    let words: FirstRunWords
    let pick: (Voice) -> Void

    var body: some View {
        ForEach(voices) { voice in
            let isPicked = voice.id == picked, playing = preview.playing == voice.id
            HStack(spacing: 10) {
                Button { pick(voice) } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "checkmark")
                            .font(.body.weight(.semibold))
                            .foregroundStyle(BeeStyle.ink)
                            .opacity(isPicked ? 1 : 0)
                            .frame(width: 22)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(voice.name)
                                .font(.body)
                                .foregroundStyle(BeeStyle.ink)
                            Text(words.manner(voice, playing: playing))
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 0)
                    }
                    .contentShape(.rect)
                }
                .buttonStyle(.borderless)
                .accessibilityAddTraits(isPicked ? .isSelected : [])
                SampleButton(playing: playing, progress: preview.progress(at:)) { preview.toggle(voice) }
                    .accessibilityLabel(playing ? words.stop(voice) : words.play(voice))
            }
            .padding(.vertical, 2)
            .alignmentGuide(.listRowSeparatorLeading) { _ in 32 }
        }
    }
}

/// The yellow play button; while its sample plays, a stop button in a ring that fills over the 5 seconds.
struct SampleButton: View {
    let playing: Bool
    let progress: (Date) -> Double
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            ZStack {
                Circle()
                    .fill(BeeStyle.yellow)
                    .frame(width: 34, height: 34)
                Image(systemName: playing ? "stop.fill" : "play.fill")
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(BeeStyle.onYellow)
                if playing {
                    Circle()
                        .stroke(BeeStyle.ink.opacity(0.14), lineWidth: 2.5)
                        .frame(width: 42, height: 42)
                    TimelineView(.animation(minimumInterval: 1 / 30)) { context in
                        Circle()
                            .trim(from: 0, to: progress(context.date))
                            .stroke(BeeStyle.ink, style: StrokeStyle(lineWidth: 2.5, lineCap: .round))
                            .rotationEffect(.degrees(-90))
                            .frame(width: 42, height: 42)
                    }
                }
            }
            .frame(width: 44, height: 44)
            .contentShape(.circle)
        }
        .buttonStyle(.borderless)
    }
}

/// The top of a first-run step: the orb (step 1), "1단계 / 2단계", the title, a line on what to do, and the list's
/// name if it has one. It is the list's header, not a row: a row is cut to the group's round corners, which took
/// the top off "2단계".
private struct StepHeader: View {
    let orb: Bool
    let step: String
    let title: String
    let lead: String
    var group: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 0) {
                if orb {
                    OrbView(look: OrbLook(.rest, lastSide: nil, reduceMotion: true), diameter: 52)
                        .padding(.bottom, 14)
                }
                Text(step)
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(.secondary)
                Text(title)
                    .font(.largeTitle.bold())
                    .padding(.top, 2)
                Text(lead)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .padding(.top, 8)
            }
            .accessibilityElement(children: .combine)
            .accessibilityAddTraits(.isHeader)
            if let group {
                Text(group)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .padding(.leading, 12) // where iOS puts a group's name: over the rows' text
                    .padding(.top, 26)
                    .padding(.bottom, 7)
            } else {
                Color.clear.frame(height: 22)
            }
        }
        .foregroundStyle(BeeStyle.ink) // the header's own grey would make "secondary" a paler grey still
        .frame(maxWidth: .infinity, alignment: .leading)
        .textCase(nil)
        .listRowInsets(EdgeInsets(top: orb ? 24 : 0, leading: 4, bottom: 0, trailing: 4))
    }
}

/// The bottom of a first-run step: which step it is, as dots, and the wide yellow button.
private struct StepFooter: View {
    let page: Int
    let button: String
    let action: () -> Void

    var body: some View {
        VStack(spacing: 16) {
            HStack(spacing: 8) {
                ForEach(0..<2, id: \.self) { dot in
                    Circle()
                        .fill(BeeStyle.ink.opacity(dot == page ? 1 : 0.22))
                        .frame(width: 7, height: 7)
                }
            }
            .accessibilityHidden(true) // the step's own line says "1단계 / 2단계"
            Button(button, action: action)
                .buttonStyle(YellowCapsule())
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .padding(.bottom, 8)
    }
}

/// The wide yellow button, with dark ink on the yellow.
struct YellowCapsule: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.headline)
            .foregroundStyle(BeeStyle.onYellow)
            .frame(maxWidth: .infinity, minHeight: 52)
            .background(BeeStyle.yellow, in: .capsule)
            .opacity(configuration.isPressed ? 0.7 : 1)
            .shadow(color: Color(red: 0.47, green: 0.37, blue: 0).opacity(0.35), radius: 9, y: 8)
    }
}
