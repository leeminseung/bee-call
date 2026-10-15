"""Keeps tool-call markup and stage directions that models write into their reply text out of the voice.

Seen in the coordinator bench and a synthetic call (2026-10-12 and 13):
- MiniMax M2.5 wrote '<minimax:tool_call> <invoke name="end_call"> </invoke>' after its goodbye,
- GLM 5 wrote '<system>end_call</system>' after a reply, and began another with a stray
  '</tool_call>' right before its spoken sentence,
- DeepSeek V3.2 wrote '<｜DSML｜function_calls' and '<function_calls>' between sentences.
Polly would read these out, and the captions and the call history would show them.

So tags are dropped, and so is anything inside a tag pair (the tool name and arguments).
A closing tag with no opening tag is dropped on its own and the words after it are kept:
cutting everything after the first tag lost GLM 5's whole sentence once. A tag left open
at the end of the reply drops the rest of the reply.

Stage directions too (AsideFilter): in the user's call of 2026-10-15 14:22, after they said only "어", GLM 5 wrote
its whole reply as "(사용자가 잠시 멈춤)" and the voice read it out. Every word the AI writes is spoken, so an aside
that stands as its own sentence, wrapped in () or [] (or their full-width forms), is dropped: one at the start of
the reply or of a sentence, followed by a space, a line break or the end, and only a few plain words long (no
digits, no commas or sentence marks inside). A parenthesis inside a sentence, as in "AWS(아마존 웹 서비스)",
stays, and so do "(02) 1234-5678" and a parenthetical sentence of real words. Asterisks are never taken for an
aside: "**Saturday**" and "* item" are markdown, which LiveKit's own filter turns into plain words. A reply that is
only an aside says nothing at all: no space or line break comes out before the first word.
"""

import re

TAG_START = re.compile(r"<\s*[/|｜!?]?\s*[A-Za-z_｜|]")  # "<word", "</word", "<|word", "<｜word"
PARTIAL = re.compile(r"<\s*[/|｜!?]?\s*$")  # could still become a tag when more text arrives
TAG_NAME = re.compile(r"<\s*(/?)\s*([^\s>/]+)")
MAX_DROPPED = 500  # characters of dropped text kept for the call log
MAX_TAG = 200  # a "tag" longer than this without ">" is cut off as markup anyway
ASIDE_CLOSERS = {"(": ")）", "（": ")）", "[": "]］", "［": "]］"}  # an aside's opener, and what closes it
SENTENCE_ENDS = ".!?。！？…"
NOT_IN_ASIDES = re.compile(r"[\d,.!?…，、。！？]")  # brackets around a number or a sentence hold speech
MAX_ASIDE = 40  # an "aside" longer than this is speech: a stage direction is a few words


class AsideFilter:
    """Drops stage directions that stand as their own sentence; feed it text in order, then flush() at the end.
    Text is held back only while a sentence starts with an aside's opener and it is not yet known whether the aside
    stands alone."""

    def __init__(self, drop) -> None:
        self._drop = drop  # where dropped text goes (SpeechFilter keeps it for the call log)
        self._start = True  # at the start of the reply or of a sentence
        self._held = ""
        self._last = ""  # the last character passed on

    def feed(self, text: str) -> str:
        text, self._held = self._held + text, ""
        out: list[str] = []
        i = 0
        while i < len(text):
            if self._start:
                j = i
                while j < len(text) and text[j].isspace():
                    j += 1
                out.append(text[i:j])
                i = j
                if i == len(text):
                    break
                if text[i] in ASIDE_CLOSERS:
                    decided = self._aside(text[i:])
                    if decided is None:  # its end has not come yet
                        self._held = text[i:]
                        break
                    length, aside = decided
                    if aside:
                        self._drop(text[i:i + length])
                        i += length
                        while i < len(text) and text[i] in " \t":
                            i += 1
                        continue  # still at the start of a sentence
                    out.append(text[i:i + length])
                    self._last = text[i + length - 1]
                    i += length
                self._start = False
                continue
            c = text[i]
            out.append(c)
            if c == "\n" or (c.isspace() and self._last in SENTENCE_ENDS):
                self._start = True
            self._last = c
            i += 1
        return "".join(out)

    def flush(self) -> str:
        """At the end of the reply: a held aside with nothing after it is dropped; anything else is speech."""
        held, self._held = self._held, ""
        if not held:
            return ""
        decided = self._aside(held + " ")
        if decided is not None and decided[1] and not held[decided[0]:].strip():
            self._drop(held)
            return ""
        self._start = False
        return held

    @staticmethod
    def _aside(text: str) -> tuple[int, bool] | None:
        """For text that starts with an opener: (its length, whether it stands alone as an aside), or None while
        more text is needed to tell."""
        closers = ASIDE_CLOSERS[text[0]]
        end = next((k for k in range(1, min(len(text), MAX_ASIDE)) if text[k] in closers), None)
        if end is None:
            return (1, False) if len(text) >= MAX_ASIDE else None
        if NOT_IN_ASIDES.search(text[1:end]):
            return end + 1, False
        stop = end + 1
        while stop < len(text) and text[stop] in SENTENCE_ENDS:
            stop += 1
        if stop == len(text):
            return None  # what comes next decides
        return (stop, True) if text[stop].isspace() else (stop, False)


class SpeechFilter:
    """Feed it a reply's text chunks in order; it returns what may be spoken."""

    def __init__(self) -> None:
        self.dropped = ""
        self._open: list[str] = []  # names of tags opened and not yet closed
        self._held = ""  # text held back: a possible tag start, or a tag still waiting for its ">"
        self._asides = AsideFilter(self._drop)
        self._spoke = False  # whether a word has come out yet

    def feed(self, text: str) -> str:
        return self._words(self._asides.feed(self._tags(text)))

    def flush(self) -> str:
        """Call at the end of the reply: returns any text still held back that is speech."""
        return self._words(self._asides.feed(self._flush_tags()) + self._asides.flush())

    def _words(self, text: str) -> str:
        # Nothing comes out before the first word: a reply that says nothing (only an aside, a tag, or a line break
        # around them) leaves no turn behind. LiveKit would file "\n" in the history, and Bedrock refuses a blank
        # text block in every request after it.
        if not self._spoke:
            text = text.lstrip()
            self._spoke = bool(text)
        return text

    def _tags(self, text: str) -> str:
        text, self._held = self._held + text, ""
        out = []
        while text:
            i = text.find("<")
            if i < 0:
                self._keep(out, text)
                break
            self._keep(out, text[:i])
            rest = text[i:]
            if TAG_START.match(rest):
                end = rest.find(">")
                if end < 0:
                    if len(rest) < MAX_TAG:
                        self._held = rest  # the tag continues in the next chunk
                    else:
                        self._drop(rest)
                        self._open.append("?")
                    break
                self._tag(rest[: end + 1])
                text = rest[end + 1:]
                continue
            if PARTIAL.match(rest):
                self._held = rest
                break
            self._keep(out, "<")  # a plain "<", as in "under < 5"
            text = rest[1:]
        return "".join(out)

    def _flush_tags(self) -> str:
        held, self._held = self._held, ""
        if not held:
            return ""
        if TAG_START.match(held):  # a tag that never closed is markup
            self._drop(held)
            return ""
        return "" if self._open else held

    def _tag(self, tag: str) -> None:
        self._drop(tag)
        m = TAG_NAME.match(tag)
        closing, name = (m.group(1) == "/", m.group(2)) if m else (False, "?")
        if name.startswith(("|", "｜")) or tag.endswith("/>"):
            return  # a special token such as <|tool_call|>, or a self-closing tag: nothing opens
        if closing:
            if name in self._open:  # close it and anything left open inside it
                del self._open[self._open.index(name):]
            return  # a stray closing tag on its own: the words after it are speech
        self._open.append(name)

    def _keep(self, out: list[str], text: str) -> None:
        if self._open:
            self._drop(text)
        else:
            out.append(text)

    def _drop(self, text: str) -> None:
        self.dropped += text[: max(0, MAX_DROPPED - len(self.dropped))]


def speech_only(text: str) -> str:
    """A whole text as it may be spoken (the prep's opening line, said before any reply)."""
    speech = SpeechFilter()
    return (speech.feed(text) + speech.flush()).strip()

