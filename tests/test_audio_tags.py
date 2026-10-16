"""ElevenLabs audio tags in the call AI's speech (voice/audio_tags.py): the call AI may write a few, such as
[laughs] and [sighs], sparingly; the speech filter keeps them; a voice that performs them (ElevenLabs v4) gets
them, any other never reads them out; the captions, the conversation's history, the call record and the call view
never show them; the model log keeps them as written."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from voice import audio_tags
from voice.audio_tags import TagStripper, strip_tags
from voice.speech_filter import SpeechFilter


def speak(chunks: list[str]) -> tuple[str, SpeechFilter]:
    f = SpeechFilter()
    return "".join(f.feed(c) for c in chunks) + f.flush(), f


def test_the_speech_filter_keeps_a_known_tag_and_still_drops_other_asides():
    for chunks in (["[laughs] That's a good one."], ["[lau", "ghs] That's ", "a good one."],
                   ["Sure. ", "[sighs]", " Long day, huh?"], ["That's funny [laughs] okay."], ["[Laughs] Okay."],
                   ["[laughs]"]):
        said, f = speak(chunks)
        assert said == "".join(chunks) and f.dropped == "", chunks
    said, f = speak(["[pause] Take your time. (smiles) Okay. [laughs softly] Sure."])
    assert said == "Take your time. Okay. Sure." and "[pause]" in f.dropped and "[laughs softly]" in f.dropped


def test_tags_are_stripped_from_text_the_user_reads():
    for written, read in (("[laughs] That's a good one.", "That's a good one."),
                          ("That's funny [laughs] okay.", "That's funny okay."),
                          ("Okay. [sighs]", "Okay."), ("[laughs]", ""), ("하하 [laughs] 좋네요.", "하하 좋네요."),
                          ("Line one.\n[sighs] Line two.", "Line one.\nLine two."),
                          ("[pause] and [1] stay", "[pause] and [1] stay")):  # not tags: left as they are
        assert strip_tags(written) == read, written


def test_a_tag_split_across_chunks_is_stripped_from_a_stream():
    stripper = TagStripper()
    out = "".join(stripper.feed(c) for c in ["Sure ", "[lau", "ghs] okay", ", [", "1] and [sig", "hs]"])
    assert out + stripper.flush() == "Sure okay, [1] and"


def test_only_an_elevenlabs_v4_voice_performs_tags():
    from voice.voices import VoiceChoice

    assert VoiceChoice("elevenlabs", "v", "eleven_v4_turbo").performs_tags
    assert VoiceChoice("elevenlabs", "v", "eleven_v4").performs_tags
    assert not VoiceChoice("elevenlabs", "v", "eleven_flash_v2_5").performs_tags
    assert not VoiceChoice("polly", "Seoyeon", "neural").performs_tags


def coordinator(tmp_path, tags: bool):
    from tests.helpers import seoul_now
    from voice.coordinator import Coordinator
    from voice.recorder import CallRecorder

    rec = CallRecorder(tmp_path / "workspace", room="r", keep_audio=False, now=seoul_now())
    return rec, Coordinator(rec, SimpleNamespace(), tmp_path / "workspace", clock=seoul_now, audio_tags=tags)


def test_the_call_ai_is_told_of_the_tags_only_when_its_voice_performs_them(tmp_path):
    _, with_tags = coordinator(tmp_path / "a", True)
    _, without = coordinator(tmp_path / "b", False)
    for tag in audio_tags.SPOKEN:
        assert f"[{tag}]" in with_tags.instructions
    assert "sparingly" in with_tags.instructions and "only where a person" in with_tags.instructions
    assert "spoken aloud" in with_tags.instructions and "stage directions" in with_tags.instructions
    assert '"[laughs]"' in without.instructions and "no stage directions, sound effects" in without.instructions
    assert "sparingly" not in without.instructions


@pytest.mark.parametrize("tags, heard", [(True, "[laughs] That's a good one."), (False, "That's a good one.")])
def test_a_voice_gets_the_tags_only_when_it_performs_them(tmp_path, monkeypatch, tags, heard):
    from livekit.agents import Agent

    rec, agent = coordinator(tmp_path, tags)
    sent = []

    async def tts_node(agent_, text, model_settings):
        async for chunk in text:
            sent.append(chunk)
        yield SimpleNamespace(data=b"")  # one frame

    monkeypatch.setattr(Agent.default, "tts_node", tts_node)

    async def written():
        for chunk in ["[lau", "ghs] That's ", "a good one."]:
            yield chunk

    async def main():
        frames = await agent.tts_node(written(), None)
        return [frame async for frame in frames]

    assert len(asyncio.run(main())) == 1 and "".join(sent).strip() == heard
    rec.close()


def test_the_history_the_record_and_the_model_log_in_a_livekit_session(tmp_path):
    """The coordinator in a real LiveKit session (text mode): the reply's tag reaches no caption, no history item and
    no call record (the transcription node strips it), and the model log keeps the reply as the model wrote it."""
    from livekit.agents import AgentSession

    from tests.helpers import ScriptedLLM
    from voice.agent import wire_events

    rec, agent = coordinator(tmp_path, True)

    async def main():
        async with AgentSession(llm=ScriptedLLM([["[laughs] ", "That's a ", "good one. [sighs] Long day?"]])) as session:
            wire_events(session, rec)
            await session.start(agent)
            await session.run(user_input="I fell asleep on the bus again")
            return [i.text_content for i in session.history.items if getattr(i, "role", None) == "assistant"]

    said = asyncio.run(main())
    rec.close()
    assert said == ["That's a good one. Long day?"]
    events = [json.loads(line) for line in (rec.dir / "events.jsonl").read_text().splitlines()]
    assert not any("[laughs]" in json.dumps(e) or "[sighs]" in json.dumps(e) for e in events)
    log = [json.loads(line) for line in (rec.dir / "model-log.jsonl").read_text().splitlines()]
    assert log[0]["reply"]["text"] == "[laughs] That's a good one. [sighs] Long day?"


def test_the_call_view_shows_no_tag(tmp_path):
    """Records written before the captions stripped tags, or by another path, still show none (contract 2.4)."""
    from tests.test_call_view import CLOSED, ENDED, INBOUND, hear, record, say, speech, view

    record(tmp_path, [INBOUND, say(2, "[laughs] That's a good one."), hear(5, "Right?"),
                      say(7, "Okay. [sighs] Long day."), CLOSED, ENDED])
    assert speech(view(tmp_path)) == [("ai", "That's a good one."), ("person", "Right?"), ("ai", "Okay. Long day.")]


# ---------- what the Claude adapter tells Claude about a reply with tags (review of 2026-10-15, finding 7) ----------

def test_a_reply_that_is_only_a_tag_is_not_reported_unheard():
    """A reply of only "[laughs]", played in full: the captions node leaves it no words, so LiveKit files no assistant
    item for it. That is not a reply the caller missed, so no "did not hear" note goes to Claude next turn."""
    from tests.test_claude_llm import FakeSpeech, new_ctx, run_turn, say
    from voice.claude_llm import ClaudeBedrockLLM
    from voice.fake_claude import FakeAnthropic

    async def main():
        client = FakeAnthropic([say("[laughs]"), say("So, what's next?")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="And then the cat sat on my keyboard and sent the email.")
        speech = FakeSpeech()
        await run_turn(model, ctx, speech=speech)
        speech._done = True  # played to the end; no assistant item, since its caption was empty
        ctx.add_message(role="user", content="Anyway.")
        await run_turn(model, ctx)
        return client

    client = asyncio.run(main())
    second = client.calls[1]["messages"]
    assert second[1]["role"] == "assistant" and second[1]["content"][-1] == {"type": "text", "text": "[laughs]"}
    assert not any("did not hear" in json.dumps(m) for m in second) and client.problems == []


def test_a_reply_heard_in_full_but_its_tag_is_not_reported_cut_off():
    """LiveKit's item for a reply holds what the caller heard, without its tags: a reply whose words were all heard is
    not one the caller cut off, even when LiveKit marks the item interrupted (the caller spoke at its very end)."""
    from tests.test_claude_llm import new_ctx, run_turn, say, spoken
    from voice.claude_llm import ClaudeBedrockLLM
    from voice.fake_claude import FakeAnthropic

    async def main():
        client = FakeAnthropic([say("[laughs] Okay, I'll look it up."), say("Here it is."),
                                say("[sighs] That one was long. Let me check."), say("Sure.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Can you check the train times?")
        chunks = await run_turn(model, ctx)
        spoken(ctx, chunks, heard="Okay, I'll look it up.", interrupted=True)
        ctx.add_message(role="user", content="Thanks.")
        chunks = await run_turn(model, ctx)
        spoken(ctx, chunks)
        ctx.add_message(role="user", content="And the bus?")
        chunks = await run_turn(model, ctx)
        spoken(ctx, chunks, heard="That one was long.", interrupted=True)  # cut off for real
        ctx.add_message(role="user", content="Wait.")
        await run_turn(model, ctx)
        return client

    client = asyncio.run(main())
    second, fourth = client.calls[1]["messages"], client.calls[3]["messages"]
    assert not any("cut you off" in json.dumps(m) for m in second)
    assert 'heard only this part of your reply: \\"That one was long.\\"' in json.dumps(fourth[-1])
    assert client.problems == []


def test_a_reply_heard_as_the_speech_filter_leaves_it_gets_no_false_note():
    """Review of the final fixes, finding 4: the captions and LiveKit's item hold a reply after the speech filter
    too, which drops stage directions such as "[chuckles]" or "(웃음)", so a reply of only one got the "did not hear"
    note, and one heard in full but marked interrupted the "cut off" one. A reply is now compared as it is heard."""
    from tests.test_claude_llm import FakeSpeech, new_ctx, run_turn, say, spoken
    from voice.claude_llm import ClaudeBedrockLLM
    from voice.fake_claude import FakeAnthropic

    async def unheard(reply: str) -> list:
        client = FakeAnthropic([say(reply), say("So, what's next?")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="And then the cat sent the email.")
        speech = FakeSpeech()
        await run_turn(model, ctx, speech=speech)
        speech._done = True  # played to the end; nothing was left to caption
        ctx.add_message(role="user", content="Anyway.")
        await run_turn(model, ctx)
        return client.calls[1]["messages"]

    async def cut_off(reply: str, heard: str) -> list:
        client = FakeAnthropic([say(reply), say("Here it is.")])
        model = ClaudeBedrockLLM(client=client)
        ctx = new_ctx()
        ctx.add_message(role="user", content="Can you check the train times?")
        chunks = await run_turn(model, ctx)
        spoken(ctx, chunks, heard=heard, interrupted=True)
        ctx.add_message(role="user", content="Thanks.")
        await run_turn(model, ctx)
        return client.calls[1]["messages"]

    for reply in ("[chuckles]", "(웃음)"):
        assert not any("did not hear" in json.dumps(m) for m in asyncio.run(unheard(reply))), reply
    for reply in ("[chuckles] Okay, I'll look it up.", "(웃음) 네, 찾아볼게요."):
        heard = reply.split(" ", 1)[1]
        assert not any("cut you off" in json.dumps(m) for m in asyncio.run(cut_off(reply, heard))), reply
