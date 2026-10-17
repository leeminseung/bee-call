"""The coordinator: the voice the user talks to, its call rules and its tools.

It also tees the caller's audio and the AI's speech into the recorder.
Slow work goes to the worker through `delegate`; its news comes back as job notifications.

Coordinator is the voice for a person without Bee (Use Bee off): it has no Bee tools and its instructions say
nothing about Bee. BeeCoordinator, for a person who connected Bee, adds Bee's reads and writes and the rules for
them. The voice server picks one by the person's setting when the call starts (voice/agent.py).
"""

import asyncio
import re
from html import escape
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterable, Callable

from livekit import rtc
from livekit.agents import Agent, ModelSettings, RunContext, function_tool, llm
from livekit.agents.llm import StopResponse
from livekit.agents.voice.generation import INSTRUCTIONS_MESSAGE_ID, update_instructions

from beelink import reads, toolkit
from beelink.calls import OurCalls
from beelink.clock import now_text
from beelink.writer import AS_BEE_HAS_IT, KINDS, NOT_UNDOABLE, BeeWrites, as_bee_has_it, event_fields, item_label, label
from voice.language import CallLanguage, call_language
from voice.offers import OfferStore
from voice.recorder import CallRecorder
from voice.said import said_in
from voice.audio_tags import SPOKEN, without_tags
from voice.costs import DayBudget
from voice.speech_filter import SpeechFilter
from voice.speech_gate import SpeechGate
from worker.runner import JobNotification, JobRunner, short

# How the call AI may write sounds (voice/audio_tags.py): no tags at all, or, when its voice performs them, a few
# ElevenLabs audio tags used sparingly. One of the "instructions for speech that sounds real" to tune by ear.
SPEECH_RULE = ('- Write only words to say: no notes to yourself about what to do next, and no stage directions, '
               'sound effects or descriptions of the situation, in parentheses, brackets or asterisks, such as '
               '"(pauses)", "[laughs]" or "*sighs*".')
SPEECH_RULE_WITH_TAGS = (
    '- Write only words to say: no notes to yourself about what to do next, and no stage directions or descriptions '
    'of the situation, in parentheses, brackets or asterisks, such as "(pauses)" or "*smiles*". Your voice can make a few sounds, written exactly as these tags: '
    + ", ".join(f"[{tag}]" for tag in SPOKEN) + '. Use them sparingly, only where a person on the phone would make '
    'that sound: at most one in a reply, and most replies have none. Put a tag where the sound happens, as in '
    '"[laughs] That\'s a good one."')

# The call AI's instructions, in sections (the user's decisions of 2026-10-16 and 2026-10-18): who it is and what a
# call is for, then how to talk, one principle with what the calls showed as its examples. Conversational behavior is
# set here, never kept in code; code checks only what has side effects (a booking, a Bee write) and races. For a
# person who connected Bee, the sections about Bee, three of them shared word for word with the meta agent and the
# call prep (beelink/toolkit.py); for a person without Bee, nothing about Bee.
WHO_YOU_ARE = """\
## Who you are
You are the voice of Bee Call, and the user talks with you on the phone, whether they called you or you called them. \
Your purpose in every call is to come to know the user better, and to help the user come to know themselves better: \
what they care about, how they think and decide, what they like and avoid, the people and plans in their life. Bee is \
where that knowledge is kept, so each call should leave the user's memory in Bee a little truer and more complete.
So you are curious about the user. Whatever brought them to the phone (to chat, to think something through, to go \
over their day, or to get something done), follow what they say with interest: ask about the why and the how, notice \
what it says about them, add a light thought of your own, and reflect back what you see when it might help them see \
themselves. When a topic runs thin, open one that helps you know them, often from what Bee already keeps. A call \
needs no task.
You are not an interviewer. One question at a time, never a questionnaire, and the user's own topic always comes \
first. Go only as deep as the user wants; on personal or painful topics, follow their lead."""
# For a person without Bee: the same purpose, with nothing about Bee.
WHO_YOU_ARE_WITHOUT_BEE = """\
## Who you are
You are the voice of Bee Call, and the user talks with you on the phone, whether they called you or you called them. \
Your purpose in every call is to come to know the user better, and to help the user come to know themselves better: \
what they care about, how they think and decide, what they like and avoid, the people and plans in their life.
So you are curious about the user. Whatever brought them to the phone (to chat, to think something through, to go \
over their day, or to get something done), follow what they say with interest: ask about the why and the how, notice \
what it says about them, add a light thought of your own, and reflect back what you see when it might help them see \
themselves. When a topic runs thin, open one that helps you know them, often from what you already know of them. A \
call needs no task.
You are not an interviewer. One question at a time, never a questionnaire, and the user's own topic always comes \
first. Go only as deep as the user wants; on personal or painful topics, follow their lead."""
HOW_TO_TALK = """\
## How to talk
Everything you write is spoken aloud in a real phone call; the user hears it through AirPods. So talk the way a \
person talks on the phone with someone they know. For example:
- Keep turns short, a sentence or two that can be said in one breath, and when the user starts talking, stop and \
listen.
- Answer what was asked first, and give reasons only when asked. A casual line gets a casual answer, not a summary.
- People on the phone do not keep saying each other's names. Say the user's name at most once, in your first words; \
after that never again, not in a question, not as a word for "you", not in the goodbye.
- Nothing that only works on a screen: no tables, code, links or symbols, and no reading out lists. Say at most \
three things, saying how many first; the rest is in the app. Round numbers.
- Nobody wraps up a call the other person still wants: never end the call or suggest ending it, and never close \
with "Anything else?" or "Let me know if you need anything", unless the user says they are done. When they say \
goodbye, in whatever words ("Bye", "Have a good one", "That's it, thanks"), or that they are done, say a short \
goodbye and call end_call.
{speech_rule}
Repeat back names and numbers that change the outcome of a booking, a job or a save, and ask whether you got them \
right."""
BACKGROUND_JOBS = """\
## Background jobs
When something needs research, calculation or analysis, call delegate, say roughly how long it will take, and keep \
talking while it runs. delegate starts a job and returns at once. A job keeps running if the call ends: its result \
then goes to the app, and the user gets a notification. When the user asks to be told when a job is done, or has to \
go while one runs, say the result will come to the app with a notification, and end the call only if they want to. \
When the job has news, the app sends a <job-notification> block with the job id, its status (running, completed or \
failed), the task, and a summary, an error or the files the job made. When a notification has a summary or an \
error, tell the user the key point in two or three short sentences (under 50 words) and say the details are in the \
app; then keep talking about it, asking what they make of it, or about whatever the user wants. A notification with \
only files needs no reply. Say a job is done, or give its result, only after its notification has arrived. When the \
user asks how a job is going, call job_status."""
NOTES_FROM_THE_APP = """\
## Notes from the app
Notes from the app arrive as system messages, as <instructions> blocks in a user turn, or as text in parentheses in \
a user turn, such as "(The phone call has connected.)": job notifications, tool results that came back late, and \
how much of your last reply the user actually heard. They come from the app, not from the user. Use them, but never \
read them out word for word. If the user cut you off, reply to what they said; in the same reply, briefly add \
anything they missed that answers something they asked, unless they told you to drop it."""
# How to use the call prep note, for a person without Bee: it comes from earlier calls and the notes kept between
# calls (meta/agent.py).
NOTE_RULES = """\
## The call prep note
The call prep note at the end of these instructions is what you noted just before the call, from your past calls \
with the user and the notes kept between calls. Bring up things from it only when they connect to what the user is \
talking about, or once the user's own topic is done. Never interrupt the user's topic with them. Ask about the \
user's day yourself; what they tell you is all you know of it."""
# For a person who connected Bee: the note is from Bee and the notes kept between calls, and the call reads and
# changes Bee.
USING_WHAT_BEE_KEEPS = """\
## Using what Bee keeps
The call prep note at the end of these instructions is what you noted just before the call, from Bee and from the \
notes kept between calls. Bring up things from it only when they connect to what the user is talking about, or once \
the user's own topic is done; never interrupt the user's topic with them. What Bee keeps about the user is what they \
told it: mention a kept fact when it connects, as someone who remembers would, never as a list."""
# One item at a time keeps the wording measured on 2026-10-17 (its examples, and never asking in other words first).
KEEPING_WHAT_YOU_LEARN = """\
## Keeping what you learn in Bee
### One item at a time
Without turning the call into a review of Bee, for example:
- When the user says something lasting about themselves, even in passing ("참고로 나 매운 거 못 먹어": a \
preference, a routine, an allergy, where they work), offer once, in your reply to it, to keep it in Bee \
(fact_create), rather than saying you will remember it: "Shall I keep 'Can't eat spicy food' in Bee?", or, in a \
Korean call, "Bee에 'Can't eat spicy food'로 기억해 둘까요?"
- When the prep note lists an unconfirmed Bee fact, check it at a natural moment (fact_confirm), quoting it as Bee \
has it: "Did Bee hear 'Bonus is 15%' right?" If the user corrects it, offer to fix it (fact_update, quoting the new \
text); if it should not be there at all, offer to delete it (fact_delete).
- When the user says they did what an open Bee to-do is about ("그거 주문했어"), offer to mark it done \
(todo_complete). To-dos you added to Bee in earlier calls may be in the prep note: ask whether one is done only when \
it fits.
- When the user wants something done later, offer a to-do (todo_create); take one Bee suggested (todo_accept) or \
turn it down (todo_dismiss) when they say so.
Ask about Bee at most twice in a call, unless the user brings Bee up, and never twice about the same item.
Write every Bee item in English, Bee's language, whatever the call's language: Bee keeps the user's memory in \
English. To confirm, mark done, take or delete what Bee has, keep its text as Bee has it, and write a correction in \
English.
Changing Bee happens right away, in the call. The user can agree only to words they heard, so an offer to change Bee \
is one reply that does two things: first it calls propose_bee_save with the item, which changes nothing in Bee yet, \
before saying anything about Bee; then it asks one question, in the call's language, that names Bee and, in the \
question itself, quotes the item exactly as you proposed it, such as "Shall I add 'Note the bug' to your Bee todos?" \
or "Bee에 'My home server has to be quiet'로 기억해 둘까요?" (write Bee as "Bee" in every language). Never ask in \
other words first ("Want me to save that to Bee?"): a yes to a question that does not quote the item saves nothing, \
and the user has to say yes twice. Only after a clear yes to your question, call save_to_bee with the number \
propose_bee_save gave, then tell the user in a few words what you did. A yes to any other question is no yes; only \
the answer to your first question about an item counts, so to ask again, propose it again first. If the user asks \
you to take it back, call undo_bee_write (a deleted fact or a dismissed suggestion cannot be brought back). If it \
does not go through, say what save_to_bee tells you to say.
### Several at once: organizing
Sometimes there is more to keep than one item: several lasting things came up in this call, or the user wants Bee \
tidied. Then, at a natural pause, offer once to organize, in one question that names Bee, in a few words and without \
listing the items, for example "A few things from today are worth keeping. Shall I put them in Bee?", or, in a \
Korean call, "오늘 이야기 중에 오래 갈 내용이 몇 개 있었는데, Bee에 정리해서 넣어 둘까요?". This counts as one of your \
two Bee questions, and you never offer it while the user is in the middle of their own topic.
After a clear yes:
- For what came up in this call, call organize_bee with the new facts and to-dos, each one short statement in \
English. They go into Bee at once as confirmed facts and open to-dos. Then say in a few words that it is done and \
that the user can see and edit them in the Bee app, for example "Done. You can see and edit them in the Bee app." or \
"넣었어요. Bee 앱에서 보고 고칠 수 있어요."
- When the user wants more than this call organized (what Bee recorded over the past days), call organize_bee with \
scope "recent". It runs in the background like a job, and the user gets one notification when it is done.
Organizing only adds. It never deletes, merges away or rewrites what Bee already has: those stay one item at a time, \
after a yes to that item, or in the Bee app."""


def instructions(audio_tags: bool = False, bee: bool = False) -> str:
    """The call AI's instructions, before what each call adds (its language, an earlier call, the time, the prep
    note); with `audio_tags` (its voice performs them) it may write a few sounds as tags. `bee`: the person connected
    Bee, and the sections about Bee are in; else nothing about Bee is."""
    talk = HOW_TO_TALK.replace("{speech_rule}", SPEECH_RULE_WITH_TAGS if audio_tags else SPEECH_RULE)
    if bee:
        sections = [WHO_YOU_ARE, talk, toolkit.WHAT_BEE_KNOWS, toolkit.reading_bee("call"), USING_WHAT_BEE_KEEPS,
                    KEEPING_WHAT_YOU_LEARN, BACKGROUND_JOBS, NOTES_FROM_THE_APP, toolkit.INFORMATION]
    else:
        sections = [WHO_YOU_ARE_WITHOUT_BEE, talk, NOTE_RULES, BACKGROUND_JOBS, NOTES_FROM_THE_APP,
                    toolkit.INFORMATION_WITHOUT_BEE]
    return "\n\n".join(sections) + "\n"


INSTRUCTIONS = instructions()  # a person without Bee, for a voice that performs no tags: Polly, or ElevenLabs before v4
BEE_INSTRUCTIONS = instructions(bee=True)  # a person who connected Bee, the same voice

NOTE_WAIT_S = 2.0  # the first reply waits at most this long for a prep note still being written (arbitrary)
CALL_READ_CHARS = 3000  # about the longest a Bee read's answer is in a call, to keep the call's history short (arbitrary)
# A tool the call AI waits on (anything outside the call: Bee today) says the language's waiting line once the
# call has been silent this long, so the caller does not think it dropped (Matt, 2026-10-15). One rule for
# every such tool; how long they may wait at most is beelink/cli.py CALL_WAIT_S.
WAITING_LINE_AFTER_S = 2.0
QUESTION = re.compile(r"[?？]")
NAMES_BEE = re.compile(r"(?<![A-Za-z])Bee(?![a-z]| Call)")  # "Bee", "Bee에"; not "been", "beer" or "Bee Call"
# A sentence of the answer that starts by saying no. Such an answer never counts as a yes to saving, whatever the
# AI makes of it; it may be a partial yes ("No, just the milk."), so the AI asks about the item alone.
REFUSAL = re.compile(r"^\W*(?:no\b|nope\b|nah\b|don'?t\b|do not\b|not now\b|never ?mind\b|wait,? no\b|"
                     r"아니|아뇨|됐어|됐습니다|싫어|하지 ?마|넣지 ?마|필요 ?없)", re.IGNORECASE)
# The answer's first sentence has to start by saying yes (after a filler such as "음" or "well"); a hedge, a
# "later", or the AI's own question heard back is no yes (pre-deploy review, 2026-10-15).
AFFIRMATIVE = re.compile(r"^\W*(?:(?:음|어|아|그|저|um|uh|hmm|well|oh)\W+)?"
                         r"(?:yes\b|yeah\b|yep\b|yup\b|sure\b|ok\b|okay\b|please\b|go ahead\b|go for it\b|do it\b|"
                         r"absolutely\b|of course\b|sounds good\b|that'?s right\b|right\b|correct\b|"
                         r"네|넵|넹|예|응|그래|그럼|좋아|좋습니다|좋네요|넣어|해 ?줘|해 ?주세요|맞아|맞습니다|그렇게|부탁|"
                         r"물론|당연|오케이)", re.IGNORECASE)
# Words anywhere in the answer that take its yes back: "Yes. Actually no, don't delete it.", "Yeah wait no, keep
# it.", "네, 아 잠깐만요 지우지 마세요." (review of 2026-10-17, finding 1: each of these deleted the fact). "아니야"
# takes it back only as a word of its own, at a clause's start or right after the yes ("응, 아니다", "응 아니야"),
# not after a noun, where it says what something is not ("급한 건 아니야").
TAKES_IT_BACK = re.compile(r"\b(?:wait,? no|no,? wait|actually,? no|actually,? don'?t|never ?mind|scratch that|"
                           r"forget (?:it|that)|on second thought|hold on|i mean,? no)\b|잠깐(?:만|[\s,.!?…]|$)|잠시만|"
                           r"(?:^|[,.!?…]\s*|(?:응|네|예|어|그래|좋아)\s+)(?:아\s+)?(?:아니다|아니야|아냐|아니요|아뇨|아니)"
                           r"(?=[\s,.!?…]|$)", re.IGNORECASE)
# What cannot be undone needs a plain yes: the answer, in letters only, is made of nothing but yes words, fillers, the
# asked-for verb and the words around a request (second review of 2026-10-17, finding 1: no list of take-backs holds
# them all; "네, 잠시만요, 지우지 마세요." and "Yes. Sorry, I mean no." deleted the fact). Anything more, and the AI
# asks again.
PLAIN_YES = re.compile(
    r"^(?:음|어|아|오|um|uh|hmm|well|oh|"
    r"응|네|예|넵|넹|그래|그래요|좋아|좋아요|오케이|맞아|맞아요|그럼|그럼요|물론|yes|yeah|yep|yup|sure|ok|okay|right|"
    r"absolutely|ofcourse|please|goahead|doit|"
    r"그거|그것|그걸|이거|그냥|지금|it|that|this|one|now|"
    r"지워|지우자|삭제|삭제해|없애|빼|delete|remove|dismiss|takeitout|getridof|drop|"
    r"해|줘|줘요|주세요|주라|요|부탁해|부탁해요|부탁드려요|thanks|thankyou|고마워|고마워요)+$")
# The question about what cannot be undone has to say it deletes or removes (second review of 2026-10-17, finding
# 4: a fact_delete proposed, then "Did Bee hear 'Bonus is 15%' right?" "Yes, that's right." deleted the fact).
SAYS_IT_REMOVES = re.compile(r"\b(?:delete|remove|dismiss|take (?:it )?out|get rid of|drop|turn (?:it )?down)|"
                             r"지우|지울|지워|삭제|빼|뺄|없애", re.IGNORECASE)
SENTENCES = re.compile(r"(?<=[.!?。])\s+|\n")
# The one question that offers to organize Bee names Bee, asks to put things in it, and quotes no item (a quoted item
# is a question about that item alone, answered with save_to_bee).
ORGANIZE_WORDS = re.compile(r"정리|넣|남겨|남길|저장|기억|추가|organi[sz]e|keep|add|put|save|store|tidy|sort", re.IGNORECASE)
ORGANIZE_MOST = 8  # items one organize in a call adds at most (arbitrary)
ORGANIZE_DAYS = (1, 7, 3)  # the past days an organize of what Bee recorded covers: fewest, most, and when not said
ORGANIZED = -1  # in BeeCoordinator._claimed: an item organize_bee added
QUOTES = "'\"‘’“”「」『』«»"
EXPIRED = "expired"  # a proposal whose question was answered and passed (BeeCoordinator._answer_to)
# How to ask about each kind of item; the item is quoted as proposed, and the AI says the question in the user's
# language.
ASK_LIKE = {"todo_create": "Shall I add '{text}' to your Bee todos?",
            "todo_accept": "Shall I add Bee's suggested todo '{text}'?",
            "todo_complete": "Shall I mark '{text}' done in Bee?",
            "todo_dismiss": "Shall I remove Bee's suggestion '{text}'?",
            "fact_create": "Shall I keep '{text}' in Bee?",
            "fact_confirm": "Did Bee hear '{text}' right?",
            "fact_update": "Shall I fix it in Bee to '{text}'?",
            "fact_delete": "Shall I delete '{text}' from Bee?"}
# The same questions for a Korean call: the English item inside the Korean question, which ends with it (the save
# check needs the quote in the question; a measured call asked "Bee에 기억해 둘까요?" and quoted the item after it).
ASK_LIKE_KO = {"todo_create": "Bee 할 일로 '{text}' 넣을까요?",
               "todo_accept": "Bee가 제안한 할 일 '{text}', 넣을까요?",
               "todo_complete": "Bee에서 '{text}' 끝난 걸로 할까요?",
               "todo_dismiss": "Bee가 제안한 '{text}', 뺄까요?",
               "fact_create": "Bee에 '{text}' 기억해 둘까요?",
               "fact_confirm": "Bee가 '{text}'라고 들었는데, 맞아요?",
               "fact_update": "Bee에서 '{text}'로 고칠까요?",
               "fact_delete": "Bee에서 '{text}' 지울까요?"}
ENGLISH = call_language("en")  # what the tools tell the model is in English, like the rest of its instructions


def model_request(agent: Agent, chat_ctx: llm.ChatContext, tools: list[llm.Tool]) -> dict[str, Any]:
    """What one request gives the model, model-agnostic: the whole chat as LiveKit holds it (the instructions are
    its first item), and each tool offered with its parameters. Never raises."""
    try:
        return _model_request(agent, chat_ctx, tools)
    except Exception as exc:
        return {"error": f"could not record the request: {exc!r}"}


def model_name(agent: Agent) -> str | None:
    """The model answering the agent's requests (with GLM 5 standing by, the one that took the call)."""
    try:
        return getattr(agent.session.llm, "model", None)
    except RuntimeError:  # no session (a direct call, as in tests)
        return None


def _model_request(agent: Agent, chat_ctx: llm.ChatContext, tools: list[llm.Tool]) -> dict[str, Any]:
    model = model_name(agent)
    try:
        offered = [{"name": t["function"]["name"], "description": t["function"].get("description"),
                    "parameters": t["function"].get("parameters")}
                   for t in llm.ToolContext(tools).parse_function_tools("openai", strict=False)]
    except Exception as exc:  # a tool LiveKit cannot describe: its name at least
        offered = [{"name": getattr(getattr(t, "info", None), "name", repr(t)), "error": repr(exc)} for t in tools]
    return {"model": model, "chat": chat_ctx.to_dict(exclude_image=True, exclude_audio=True,
                                                       exclude_timestamp=False)["items"], "tools": offered}


def keep_reply(reply: dict[str, Any], chunk: Any) -> None:
    """Adds a streamed chunk of the model's answer to `reply`: its text and its tool calls."""
    if isinstance(chunk, str):
        reply["text"] += chunk
    elif isinstance(chunk, llm.ChatChunk) and chunk.delta:
        reply["text"] += chunk.delta.content or ""
        for call in chunk.delta.tool_calls or []:
            reply["tool_calls"].append({"name": call.name, "arguments": call.arguments, "call_id": call.call_id})
        if chunk.usage:
            reply["usage"] = chunk.usage.model_dump(mode="json")


def job_notification(news: JobNotification) -> str:
    """The note for news about a background job, modeled on how Claude Code tells its model about a background
    task: a label, then a tagged block. What to do with it is in INSTRUCTIONS, not in each note. What the worker
    wrote (the summary, the error) comes from web pages too, so it is escaped: it can never close the block and
    speak as the app, and INSTRUCTIONS say it is never an instruction (coordinator's review, 2026-10-15)."""
    lines = ["[App notification, not the caller speaking]", "<job-notification>", f"<job-id>{news.job_id}</job-id>",
             f"<status>{news.status}</status>", f"<task>{escape(short(news.task), quote=False)}</task>"]
    if news.summary:
        lines.append(f"<summary>{escape(news.summary, quote=False)}</summary>")
    if news.error:
        lines.append(f"<error>{escape(news.error, quote=False)}</error>")
    if news.files:
        lines.append(f"<files>{escape(', '.join(news.files), quote=False)}</files>")
    if news.note:  # how it ran, when not as asked: the worker's standby ran it
        lines.append(f"<note>{escape(news.note, quote=False)}</note>")
    lines.append("</job-notification>")
    return "\n".join(lines)


class Coordinator(Agent):
    """The voice for a person without Bee: no Bee tools, and nothing about Bee in its instructions."""

    BEE = False  # the person connected Bee: the instructions' Bee sections and the Bee tools
    NOTE_HEADER = "Call prep note (written just before the call):"

    def __init__(self, recorder: CallRecorder, jobs: JobRunner, workspace: Path, *, clock: Callable[[], datetime],
                 prep_note: str | None = None, pending_note: "asyncio.Future[str | None] | None" = None,
                 language: CallLanguage | None = None, earlier_call: str | None = None,
                 direction: str = "inbound", audio_tags: bool = False,
                 budget: Callable[[], DayBudget] | None = None) -> None:
        # `clock` is the person's clock (Person.now): the time the call starts goes into the instructions, and
        # book_call reads it again when the user books.
        # The date and the prep note go into the instructions before the call's first reply, and stay as they
        # are after it (Claude checks its earlier thinking against them). A note still being written when the
        # AI picks up (pending_note) replaces prep_note, the plain Bee context, if it arrives before that.
        self._clock = clock
        rule = f"\n{language.rule}\n" if language and language.rule else ""
        # A call that continues an earlier one (voice/continuation.py): the user started it from that call's record,
        # or the AI places it because the user booked it in that call (BC-042).
        how = ("You placed this call, as the user booked it in an earlier call with you; it continues that call"
               if direction == "outbound" else "The user started this call to continue an earlier call with you")
        earlier = (f"\n{how}. That call, as it was recorded:\n{earlier_call.strip()}\n") if earlier_call else ""
        # `audio_tags`: the call's voice performs ElevenLabs audio tags such as [laughs] (voice/audio_tags.py).
        self._audio_tags = audio_tags
        started = clock()
        epoch = f" {toolkit.epoch_now(started)}" if self.BEE else ""  # Bee's times are epoch milliseconds
        self._base_instructions = instructions(audio_tags, bee=self.BEE) + rule + earlier + \
            f"\nIt is now {now_text(started)}. Use this for words like today, tomorrow and this Saturday.{epoch}\n"
        self._recorder = recorder
        self._jobs = jobs
        self._workspace = workspace
        super().__init__(instructions=self._with_note(prep_note), tools=self._bee_reads())
        self._language = language or call_language()
        self._pending_note = pending_note
        self._note_open = pending_note is not None  # until the first reply
        self._note_task: asyncio.Task | None = None
        self._booked: str | None = None  # the call back booked in this call (book_call), if any
        # end_call ran: the goodbye is said, and nothing more is (a reply queued by job news before it would follow)
        self.ending = False
        # The person's budget for today (voice/costs.py day_budget): checked as a call starts, and before a job, which
        # runs on after its call.
        self._budget = budget

    def _bee_reads(self) -> list[llm.Tool]:
        """The Bee read tools this call AI has: none without Bee."""
        return []

    def _with_note(self, note: str | None) -> str:
        if not note:
            return self._base_instructions
        return self._base_instructions + f"\n{self.NOTE_HEADER}\n{note.strip()}\n"

    async def add_note(self, note: str) -> None:
        """Adds an app note to the conversation in turn order. The Claude adapter sends it as a system
        message after the next user message; for other models LiveKit's provider format keeps it in place
        as a user turn wrapped in <instructions> tags (convert_mid_conversation_instructions)."""
        chat_ctx = self.chat_ctx.copy()
        chat_ctx.add_message(role="system", content=note)
        await self.update_chat_ctx(chat_ctx)

    async def on_enter(self) -> None:
        if self._pending_note is not None:
            self._note_task = asyncio.create_task(self._take_note_when_ready())

    async def _take_note_when_ready(self) -> None:
        await self._take_note(await asyncio.shield(self._pending_note))

    async def _take_note(self, note: str | None) -> None:
        if note and self._note_open:
            self._note_open = False
            await self.update_instructions(self._with_note(note))
            self._recorder.event("prep_note_taken")

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        pending = self._pending_note
        if not self._note_open or pending is None:
            return
        if not pending.done():
            try:
                await asyncio.wait_for(asyncio.shield(pending), NOTE_WAIT_S)
            except asyncio.TimeoutError:
                self._recorder.event("prep_note_late", waited_s=NOTE_WAIT_S)
        if pending.done() and not pending.cancelled():
            await self._take_note(pending.result())
        self._note_open = False
        # This turn's context was copied from the agent before the note landed in its instructions.
        current = turn_ctx.get_by_id(INSTRUCTIONS_MESSAGE_ID)
        if current is not None and current.text_content != self.instructions:
            update_instructions(turn_ctx, instructions=self.instructions, add_if_missing=False)

    async def tell_job(self, news: JobNotification) -> bool:
        """Adds news about a background job to the conversation; True when the user should hear it now."""
        await self.add_note(job_notification(news))
        return news.needs_reply

    # ---------- speech ----------

    async def llm_node(self, chat_ctx: llm.ChatContext, tools: list[llm.Tool], model_settings: ModelSettings):
        # Tool-call markup written as reply text is cut before it reaches the voice, the captions
        # and the history (see voice/speech_filter.py). Tool calls themselves pass through.
        # Every request, whatever the model, goes into the call's model log as the model received it, with what
        # came back before the filter (voice/recorder.py MODEL_LOG).
        if self.ending:  # the AI said goodbye (end_call): its model is not asked again
            return
        sent = model_request(self, chat_ctx, tools)
        reply: dict[str, Any] = {"text": "", "tool_calls": []}
        ended = "error"
        speech = SpeechFilter()
        answer = Agent.default.llm_node(self, chat_ctx, tools, model_settings)
        first = True
        try:
            async for chunk in answer:
                if first and self.ending:
                    # Asked for before end_call ran (job news while the AI said goodbye) and answered after it: the
                    # Claude adapter sends one request at a time. The goodbye stays the last thing said.
                    ended = "after end_call"
                    await answer.aclose()
                    return
                first = False
                keep_reply(reply, chunk)
                if isinstance(chunk, llm.ChatChunk) and chunk.delta and chunk.delta.content:
                    kept = speech.feed(chunk.delta.content)
                    if kept != chunk.delta.content:
                        chunk = chunk.model_copy(update={"delta": chunk.delta.model_copy(update={"content": kept})})
                elif isinstance(chunk, str):
                    chunk = speech.feed(chunk)
                yield chunk
            ended = "done"
        except (GeneratorExit, asyncio.CancelledError):
            ended = "cut off"  # the user spoke, or the call ended
            raise
        except Exception as exc:
            ended = f"error: {exc!r}"[:500]
            raise
        finally:
            # The model is named once the reply is in: the first request of a call can go to GLM 5 instead.
            self._recorder.model_request({**sent, "model": model_name(self), "reply": reply, "ended": ended})
        tail = speech.flush()
        if tail:
            yield tail
        if speech.dropped:
            self._recorder.event("speech_markup_dropped", text=speech.dropped)

    # ---------- recording ----------

    @asynccontextmanager
    async def waiting(self, context: RunContext | None):
        """Around what a tool waits on outside the call: LiveKit says the language's waiting line once the call has
        been silent for WAITING_LINE_AFTER_S (RunContext.with_filler)."""
        if context is None:  # a tool called directly, as in tests
            yield
            return
        async with context.with_filler(self._language.waiting, delay=WAITING_LINE_AFTER_S):
            yield

    async def stt_node(self, audio: AsyncIterable[rtc.AudioFrame], model_settings: ModelSettings):
        # A transcript too quiet to be the user (the AI's own voice coming back faintly) is kept out of the
        # conversation and noted in the call record (voice/speech_gate.py).
        gate = SpeechGate(on_drop=lambda text, level: self._recorder.event("transcript_dropped", text=text,
                                                                           level=round(level)))

        async def tee() -> AsyncIterable[rtc.AudioFrame]:
            async for frame in audio:
                self._recorder.user_audio.write(frame)
                yield frame

        async for event in Agent.default.stt_node(self, gate.listen(tee()), model_settings):
            if gate.allows(event):
                yield event

    async def tts_node(self, text: AsyncIterable[str], model_settings: ModelSettings):
        if not self._audio_tags:  # a voice that would read a tag out never gets one (voice/audio_tags.py)
            text = without_tags(text)

        async def tee() -> AsyncIterable[rtc.AudioFrame]:
            async for frame in Agent.default.tts_node(self, text, model_settings):
                self._recorder.ai_audio.write(frame)
                yield frame

        return tee()

    def transcription_node(self, text: AsyncIterable[str], model_settings: ModelSettings) -> AsyncIterable[str]:
        # The captions never show an audio tag, and neither do the conversation's history and the call record, which
        # LiveKit fills from this same text; the model log keeps the reply as the model wrote it.
        return without_tags(text)

    # ---------- tools ----------

    @function_tool
    async def delegate(self, context: RunContext, task: str, title: str) -> str:
        """Hand slow work (research, calculation, analysis) to the background worker. Returns at once.

        A started job cannot be changed. For more or different work, call delegate again.

        Args:
            task: A self-contained description of the work and the result to bring back.
            title: A few words naming the job for the user's screen, in the language you speak with the user, such
                as "Saturday rain in Seoul by the hour".
        """
        budget = self._budget() if self._budget is not None else None
        if budget is not None and budget.used_up:
            return f"Not started: {budget.why()}. Tell the user in a few words that the job could not start."
        job = self._jobs.start(task, title=" ".join(title.split()) or None, language=self._language.code)
        return (f"Job {job.job_id} is running in the background. A job notification will come when it has news. "
                "If you already told the user you are on it, do not say it again; carry on the conversation.")

    @function_tool
    async def job_status(self, context: RunContext) -> str:
        """See how this call's background jobs are going: the latest step of each running job, or how it ended."""
        return self._jobs.status_text()

    @function_tool
    async def search_workspace(self, context: RunContext, query: str) -> str:
        """Look up what the user and you said in earlier Bee Call phone calls.

        Args:
            query: A few keywords to look for.
        """
        words = [w.lower() for w in query.split() if len(w) > 2]
        hits = []
        for transcript in sorted(self._workspace.glob("*/*/transcript.md"), reverse=True):
            if transcript.parent == self._recorder.dir:
                continue
            for line in transcript.read_text().splitlines():
                if words and all(w in line.lower() for w in words):
                    hits.append(f"{transcript.parent.parent.name} {transcript.parent.name}: {line.strip('- ')}")
                    if len(hits) >= 5:
                        return "\n".join(hits)
        return "\n".join(hits) or "Nothing found in earlier calls."

    @function_tool
    async def book_call(self, context: RunContext, topic: str, asked: str, at: str | None = None,
                        in_minutes: int | None = None) -> str:
        """Book a call back at a time the user asked for or agreed to out loud, such as "call me tomorrow at 8" or
        "call me back in ten minutes". At that time you call them, picking up from this call. Never book a call
        the user did not ask for. Give the time one way: in_minutes for a time counted from now, at for a clock
        time. The answer says the time booked; read it back to the user.

        Args:
            topic: What the call is for, in a few words.
            asked: The user's own words asking for the call or agreeing to it, copied word for word from the
                conversation: their sentence, or most of it.
            at: For a clock time: the local date and time to call, in ISO 8601, like 2026-10-15T08:00.
            in_minutes: For a time from now, like "in ten minutes" (10) or "in an hour" (60): minutes from now.
        """
        if not self._user_said(asked):  # checked in code, not only asked of the model (coordinator's review)
            self._recorder.event("call_booking_refused", asked=str(asked or "")[:200], topic=topic)
            return ("Not booked: asked must be the user's own words asking for the call or agreeing to it, copied "
                    "word for word from this call's conversation: their whole sentence, or most of it with nothing "
                    "left out that changes it. Book a call only when they asked for it or agreed to it.")
        now = self._clock()  # the person's clock now, not the time this call started
        if (at is None) == (in_minutes is None):
            return "Give the time one way: in_minutes for a time from now, or at for a clock time."
        if in_minutes is not None:
            when = now + timedelta(minutes=in_minutes)
        else:
            try:
                when = datetime.fromisoformat(at.strip())
            except ValueError:
                return "Give the time in ISO 8601, like 2026-10-15T08:00."
            # A time without a zone is the user's own; one with a zone is the same moment on their clock.
            when = when.replace(tzinfo=now.tzinfo) if when.tzinfo is None else when.astimezone(now.tzinfo)
        if when < now + timedelta(minutes=1):
            return "That time has passed. Ask the user for a later time."
        if when > now + timedelta(days=7):
            return "Calls can be booked at most a week ahead."
        store = OfferStore(self._workspace)
        moved = self._booked is not None and store.call_off(self._booked, now, "moved to a new time") is not None
        session = f"{self._recorder.dir.parent.name}/{self._recorder.dir.name}"  # the booked call continues this one
        offer = store.book(topic.strip() or "a call back", when, now, continue_from=session, language=self._language)
        self._booked = offer.id  # one call back per call: booking again moves it
        self._recorder.event("call_booked", offer=offer.id, at=offer.call_at, topic=offer.topic)
        verb = "Moved the call back" if moved else "Booked"
        day = "today" if when.date() == now.date() else "tomorrow" if when.date() == now.date() + timedelta(days=1) \
            else f"on {when:%A}"
        return (f"{verb}: you will call at {when:%-I:%M %p} {day} ({when:%A, %B %-d}); it is {now:%-I:%M %p} now. "
                "Read this time back to the user in a few words.")

    def _user_turns(self) -> list[str]:
        """What the user said in this call, turn by turn."""
        return [item.text_content or "" for item in self.chat_ctx.items
                if getattr(item, "type", None) == "message" and item.role == "user"]

    def _user_said(self, words: Any) -> bool:
        """Whether `words` are what the user said in this call (voice/said.py)."""
        return said_in(self._user_turns(), words)

    @function_tool
    async def cancel_call(self, context: RunContext) -> str:
        """Call off the call back booked in this call, when the user no longer wants it."""
        offer = OfferStore(self._workspace).call_off(self._booked, self._clock(), "called off in the call") \
            if self._booked else None
        if offer is None:
            return "No call back is booked in this call."
        self._booked = None
        self._recorder.event("call_cancelled", offer=offer.id)
        return "Called off. Tell the user in a few words."

    @function_tool
    async def end_call(self, context: RunContext) -> str:
        """End the call, only after the user said goodbye or that they are done. Say goodbye first."""
        self._recorder.event("end_call_requested")
        self.ending = True
        self.session.shutdown(drain=True)
        # No reply to this tool: the goodbye is already said, and GLM 5 once answered the tool result
        # by saying "end_call" out loud (2026-10-13).
        raise StopResponse()


@dataclass
class Proposal:
    """An item the AI means to ask about saving to Bee. The user's yes counts only if it comes after this point in
    the conversation (the id of the user's last turn when it was proposed; None: before they spoke), and only as
    the answer to the first question about it; after that answer has passed, the proposal has expired."""
    item: dict
    after: str | None
    saved: bool = False
    expired: bool = False
    before: dict | None = None  # what it changes, as Bee had it when it was proposed (AS_BEE_HAS_IT)


class BeeCoordinator(Coordinator):
    """The voice for a person who connected Bee: it reads their Bee during the call and saves to it what the user
    agrees to."""

    BEE = True
    NOTE_HEADER = "Call prep note (from Bee, just before the call):"

    def __init__(self, recorder: CallRecorder, jobs: JobRunner, workspace: Path, **kwargs) -> None:
        super().__init__(recorder, jobs, workspace, **kwargs)
        self._bee_writes = BeeWrites(recorder.dir)  # this call's Bee writes, kept in its folder
        self._proposals: dict[int, Proposal] = {}  # items the AI means to ask about, by number (propose_bee_save)
        self._claimed: dict[tuple, int] = {}  # each item saved or tried in this call (same_item), by its proposal
        self._organized: set[str] = set()  # the user's yeses organize_bee has taken (their last turn's id)
        # What the user said yes to organizing beyond this call: the voice server hands it to the meta agent once the
        # call ends (voice/agent.py end_call).
        self.organize_request: dict | None = None

    def _bee_reads(self) -> list[llm.Tool]:
        """The Bee read toolkit (beelink/toolkit.py), the same tools the meta agent and the call prep have: each a
        LiveKit tool with the toolkit's own name, description and parameters. A read waits at most READ_TIMEOUT_S
        (under LiveKit's wait for tools after the user cuts in), says the waiting line after 2 s of silence, and leaves
        out what Bee recorded during the person's calls with us."""
        return [self._bee_read(tool) for tool in toolkit.TOOLS]

    def _bee_read(self, tool: toolkit.BeeTool) -> llm.Tool:
        async def read(raw_arguments: dict[str, object], context: RunContext) -> str:
            async with self.waiting(context):
                found = await toolkit.read(tool.name, raw_arguments, ours=OurCalls.of(self._workspace),
                                           zone=self._clock().tzinfo, timeout=reads.READ_TIMEOUT_S,
                                           max_chars=CALL_READ_CHARS)
            return found.text

        return function_tool(read, raw_schema={"name": tool.name, "description": tool.description,
                                               "parameters": tool.parameters})

    @function_tool
    async def propose_bee_save(self, context: RunContext, kind: str, text: str, fact_id: int | None = None,
                               suggestion_id: int | None = None, todo_id: int | None = None,
                               alarm_at: str | None = None) -> str:
        """Call it in the reply that asks the user whether to change something in their Bee, before the question: it
        fixes the item and gives it a number, and changes nothing in Bee, so it needs no yes. Then ask one question
        that names Bee and quotes the item, and call save_to_bee with the number only after the user says yes to it.

        Args:
            kind: fact_create (keep something lasting the user said about themselves), fact_confirm (Bee heard a
                fact right), fact_update (fix a fact Bee has wrong), fact_delete (take a wrong fact out; cannot be
                undone), todo_create (a new todo), todo_accept (take Bee's suggested todo), todo_complete (mark a
                todo done) or todo_dismiss (turn down Bee's suggested todo; cannot be undone).
            text: The item as your question will quote it: in English, Bee's language, for what you write
                (fact_create, fact_update, todo_create), whatever the call's language; for the others, the fact or
                the todo as Bee has it.
            fact_id: The Bee fact id (fact_confirm, fact_update, fact_delete).
            suggestion_id: The Bee todo suggestion id (todo_accept, todo_dismiss).
            todo_id: The Bee todo id (todo_complete).
            alarm_at: Optional ISO 8601 time for a todo alarm (todo_create).
        """
        if kind not in KINDS:
            return f"Unknown kind {kind!r}; use one of {', '.join(KINDS)}."
        if not text or not text.strip():
            return "text is required: the item as your question will quote it."
        item: dict = {"kind": kind, "text": text.strip()}
        if kind in ("fact_confirm", "fact_update", "fact_delete"):
            if fact_id is None:
                return "fact_id is required."
            item["fact_id"] = fact_id
        if kind == "todo_create" and alarm_at:
            item["alarm_at"] = alarm_at
        if kind in ("todo_accept", "todo_dismiss"):
            if suggestion_id is None:
                return "suggestion_id is required."
            item["suggestion_id"] = suggestion_id
        if kind == "todo_complete":
            if todo_id is None:
                return "todo_id is required."
            item["todo_id"] = todo_id
        before, as_bee_has_it_note = None, ""
        if kind in AS_BEE_HAS_IT:  # asked about as Bee has it, so the user hears what the id points to
            what = {"fact_delete": f"fact {fact_id}", "todo_complete": f"todo {todo_id}",
                    "todo_dismiss": f"todo suggestion {suggestion_id}"}[kind]
            try:
                async with self.waiting(context):
                    before = await as_bee_has_it(item)
            except Exception as exc:
                return (f"Could not read {what} from Bee ({short(str(exc) or type(exc).__name__, 120)}), so it cannot "
                        "be changed now. Tell the user in a few words.")
            if not before or not isinstance(before.get("text"), str) or not before["text"].strip():
                return f"Bee has no {what}. Take the id from the prep note or from a Bee read."
            if _plain(before["text"]) != _plain(item["text"]):
                as_bee_has_it_note = f" Bee has it as '{before['text']}': quote that, not other words."
            item["text"] = before["text"]
        number = len(self._proposals) + 1
        proposal = self._proposals[number] = Proposal(item, after=self._last_user_turn(), before=before)
        if kind not in NOT_UNDOABLE and self._asked_just_before(proposal, self._turns(), item["text"]):
            return (f"Proposal {number}: {item_label(item, ENGLISH)}. Your words just before the user's last answer "
                    f"asked about it, quoting it: if that answer was a clear yes, call save_to_bee with proposal "
                    f"{number} now, without asking again; if it was not, do not save it.")
        return (f"Proposal {number}: {item_label(item, ENGLISH)}.{as_bee_has_it_note} Ask the user now (unless you "
                f"already asked it in this reply), in one question that names Bee and quotes the item exactly, {self._ask_like(item)} Call "
                f"save_to_bee with proposal {number} only after they say yes to that question.")

    @function_tool
    async def save_to_bee(self, context: RunContext, proposal: int) -> str:
        """Save a proposed item to the user's Bee now, after the user said yes to your question about it.

        Args:
            proposal: The number propose_bee_save gave the item.
        """
        found = self._proposals.get(proposal)
        if found is None:
            return (f"There is no proposal {proposal}; call propose_bee_save first, and it says whether you still "
                    "need to ask.")
        if found.saved:
            return f"Proposal {proposal} was already saved; it is not saved twice."
        heard = EXPIRED if found.expired else self._answer_to(found)
        if heard is None:
            return (f"Not saved: the user has not yet said yes to a question about proposal {proposal}. Ask now, in "
                    f"one question that names Bee and quotes the item exactly, {self._ask_like(found.item)} Call "
                    "save_to_bee again only after they say yes to it.")
        if heard == EXPIRED:
            found.expired = True
            return (f"Not saved: proposal {proposal} was asked about, and the conversation went on past the answer. If "
                    "the user still wants it, propose it again with propose_bee_save and ask about it alone.")
        question, breaths = heard
        answer = " ".join(breaths)
        if any(REFUSAL.search(sentence) for breath in breaths for sentence in SENTENCES.split(breath)):
            found.expired = True
            return (f"Not saved: the answer sounds like a no (\"{answer}\"). If the user did want it, propose it "
                    "again with propose_bee_save and ask about it alone.")
        if not AFFIRMATIVE.search(SENTENCES.split(answer.strip())[0]):
            found.expired = True
            return (f"Not saved: the answer is not a yes (\"{answer}\"). If the user wants it, propose it again with "
                    "propose_bee_save and ask about it alone.")
        if TAKES_IT_BACK.search(answer):
            found.expired = True
            return (f"Not saved: the answer takes the yes back (\"{answer}\"). If the user does want it, propose it "
                    "again with propose_bee_save and ask about it alone.")
        if found.item["kind"] in NOT_UNDOABLE:
            if not any(SAYS_IT_REMOVES.search(s) for s in asking_sentences(question, found.item["text"])):
                found.expired = True
                return ("Not saved: this cannot be undone, and your question did not say it deletes or removes it. "
                        f"Propose it again and ask, {self._ask_like(found.item)}")
            if not PLAIN_YES.match(_letters_only(answer)):
                found.expired = True
                return (f"Not saved: this cannot be undone, and the answer is more than a plain yes (\"{answer}\"). "
                        "If the user does want it, propose it again with propose_bee_save and ask about it alone.")
        same = self._claimed.get(same_item(found.item))
        if same is not None:  # proposed twice, both answered by one yes (measured 2026-10-17)
            found.expired = True
            already = "added when organizing Bee" if same == ORGANIZED else f"proposal {same}, already saved or tried"
            return (f"Not saved: {item_label(found.item, ENGLISH)} is {already} in this call, and it is not saved "
                    "twice.")
        found.saved = True
        self._claimed[same_item(found.item)] = proposal  # before anything waits, so two saves at once see it
        async with self.waiting(context):
            write = await self._bee_writes.begin(found.item, question, answer, before=found.before)
            try:
                await self._bee_writes.apply(write)
            finally:  # also when the tool is cut off mid-write: the record shows the write, and that Bee may have it
                self._recorder.event("bee_write", **event_fields(write))
        what = label(write, ENGLISH)
        if write.status == "ok":
            return f"Saved to Bee: {what}. Tell the user in a few words what you saved."
        if write.status == "unknown":
            return (f"Bee did not confirm saving {what} ({write.error}): it may or may not be in Bee, and it will not "
                    "be saved again by itself. Tell the user that, and that they can check Bee after the call.")
        return (f"Saving {what} to Bee did not go through ({write.error}). Tell the user it did not go through and "
                "that it will be tried again after the call.")

    @function_tool
    async def undo_bee_write(self, context: RunContext) -> str:
        """Take the last item you saved to Bee in this call back out, when the user asks you to."""
        saved = [w for w in self._bee_writes.all() if w.status == "ok"]
        if not saved:
            return "Nothing saved to Bee in this call is left to take out."
        if saved[-1].kind in NOT_UNDOABLE:
            return (f"{label(saved[-1], ENGLISH)} cannot be undone: Bee cannot bring back a deleted fact or a "
                    "dismissed suggestion. Tell the user in a few words.")
        async with self.waiting(context):
            write = await self._bee_writes.undo(saved[-1].key)
        what = label(write, ENGLISH)
        if write.status != "undone":
            return f"Could not take {what} out of Bee ({write.error}). Tell the user."
        self._recorder.event("bee_write_undone", key=write.key, kind=write.kind, text=write.text)
        self._claimed.pop(same_item(write.item), None)  # taken out, it may be asked about and saved again
        return f"Took it out of Bee: {what}. Tell the user in a few words."

    @function_tool
    async def organize_bee(self, context: RunContext, scope: str = "call", facts: list[str] | None = None,
                           todos: list[str] | None = None, days: int | None = None) -> str:
        """Organize the user's Bee memory, only after a clear yes to your one question offering it (one that names
        Bee and lists no items). Organizing only adds: it never deletes or rewrites what Bee has.

        Args:
            scope: "call" (the default): add what came up in this call at once, facts as confirmed facts and todos as
                open to-dos. "recent": organize what Bee recorded over the past days, in the background after this
                call; the user gets one notification when it is done.
            facts: For "call": the new facts, lasting things about the user, each one short statement in English.
            todos: For "call": the new to-dos, things the user means to do, each one short statement in English.
            days: For "recent": how many past days to organize, 1 to 7 (3 when not given).
        """
        heard = self._organize_answer()
        if isinstance(heard, str):
            return heard
        question, answer, answered = heard
        scope = str(scope or "call").strip().lower()
        if scope == "recent":
            fewest, most, usual = ORGANIZE_DAYS
            try:
                days = min(most, max(fewest, int(days))) if days is not None else usual
            except (TypeError, ValueError):
                days = usual
            self._organized.add(answered)
            self.organize_request = {"days": days, "question": question, "answer": answer}
            self._recorder.event("bee_organize_requested", days=days, question=question, answer=answer)
            return (f"Started: what Bee recorded over the past {days} days is organized in the background once this "
                    "call ends, and the user gets one notification when it is done. Tell the user that in a few words.")
        if scope != "call":
            return 'scope is "call" (what came up in this call) or "recent" (what Bee recorded over the past days).'
        items, seen, again = [], set(), []
        for kind, texts in (("fact_create", facts), ("todo_create", todos)):
            for text in texts or []:
                text = " ".join(str(text or "").split())
                item = {"kind": kind, "text": text}
                if not text or (kind, _plain(text)) in seen:
                    continue
                seen.add((kind, _plain(text)))
                (again if same_item(item) in self._claimed else items).append(item)
        if not items:
            return ("Nothing added: " + (f"{', '.join(item_label(i, ENGLISH) for i in again)} went into Bee in this "
                                         "call already." if again else "give the new facts and to-dos, each one "
                                         "short statement in English."))
        if len(items) > ORGANIZE_MOST:
            return f"Not added: at most {ORGANIZE_MOST} items at once. Keep the ones that matter most and call again."
        if any(len(item["text"]) > 200 for item in items):
            return "Not added: each item is one short statement, under 200 characters. Call again with shorter ones."
        self._organized.add(answered)
        for item in items:  # before anything waits, so a save at the same time sees them
            self._claimed[same_item(item)] = ORGANIZED
        async with self.waiting(context):
            writes = [await self._bee_writes.begin(item, question, answer) for item in items]  # their keys in order

            async def write(one) -> None:
                try:
                    await self._bee_writes.apply(one)
                finally:  # also when the tool is cut off mid-write: the record shows the write, and that Bee may have it
                    self._recorder.event("bee_write", **event_fields(one))

            await asyncio.gather(*(write(one) for one in writes))
        went = [w for w in writes if w.status == "ok"]
        failed = [w for w in writes if w.status == "failed"]
        maybe = [w for w in writes if w.status not in ("ok", "failed")]
        parts = []
        if went:
            parts.append(f"Added to Bee: {', '.join(label(w, ENGLISH) for w in went)}.")
        if failed:
            parts.append(f"Did not go through: {', '.join(label(w, ENGLISH) for w in failed)}; tried again after the "
                         "call.")
        if maybe:
            parts.append(f"Bee did not confirm {', '.join(label(w, ENGLISH) for w in maybe)}: they may be in Bee, and "
                         "are not added again by themselves.")
        if again:
            parts.append(f"Already in Bee from this call, so not added again: "
                         f"{', '.join(item_label(i, ENGLISH) for i in again)}.")
        parts.append("Tell the user in a few words that they are in and that they can see and edit them in the Bee app."
                     if went else "Tell the user in a few words what happened.")
        return " ".join(parts)

    def _organize_answer(self) -> tuple[str, str, str] | str:
        """The question that offered to organize and the user's yes to it: (question, answer, the answer's last turn
        id); or why there is none, as the tool's answer. The question is the AI's turn just before the user's last
        words: one of its sentences asks ("?"), names Bee, says to put things in it and quotes no item. The answer is
        the user's turns after it, with nothing after them but the AI's words that ask nothing; a yes as save_to_bee
        takes one (it starts by saying yes, says no nowhere, takes nothing back), and each yes is taken once."""
        turns = self._turns()
        end = len(turns)
        while end and turns[end - 1].role == "assistant" and not QUESTION.search(turns[end - 1].text_content or ""):
            end -= 1
        start = end
        while start and turns[start - 1].role == "user":
            start -= 1
        if start == end or start == 0 or turns[start - 1].role != "assistant":
            return ("Not done: organize only after asking the user one question that names Bee and offers to "
                    "organize, and hearing a clear yes to it. Ask it now.")
        question = turns[start - 1].text_content or ""
        offering = [sentence for sentence in SENTENCES.split(re.sub(r"\s+", " ", question))
                    if QUESTION.search(sentence) and NAMES_BEE.search(sentence) and ORGANIZE_WORDS.search(sentence)
                    and not re.search(f"[{re.escape(QUOTES)}]", sentence)]
        if not offering:
            return ("Not done: your question just before the user's answer did not offer to organize Bee. Ask one "
                    "question that names Bee and offers to put these things in it, without listing them, and do it "
                    "only after a clear yes.")
        breaths = [(turn.text_content or "").strip() for turn in turns[start:end]]
        answer = " ".join(b for b in breaths if b)
        answered = turns[end - 1].id
        if answered in self._organized:
            return "Already done for that yes. To organize more, offer it again and wait for the user's yes."
        if not answer or any(REFUSAL.search(sentence) for b in breaths for sentence in SENTENCES.split(b)) or \
                not AFFIRMATIVE.search(SENTENCES.split(answer)[0]) or TAKES_IT_BACK.search(answer):
            return (f"Not done: the user's answer is not a clear yes (\"{answer}\"). Organize only after a clear yes; "
                    "if they want it, ask again.")
        return question, answer, answered

    def _ask_like(self, item: dict) -> str:
        """How to ask about an item, in the call's language: one sentence that ends with "?" and holds the item."""
        templates = ASK_LIKE_KO if self._language.code == "ko" else ASK_LIKE
        return (f"in one sentence that ends with \"?\" and holds the quoted item, such as: "
                f"{templates[item['kind']].format(text=item['text'])}")

    def _last_user_turn(self) -> str | None:
        """The id of the user's last turn so far (None before they said anything)."""
        users = [m.id for m in self._turns() if m.role == "user"]
        return users[-1] if users else None

    def _turns(self) -> list:
        return [item for item in self.chat_ctx.items
                if getattr(item, "type", None) == "message" and item.role in ("user", "assistant")]

    def _answer_to(self, proposal: Proposal) -> tuple[str, list[str]] | str | None:
        """The question the AI asked about a proposed item and the user's answer to it, as the conversation shows
        them: (question, the answer's breaths). None while there is none yet: no question since the proposal (a save
        in the reply that asks cannot see that reply yet), or no answer to it yet. EXPIRED once the answer is past.
        - The question is the first AI turn since the proposal that names Bee (the word), quotes the item whole and
          asks ("?"); if the AI said it again before the user answered, its last saying. With none since, it is the
          AI turn just before the user's last answer, if that one names Bee and quotes the item (_asked_just_before).
        - The answer is the user's turns right after it (one breath or several), with nothing between but the AI's
          words that ask nothing.
        - The answer is past once the conversation went on: the user spoke again after the AI did, or the AI asked
          something else. A yes after that, to whatever, is no answer to this proposal."""
        turns = self._turns()
        ids = [turn.id for turn in turns]
        if proposal.after is None:
            later = turns
        elif proposal.after in ids:
            later = turns[ids.index(proposal.after) + 1:]
        else:
            return EXPIRED  # the turn it followed is no longer in the conversation
        item = proposal.item["text"]
        asked = [i for i, turn in enumerate(later) if turn.role == "assistant" and asks_about(turn.text_content, item)]
        if not asked:  # what cannot be undone counts only when asked about after it was proposed
            return None if proposal.item["kind"] in NOT_UNDOABLE else self._asked_just_before(proposal, turns, item)
        answered = next((i for i in range(asked[0] + 1, len(later)) if later[i].role == "user"), None)
        if answered is None:
            return None
        question = max(i for i in asked if i < answered)
        if any(QUESTION.search(turn.text_content or "") for turn in later[question + 1:answered]):
            return EXPIRED  # the AI asked something else before the answer
        end = next((i for i in range(answered, len(later)) if later[i].role != "user"), len(later))
        if any(turn.role == "user" or QUESTION.search(turn.text_content or "") for turn in later[end:]):
            return EXPIRED
        breaths = [(turn.text_content or "").strip() for turn in later[answered:end]]
        return (later[question].text_content or "", [b for b in breaths if b]) if any(breaths) else None


    @staticmethod
    def _asked_just_before(proposal: Proposal, turns: list, item: str) -> tuple[str, list[str]] | None:
        """The question just before the user's answer when the item was proposed only after that answer (the model
        often asks first and proposes in its reply to the yes; measured 2026-10-17, 4 of 10 saves): the AI turn right
        before the answer names Bee and quotes the item whole, and the answer is still the user's last words."""
        ids = [turn.id for turn in turns]
        if proposal.after is None or proposal.after not in ids:
            return None
        end = ids.index(proposal.after)
        if any(turn.role == "user" for turn in turns[end + 1:]):  # the conversation went on since
            return None
        start = end
        while start > 0 and turns[start - 1].role == "user":
            start -= 1
        if start == 0 or turns[start - 1].role != "assistant" or not asks_about(turns[start - 1].text_content, item):
            return None
        breaths = [(turn.text_content or "").strip() for turn in turns[start:end + 1]]
        return (turns[start - 1].text_content or "", [b for b in breaths if b]) if any(breaths) else None


def asks_about(text: str | None, item: str) -> bool:
    """Whether an AI turn asks about the item: it names Bee (the word), and one of its sentences is a question
    ("?") that quotes the item's text whole, in quotation marks. So "I'll add 'Buy milk' to Bee. Anything else?"
    asks about something else, and 'Buy milk and eggs' does not quote 'Buy milk'."""
    return bool(asking_sentences(text, item))


def asking_sentences(text: str | None, item: str) -> list[str]:
    """The sentences of an AI turn that ask about the item (asks_about); none when the turn does not name Bee."""
    text = re.sub(r"\s+", " ", text or "")
    if not NAMES_BEE.search(text):
        return []
    quote = f"[{re.escape(QUOTES)}]"
    quoted = re.compile(f"{quote}{re.escape(_plain(item))}[.!?。]?{quote}")
    return [sentence for sentence in SENTENCES.split(text)
            if QUESTION.search(sentence.rstrip(QUOTES + " ")[-1:]) and quoted.search(sentence.casefold())]


def _letters_only(text: str) -> str:
    """Letters and digits only, in lower case: how an answer is matched against PLAIN_YES."""
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def same_item(item: dict) -> tuple:
    """What makes two items the same change to Bee: the kind, the text compared loosely, and what it changes."""
    return (item.get("kind"), _plain(str(item.get("text") or "")), item.get("fact_id"), item.get("suggestion_id"),
            item.get("todo_id"), item.get("alarm_at"))


def _plain(text: str) -> str:
    """Text compared loosely: spaces collapsed, case and end punctuation ignored."""
    return re.sub(r"\s+", " ", text).strip().strip(".!?。").casefold()
