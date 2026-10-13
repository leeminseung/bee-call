"""Keeps tool-call markup that some models write into their reply text out of the voice.

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
"""

import re

TAG_START = re.compile(r"<\s*[/|｜!?]?\s*[A-Za-z_｜|]")  # "<word", "</word", "<|word", "<｜word"
PARTIAL = re.compile(r"<\s*[/|｜!?]?\s*$")  # could still become a tag when more text arrives
TAG_NAME = re.compile(r"<\s*(/?)\s*([^\s>/]+)")
MAX_DROPPED = 500  # characters of dropped text kept for the call log
MAX_TAG = 200  # a "tag" longer than this without ">" is cut off as markup anyway


class SpeechFilter:
    """Feed it a reply's text chunks in order; it returns what may be spoken."""

    def __init__(self) -> None:
        self.dropped = ""
        self._open: list[str] = []  # names of tags opened and not yet closed
        self._held = ""  # text held back: a possible tag start, or a tag still waiting for its ">"

    def feed(self, text: str) -> str:
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

    def flush(self) -> str:
        """Call at the end of the reply: returns any text still held back that is speech."""
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
