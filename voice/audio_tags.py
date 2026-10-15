"""ElevenLabs audio tags in the call AI's speech: sounds such as [laughs] and [sighs] that ElevenLabs' v3 and v4
models perform instead of reading out. The user heard [laughs] and [sighs] work on eleven_v4_turbo with our voices
(2026-10-15; Scribe's transcripts do not show such sounds).

Where a tag goes:
  the call AI        is told it may use the SPOKEN tags sparingly, only where a person would, when its voice
                     performs them (VoiceChoice.performs_tags: ElevenLabs v4); otherwise it is told to write none
                     (voice/coordinator.py speech_rule)
  the speech filter  keeps a KNOWN tag where it drops other bracketed asides (voice/speech_filter.py)
  the voice          gets the tags only when it performs them: for Polly or another ElevenLabs model they are
                     removed before speaking, so they are never read out (Coordinator.tts_node)
  the captions       never show them, nor do the conversation's history and the call record, which LiveKit fills
                     from the same text (Coordinator.transcription_node); the call view strips them too, for any
                     record written otherwise (voice/call_view.py)
  the model log      keeps them as the model wrote them
This starts the tuning the user named "실제 목소리 같은 음성을 위한 지시문" (instructions for speech that sounds
real): what is added here is listed in the README, to be tuned by ear, later per voice too (the voice catalog's
tuning: a tag or written laughter, voice/voices.py).
"""

import re
from collections.abc import AsyncIterable, AsyncIterator

# What the call AI is told it may write.
SPOKEN = ("laughs", "sighs", "exhales")
# Every tag kept, stripped and removed as one: the voice tags ElevenLabs documents for v3 and v4 (its prompting
# guide, 2026-10-15), so one the model writes of its own accord is never read out or shown either.
KNOWN = SPOKEN + ("laughs harder", "starts laughing", "wheezing", "whispers", "sarcastic", "curious", "excited",
                  "crying", "snorts", "mischievously")
TAG = re.compile(r"\[\s*(?:" + "|".join(re.escape(t) for t in KNOWN) + r")\s*\]", re.IGNORECASE)
LONGEST = max(len(t) for t in KNOWN) + 4  # a "[" with more than this after it and no "]" is no tag


def is_tag(text: str) -> bool:
    return bool(TAG.fullmatch(text))


class TagStripper:
    """Removes known tags from text that arrives in chunks: feed() each chunk in order, then flush() at the end.
    A removed tag takes one space with it, so the words around it keep one between them; a tag split across chunks
    is held until its "]" comes. Anything else, other brackets among it, passes as it was written."""

    def __init__(self) -> None:
        self._space = ""  # whitespace not yet passed on: whether it goes depends on what follows
        self._held = ""  # a "[" and what came after it, until it is known whether it is a tag
        self._spoke = False  # anything passed on yet
        self._after_tag = False  # a tag was removed and no word has come since

    def feed(self, text: str) -> str:
        text, self._held = self._held + text, ""
        out: list[str] = []
        i = 0
        while i < len(text):
            c = text[i]
            if c == "[":
                end = text.find("]", i)
                if end < 0 and len(text) - i <= LONGEST:
                    self._held = text[i:]
                    break
                if end >= 0 and is_tag(text[i:end + 1]):
                    self._after_tag = True
                    i = end + 1
                    continue
            if c.isspace():
                # Space right after a removed tag goes when there is space before it already, or nothing yet.
                if not (self._after_tag and (self._space or not self._spoke)):
                    self._space += c
                i += 1
                continue
            out.append(self._space + c)
            self._space, self._spoke, self._after_tag = "", True, False
            i += 1
        return "".join(out)

    def flush(self) -> str:
        """At the end of the text: a "[" still held never closed, so it is no tag and passes as written; the space
        before a tag that ended the text goes with it."""
        held, self._held = self._held, ""
        out = ""
        if held:
            out, self._space, self._spoke, self._after_tag = self._space + held, "", True, False
        space = "" if self._after_tag else self._space
        self._space, self._after_tag = "", False
        return out + space


def strip_tags(text: str) -> str:
    """A whole text without its known tags (the call view)."""
    stripper = TagStripper()
    return stripper.feed(text) + stripper.flush()


async def without_tags(text: AsyncIterable[str]) -> AsyncIterator[str]:
    """A stream of text without its known tags (the call AI's voice and captions, voice/coordinator.py)."""
    stripper = TagStripper()
    async for chunk in text:
        kept = stripper.feed(str(chunk))
        if kept:
            yield kept
    tail = stripper.flush()
    if tail:
        yield tail
