"""A job's report as a readable page: report.md rendered as HTML beside it (report.html), with its own light and dark
styles, which the app opens instead of the raw text (contract 2.4, page_url).

The report is written by a model that read web pages, so the page is inert by construction. Markdown's raw HTML shows
as text (markdown-it-py with html off); a link goes only to an http, https or mailto address, and a bare address in the
text becomes one too; a picture shows its words and loads nothing (a remote one would tell its server the page was
opened); code keeps no class. The page's own Content-Security-Policy allows nothing but its inline styles, and the
files route adds its sandbox and nosniff headers. What comes out is checked against the tags and attributes this
renderer makes (check): a page that fails is not written, and the app shows the report's text as it is.

Reports written before pages existed: uv run python -m worker.report_page data/people/<id>   renders each missing page
"""

import argparse
import re
from collections.abc import Iterator
from html import escape
from html.parser import HTMLParser
from pathlib import Path

from markdown_it import MarkdownIt
from markdown_it.token import Token

REPORT, PAGE = "report.md", "report.html"
LINK = re.compile(r"(?:https?://|mailto:)[^\s\"'<>`]*", re.I)  # where a link may go
# An address written out in the text: ASCII only, so it ends where Korean words begin ("https://apple.com/kr에서").
BARE = re.compile(r"https?://[A-Za-z0-9][A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*", re.I)
REL = "noopener noreferrer nofollow"  # a followed link never carries this page's address (a call's id in it)
HANGUL = re.compile(r"[가-힣]")
CSP = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"

# The app's colour tokens from the dark mode design (2026-10-15): light, and dark as inside a sheet, where a report
# opens. Links are words in the ink with a yellow underline: blue is the person's own colour in the app.
STYLE = """
:root {
  color-scheme: light dark;
  --bg: #FFFFFF; --card: #F2F2F7; --ink: #21200B; --ink2: #6F6E60; --line: #C6C6C8; --link: #B89A00;
  --shade: rgba(33, 32, 11, 0.18);
}
@media (prefers-color-scheme: dark) {
  :root { --bg: #1C1C1E; --card: #2C2C2E; --ink: #FFFFFF; --ink2: #ABABB0; --line: #48484A; --link: #FFD900;
    --shade: rgba(0, 0, 0, 0.6); }
}
html { -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 17px/1.6 -apple-system, BlinkMacSystemFont, system-ui, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
  word-break: keep-all; overflow-wrap: anywhere;
}
main { max-width: 40rem; margin: 0 auto; padding: 20px 20px 48px; }
h1, h2, h3, h4, h5, h6 { line-height: 1.3; margin: 1.5em 0 0.5em; }
h1 { font-size: 1.5rem; margin-top: 0.3em; }
h2 { font-size: 1.25rem; }
h3 { font-size: 1.1rem; }
h4, h5, h6 { font-size: 1rem; }
p, ul, ol, blockquote, pre { margin: 0 0 1em; }
ul, ol { padding-left: 1.3em; }
li { margin: 0.3em 0; }
li > p { margin: 0; }
a { color: inherit; text-decoration: underline 2px var(--link); text-underline-offset: 3px; }
code { font: 0.88em/1.5 ui-monospace, "SF Mono", Menlo, monospace; background: var(--card); border-radius: 5px;
  padding: 0.1em 0.35em; }
pre { background: var(--card); border-radius: 12px; padding: 12px 14px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { margin-left: 0; padding-left: 14px; border-left: 3px solid var(--line); color: var(--ink2); }
hr { border: 0; border-top: 1px solid var(--line); margin: 1.5em 0; }
/* A table wider than the phone scrolls sideways, out to the screen's edges, with a shade on a side that has more. */
.table { overflow-x: auto; margin: 0 -20px 1em; padding: 0 20px;
  background: linear-gradient(to right, var(--bg) 40%, transparent) left / 40px 100% no-repeat local,
    linear-gradient(to left, var(--bg) 40%, transparent) right / 40px 100% no-repeat local,
    linear-gradient(to right, var(--shade), transparent) left / 14px 100% no-repeat scroll,
    linear-gradient(to left, var(--shade), transparent) right / 14px 100% no-repeat scroll; }
table { border-collapse: collapse; font-size: 0.9em; line-height: 1.45; }
th, td { min-width: 6em; padding: 8px 16px 8px 0; border-bottom: 1px solid var(--line); text-align: left;
  vertical-align: top; overflow-wrap: normal; }
th { color: var(--ink2); font-weight: 600; }
.image { color: var(--ink2); }
"""

# What the renderer makes, and so all a page's body may have: each tag with its attributes and the values they take.
TAGS: dict[str, dict[str, re.Pattern]] = {
    **{tag: {} for tag in ("body", "main", "h1", "h2", "h3", "h4", "h5", "h6", "p", "ul", "li", "em", "strong", "s",
                           "code", "pre", "blockquote", "hr", "br", "table", "thead", "tbody", "tr")},
    "ol": {"start": re.compile(r"\d{1,9}")},
    "a": {"href": LINK, "rel": re.compile(re.escape(REL))},
    "th": {"style": re.compile(r"text-align:(?:left|center|right)")},
    "td": {"style": re.compile(r"text-align:(?:left|center|right)")},
    "div": {"class": re.compile(r"table")},
    "span": {"class": re.compile(r"image")},
}


def _trim(address: str) -> str:
    """An address as written, without what ends the sentence around it: a full stop, a closing bracket it never
    opened."""
    while True:
        before = address
        address = address.rstrip(".,;:!?'\"*")
        for close, opening in ((")", "("), ("]", "[")):
            if address.endswith(close) and address.count(opening) < address.count(close):
                address = address[:-1]
        if address == before:
            return address


def _text(content: str) -> Token:
    return Token("text", "", 0, content=content)


def _linked(token: Token, md: MarkdownIt) -> Iterator[Token]:
    """A text token, its bare addresses made links."""
    text, at = token.content, 0
    for found in BARE.finditer(text):
        address = _trim(found.group())
        href = md.normalizeLink(address)
        if not (LINK.fullmatch(href) and md.validateLink(href)) or len(address) <= len("https://"):
            continue
        if found.start() > at:
            yield _text(text[at:found.start()])
        yield Token("link_open", "a", 1, attrs={"href": href, "rel": REL})
        yield _text(address)
        yield Token("link_close", "a", -1)
        at = found.start() + len(address)
    if not at:
        yield token
    elif at < len(text):
        yield _text(text[at:])


def _inert(state) -> None:
    """Links kept only to an http, https or mailto address (others keep their words), with REL and nothing else;
    bare addresses outside links made links."""
    for block in state.tokens:
        if block.type != "inline" or not block.children:
            continue
        children, kept = [], []  # kept: for each link open here, whether it stays a link
        for token in block.children:
            if token.type == "link_open":
                href = str(token.attrGet("href") or "")
                kept.append(bool(LINK.fullmatch(href)))
                if kept[-1]:
                    token.attrs = {"href": href, "rel": REL}
                    children.append(token)
            elif token.type == "link_close":
                if kept.pop():
                    children.append(token)
            elif token.type == "text" and not kept:
                children.extend(_linked(token, state.md))
            else:
                children.append(token)
        block.children = children


def _image(self, tokens: list[Token], idx: int, options, env) -> str:
    words = self.renderInlineAsText(tokens[idx].children or [], options, env)
    return f'<span class="image">{escape(words)}</span>' if words else ""


def _code(self, tokens: list[Token], idx: int, options, env) -> str:
    return f"<pre><code>{escape(tokens[idx].content)}</code></pre>\n"


def _markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    md.core.ruler.push("inert", _inert)
    md.add_render_rule("image", _image)
    md.add_render_rule("fence", _code)
    md.add_render_rule("code_block", _code)
    md.add_render_rule("table_open", lambda self, tokens, idx, options, env: '<div class="table"><table>\n')
    md.add_render_rule("table_close", lambda self, tokens, idx, options, env: "</table></div>\n")
    return md


MARKDOWN = _markdown()


class _Checker(HTMLParser):
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        allowed = TAGS.get(tag)
        if allowed is None:
            raise ValueError(f"a tag the renderer never makes: {tag[:20]}")
        for name, value in attrs:
            if name not in allowed or value is None or not allowed[name].fullmatch(value):
                raise ValueError(f"an attribute the renderer never makes on {tag}: {name[:20]}")

    def handle_comment(self, data: str) -> None:
        raise ValueError("a comment")

    def handle_decl(self, decl: str) -> None:
        raise ValueError("a declaration")

    def handle_pi(self, data: str) -> None:
        raise ValueError("a processing instruction")

    def unknown_decl(self, data: str) -> None:
        raise ValueError("a declaration")


def check(body: str) -> None:
    """ValueError unless every tag and attribute of a page's body is one the renderer makes (TAGS)."""
    checker = _Checker()
    checker.feed(body)
    checker.close()


def page_html(markdown: str) -> str:
    """The report as a whole page; ValueError if its body fails the check (it cannot, by construction)."""
    env: dict = {}
    tokens = MARKDOWN.parse(markdown, env)
    heading = next((tokens[i + 1] for i, t in enumerate(tokens[:-1]) if t.type == "heading_open"), None)
    korean = bool(HANGUL.search(markdown))
    title = (MARKDOWN.renderer.renderInlineAsText(heading.children or [], MARKDOWN.options, env).strip()
             if heading else "") or ("보고서" if korean else "Report")
    body = f"<body>\n<main>\n{MARKDOWN.renderer.render(tokens, MARKDOWN.options, env)}</main>\n</body>\n"
    check(body)
    return (f'<!doctype html>\n<html lang="{"ko" if korean else "en"}">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<meta http-equiv="Content-Security-Policy" content="{CSP}">\n'
            '<meta name="referrer" content="no-referrer">\n'
            '<meta name="color-scheme" content="light dark">\n'
            f"<title>{escape(title)}</title>\n<style>{STYLE}</style>\n</head>\n{body}</html>\n")


def write_page(folder: Path) -> Path | None:
    """folder/report.md rendered as folder/report.html, replaced whole so no reader sees half of it; None when the
    folder has no report. ValueError when the page fails its check."""
    try:
        text = (folder / REPORT).read_text(errors="replace")
    except FileNotFoundError:
        return None
    page = page_html(text)
    tmp = folder / f".{PAGE}.tmp"
    tmp.write_text(page)
    tmp.replace(folder / PAGE)
    return folder / PAGE


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m worker.report_page", description="Renders the page of each "
                                     "report a person's background jobs wrote before pages were rendered with them.")
    parser.add_argument("workspace", type=Path, help="the person's folder, data/people/<id>")
    args = parser.parse_args(argv)
    for report in sorted(args.workspace.glob(f"*/*/jobs/*/{REPORT}")):
        page = report.with_name(PAGE)
        if page.exists():
            continue
        try:
            write_page(report.parent)
        except ValueError as exc:
            print(f"{page}: left alone ({exc})")
            continue
        print(f"{page}: rendered")


if __name__ == "__main__":
    main()
