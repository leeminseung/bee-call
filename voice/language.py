"""The call language, picked with CALL_LANGUAGE when the voice server and voice/outbound.py start.

English is the default and the language of the demo; Korean is for trying real situations in the user's own
language. The language sets what Amazon Transcribe listens for, Amazon Polly's voice, the language the
coordinator speaks (one rule added to its instructions; the rest of the instructions stay in English), the
prep's opening line, and the fallback greetings. LiveKit's turn detector follows the transcript's language.
One call is in one language: Transcribe listens for one language per stream.
"""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class CallLanguage:
    code: str
    name: str
    transcribe: str  # Amazon Transcribe streaming language code
    voice: str  # Amazon Polly voice; both work with the generative engine in Sydney (checked 2026-10-14)
    opening: str  # how the prep's opening line should sound (voice/prep.py)
    rule: str = ""  # added to the coordinator's instructions
    greetings: dict[str, str] = field(default_factory=dict)


LANGUAGES = {
    "en": CallLanguage(
        code="en", name="English", transcribe="en-US", voice="Ruth",
        opening="under 20 words of spoken English",
        greetings={
            "echo": "Hi! This is the echo test. Say something and I'll repeat it.",
            "voice note": "Hi, it's Bee Call. I'm calling about your new voice note. Do you have a minute?",
            "topic": "Hi, it's Bee Call. I'm calling about your conversation: {topic}. Do you have a minute?",
            "outbound": "Hi, it's Bee Call. Do you have a minute to talk through your day?",
            "inbound": "Hi, it's Bee Call. What's on your mind?",
        }),
    "ko": CallLanguage(
        code="ko", name="Korean", transcribe="ko-KR", voice="Seoyeon",
        opening="one or two short sentences of spoken Korean, in the polite style",
        rule=("Speak Korean with the user, in natural, polite spoken Korean. The app's notes, Bee data and job "
              "results may be in English; say what matters in Korean, and keep names and numbers exact."),
        greetings={
            "echo": "안녕하세요, 에코 시험이에요. 말씀하시면 그대로 따라 할게요.",
            "voice note": "안녕하세요, Bee Call이에요. 새 음성 메모 때문에 전화드렸어요. 잠깐 통화 괜찮으세요?",
            "topic": "안녕하세요, Bee Call이에요. 방금 나누신 대화 때문에 전화드렸어요. 잠깐 통화 괜찮으세요?",
            "outbound": "안녕하세요, Bee Call이에요. 잠깐 오늘 하루 이야기 나눌 수 있을까요?",
            "inbound": "네, Bee Call이에요. 어떤 이야기 해 볼까요?",
        }),
}


def call_language(code: str | None = None) -> CallLanguage:
    code = (code if code is not None else os.environ.get("CALL_LANGUAGE", "en")).strip().lower() or "en"
    if code not in LANGUAGES:
        raise SystemExit(f"CALL_LANGUAGE must be one of {', '.join(LANGUAGES)}, not {code!r}")
    return LANGUAGES[code]
