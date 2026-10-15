import SwiftUI
import UIKit

/// What a job made, on the token server (`url` and `image_url`, contract section 2.6): loaded with the sign-in
/// through ServerClient, never with a plain AsyncImage or a browser. A finished job's files never change, so an
/// image is kept in memory by its path, and a file opened once is kept on disk for the session.
@MainActor
final class ServerFiles {
    static let shared = ServerFiles()

    private let images = NSCache<NSString, UIImage>()
    private let folder = FileManager.default.temporaryDirectory.appending(path: "job-files")

    func image(_ path: String, server: ServerClient = ContractServer.client) async -> UIImage? {
        if let kept = images.object(forKey: path as NSString) { return kept }
        guard case .ok(200, let data) = await server.send(path), let image = UIImage(data: data) else { return nil }
        images.setObject(image, forKey: path as NSString)
        return image
    }

    /// The file as a local copy named as the job named it, for Quick Look. Nil when it could not be fetched.
    func open(_ file: JobFile, server: ServerClient = ContractServer.client) async -> URL? {
        let place = folder.appending(path: file.url.replacingOccurrences(of: "/", with: "_")).appending(path: file.name)
        if FileManager.default.fileExists(atPath: place.path) { return place }
        guard case .ok(200, let data) = await server.send(file.url) else { return nil }
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
