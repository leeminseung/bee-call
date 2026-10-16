"""Keeps the user's name out of the call AI's replies after its first words.

The user's rule (2026-10-16): the name at most once in a call, near its start. That day's calls had "민승 님" in 15 of
24 replies. The instructions say so (rule 9, and the Korean rule with an example), but in calls in text Claude Haiku
5.5 still put the name where English says "you" in Korean, 2 to 9 times in a call of 12 turns; so, as with
book_call's words and the yes to a Bee save, the rule is also kept in code.

The first words are the prep's opening, said before any reply; it may greet the user by name. Every reply after it
passes through this guard, which takes out, for each form of the name the call knows (Bee's first name, and the form
the opening greeted with, such as 민승 in "민승 님, 안녕하세요"):
- the name with 님 or 씨 and the particles fixed to them, as Korean speakers use it for "you": "민승 님은
  어떠세요?" is said "어떠세요?", and "민승 씨처럼 여러 분야를" as "여러 분야를";
- the name said to the user and set off by a comma: "알겠어요, 민승 님." is said "알겠어요.", "Minseung, how was
  it?" "How was it?", and "Take care, Minseung." "Take care.".
Anything else stays: the name as a word in a sentence, inside a Bee item ("'민승 생일 선물'"), or another person's
name. Korean leaves "you" unsaid anyway, so what is left reads as a whole sentence.
"""

import re
from collections.abc import Iterable

HONORIFIC = r"\s?(?:님|씨)[가-힣]{0,4}"  # 님 or 씨 and the particles fixed to them: 님은, 씨처럼, 님한테는
ENDS = ".!?。！？…"
# The name an opening greets the user with: 민승 in "민승 님, 안녕하세요." or "안녕하세요, 민승 님.", said to them (a comma or
# the sentence's end right after it) in the opening's first sentence. "팀장님과" later in it is someone else.
GREETED = re.compile(r"(?:^|[\s,])([가-힣]{2,4})\s?(?:님|씨)\s*(?=[,.!?。！？]|$)")


def user_names(first_name: str | None, opening: str | None) -> list[str]:
    """The forms of the user's name a call knows: Bee's first name, and the Korean name the opening greeted with."""
    names = [first_name.strip()] if first_name and first_name.strip() else []
    first = re.split(r"(?<=[.!?。！？])\s", (opening or "").strip(), maxsplit=1)[0]
    names += [found for found in GREETED.findall(first) if found not in names]
    return names


def _word(char: str) -> bool:
    return char.isalnum() or "가" <= char <= "힣"


class NameGuard:
    """Feed it a reply's text in order, then flush() at the end. Text is held back only while it may still be, or
    lead into, a name to take out: a name's first letters at the end, a name whose next words have not come, or a
    comma at the end (a name set off by it may follow)."""

    def __init__(self, names: Iterable[str]) -> None:
        self._forms = sorted({n.strip() for n in names if n and len(n.strip()) >= 2}, key=len, reverse=True)
        self._held = ""
        self._start = True  # at the start of the reply, or right after a sentence ended
        self.left_out = 0  # how many times the name was taken out
        if not self._forms:
            return
        name = "(?:" + "|".join(re.escape(form) for form in self._forms) + ")"
        said = r"(?<![\w가-힣])" + name + f"(?:{HONORIFIC})?" + r"(?![\w가-힣])"  # with or without 님/씨
        self._said = re.compile(said)
        self._said_last = re.compile(r",\s*" + said + rf"(?=\s*(?:[{re.escape(ENDS)}]|$))")  # "알겠어요, 민승 님."
        self._said_first = re.compile(said + r"\s*,\s*")  # "민승 님, 천천히", "Minseung, how was it?"
        self._as_you = re.compile(r"(?<![\w가-힣])" + name + HONORIFIC + r"(?![\w가-힣])\s*,?\s*")  # "민승 님은 "
        self._prefixes = {form[:k] for form in self._forms for k in range(1, len(form) + 1)}

    def feed(self, text: str) -> str:
        if not self._forms:
            return text
        text, self._held = self._held + text, ""
        cut = self._safe_end(text)
        self._held = text[cut:]
        return self._clean(text[:cut], final=False)

    def flush(self) -> str:
        text, self._held = self._held, ""
        return self._clean(text, final=True) if self._forms else text

    def _safe_end(self, text: str) -> int:
        """How far the text may be passed on: up to anything at its end that could still be, or lead into, a name
        to take out."""
        cut = len(text)
        for k in range(min(len(text), len(self._forms[0])), 0, -1):  # a name's first letters at the end
            if text[-k:] in self._prefixes and (k == len(text) or not _word(text[-k - 1])):
                cut = len(text) - k
                break
        for found in self._said.finditer(text):  # a name with nothing after it yet but spaces and commas
            if not text[found.end():].strip(" ,\t"):
                cut = min(cut, found.start())
                break
        comma = re.search(r",\s*$", text[:cut])  # a name set off by this comma may follow
        return comma.start() if comma else cut

    def _clean(self, text: str, final: bool) -> str:
        if not text:
            return text
        count = 0

        def last(found: re.Match) -> str:  # the clause-final name, where what follows it is known
            nonlocal count
            if not final and found.end() == len(text):
                return found.group(0)
            count += 1
            return ""

        text = self._said_last.sub(last, text)
        out, at, capital = [], 0, []
        for found in self._said_first.finditer(text):  # the name at a sentence's start
            before = text[:found.start()]
            told = before.rstrip()
            if not ((not told and self._start) or (told and told[-1] in ENDS) or before.rstrip(" \t").endswith("\n")):
                continue
            out.append(text[at:found.start()])
            capital.append(sum(map(len, out)))
            at = found.end()
            count += 1
        out.append(text[at:])
        text = "".join(out)
        for i in capital:  # the sentence still starts with a capital letter
            if i < len(text) and "a" <= text[i] <= "z":
                text = text[:i] + text[i].upper() + text[i + 1:]

        def you(found: re.Match) -> str:  # the name as "you", with 님 or 씨
            nonlocal count
            count += 1
            return ""

        text = self._as_you.sub(you, text)
        self.left_out += count
        told = text.rstrip()
        if told:
            self._start = told[-1] in ENDS
        return text
