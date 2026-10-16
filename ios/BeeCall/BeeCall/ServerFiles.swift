import QuickLook
import SwiftUI
import UIKit
import WebKit

/// What a job made, on the token server (`url`, `page_url` and the images, contract section 2.6): loaded with the
/// sign-in through ServerClient, never with a plain AsyncImage or a browser. A finished job's files never change, so
/// an image is kept in memory by its path, and a file opened once is kept on disk for the session.
@MainActor
final class ServerFiles {
    static let shared = ServerFiles()

    private let images = NSCache<NSString, UIImage>()
    private let folder: URL

    init(folder: URL = FileManager.default.temporaryDirectory.appending(path: "job-files")) {
        self.folder = folder
    }

    func image(_ path: String, server: ServerClient = ContractServer.client) async -> UIImage? {
        if let kept = images.object(forKey: path as NSString) { return kept }
        guard case .ok(200, let data) = await server.send(path), let image = UIImage(data: data) else { return nil }
        images.setObject(image, forKey: path as NSString)
        return image
    }

    /// What a chip opens, as a local copy: a report's readable page when it has one (an .html file named as the
    /// report), else the file itself, named as the job named it. Nil when it could not be fetched.
    func open(_ file: JobFile, server: ServerClient = ContractServer.client) async -> URL? {
        let (path, name) = Self.source(of: file)
        let place = folder.appending(path: path.replacingOccurrences(of: "/", with: "_")).appending(path: name)
        if FileManager.default.fileExists(atPath: place.path) { return place }
        guard case .ok(200, let data) = await server.send(path) else { return nil }
        do {
            try FileManager.default.createDirectory(at: place.deletingLastPathComponent(),
                                                    withIntermediateDirectories: true)
            try data.write(to: place)
            return place
        } catch {
            return nil
        }
    }
}

extension ServerFiles {
    /// The path to fetch for a file and the local name to keep it under: a report's page instead of its raw text.
    nonisolated static func source(of file: JobFile) -> (path: String, name: String) {
        guard file.kind == .report, let page = file.pageURL else { return (file.url, file.name) }
        return (page, (file.name as NSString).deletingPathExtension + ".html")
    }

    /// A local copy of a report's page, shown as a web page that follows light and dark mode rather than in Quick Look.
    nonisolated static func isPage(_ url: URL) -> Bool {
        url.pathExtension.lowercased() == "html"
    }
}

/// A chart's image from the token server, or a quiet placeholder while it loads or when it cannot.
struct ServerImage: View {
    let path: String
    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
            } else {
                Image(systemName: "chart.bar.xaxis")
                    .font(.title3)
                    .foregroundStyle(.tertiary)
                    .frame(maxWidth: .infinity, minHeight: 60)
            }
        }
        .task(id: path) { image = await ServerFiles.shared.image(path) }
    }
}

/// The job file open in Quick Look, held where the call screen can close it: a call that starts closes it first,
/// and the call screen waits until it is down, as for the other sheets (second review of the D wiring, finding 10).
@Observable
final class FilePreview {
    var url: URL?
}

/// A file opened as a sheet, so its dismissal is known, unlike the plain Quick Look modifier.
struct OpenedFile: Identifiable {
    let url: URL
    var id: URL { url }
}

/// Quick Look in a sheet.
struct QuickLookSheet: UIViewControllerRepresentable {
    let url: URL

    func makeUIViewController(context: Context) -> UINavigationController {
        let preview = QLPreviewController()
        preview.dataSource = context.coordinator
        return UINavigationController(rootViewController: preview)
    }

    func updateUIViewController(_ controller: UINavigationController, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(url: url)
    }

    final class Coordinator: NSObject, QLPreviewControllerDataSource {
        let url: URL

        init(url: URL) {
            self.url = url
        }

        func numberOfPreviewItems(in controller: QLPreviewController) -> Int { 1 }

        func previewController(_ controller: QLPreviewController, previewItemAt index: Int) -> any QLPreviewItem {
            url as NSURL
        }
    }
}

extension View {
    /// Presents the files that `preview` opens, from here.
    func filePreviews(_ preview: FilePreview, onDismiss: @escaping () -> Void = {}) -> some View {
        sheet(item: Binding(get: { preview.url.map(OpenedFile.init) }, set: { if $0 == nil { preview.url = nil } }),
              onDismiss: onDismiss) { file in
            if ServerFiles.isPage(file.url) {
                ReportPageSheet(url: file.url)
            } else {
                QuickLookSheet(url: file.url)
                    .ignoresSafeArea()
            }
        }
        .environment(preview)
    }
}

/// A report's readable page (contract 2.6, page_url), from its local copy: HTML with its own light and dark styles,
/// which follow the phone's mode. Its links open in Safari, never inside the sheet.
struct ReportPageSheet: View {
    let url: URL
    @Environment(\.dismiss) private var dismiss
    @AppStorage("language") private var language = "en"
    @State private var title = ""

    var body: some View {
        NavigationStack {
            ReportPageView(url: url, title: $title)
                .ignoresSafeArea(edges: .bottom)
                .navigationTitle(title)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { dismiss() } label: {
                            Image(systemName: "xmark")
                                .font(.subheadline.weight(.semibold))
                                .foregroundStyle(BeeStyle.ink)
                        }
                        .accessibilityLabel(language == "ko" ? "닫기" : "Close")
                    }
                }
        }
        .presentationDragIndicator(.visible)
    }
}

/// The web view under ReportPageSheet: the local page, with read access to its folder only.
struct ReportPageView: UIViewRepresentable {
    let url: URL
    @Binding var title: String

    func makeUIView(context: Context) -> WKWebView {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        let view = WKWebView(frame: .zero, configuration: configuration)
        view.navigationDelegate = context.coordinator
        view.isOpaque = false // the page's own background, light or dark, from its first frame
        view.backgroundColor = .clear
        view.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(title: $title)
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        @Binding var title: String

        init(title: Binding<String>) {
            _title = title
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            title = webView.title ?? ""
        }

        /// A link leaves for Safari; only the page itself loads here. The completion-handler form, answered at once on
        /// the main thread (an async delegate method's made-up handler can run elsewhere: build 4's crash).
        func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                     decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void) {
            guard action.navigationType == .linkActivated, let link = action.request.url else { return decisionHandler(.allow) }
            UIApplication.shared.open(link)
            decisionHandler(.cancel)
        }
    }
}
