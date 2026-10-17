"""Whether words given as someone's own are what they said: book_call's check that the words it is given for a call
back are the user's (voice/coordinator.py), and the meta agent's for a call a voice memo asks for (meta/agent.py)."""

import re
from typing import Any

# What ends a sentence or a clause in what the user said, for book_call's `asked` (a turn's end does too). A dot
# ends one only before a space or the end, so "7 a.m." and "U.S." stay whole.
CLAUSE_END = re.compile(r"(?:[!?。？！…,，、;\n]|\.(?=\s|$))+")
# What takes back the request it follows in the same turn: "Call me at nine. Wait, no.", "Call me at 9 a.m. no
# actually don't.", "내일 아침 9시에 전화해 줘. 아니다, 하지 마." Words that only add to it take nothing back ("No rush
# though.", "Don't be late.", "Not earlier, please.", "아니면 10시도 괜찮아.").
TAKEN_BACK = re.compile(
    r"^\W*(?:no(?=\s*(?:[,.!?]|$)|\s+(?:wait|actually|don'?t|do not|never|cancel|forget|scratch|not)\b)|nope\b|nah\b|"
    r"(?:don'?t|do not)(?=\s*(?:[,.!?]|$)|\s+(?:call|bother|do it|worry about it)\b)|never ?mind\b|wait,? no\b|"
    r"actually,? no\b|cancel\b|forget it\b|scratch that\b|not now\b|"
    r"아니(?:다|야|요)?(?=[\s,.!?]|$)|아냐|아뇨|하지 ?마|하지 ?말|취소)", re.IGNORECASE)
# Words that turn around the words beside them: "Don't call me back", "No need to call", "전화 안 해도 돼요".
NEGATION = re.compile(r"\b(?:no|not|never|nope|dont|cannot)\b|n['’]t\b|(?<!\S)(?:안|못)(?!\S)", re.IGNORECASE)
HANGUL = re.compile(r"[\uac00-\ud7a3\u3131-\u318e]")


def _letters(text: str) -> str:
    """Text as it is compared for book_call: letters and digits only, in lower case."""
    return "".join(c for c in text.casefold() if c.isalnum())


def _lead(text: str, letters: int) -> str:
    """The start of `text` that holds its first `letters` letters and digits, as _letters counts them."""
    count = 0
    for i, c in enumerate(text):
        if count >= letters:
            return text[:i]
        count += len(_letters(c))
    return text


def _tail(text: str, letters: int) -> str:
    """The end of `text` that holds its last `letters` letters and digits."""
    return _lead(text[::-1], letters)[::-1]


def said_in(turns: list[str], words: Any) -> bool:
    """Whether `words` are what was said in `turns` (punctuation, spacing and case aside): most of a
    sentence or a clause between commas, or of several in a row (an answer split at pauses, over turns too).
    What they leave out of the first may be a lead-in such as "so" or "그럼", and of the last an English tail
    such as "about the dentist", but never a "not", a "don't" or a "안", and never the end of Korean words, where
    Korean says no: "call me back" out of "Don't call me back." is refused, and so are "내일은 전화하지" out of
    "내일은 전화하지 마세요." and "전화" out of "아니요, 전화는 안 해도 돼요.". Words the rest of the same turn
    takes back are refused too ("Call me at nine. Wait, no.", TAKEN_BACK). Whether the words ask for a call stays
    the call AI's reading."""
    wanted = _letters(words) if isinstance(words, str) else ""
    if not wanted:
        return False
    # Each piece: its text, its letters, and the rest of the same turn after it (all of it: "Wait, no." is split
    # at its comma), which may take back words that end in this piece.
    pieces: list[tuple[str, str, str | None]] = []
    for turn in turns:
        spans, at = [], 0
        for mark in CLAUSE_END.finditer(turn):
            spans.append((at, mark.start()))
            at = mark.end()
        spans.append((at, len(turn)))
        for start, end in spans:
            part = turn[start:end]
            if not _letters(part):
                continue
            rest = turn[end:]
            pieces.append((part, _letters(part), rest if _letters(rest) else None))
    for start, (first_text, first, _) in enumerate(pieces):
        run = ""
        for last_text, last, next_text in pieces[start:]:
            run += last
            if len(run) > 2 * len(wanted):  # the words would be less than half of what the user said
                break
            at = run.find(wanted)
            while at != -1:
                after = len(run) - at - len(wanted)
                if at < len(first) and after < len(last):  # within the first piece and the last one
                    lead, tail = _lead(first_text, at), _tail(last_text, after)  # what the words leave out
                    if not NEGATION.search(f"{lead} {tail}") and not HANGUL.search(tail) \
                            and not (next_text and TAKEN_BACK.search(next_text)):
                        return True
                at = run.find(wanted, at + 1)
    return False
