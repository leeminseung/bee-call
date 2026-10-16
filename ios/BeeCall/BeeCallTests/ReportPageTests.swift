import XCTest
@testable import BeeCall

/// A report opens as its readable page (contract 2.6, page_url: HTML with its own light and dark styles), fetched
/// with the sign-in; a report from before pages existed opens as its text, and other files as themselves.
final class ReportPageTests: XCTestCase {
    /// The example job's files, as the file has them.
    @MainActor private func files() throws -> [JobFile] {
        let view = try Examples.decode(CallView.self, "call-view")
        return view.items.flatMap { item -> [JobFile] in if case .job(let job) = item { job.files } else { [] } }
    }

    @MainActor private func report(withPage: Bool) throws -> JobFile {
        let report = try XCTUnwrap(try files().first { $0.kind == .report }, "the example has a report")
        guard !withPage else { return report }
        var body = try Examples.object("call-view")
        var items = try XCTUnwrap(body["items"] as? [[String: Any]])
        for (index, item) in items.enumerated() {
            guard var files = item["files"] as? [[String: Any]] else { continue }
            for file in files.indices { files[file]["page_url"] = NSNull() }
            items[index]["files"] = files
        }
        body["items"] = items
        let view = try Contract.decode(CallView.self, from: JSONSerialization.data(withJSONObject: body))
        return try XCTUnwrap(view.items.lazy.compactMap { item -> JobFile? in
            if case .job(let job) = item { job.files.first { $0.kind == .report } } else { nil }
        }.first)
    }

    @MainActor func testAReportOpensItsPage() async throws {
        let report = try report(withPage: true), page = try XCTUnwrap(report.pageURL)
        let server = FakeServer()
        server.replies["GET \(page)"] = .status(200, "<!doctype html><title>보고서</title><h1>코딩용 맥북</h1>")
        let files = ServerFiles(folder: try temporaryFolder())
        let local = await files.open(report, server: server.client())
        let opened = try XCTUnwrap(local)
        XCTAssertEqual(server.sent, ["GET \(page)"], "the page, fetched with the sign-in")
        XCTAssertEqual(server.requests.last?.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
        XCTAssertEqual(opened.pathExtension, "html")
        XCTAssertEqual(try String(contentsOf: opened, encoding: .utf8), "<!doctype html><title>보고서</title><h1>코딩용 맥북</h1>")
        XCTAssertTrue(ServerFiles.isPage(opened))
    }

    @MainActor func testAReportWithoutAPageOpensItsText() async throws {
        let report = try report(withPage: false)
        XCTAssertNil(report.pageURL)
        let server = FakeServer()
        server.replies["GET \(report.url)"] = .status(200, "# 코딩용 맥북")
        let files = ServerFiles(folder: try temporaryFolder())
        let local = await files.open(report, server: server.client())
        let opened = try XCTUnwrap(local)
        XCTAssertEqual(server.sent, ["GET \(report.url)"])
        XCTAssertEqual(opened.lastPathComponent, report.name)
        XCTAssertFalse(ServerFiles.isPage(opened))
    }

    @MainActor func testOtherFilesOpenAsThemselves() async throws {
        let table = try XCTUnwrap(try files().first { $0.kind == .table }, "the example has a table")
        XCTAssertNil(table.pageURL)
        let server = FakeServer()
        server.replies["GET \(table.url)"] = .status(200, "a,b\n1,2\n")
        let files = ServerFiles(folder: try temporaryFolder())
        let local = await files.open(table, server: server.client())
        let opened = try XCTUnwrap(local)
        XCTAssertEqual(server.sent, ["GET \(table.url)"])
        XCTAssertEqual(opened.lastPathComponent, table.name)
    }

    /// A folder of the test's own, removed when the test ends.
    @MainActor private func temporaryFolder() throws -> URL {
        let folder = FileManager.default.temporaryDirectory.appending(path: "report-page-tests-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: folder) }
        return folder
    }
}
