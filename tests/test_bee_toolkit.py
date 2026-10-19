"""The Bee read toolkit (beelink/toolkit.py): one set of tools for the call AI, the meta agent and the call prep.

Each tool is one Bee read with Bee's own parameters, and its answer keeps Bee's fields; the only changes are our own
calls left out (with a count), locations and the email brief dropped, and long answers cut with a note on how to get
the rest. The fake `bee` answers (tests/fakebee/bee, FAKE_BEE_DATA for the answers a test sets) and logs each call.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from beelink import toolkit
from beelink.calls import OurCalls
from tests.helpers import SEOUL
from tests.test_beelink import calls, fake_bee  # noqa: F401  (pytest fixture)

NOW = datetime(2026, 10, 18, 15, 0, tzinfo=SEOUL)


def ms(text: str) -> int:
    """The user's local time as Bee's epoch milliseconds."""
    return int(datetime.fromisoformat(text).replace(tzinfo=SEOUL).timestamp() * 1000)


def answers(tmp_path: Path, monkeypatch, data: dict) -> None:
    path = tmp_path / "bee-answers.json"
    path.write_text(json.dumps(data, ensure_ascii=False))
    monkeypatch.setenv("FAKE_BEE_DATA", str(path))


def read(name: str, args: dict | None = None, *, ours: OurCalls | None = None, max_chars: int = 8000) -> toolkit.Read:
    return asyncio.run(toolkit.read(name, args or {}, ours=ours, zone=SEOUL, timeout=5.0, max_chars=max_chars))


def a_call(workspace: Path, start: str, end: str) -> None:
    """One of our calls in the person's folder, from `start` to `end` (the user's local times)."""
    began = datetime.fromisoformat(start).replace(tzinfo=SEOUL)
    seconds = (datetime.fromisoformat(end).replace(tzinfo=SEOUL) - began).total_seconds()
    folder = workspace / began.strftime("%Y-%m-%d") / began.strftime("%H%M")
    folder.mkdir(parents=True)
    events = [{"t": 0, "type": "call_started", "room": "r", "started_at": began.isoformat()},
              {"t": seconds, "type": "call_ended"}]
    (folder / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))


def test_one_set_of_twelve_tools_and_nothing_of_locations_photos_email_or_threads():
    assert toolkit.NAMES == ("bee_me", "bee_today", "bee_conversations", "bee_conversation", "bee_transcript",
                             "bee_related", "bee_search", "bee_daily", "bee_facts", "bee_todos", "bee_voice_notes",
                             "bee_insights")
    for tool in toolkit.TOOLS:
        assert tool.description and tool.parameters["type"] == "object", tool.name
        assert set(tool.parameters["required"]) <= set(tool.parameters["properties"]), tool.name
        words = json.dumps([tool.name, tool.parameters]).casefold()
        assert not any(w in words for w in ("location", "photo", "email", "thread")), tool.name


@pytest.mark.parametrize("name,args,said", [
    ("bee_me", {}, ["me"]),
    ("bee_today", {}, ["today", "--context"]),
    ("bee_conversations", {}, ["conversations", "list", "--limit", "5"]),
    ("bee_conversations", {"limit": 3, "cursor": "c1"}, ["conversations", "list", "--limit", "3", "--cursor", "c1"]),
    ("bee_conversation", {"id": 5}, ["conversations", "get", "5"]),
    ("bee_transcript", {"id": 5, "since": "2026-10-18T09:00"}, ["conversations", "get", "5"]),
    ("bee_related", {"id": 5}, ["conversations", "related", "5", "--limit", "5"]),
    ("bee_related", {"id": 5, "limit": 2}, ["conversations", "related", "5", "--limit", "2"]),
    ("bee_search", {"query": "hike"}, ["search", "--query", "hike", "--limit", "5"]),
    ("bee_search", {"query": "hike", "filter": "facts", "sort": "mostRecent"},
     ["search", "--query", "hike", "--limit", "5", "--filter", "facts", "--sort", "mostRecent"]),
    ("bee_search", {"query": "hike", "mode": "neural", "since": "2026-10-17T00:00", "until": 1792310400000},
     ["search", "--query", "hike", "--limit", "5", "--neural", "--since", str(ms("2026-10-17T00:00")),
      "--until", "1792310400000"]),
    ("bee_daily", {"date": "2026-10-17"}, ["daily", "find", "2026-10-17"]),
    ("bee_daily", {}, ["daily", "list", "--limit", "3"]),
    ("bee_daily", {"limit": 7, "cursor": "d2"}, ["daily", "list", "--limit", "7", "--cursor", "d2"]),
    ("bee_facts", {}, ["facts", "list", "--limit", "20"]),
    ("bee_facts", {"status": "unconfirmed", "cursor": "f2"}, ["facts", "list", "--unconfirmed", "--limit", "20",
                                                             "--cursor", "f2"]),
    ("bee_facts", {"query": "coffee", "limit": 10}, ["facts", "search", "--query", "coffee", "--limit", "10"]),
    ("bee_facts", {"fact_id": 7}, ["facts", "get", "7"]),
    ("bee_todos", {}, ["todos", "list", "--limit", "50"]),
    ("bee_todos", {"status": "done", "cursor": "50"}, ["todos", "list", "--limit", "50", "--cursor", "50"]),
    ("bee_todos", {"status": "suggested"}, ["todos", "suggestions", "--limit", "20"]),
    ("bee_todos", {"todo_id": 51}, ["todos", "get", "51"]),
    ("bee_voice_notes", {}, ["journals", "list", "--limit", "5"]),
    ("bee_voice_notes", {"query": "dentist"}, ["journals", "search", "--query", "dentist", "--limit", "5"]),
    ("bee_voice_notes", {"voice_note_id": "j-41"}, ["journals", "get", "j-41"]),
    ("bee_insights", {}, ["insights", "list", "--limit", "5"]),
    ("bee_insights", {"insight_id": 3}, ["insights", "get", "3"]),
])
def test_each_tool_is_one_bee_read_with_bee_s_own_parameters(fake_bee, tmp_path, monkeypatch, name, args, said):
    answers(tmp_path, monkeypatch, {"insights": {"insights": []}, "journals": {"journals": []},
                                    "facts search": {"results": []}, "daily list": {"daily_summaries": []},
                                    "conversations related": {"conversations": []}, "todos list": {"todos": []}})
    found = read(name, args)
    assert not found.text.startswith(("Bee read failed", "Not read")), found.text
    assert calls(fake_bee) == [said + ["--json"]]


def test_answers_keep_bee_s_fields_without_locations_or_the_email_brief(fake_bee, tmp_path, monkeypatch):
    place = {"address": "12 Some Street", "latitude": 37.5, "longitude": 127.0, "created_at": 1}
    answers(tmp_path, monkeypatch, {
        "conversations get 5": {"conversation": {
            "id": 5, "start_time": ms("2026-10-18T09:00"), "end_time": ms("2026-10-18T09:40"), "device_type": "Watch",
            "summary": "Talked through the home server.", "short_summary": "Home server", "state": "COMPLETED",
            "suggested_links": [], "primary_location": place,
            "transcriptions": [{"id": 1, "realtime": False, "utterances": [
                {"id": 1, "speaker": "Matt", "text": "It has to be quiet.", "spoken_at": ms("2026-10-18T09:01")},
                {"id": 2, "speaker": "Unknown", "text": "Then no fans.", "spoken_at": ms("2026-10-18T09:02")}]}]},
            "timezone": "Asia/Seoul"},
        "today --context": {"date": "2026-10-18", "todayBrief": {
            "calendar_events": [{"title": "Dentist"}], "emails": [{"subject": "Your bank statement"}]},
            "dailySummary": None, "activeTodos": [], "recentNotes": [], "recentConversations": []},
        "daily find 2026-10-17": {"daily_summary": {
            "id": 3, "date": "2026-10-17", "short_summary": "A walk", "summary": "Walked and talked about servers.",
            "email_summary": "Three emails about the bank.", "calendar_summary": "Dentist at 3.",
            "locations": [place], "conversations": [{"id": 4, "start_time": 1, "primary_location": place}]}},
    })
    one = read("bee_conversation", {"id": 5})
    assert one.data["conversation"]["device_type"] == "Watch" and one.data["conversation"]["suggested_links"] == []
    assert one.data["conversation"]["summary"] == "Talked through the home server."
    assert "transcriptions" not in one.data["conversation"] and one.data["conversation"]["utterances_count"] == 2
    assert "bee_transcript" in one.text  # where its lines are
    today = read("bee_today")
    assert today.data["todayBrief"]["calendar_events"] == [{"title": "Dentist"}]
    day = read("bee_daily", {"date": "2026-10-17"})
    assert day.data["daily_summary"]["calendar_summary"] == "Dentist at 3."
    for found in (one, today, day):
        for gone in ("12 Some Street", "latitude", "primary_location", "bank", "email"):
            assert gone not in found.text, (gone, found.text)


def test_what_bee_recorded_during_our_calls_is_left_out_and_counted(fake_bee, tmp_path, monkeypatch):
    """Bee's recording of one of our calls is left out whole; a longer recording that runs through a call keeps its
    other lines (a watch can record for hours); facts Bee made of a call and a memo recorded during one are left out.
    Each answer says how many items it left out."""
    ws = tmp_path / "workspace"
    a_call(ws, "2026-10-18T10:00", "2026-10-18T10:20")
    ours = OurCalls.of(ws)
    lines = [{"id": n, "speaker": "Matt", "text": text, "spoken_at": ms(at)}
             for n, (at, text) in enumerate([("2026-10-18T09:30", "Walking to the station."),
                                             ("2026-10-18T10:05", "Bee Call, remind me about the server."),
                                             ("2026-10-18T10:40", "Back at my desk.")])]
    answers(tmp_path, monkeypatch, {
        "conversations list": {"conversations": [
            {"id": 11, "start_time": ms("2026-10-18T10:01"), "end_time": ms("2026-10-18T10:19"),
             "short_summary": "Our call"},
            {"id": 12, "start_time": ms("2026-10-18T09:00"), "end_time": ms("2026-10-18T11:00"),
             "short_summary": "A long walk"},
            {"id": 13, "start_time": ms("2026-10-18T08:00"), "end_time": ms("2026-10-18T08:10"),
             "short_summary": "Breakfast"}], "next_cursor": None},
        "conversations get 11": {"conversation": {"id": 11, "start_time": ms("2026-10-18T10:01"),
                                                  "end_time": ms("2026-10-18T10:19")}},
        "conversations get 12": {"conversation": {"id": 12, "start_time": ms("2026-10-18T09:00"),
                                                  "end_time": ms("2026-10-18T11:00"),
                                                  "transcriptions": [{"realtime": False, "utterances": lines}]}},
        "facts list": {"facts": [
            {"id": 21, "text": "Wants a quiet home server", "created_at": ms("2026-10-18T10:25"), "confirmed": True},
            {"id": 22, "text": "Drinks coffee", "created_at": ms("2026-10-15T08:00"), "confirmed": True}]},
        "journals list": {"journals": [
            {"id": "j-1", "text": "Said during the call", "created_at": ms("2026-10-18T10:10")},
            {"id": "j-2", "text": "Buy milk", "created_at": ms("2026-10-18T12:00")}]},
    })
    listed = read("bee_conversations", ours=ours)
    assert [c["id"] for c in listed.data["conversations"]] == [12, 13]
    assert "1 conversation Bee recorded during your calls with the user is left out" in listed.text
    assert "12" in listed.text and "runs through" in listed.text  # the long walk: its summary may cover the call
    assert "left out" in read("bee_conversation", {"id": 11}, ours=ours).text
    said = read("bee_transcript", {"id": 12}, ours=ours)
    assert [u["text"] for u in said.data["transcript"]] == ["Walking to the station.", "Back at my desk."]
    assert "1 line said during your calls with the user is left out" in said.text
    facts = read("bee_facts", ours=ours)
    assert [f["id"] for f in facts.data["facts"]] == [22] and "1 fact" in facts.text
    memos = read("bee_voice_notes", ours=ours)
    assert [m["id"] for m in memos.data["journals"]] == ["j-2"] and "1 voice memo" in memos.text


def test_a_fact_our_call_wrote_on_the_user_s_yes_is_theirs_and_kept(fake_bee, tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    a_call(ws, "2026-10-18T10:00", "2026-10-18T10:20")
    folder = ws / "2026-10-18" / "1000"
    (folder / "bee-writes.jsonl").write_text(
        json.dumps({"key": "w1", "status": "applying", "item": {"kind": "fact_create", "text": "Wants a quiet home server"},
                    "question": "q", "answer": "a", "at": "2026-10-18T10:10:00+09:00"}) + "\n"
        + json.dumps({"key": "w1", "status": "ok", "response": {"fact": {"id": 21}}, "at": "2026-10-18T10:10:01+09:00"})
        + "\n")
    answers(tmp_path, monkeypatch, {"facts list": {"facts": [
        {"id": 21, "text": "Wants a quiet home server", "created_at": ms("2026-10-18T10:10"), "confirmed": True}]}})
    assert [f["id"] for f in read("bee_facts", ours=OurCalls.of(ws)).data["facts"]] == [21]


def test_a_long_list_is_cut_with_the_ids_left_out_and_the_next_cursor(fake_bee, tmp_path, monkeypatch):
    long = "word " * 400
    answers(tmp_path, monkeypatch, {"conversations list": {"conversations": [
        {"id": 100 + n, "start_time": ms("2026-10-17T09:00") + n, "summary": long, "short_summary": f"Talk {n}"}
        for n in range(6)], "next_cursor": "next-page"}})
    found = read("bee_conversations", {"limit": 6}, max_chars=2500)
    assert len(found.text) <= 2500 + 600  # the notes may follow
    shown = [c["id"] for c in found.data["conversations"]]
    assert shown and shown == [100 + n for n in range(len(shown))]
    assert "(cut)" in found.text and "bee_conversation" in found.text  # a long summary is read whole from there
    if len(shown) < 6:
        assert str(105) in found.text  # the ids left out, to read one by one
    assert "next-page" in found.text


def test_a_long_recording_is_read_by_time_and_cut_with_how_to_read_on(fake_bee, tmp_path, monkeypatch):
    """One recording can run for days: since and until read one part, given as the user's local time or as Bee's
    epoch milliseconds; a cut answer says which since or until reads on."""
    lines = [{"id": n, "speaker": "Matt", "text": f"Line {n} " + "x" * 80,
              "spoken_at": ms("2026-10-17T09:00") + n * 60_000} for n in range(120)]
    live = [dict(u, id=1000 + u["id"]) for u in lines]  # a live transcription of the same words: read once
    answers(tmp_path, monkeypatch, {"conversations get 9": {"conversation": {
        "id": 9, "start_time": ms("2026-10-17T09:00"), "end_time": None,
        "transcriptions": [{"realtime": True, "utterances": live}, {"realtime": False, "utterances": lines}]}}})
    part = read("bee_transcript", {"id": 9, "since": "2026-10-17T10:00", "until": "2026-10-17T10:09"})
    assert [u["id"] for u in part.data["transcript"]] == list(range(60, 70))
    newest = read("bee_transcript", {"id": 9}, max_chars=2000)
    shown = [u["id"] for u in newest.data["transcript"]]
    assert shown[-1] == 119 and len(shown) < 120 and len(set(shown)) == len(shown)
    assert f"until {ms('2026-10-17T09:00') + shown[0] * 60_000 - 1}" in newest.text  # the lines before these
    onward = read("bee_transcript", {"id": 9, "since": ms("2026-10-17T09:00")}, max_chars=2000)
    first = [u["id"] for u in onward.data["transcript"]]
    assert first[0] == 0 and first[-1] < 119
    assert f"since {ms('2026-10-17T09:00') + (first[-1] + 1) * 60_000}" in onward.text  # the lines after these


def test_open_or_done_to_dos_from_one_page_say_what_the_page_also_had(fake_bee, tmp_path, monkeypatch):
    answers(tmp_path, monkeypatch, {"todos list": {"todos": [
        {"id": 1, "text": "Book the dentist", "completed": False, "created_at": 1},
        {"id": 2, "text": "Order the tripod", "completed": True, "created_at": 2}], "next_cursor": "50"}})
    open_ = read("bee_todos")
    assert [t["id"] for t in open_.data["todos"]] == [1]
    assert "1 done to-do on this page is not shown" in open_.text and "50" in open_.text
    assert [t["id"] for t in read("bee_todos", {"status": "done"}).data["todos"]] == [2]


def test_a_read_that_fails_says_so_and_never_raises(fake_bee, monkeypatch):
    monkeypatch.setenv("FAKE_BEE_DOWN", "insights list")
    found = read("bee_insights")
    assert found.text.startswith("Bee read failed") and found.data is None
    assert read("bee_transcript", {}).text.startswith("Not read")
    assert read("bee_search", {"query": "x", "mode": "neural", "filter": "facts"}).text.startswith("Not read")
    assert read("bee_location").text == "There is no tool named bee_location."



def test_a_search_leaves_out_facts_bee_made_of_our_calls_too(fake_bee, tmp_path, monkeypatch):
    """Review finding 2: bee_search filtered only conversations."""
    ws = tmp_path / "workspace"
    a_call(ws, "2026-10-18T10:00", "2026-10-18T10:20")
    answers(tmp_path, monkeypatch, {"search": {"results": [
        {"type": "fact", "id": 21, "text": "Wants a quiet home server", "created_at": ms("2026-10-18T10:25"),
         "confirmed": False},
        {"type": "fact", "id": 22, "text": "Drinks coffee", "created_at": ms("2026-10-15T08:00"), "confirmed": True},
        {"type": "daily", "id": 9, "date": "2026-10-17", "short_summary": "A walk"}]}})
    found = read("bee_search", {"query": "server", "filter": "all"}, ours=OurCalls.of(ws))
    assert [r["id"] for r in found.data["results"]] == [22, 9] and "1 fact" in found.text


def test_an_answer_with_lists_inside_is_cut_to_fit_saying_where_the_rest_is(fake_bee, tmp_path, monkeypatch):
    """Review finding 3: bee_today came out at about 10,000 characters against a 3,000 limit."""
    memo = {"id": "j", "text": "x" * 400, "aiResponse": {"message": "y" * 400}, "created_at": 1}
    answers(tmp_path, monkeypatch, {"today --context": {
        "date": "2026-10-18", "todayBrief": {"calendar_events": []}, "dailySummary": {"summary": "s" * 1500},
        "activeTodos": [{"id": n, "text": "t" * 120} for n in range(10)],
        "recentNotes": [dict(memo, id=f"j{n}") for n in range(5)],
        "recentConversations": [{"id": n, "short_summary": "c" * 100, "summary": "d" * 600} for n in range(5)]}})
    found = read("bee_today", max_chars=3000)
    assert len(found.text) <= 3000
    # each list cut says how much of it is shown, and the ids of the rest with the tool that reads each
    assert "(recentConversations: 2 of 5 shown; the rest are ids 2, 3, 4, read each with bee_conversation.)" in found.text
    assert "the rest are ids j1, j2, j3, j4, read each with bee_voice_notes with voice_note_id" in found.text
    assert "read each with bee_todos with todo_id" in found.text


def test_the_user_s_own_to_dos_are_never_left_out_by_when_they_were_made(fake_bee, tmp_path, monkeypatch):
    """Review finding 6: Bee suggests to-dos but never makes them; the list is the user's own, also one made right
    after one of our calls. Suggestions Bee made of our calls are left out."""
    ws = tmp_path / "workspace"
    a_call(ws, "2026-10-18T10:00", "2026-10-18T10:20")
    answers(tmp_path, monkeypatch, {
        "todos list": {"todos": [{"id": 1, "text": "Call mom", "completed": False, "created_at": ms("2026-10-18T10:25")}]},
        "todos suggestions": {"todoSuggestions": [
            {"id": 7, "text": "Order RAM", "created_at": ms("2026-10-18T10:26")},
            {"id": 8, "text": "Book the dentist", "created_at": ms("2026-10-15T09:00")}]}})
    ours = OurCalls.of(ws)
    assert [t["id"] for t in read("bee_todos", ours=ours).data["todos"]] == [1]
    suggested = read("bee_todos", {"status": "suggested"}, ours=ours)
    assert [t["id"] for t in suggested.data["todoSuggestions"]] == [8] and "1 suggested to-do" in suggested.text
