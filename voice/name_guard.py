"""Keeps the user's name out of the call AI's replies after its first words.

The user's rule (2026-10-16): the name at most once in a call, near its start. That day's calls had "민승 님" in 15 of
24 replies. The instructions say so (rule 9, and the Korean rule with an example), but in calls in text Claude Haiku
5.5 still put the name where English says "you" in Korean, 2 to 9 times in a call of 12 turns; so, as with
book_call's words and the yes to a Bee save, the rule is also kept in code.

The first words are the prep's opening, said before any reply; it may greet the user by name. Every reply after it
passes through this guard, which takes out, for each form of the name the call knows (Bee's first name, and the name
as the prep says it in the call's language, such as 민승):
- the name with 님 or 씨 used for "you": followed by a particle that makes it the one who does or has something
  ("민승 님은 어떠세요?" is said "어떠세요?", "민승 씨처럼 여러 분야를" as "여러 분야를"), or by the next word ("민승 님
  주변에도" as "주변에도");
- the name said to the user, set off by its comma or "!": at a sentence's start ("민승 님, 천천히" as "천천히",
  "Minseung, how was it?" as "How was it?"), inside one ("좋은데, 민승 님, 뭐 드셨어요?" as "좋은데, 뭐 드셨어요?") or at its
  end ("알겠어요, 민승 님." as "알겠어요.", "Take care, Minseung." as "Take care.").
Anything else stays: the name beside another person's ("민승 님과 Dana 님", "민승 님 말고"), in a list ("Dana 님, 지수 님,
민승 님", "Dana, Jisoo, Minseung"), glued to a word, inside quotation marks (a Bee item is asked about as proposed), and
another person's name. Korean leaves "you" unsaid anyway, so what is left reads as a whole sentence.
"""

import re
from collections.abc import Iterable

HONORIFIC = r"(?:\s?(?:님|씨))"
# Particles after 님/씨 that make the name the one who does, has or is like something: it then stands for "you".
YOU = r"(?:께서는|께서도|께서|한테는|한테도|한테|에게는|에게도|에게|께는|께|은|는|이|가|도|의|처럼|만의|만)"
# Words after the name that set it beside someone else: the name stays.
BESIDE = r"(?:말고|빼고|대신|아니고|아니라|보다|하고|과|와|랑|이랑|및|또는|이나|나)(?![가-힣])"
ENDS = ".!?。！？…"
HOLD_AFTER = 8  # a name this close to where the text has got to is held back: what follows it decides
QUOTES = "'\"‘’“”「」『』«»"
APOSTROPHES = "'’"
# The name an opening greets the user with: 민승 in "민승 님, 안녕하세요." or "안녕하세요, 민승 님.", said to them (a comma or
# the sentence's end right after it) in the opening's first sentence. "팀장님과" later in it is someone else.
GREETED = re.compile(r"(?:^|[\s,])([가-힣]{2,4})\s?(?:님|씨)\s*(?=[,.!?。！？]|$)")


def user_names(first_name: str | None, opening: str | None, spoken: str | None = None) -> list[str]:
    """The forms of the user's name a call knows: Bee's first name, and the name as the prep says it in the call
    (`spoken`, voice/prep.py), or else the Korean name the opening greeted with."""
    names = [first_name.strip()] if first_name and first_name.strip() else []
    if spoken and spoken.strip():
        found = [spoken.strip()]
    else:
        first = re.split(r"(?<=[.!?。！？])\s", (opening or "").strip(), maxsplit=1)[0]
        found = GREETED.findall(first)
    names += [name for name in found if name not in names]
    return names


def _word(char: str) -> bool:
    return char.isalnum() or "가" <= char <= "힣"


def _apostrophe(before: str, after: str) -> bool:
    """A ' between two letters or digits of a Latin word ("it's", "Minseung's") is no quotation mark."""
    return bool(before and after) and before.isascii() and before.isalnum() and after.isascii() and after.isalnum()


def _listed(before: str) -> bool:
    """Whether a name after `before` (the clause up to its comma) is one of several people named: the item before it
    is a name with 님 or 씨, or one capitalised word after another comma ("Dana, Jisoo," but not "Thanks,")."""
    items = before.split(",")
    item = items[-1].strip()
    if re.search(r"(?:님|씨)$", item):
        return True
    return len(items) >= 2 and bool(re.fullmatch(r"[A-Z][a-z]+", item))


class NameGuard:
    """Feed it a reply's text in order, then flush() at the end. Text is held back only while it may still be, or
    lead into, a name to take out: a name's first letters at the end, a name whose next words have not come, a comma
    at the end (a name set off by it may follow), or an apostrophe (the next letter tells a quotation mark)."""

    def __init__(self, names: Iterable[str]) -> None:
        self._forms = sorted({n.strip() for n in names if n and len(n.strip()) >= 2}, key=len, reverse=True)
        self._held = ""
        self._last = ""  # the last character passed on: what comes before the next text
        self._sentence = ""  # what was passed on of the sentence still going (a list's earlier names, a vocative's comma)
        self._start = True  # at the start of the reply, or right after a sentence ended
        self._quoted = False  # inside quotation marks
        self.left_out = 0  # how many times the name was taken out
        if not self._forms:
            return
        name = "(?:" + "|".join(re.escape(form) for form in self._forms) + ")"
        alone = r"(?<![\w가-힣])" + name
        self._name = re.compile(alone)
        self._set_off = re.compile(alone + HONORIFIC + r"?\s*[,!！]\s*")  # "민승 님, ", "Minseung! "
        self._last_words = re.compile(r",\s*" + alone + HONORIFIC + r"?(?![\w가-힣])(?=\s*(?:[" + re.escape(ENDS)
                                      + r"]|$))")  # "알겠어요, 민승 님."
        self._as_you = re.compile(alone + HONORIFIC + "(?:" + YOU + r"(?![\w가-힣])\s*|\s+(?!" + BESIDE + r")(?=[가-힣]))")
        self._prefixes = {form[:k] for form in self._forms for k in range(1, len(form) + 1)}

    def feed(self, text: str) -> str:
        if not self._forms:
            return text
        text, self._held = self._held + text, ""
        cut = self._safe_end(text)
        self._held = text[cut:]
        return self._pass(text[:cut], final=False)

    def flush(self) -> str:
        text, self._held = self._held, ""
        return self._pass(text, final=True) if self._forms else text

    def _safe_end(self, text: str) -> int:
        """How far the text may be passed on: up to anything at its end that could still be, or lead into, a name
        to take out, or an apostrophe whose kind the next letter tells."""
        cut = len(text)
        for k in range(min(len(text), len(self._forms[0])), 0, -1):  # a name's first letters at the end
            before = text[-k - 1] if k < len(text) else self._last
            if text[-k:] in self._prefixes and not _word(before):
                cut = len(text) - k
                break
        for found in self._name.finditer(text):  # a name whose 님, particle or mark may not all have come yet
            rest = text[found.end():]
            if len(rest) <= HOLD_AFTER and not re.search(r"[.?。？…\n]", rest):
                cut = min(cut, found.start())
                break
        comma = re.search(r",\s*$", text[:cut])  # a name set off by this comma may follow
        if comma:
            cut = comma.start()
        if cut and text[cut - 1] in APOSTROPHES:
            cut -= 1
        return cut

    def _pass(self, text: str, final: bool) -> str:
        """The text with the name taken out where it is outside quotation marks, keeping what comes before it."""
        out: list[str] = []
        piece: list[str] = []
        for i, char in enumerate(text):
            before = text[i - 1] if i else self._last
            after = text[i + 1] if i + 1 < len(text) else ""
            if char in QUOTES and not (char in APOSTROPHES and _apostrophe(before, after)):
                self._put(out, "".join(piece), final=False)
                self._put(out, char, quoted=True)
                self._quoted = not self._quoted
                piece = []
            else:
                piece.append(char)
        self._put(out, "".join(piece), final=final)
        return "".join(out)

    def _put(self, out: list[str], text: str, *, final: bool = False, quoted: bool = False) -> None:
        if not text:
            return
        if not (quoted or self._quoted):
            text = self._clean(text, final)
        if text:
            out.append(text)
            self._last = text[-1]
            if text.rstrip():
                self._start = text.rstrip()[-1] in ENDS
            ended = list(re.finditer(r"[" + re.escape(ENDS) + r"](?=\s|$)|\n", text))
            self._sentence = text[ended[-1].end():] if ended else self._sentence + text

    def _clean(self, text: str, final: bool) -> str:
        lead = self._last  # the character before, for the patterns' look-behind
        whole = lead + text
        count = 0

        def clause(string: str, at: int) -> str:
            """The sentence so far up to `at` in `string`: what was passed on of it, then this text's part."""
            return re.split(r"[" + re.escape(ENDS) + r"]\s", self._sentence + string[len(lead):at])[-1]

        def drop(pattern: re.Pattern, string: str, keep) -> str:
            nonlocal count
            parts, at = [], len(lead)
            for found in pattern.finditer(string, len(lead)):
                if found.start() < at:
                    continue
                kept = keep(found, string)
                if kept is None:
                    continue
                parts.append(string[at:found.start()] + kept)
                at = found.end()
                count += 1
            return string[:len(lead)] + "".join(parts) + string[at:]

        def at_end(found: re.Match, string: str) -> str | None:  # ", 민승 님." at a clause's end
            if not final and found.end() == len(string):  # where the text has got to so far, not the reply's end
                return None
            return None if _listed(clause(string, found.start())) else ""

        whole = drop(self._last_words, whole, at_end)

        def set_off(found: re.Match, string: str) -> str | None:
            told = string[:found.start()].rstrip()
            if (not told and self._start) or (told and (told[-1] in ENDS or string[:found.start()].endswith("\n"))):
                return "\0"  # a sentence's start: its next letter is a capital, below
            if told.endswith(",") and string[found.start():found.end()].rstrip().endswith(","):
                return None if _listed(clause(string, found.start()).rstrip()[:-1]) else ""  # "좋은데, 민승 님, 뭐"
            return None

        whole = drop(self._set_off, whole, set_off)
        whole = re.sub("\0([a-z]?)", lambda m: m.group(1).upper(), whole)
        whole = drop(self._as_you, whole, lambda found, string: "")
        self.left_out += count
        return whole[len(lead):]
