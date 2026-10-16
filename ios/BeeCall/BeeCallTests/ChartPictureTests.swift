import XCTest
@testable import BeeCall

/// Which picture of a chart the app shows (contract 2.6): in dark mode the chart drawn for it (image_url_dark); a
/// chart drawn before dark charts existed has none, and shows its light picture on a white plate, as a picture would.
final class ChartPictureTests: XCTestCase {
    /// The example's chart, as the file has it: drawn for both modes.
    @MainActor private func exampleChart() throws -> JobFile {
        try XCTUnwrap(Self.charts(try Examples.decode(CallView.self, "call-view")).first, "the example has a chart")
    }

    /// The same chart from before dark charts existed: no dark picture.
    @MainActor private func olderChart() throws -> JobFile {
        var body = try Examples.object("call-view")
        var items = try XCTUnwrap(body["items"] as? [[String: Any]])
        for (index, item) in items.enumerated() {
            guard var files = item["files"] as? [[String: Any]] else { continue }
            for file in files.indices where files[file]["kind"] as? String == "chart" {
                files[file]["image_url_dark"] = NSNull()
            }
            items[index]["files"] = files
        }
        body["items"] = items
        let view = try Contract.decode(CallView.self, from: JSONSerialization.data(withJSONObject: body))
        return try XCTUnwrap(Self.charts(view).first)
    }

    private static func charts(_ view: CallView) -> [JobFile] {
        view.items.flatMap { item -> [JobFile] in
            if case .job(let job) = item { job.files.filter { $0.kind == .chart } } else { [] }
        }
    }

    @MainActor func testLightModeShowsTheLightPicture() throws {
        let chart = try exampleChart(), picture = try XCTUnwrap(ChartPicture(chart, dark: false))
        XCTAssertEqual(picture.path, chart.imageURL)
        XCTAssertFalse(picture.onWhitePlate)
    }

    @MainActor func testDarkModeShowsTheDarkPicture() throws {
        let chart = try exampleChart(), picture = try XCTUnwrap(ChartPicture(chart, dark: true))
        XCTAssertNotNil(chart.imageURLDark)
        XCTAssertEqual(picture.path, chart.imageURLDark)
        XCTAssertFalse(picture.onWhitePlate)
    }

    @MainActor func testAnOlderChartShowsItsLightPictureOnAWhitePlateInDarkMode() throws {
        let chart = try olderChart(), picture = try XCTUnwrap(ChartPicture(chart, dark: true))
        XCTAssertNil(chart.imageURLDark)
        XCTAssertEqual(picture.path, chart.imageURL)
        XCTAssertTrue(picture.onWhitePlate)
        XCTAssertEqual(ChartPicture(chart, dark: false)?.onWhitePlate, false, "no plate in light mode")
    }

    @MainActor func testAFileWithoutAPictureHasNone() throws {
        let view = try Examples.decode(CallView.self, "call-view")
        let report = try XCTUnwrap(view.items.lazy.compactMap { item -> JobFile? in
            if case .job(let job) = item { job.files.first { $0.kind == .report } } else { nil }
        }.first)
        XCTAssertNil(ChartPicture(report, dark: true))
        XCTAssertNil(ChartPicture(report, dark: false))
    }
}
