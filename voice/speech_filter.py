"""Keeps tool-call markup that some models write into their reply text out of the voice.

In the coordinator bench (2026-10-12), DeepSeek V3.2, GLM 5 and MiniMax M2.5 sometimes
wrote their tool call as text after the spoken part, for example
'<minimax:tool_call> <invoke name="end_call">' or '<system>end_call</system>', while also
making the real tool call. Polly would read it out, and the captions and the call history
would show it. Spoken replies have no reason to contain a tag, so once a reply starts one,
the rest of that reply is not speech.
"""

import re

TAG = re.compile(r"<\s*[/|｜!?]?\s*[A-Za-z_｜]")  # "<word", "</word", "<|word", "<｜word"
PARTIAL = re.compile(r"<\s*[/|｜!?]?\s*$")  # could still become a tag when more text arrives
MAX_DROPPED = 500  # characters of dropped text kept for the call log


class SpeechFilter:
    """Feed it a reply's text chunks in order; it returns what may be spoken."""

    def __init__(self) -> None:
        self.stopped = False
        self.dropped = ""
        self._pending = ""  # a "<" held back until it is clear whether a tag starts there

    def feed(self, text: str) -> str:
        if self.stopped:
            self._drop(text)
            return ""
        text, self._pending = self._pending + text, ""
        out = []
        while text:
            i = text.find("<")
            if i < 0:
                out.append(text)
                break
            out.append(text[:i])
            rest = text[i:]
            if TAG.match(rest):
                self.stopped = True
                self._drop(rest)
                break
            if PARTIAL.match(rest):
                self._pending = rest
                break
            out.append("<")  # a plain "<", as in "under < 5"
            text = rest[1:]
        return "".join(out)

    def flush(self) -> str:
        """Call at the end of the reply: returns any text still held back."""
        rest, self._pending = self._pending, ""
        return "" if self.stopped else rest

    def _drop(self, text: str) -> None:
        self.dropped += text[: max(0, MAX_DROPPED - len(self.dropped))]
