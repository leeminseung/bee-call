"""The call list and the call view (contract/app-contract.md, sections 1 and 2), from call records.

The fixture records under tests/fixtures/sessions/ are one person's workspace. The first test builds the list and the
three views of the contract's examples from them and writes them over contract/examples/, so the examples are this
code's output; every body is checked against contract/schemas/. The other tests are the rules, one each, including
the cases the web page's tests (web/test/page.test.js) checked when the page read events.jsonl itself.
"""

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.contract import check_body
from voice import call_view
from voice.call_view import BadRequest

FIXTURES = Path(__file__).parent / "fixtures" / "sessions"
EXAMPLES = Path(__file__).parent.parent / "contract" / "examples"
SEOUL = ZoneInfo("Asia/Seoul")
AFTERNOON = datetime(2026, 10, 15, 15, 0, tzinfo=SEOUL)  # after every fixture call
MAIN = "call-minseung-20261015-012301"
AI_CALL = "call-minseung-20261014-180158"


def write_example(name: str, body: dict) -> None:
    (EXAMPLES / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


def at_3_41(tmp_path: Path) -> tuple[Path, float]:
    """The 01:23 call as it was 3:41 into it (01:26:46): its files cut at that moment, its record written just now."""
    workspace = tmp_path / "live"
    source, folder = FIXTURES / "2026-10-15" / "0123", workspace / "2026-10-15" / "0123"
    folder.mkdir(parents=True)
    started = datetime(2026, 10, 15, 1, 23, 5, tzinfo=SEOUL)
    cut = started + timedelta(seconds=221)
    lines = (source / "events.jsonl").read_text().splitlines(keepends=True)
    (folder / "events.jsonl").write_text("".join(l for l in lines if json.loads(l)["t"] <= 221))
    writes = (source / "bee-writes.jsonl").read_text().splitlines(keepends=True)
    (folder / "bee-writes.jsonl").write_text(
        "".join(l for l in writes if datetime.fromisoformat(json.loads(l)["at"]) <= cut))
    job = folder / "jobs" / "01"
    job.mkdir(parents=True)
    shutil.copy(source / "jobs" / "01" / "request.md", job / "request.md")
    job_lines = (source / "jobs" / "01" / "events.jsonl").read_text().splitlines(keepends=True)
    (job / "events.jsonl").write_text("".join(l for l in job_lines if json.loads(l)["at"] <= cut.timestamp()))
    wall = 1_900_000_000.0
    os.utime(folder / "events.jsonl", (wall, wall))
    return workspace, wall + 5


def test_the_examples_are_the_code_s_output(tmp_path):
    """The list's first six calls and the three views, as the server answers them, written over contract/examples/
    and valid against the contract's schemas."""
    listed = call_view.call_list(FIXTURES, zone=SEOUL, language="ko", now=AFTERNOON, limit=6)
    ended = call_view.call_view(FIXTURES, MAIN, zone=SEOUL, language="ko", now=AFTERNOON)
    ai = call_view.call_view(FIXTURES, AI_CALL, zone=SEOUL, language="ko", now=AFTERNOON)
    workspace, wall = at_3_41(tmp_path)
    live = call_view.call_view(workspace, MAIN, zone=SEOUL, language="ko",
                               now=datetime(2026, 10, 15, 1, 26, 51, tzinfo=SEOUL), wall=wall)
    change = {"path": f"/calls/{MAIN}", "version": live["version"]}  # what /changes says as the 3:41 view lands
    for name, body in (("calls", listed), ("call-view", ended), ("call-view-ai", ai), ("call-view-live", live),
                       ("change", change)):
        check_body(name, body)
        write_example(name, body)

    assert [c["call_id"] for c in listed["calls"]] == [
        "call-minseung-20261015-142209", MAIN, "call-minseung-20261014-214502", AI_CALL,
        "call-minseung-20261014-122604", "call-minseung-20261014-083100"]
    assert [c["state"] for c in listed["calls"]] == ["ended"] * 5 + ["missed"]
    assert [c["title_source"] for c in listed["calls"]] == ["summary", "summary", "none", "summary", "summary", "topic"]
    assert listed["more"] is True and listed["before"]

    assert ended["call"]["duration_s"] == 460 and ended["call"]["marks"] == {
        "report": True, "table": True, "chart": True, "bee_writes": 2}
    kept = [(i["kind"], i["text"], i["state"], i["undoable"]) for i in ended["items"] if i["type"] == "bee_write"]
    assert kept == [("todo_create", "토요일 촬영 장소 정하기", "written", False),
                    ("fact_create", "Works at the library almost every day", "written", False)]  # 2026-10-17: a new kind, in English
    assert [i["id"] for i in ended["items"]][:6] == ["speech-9", "speech-14", "speech-19", "speech-24", "write-w1",
                                                     "speech-29"]
    assert ended["captions_included"] == {"person": 14, "ai": 13}
    job = next(i for i in ended["items"] if i["type"] == "job")
    assert (job["state"], job["duration_s"], job["at"]) == ("done", 87, "2026-10-15T01:27:03+09:00")
    assert [(f["name"], f["kind"]) for f in job["files"]] == [
        ("price-chart.svg", "chart"), ("report.md", "report"), ("comparison.csv", "table")]
    assert ended["prep"]["note"] == ("오늘 아침 회의에서 노트북 이야기를 했고, 한국 가격을 궁금해했어요. 9월 28일부터 남은 일: "
                                     "아이폰과 애플워치에 Bee 설정하기.")

    assert live["call"]["state"] == "live" and live["call"]["duration_s"] is None
    assert (live["call"]["title"], live["call"]["title_source"]) == ("토요일 촬영 장소를 아직 못 정했어요.", "first_sentence")
    assert live["captions_included"] == {"person": 8, "ai": 7}
    assert live["items"][-1]["type"] == "job" and live["items"][-1]["state"] == "running"
    assert live["items"][-1]["step"] == "애플 한국 가격표 읽는 중"
    assert live["items"][-2]["text"] == "주로 파이썬이고 머신러닝도 조금 해요."
    write = next(i for i in live["items"] if i["type"] == "bee_write")
    assert (write["state"], write["undoable"]) == ("written", True)

    assert ai["call"]["started_by"] == "ai" and ai["why_calling"]["reason"].startswith("새 음성 메모에서")
    assert [q["speaker"] for q in ai["why_calling"]["conversation"]["quotes"]] == ["other", "person"]
    assert [i["interrupted"] for i in ai["items"] if i["speaker"] == "ai"] == [False, True, False, False]
    assert ai["captions_included"] == {"person": 3, "ai": 4}


def test_the_same_files_give_the_same_version_and_a_change_a_new_one(tmp_path):
    """A version changes when anything the body shows changes, never because time passed: a running job's body is
    the same a minute later."""
    workspace, wall = at_3_41(tmp_path)
    when = datetime(2026, 10, 15, 1, 26, 51, tzinfo=SEOUL)
    first = call_view.call_view(workspace, MAIN, zone=SEOUL, language="ko", now=when, wall=wall)
    later = call_view.call_view(workspace, MAIN, zone=SEOUL, language="ko", now=when + timedelta(seconds=60),
                                wall=wall + 60)
    assert first == later
    with (workspace / "2026-10-15" / "0123" / "events.jsonl").open("a") as f:
        f.write(json.dumps({"t": 225.0, "type": "user_transcript", "text": "도서관이나 카페에서 일할 때가 많아요."},
                           ensure_ascii=False) + "\n")
    os.utime(workspace / "2026-10-15" / "0123" / "events.jsonl", (wall, wall))
    grown = call_view.call_view(workspace, MAIN, zone=SEOUL, language="ko", now=when, wall=wall)
    assert grown["version"] != first["version"]
    assert [i["id"] for i in grown["items"]] == [i["id"] for i in first["items"]], "an item keeps its id as it grows"
    assert grown["captions_included"]["person"] == 9


# ---------- building records for the rules ----------

STARTED = "2026-10-15T20:00:00+09:00"
EVENING = datetime(2026, 10, 15, 21, 0, tzinfo=SEOUL)


def record(workspace: Path, events: list[dict], *, folder: str = "2026-10-15/2000", room: str = "call-matt-1",
           writes: list[dict] | None = None, started: str = STARTED, age_s: float = 3600) -> Path:
    """A call folder with these events after its call_started line; its file last written `age_s` before WALL."""
    path = workspace / folder
    path.mkdir(parents=True)
    lines = [{"t": 0.0, "type": "call_started", "room": room, "started_at": started}] + events
    (path / "events.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lines))
    if writes is not None:
        (path / "bee-writes.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in writes))
    os.utime(path / "events.jsonl", (WALL - age_s, WALL - age_s))
    return path


WALL = 2_000_000_000.0


def view(workspace: Path, call_id: str = "call-matt-1", now: datetime = EVENING) -> dict:
    body = call_view.call_view(workspace, call_id, zone=SEOUL, language="en", now=now, wall=WALL)
    assert body is not None
    check_body("call-view", body)
    return body


def listed(workspace: Path, now: datetime = EVENING, **kwargs) -> dict:
    body = call_view.call_list(workspace, zone=SEOUL, language="en", now=now, wall=WALL, **kwargs)
    check_body("calls", body)
    return body


def say(t: float, text: str, interrupted: bool = False) -> dict:
    return {"t": t, "type": "message", "role": "assistant", "text": text, "interrupted": interrupted, "metrics": None}


def hear(t: float, text: str) -> dict:
    return {"t": t, "type": "user_transcript", "text": text}


INBOUND = {"t": 0.01, "type": "call", "direction": "inbound", "topic": None, "reason": None, "continue_from": None}
CLOSED = {"t": 60.0, "type": "session_closed", "reason": "CloseReason.PARTICIPANT_DISCONNECTED"}
ENDED = {"t": 61.0, "type": "call_ended"}


def speech(body: dict) -> list[tuple[str, str]]:
    return [(i["speaker"], i["text"]) for i in body["items"] if i["type"] == "speech"]


# ---------- speech ----------

def test_three_pieces_of_the_person_s_speech_make_one_bubble(tmp_path):
    """Moved from the page's tests: the person's final transcripts before the AI's next words read as one bubble."""
    record(tmp_path, [INBOUND, say(3, "Hi, what's up?"), hear(6, "So I was thinking"), hear(8, "about Saturday."),
                      hear(10, "Can we go early?"), say(14, "Sure, how early?"), CLOSED, ENDED])
    body = view(tmp_path)
    assert speech(body) == [("ai", "Hi, what's up?"), ("person", "So I was thinking about Saturday. Can we go early?"),
                            ("ai", "Sure, how early?")]
    assert body["captions_included"] == {"person": 3, "ai": 2}


def test_an_empty_piece_adds_nothing_and_a_card_between_pieces_splits_the_run(tmp_path):
    job = [{"t": 5, "type": "job_started", "job_id": "01", "task": "Check the forecast", "title": "Forecast"},
           {"t": 9, "type": "job_done", "job_id": "01", "summary": "Dry all day.", "files": []}]
    record(tmp_path, [INBOUND, say(3, "Hi."), hear(6, "First part,"), hear(7, "  "), say(8, ""), *job,
                      hear(11, "second part."), CLOSED, ENDED])
    body = view(tmp_path)
    assert [(i["type"], i.get("text")) for i in body["items"]] == [
        ("speech", "Hi."), ("speech", "First part,"), ("job", None), ("speech", "second part.")]
    assert body["captions_included"] == {"person": 2, "ai": 1}


def test_an_ai_reply_sits_where_its_voice_started_so_the_words_that_cut_it_come_after(tmp_path):
    """The AI's message is recorded when its speech ends; it sits where its voice started (LiveKit's metrics), and
    is marked when the person cut it off."""
    cut = {"t": 9.0, "type": "message", "role": "assistant", "text": "Let me tell you about", "interrupted": True,
           "metrics": {"started_speaking_at": 1000.0, "stopped_speaking_at": 1004.0}}
    record(tmp_path, [INBOUND, hear(4, "Tell me."), hear(8.5, "Actually, wait."), cut, CLOSED, ENDED])
    body = view(tmp_path)
    assert speech(body) == [("person", "Tell me."), ("ai", "Let me tell you about"), ("person", "Actually, wait.")]
    assert [i["interrupted"] for i in body["items"]] == [False, True, False]
    assert body["items"][1]["at"] == "2026-10-15T20:00:05+09:00"


def test_a_record_without_final_transcripts_reads_the_person_s_turns(tmp_path):
    """Records from before user_transcript: the person's committed turns are their speech."""
    record(tmp_path, [INBOUND, say(2, "Hello."), {"t": 5, "type": "message", "role": "user", "text": "Hi there.",
                                                  "interrupted": False, "metrics": None}, CLOSED, ENDED])
    assert speech(view(tmp_path)) == [("ai", "Hello."), ("person", "Hi there.")]


# ---------- how the call ended ----------

@pytest.mark.parametrize("closed, state", [
    ("CloseReason.USER_INITIATED", "ended"), ("CloseReason.PARTICIPANT_DISCONNECTED", "ended"),
    ("task_completed", "ended"), ("CloseReason.JOB_SHUTDOWN", "dropped"), ("CloseReason.ERROR", "dropped")])
def test_the_close_reason_decides_ended_or_dropped(tmp_path, closed, state):
    record(tmp_path, [INBOUND, say(2, "Hi."), {"t": 30.4, "type": "session_closed", "reason": closed}, ENDED])
    call = view(tmp_path)["call"]
    assert (call["state"], call["duration_s"]) == (state, 30)


def test_a_record_that_ended_without_closing_its_session_was_dropped(tmp_path):
    record(tmp_path, [INBOUND, say(2, "Hi."), {"t": 12.0, "type": "call_ended"}])
    assert (view(tmp_path)["call"]["state"], view(tmp_path)["call"]["duration_s"]) == ("dropped", 12)


def test_a_record_without_an_end_is_live_while_it_shows_life_and_dropped_after_two_quiet_minutes(tmp_path):
    record(tmp_path, [INBOUND, say(2, "Hi."), hear(40.6, "Hello?")], age_s=119)
    assert (view(tmp_path)["call"]["state"], view(tmp_path)["call"]["duration_s"]) == ("live", None)
    shutil.rmtree(tmp_path / "2026-10-15")
    record(tmp_path, [INBOUND, say(2, "Hi."), hear(40.6, "Hello?"),
                      {"t": 99, "type": "call_titled", "title": "Hello"}], age_s=121)
    assert (view(tmp_path)["call"]["state"], view(tmp_path)["call"]["duration_s"]) == ("dropped", 41)


def test_a_job_the_call_outlived_reads_as_failed_when_the_call_ended(tmp_path):
    """A running job of a dropped call failed with call_ended, at the call's end; a job the call's end cancelled
    says the same; any other failure is an error."""
    events = [INBOUND, {"t": 5, "type": "job_started", "job_id": "01", "task": "A", "title": "First"},
              {"t": 6, "type": "job_started", "job_id": "02", "task": "B", "title": "Second"},
              {"t": 8, "type": "job_failed", "job_id": "02", "error": "the model refused"},
              {"t": 9, "type": "job_started", "job_id": "03", "task": "C", "title": "Third"}, hear(20, "Still there?")]
    record(tmp_path, events, age_s=600)
    jobs = {i["title"]: i for i in view(tmp_path)["items"] if i["type"] == "job"}
    assert (jobs["First"]["state"], jobs["First"]["failure"], jobs["First"]["duration_s"]) == ("failed", "call_ended", 15)
    assert jobs["First"]["at"] == "2026-10-15T20:00:20+09:00"
    assert (jobs["Second"]["failure"], jobs["Second"]["duration_s"]) == ("error", 2)
    cancelled = [INBOUND, {"t": 5, "type": "job_started", "job_id": "01", "task": "A", "title": "First"},
                 CLOSED, {"t": 60.5, "type": "job_failed", "job_id": "01", "error": "cancelled"}, ENDED]
    record(tmp_path, cancelled, folder="2026-10-15/2100", room="call-matt-2")
    (job,) = [i for i in view(tmp_path, "call-matt-2")["items"] if i["type"] == "job"]
    assert (job["state"], job["failure"]) == ("failed", "call_ended")


def job_log(folder: Path, job_id: str, touched: float, lines: list[dict] | None = None) -> None:
    """A job's own log (jobs/<NN>/events.jsonl), last touched at `touched`: a running job touches it as it works."""
    job = folder / "jobs" / job_id
    job.mkdir(parents=True)
    (job / "events.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines or [{"at": 0, "type": "started"}]))
    os.utime(job / "events.jsonl", (touched, touched))


CALL_START = datetime.fromisoformat(STARTED).timestamp()


def test_a_job_that_outlives_its_call_runs_on_and_lands_after_the_call(tmp_path):
    """A job keeps working after its call ends (2026-10-16): while its log shows life it reads as running, docked last;
    once done it sits where it finished, after the call's last words."""
    started = {"t": 5, "type": "job_started", "job_id": "01", "task": "A", "title": "Prices"}
    folder = record(tmp_path, [INBOUND, say(2, "Hi."), started, hear(20, "Got to go, bye."), CLOSED, ENDED], age_s=30)
    job_log(folder, "01", touched=WALL - 10)
    body = view(tmp_path)
    assert body["call"]["state"] == "ended" and body["items"][-1]["id"] == "job-01"
    job = body["items"][-1]
    assert (job["state"], job["duration_s"], job["failure"], job["summary"]) == ("running", None, None, None)
    with (folder / "events.jsonl").open("a") as f:  # the job's end, after call_ended
        f.write(json.dumps({"t": 200.5, "type": "job_done", "job_id": "01", "summary": "Cheapest in March.",
                            "files": []}) + "\n")
    body = view(tmp_path)
    assert [i["type"] for i in body["items"]] == ["speech", "speech", "job"]
    job = body["items"][-1]
    assert (job["state"], job["summary"], job["duration_s"]) == ("done", "Cheapest in March.", 196)
    assert job["at"] == "2026-10-15T20:03:20+09:00" and body["call"]["duration_s"] == 60


def test_a_job_whose_log_went_quiet_without_an_end_failed(tmp_path):
    """A job that stopped with no end on record (its process was killed: a restart) never stays running. One whose log
    went quiet after its call ended was cut off: failed with error. One that went quiet when its call did (a call
    from before jobs outlived calls, or a dropped one) failed with call_ended, as before."""
    started = {"t": 5, "type": "job_started", "job_id": "01", "task": "A", "title": "Prices"}
    folder = record(tmp_path, [INBOUND, started, hear(20, "Bye."), CLOSED, ENDED], age_s=900)
    job_log(folder, "01", touched=WALL - 600)
    (job,) = [i for i in view(tmp_path)["items"] if i["type"] == "job"]
    assert (job["state"], job["failure"]) == ("failed", "error")
    folder = record(tmp_path, [INBOUND, started, hear(20, "Bye."), CLOSED, ENDED], folder="2026-10-15/2100",
                    room="call-matt-2", age_s=900)
    job_log(folder, "01", touched=CALL_START + 30)
    (job,) = [i for i in view(tmp_path, "call-matt-2")["items"] if i["type"] == "job"]
    assert (job["state"], job["failure"], job["at"]) == ("failed", "call_ended", "2026-10-15T20:01:00+09:00")


def test_running_jobs_come_last_and_a_job_without_a_title_is_named_by_its_task(tmp_path):
    task = "Compare Bukhansan and Gwanaksan for an easy Saturday hike and bring back a chart"
    record(tmp_path, [INBOUND, {"t": 5, "type": "job_started", "job_id": "01", "task": task},
                      {"t": 6, "type": "job_progress", "job_id": "01", "text": "Reading the forecast"},
                      say(9, "On it.")], age_s=10)
    body = view(tmp_path)
    assert [i["type"] for i in body["items"]] == ["speech", "job"]
    job = body["items"][-1]
    assert job["title"] == "Compare Bukhansan and Gwanaksan for an…" and len(job["title"]) <= 40
    assert (job["state"], job["step"], job["at"], job["started_at"]) == (
        "running", "Reading the forecast", "2026-10-15T20:00:05+09:00", "2026-10-15T20:00:05+09:00")


def test_a_finished_job_lists_its_files_charts_first_and_never_its_bookkeeping(tmp_path):
    """A chart is listed once, by its SVG, with its PNG and its dark PNG (null for a chart drawn before dark PNGs, or a
    picture that is no chart of the worker's); the report with its page. A chart's PNGs and the report's page are
    never files of their own, even where a record lists them."""
    folder = record(tmp_path, [INBOUND, {"t": 5, "type": "job_started", "job_id": "01", "task": "x", "title": "X"},
                               {"t": 9, "type": "job_done", "job_id": "01", "summary": "Done.",
                                "files": [f"jobs/01/{n}" for n in ("b.csv", "a.tsv", "notes.txt", "old.svg",
                                                                   "photo.png", "price.png", "price.svg",
                                                                   "price-dark.png", "rain.png", "rain.svg",
                                                                   "report.md", "report.html", "usage.json",
                                                                   "request.md", "gone.md")]},
                               CLOSED, ENDED])
    job = folder / "jobs" / "01"
    job.mkdir(parents=True)
    for name in ("b.csv", "a.tsv", "notes.txt", "old.svg", "photo.png", "price.png", "price-dark.png", "rain.png",
                 "rain.svg", "report.md", "report.html", "usage.json", "request.md"):
        (job / name).write_text("x")
    (job / "price.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><title>Prices</title></svg>')
    (item,) = [i for i in view(tmp_path)["items"] if i["type"] == "job"]
    base = "/calls/call-matt-1/files/jobs/01"
    assert [(f["name"], f["kind"], f["title"], f["image_url"], f["image_url_dark"]) for f in item["files"]] == [
        ("photo.png", "chart", None, f"{base}/photo.png", None),
        ("price.svg", "chart", "Prices", f"{base}/price.png", f"{base}/price-dark.png"),
        ("rain.svg", "chart", None, f"{base}/rain.png", None),
        ("report.md", "report", None, None, None),
        ("a.tsv", "table", None, None, None), ("b.csv", "table", None, None, None),
        ("notes.txt", "other", None, None, None), ("old.svg", "other", None, None, None)]
    assert [f["page_url"] for f in item["files"]] == [None] * 3 + [f"{base}/report.html"] + [None] * 4
    assert item["files"][3]["url"] == f"{base}/report.md"


def test_a_chart_named_like_another_s_dark_png_never_lends_it_its_picture(tmp_path):
    """Before save_chart refused such names, a job could draw charts rain and rain-dark: rain-dark.png is then the
    second chart's own picture, never the first's dark one (review of the dark charts, 2026-10-16)."""
    names = ("rain.png", "rain.svg", "rain-dark.png", "rain-dark.svg", "rain-dark-dark.png")
    folder = record(tmp_path, [INBOUND, {"t": 5, "type": "job_started", "job_id": "01", "task": "x", "title": "X"},
                               {"t": 9, "type": "job_done", "job_id": "01", "summary": "Done.",
                                "files": [f"jobs/01/{n}" for n in names[:4]]}, CLOSED, ENDED])
    job = folder / "jobs" / "01"
    job.mkdir(parents=True)
    for name in names:
        (job / name).write_text("x")
    (item,) = [i for i in view(tmp_path)["items"] if i["type"] == "job"]
    base = "/calls/call-matt-1/files/jobs/01"
    assert [(f["name"], f["image_url"], f["image_url_dark"]) for f in item["files"]] == [
        ("rain-dark.svg", f"{base}/rain-dark.png", f"{base}/rain-dark-dark.png"),
        ("rain.svg", f"{base}/rain.png", None)]


def test_a_chart_s_dark_png_or_a_report_s_page_made_later_shows_at_once(tmp_path):
    """The one-off commands draw a dark PNG for a chart from before dark PNGs and render the page of a report from
    before pages: the view has them at once, by a new version, though the job's record never named the files."""
    folder = record(tmp_path, [INBOUND, {"t": 5, "type": "job_started", "job_id": "01", "task": "x", "title": "X"},
                               {"t": 9, "type": "job_done", "job_id": "01", "summary": "Done.",
                                "files": ["jobs/01/rain.png", "jobs/01/rain.svg", "jobs/01/report.md"]},
                               CLOSED, ENDED])
    job = folder / "jobs" / "01"
    job.mkdir(parents=True)
    for name in ("rain.png", "rain.svg", "report.md"):
        (job / name).write_text("x")
    before = view(tmp_path)
    (job / "rain-dark.png").write_text("x")
    (job / "report.html").write_text("x")
    after = view(tmp_path)
    files = [[(f["image_url_dark"], f["page_url"]) for f in i["files"]] for body in (before, after)
             for i in body["items"] if i["type"] == "job"]
    base = "/calls/call-matt-1/files/jobs/01"
    assert files == [[(None, None), (None, None)], [(f"{base}/rain-dark.png", None), (None, f"{base}/report.html")]]
    assert before["version"] != after["version"]


# ---------- Bee writes ----------

def bee(key: str, status: str, at: str, **more) -> dict:
    return {"key": key, "status": status, "at": at, **more}


ASKED = {"question": "Shall I add 'Call mom' to your Bee todos?", "answer": "Yes."}


def test_bee_write_states_come_from_the_writer_s_record(tmp_path):
    """written (ok, or an undo that failed), undone, unknown (Bee may have it, or applying when the process stopped)
    and failed; the text is the item as the AI quoted it."""
    todo = {"kind": "todo_create", "text": "Call mom"}
    writes = [
        bee("w1", "applying", "2026-10-15T20:00:10+09:00", item=todo, **ASKED), bee("w1", "ok", "2026-10-15T20:00:11+09:00"),
        bee("w2", "applying", "2026-10-15T20:00:20+09:00", item={"kind": "fact_confirm", "fact_id": 12}, **ASKED),
        bee("w2", "ok", "2026-10-15T20:00:21+09:00"), bee("w2", "undone", "2026-10-15T20:00:25+09:00"),
        bee("w3", "applying", "2026-10-15T20:00:30+09:00", item=todo, **ASKED),
        bee("w3", "unknown", "2026-10-15T20:00:36+09:00", error="timed out"),
        bee("w4", "applying", "2026-10-15T20:00:40+09:00", item=todo, **ASKED),
        bee("w5", "applying", "2026-10-15T20:00:45+09:00", item={"kind": "fact_update", "fact_id": 3, "text": "B"},
            **ASKED),
        bee("w5", "failed", "2026-10-15T20:00:46+09:00", error="Bee said no"),
        bee("w6", "applying", "2026-10-15T20:00:50+09:00", item=todo, **ASKED), bee("w6", "ok", "2026-10-15T20:00:51+09:00"),
        bee("w6", "undo_failed", "2026-10-15T20:00:55+09:00", error="gone"),
        bee("w7", "applying", "2026-10-15T20:00:56+09:00", item={"kind": "memory_note", "text": "?"}, **ASKED),
    ]
    record(tmp_path, [INBOUND, say(2, "Hi."), CLOSED, ENDED], writes=writes)
    body = view(tmp_path)
    found = [(i["id"], i["kind"], i["text"], i["state"], i["will_retry"], i["undoable"])
             for i in body["items"] if i["type"] == "bee_write"]
    assert found == [("write-w1", "todo_create", "Call mom", "written", False, False),
                     ("write-w2", "fact_confirm", None, "undone", False, False),
                     ("write-w3", "todo_create", "Call mom", "unknown", False, False),
                     ("write-w4", "todo_create", "Call mom", "unknown", False, False),
                     ("write-w5", "fact_update", "B", "failed", False, False),
                     ("write-w6", "todo_create", "Call mom", "written", False, False)]
    assert body["call"]["marks"]["bee_writes"] == 2
    assert body["items"][1]["at"] == "2026-10-15T20:00:10+09:00"


def test_in_a_live_call_a_failed_write_will_be_retried_and_the_newest_write_in_bee_carries_the_hint(tmp_path):
    """undoable is one card: the newest change of a live call that saying "빼 줘" would take out now (in Bee: ok, or
    a take-back Bee refused), since the app's fixed hint on several cards would point at none (fourth review of
    2026-10-18); the call AI takes back any change the person names, by its key. A failed write will be retried; a
    write still being made shows once it has a result."""
    todo = {"kind": "todo_create", "text": "Call mom"}
    writes = [bee("w1", "applying", "2026-10-15T20:00:10+09:00", item=todo, **ASKED), bee("w1", "ok", "2026-10-15T20:00:11+09:00"),
              bee("w2", "applying", "2026-10-15T20:00:20+09:00", item=todo, **ASKED), bee("w2", "ok", "2026-10-15T20:00:21+09:00"),
              bee("w2", "undo_failed", "2026-10-15T20:00:25+09:00"),
              bee("w3", "applying", "2026-10-15T20:00:30+09:00", item=todo, **ASKED),
              bee("w3", "failed", "2026-10-15T20:00:31+09:00")]
    record(tmp_path, [INBOUND, say(2, "Hi.")], writes=writes, age_s=5)
    found = [(i["id"], i["state"], i["will_retry"], i["undoable"]) for i in view(tmp_path)["items"]
             if i["type"] == "bee_write"]
    assert found == [("write-w1", "written", False, False), ("write-w2", "written", False, True),
                     ("write-w3", "failed", True, False)]
    making = writes + [bee("w4", "applying", "2026-10-15T20:00:40+09:00", item=todo, **ASKED)]
    record(tmp_path / "making", [INBOUND, say(2, "Hi.")], writes=making, age_s=5)
    assert [(i["id"], i["undoable"]) for i in view(tmp_path / "making")["items"] if i["type"] == "bee_write"] == [
        ("write-w1", False), ("write-w2", True), ("write-w3", False)]
    record(tmp_path / "ended", [INBOUND, say(2, "Hi."), CLOSED, ENDED], writes=writes)
    assert [i["undoable"] for i in view(tmp_path / "ended")["items"] if i["type"] == "bee_write"] == [False] * 3


def test_the_new_kinds_show_and_a_removal_never_carries_the_take_it_out_hint(tmp_path):
    """Contract section 2.4 (2026-10-17): fact_create, fact_delete, todo_complete and todo_dismiss are writes too.
    Since 2026-10-18 a delete or a dismissed suggestion can be taken back too (it is made again, voice/coordinator.py
    undo_bee_write), but `undoable` drives the app's hint "빼려면 '빼 줘'라고 말하세요", which does not fit bringing
    something back, so in a live call they are never undoable."""
    writes = [bee("w1", "applying", "2026-10-15T20:00:10+09:00", item={"kind": "fact_create", "text": "Allergic to peanuts"},
                  **ASKED), bee("w1", "ok", "2026-10-15T20:00:11+09:00"),
              bee("w2", "applying", "2026-10-15T20:00:20+09:00", item={"kind": "todo_complete", "todo_id": 51,
                                                                       "text": "Reply to the recruiter"}, **ASKED),
              bee("w2", "ok", "2026-10-15T20:00:21+09:00"),
              bee("w3", "applying", "2026-10-15T20:00:30+09:00", item={"kind": "fact_delete", "fact_id": 7,
                                                                       "text": "Bonus is 15%"}, **ASKED),
              bee("w3", "ok", "2026-10-15T20:00:31+09:00")]
    record(tmp_path, [INBOUND, say(2, "Hi.")], writes=writes, age_s=5)
    found = [(i["id"], i["kind"], i["state"], i["undoable"]) for i in view(tmp_path)["items"] if i["type"] == "bee_write"]
    assert found == [("write-w1", "fact_create", "written", False), ("write-w2", "todo_complete", "written", True),
                     ("write-w3", "fact_delete", "written", False)]  # a todo marked done can be marked open again


def test_a_failed_write_tried_again_after_the_call_reads_failed_until_the_try_ends(tmp_path):
    """The retry after the call (voice/agent.py after_bee_writes) marks the write applying again: while it runs the
    write still reads failed, not unknown. A retry's applying line left far past the retry's time limit was cut off
    by a stopped process, so Bee may have it."""
    todo = {"kind": "todo_create", "text": "Call mom"}
    retry_at = datetime.fromtimestamp(WALL - 3, SEOUL).isoformat(timespec="seconds")
    writes = [bee("w1", "applying", "2026-10-15T20:00:30+09:00", item=todo, **ASKED),
              bee("w1", "failed", "2026-10-15T20:00:31+09:00", error="Bee said no"), bee("w1", "applying", retry_at)]
    record(tmp_path, [INBOUND, say(2, "Hi."), CLOSED, ENDED], writes=writes, age_s=5)

    def written(wall: float) -> tuple:
        body = call_view.call_view(tmp_path, "call-matt-1", zone=SEOUL, language="en", now=EVENING, wall=wall)
        check_body("call-view", body)
        (item,) = [i for i in body["items"] if i["type"] == "bee_write"]
        return item["state"], item["will_retry"]

    assert written(WALL) == ("failed", False)
    assert written(WALL + call_view.RETRY_SETTLES_S) == ("unknown", False)


def test_without_the_writer_s_record_writes_are_read_from_the_call_s_events(tmp_path):
    """Moved from the page's tests: a write and its undo by key, the retry after the call by key, a write Bee may
    have taken; without keys, an undo takes out the last saved write and only a retry updates the failed one."""
    events = [INBOUND, say(2, "Hi."),
              {"t": 3, "type": "bee_write", "key": "w1", "kind": "todo_create", "text": "Same", "result": "ok"},
              {"t": 5, "type": "bee_write", "key": "w2", "kind": "todo_create", "text": "Same", "result": "ok"},
              {"t": 6, "type": "bee_write_undone", "key": "w1", "kind": "todo_create", "text": "Same"},
              {"t": 7, "type": "bee_write", "key": "w3", "kind": "fact_update", "text": "A", "result": "failed"},
              {"t": 8, "type": "bee_write", "key": "w4", "kind": "todo_create", "text": "Note the bug",
               "result": "failed", "may_have_gone_in": True},
              {"t": 9, "type": "bee_write", "kind": "fact_update", "text": "Dana likes easy trails", "result": "failed"},
              {"t": 10, "type": "bee_write", "kind": "todo_create", "text": "First", "result": "ok"},
              {"t": 11, "type": "bee_write", "kind": "todo_create", "text": "Second", "result": "ok"},
              {"t": 12, "type": "bee_write_undone"},
              CLOSED,
              {"t": 62, "type": "bee_write", "key": "w3", "kind": "fact_update", "text": "A", "result": "ok",
               "retried": True},
              {"t": 63, "type": "bee_write", "kind": "fact_update", "text": "Dana likes easy trails", "result": "ok",
               "retried": True}, ENDED]
    record(tmp_path, events)
    found = [(i["text"], i["state"]) for i in view(tmp_path)["items"] if i["type"] == "bee_write"]
    assert found == [("Same", "undone"), ("Same", "written"), ("A", "written"), ("Note the bug", "unknown"),
                     ("Dana likes easy trails", "written"), ("First", "written"), ("Second", "undone")]


def test_an_older_record_s_queued_writes_read_by_their_result_after_the_call(tmp_path):
    shutil.copytree(FIXTURES / "2026-10-11" / "1930", tmp_path / "2026-10-11" / "1930")
    body = view(tmp_path, "call-20261011-193004", now=datetime(2026, 10, 12, tzinfo=SEOUL))
    writes = [(i["kind"], i["text"], i["state"]) for i in body["items"] if i["type"] == "bee_write"]
    assert writes == [("fact_update", "Dana's knee is still sore, so she needs easy trails for now", "written"),
                      ("todo_create", "Text Dana the trail plan tonight", "written")]
    assert body["call"]["started_at"] == "2026-10-11T19:30:04+09:00"


# ---------- why the AI called, and its prep ----------

def test_only_a_call_the_ai_placed_has_a_why_card(tmp_path):
    """Moved from the page's tests: a call the person started never has one, even with unconfirmed facts in Bee;
    an AI call has one, also without a Bee conversation behind it (a booked call)."""
    facts = {"t": 0.3, "type": "why_calling", "summary": "", "conversation_id": None, "utterances": [],
             "pending_fact_ids": [8812]}
    record(tmp_path, [INBOUND, facts, say(2, "Hi."), CLOSED, ENDED])
    assert view(tmp_path)["why_calling"] is None
    booked = {"t": 0.01, "type": "call", "direction": "outbound", "topic": "Dentist booking",
              "reason": "In a call at 18:05, the user asked to be called back: Dentist booking"}
    record(tmp_path, [booked, say(2, "Hi."), CLOSED, ENDED], folder="2026-10-15/2100", room="call-matt-2")
    body = view(tmp_path, "call-matt-2")
    assert body["why_calling"] == {"reason": booked["reason"], "conversation": None}
    assert (body["call"]["topic"], body["call"]["title"], body["call"]["title_source"]) == (
        "Dentist booking", "Dentist booking", "topic")


def test_an_ai_call_without_a_topic_gets_the_notification_s_line_in_the_person_s_language(tmp_path):
    record(tmp_path, [{"t": 0.01, "type": "call", "direction": "outbound", "topic": None, "reason": None,
                       "kind": "voice note"}, CLOSED, ENDED])
    record(tmp_path, [{"t": 0.01, "type": "call", "direction": "outbound", "topic": "", "reason": None}, CLOSED, ENDED],
           folder="2026-10-15/2100", room="call-matt-2")
    assert view(tmp_path)["call"]["topic"] == "Your new voice note"
    ko = call_view.call_view(tmp_path, "call-matt-2", zone=SEOUL, language="ko", now=EVENING, wall=WALL)
    assert ko["call"]["topic"] == "오늘 하루 이야기"


def test_older_quotes_with_a_bare_clock_time_get_the_day_before_the_call(tmp_path):
    why = {"t": 0.3, "type": "why_calling", "summary": "Planned a hike.", "conversation_id": 5121,
           "utterances": [{"at": "18:41", "speaker": "speaker_1", "text": "Still on for Saturday?"},
                          {"at": "21:10", "speaker": "me", "text": "Yes!"}, {"at": None, "speaker": "me", "text": ""}]}
    record(tmp_path, [why, say(2, "Hi."), CLOSED, ENDED])  # no call event: from before it existed
    body = view(tmp_path)
    assert body["call"]["started_by"] == "ai"
    assert body["why_calling"]["conversation"] == {"summary": "Planned a hike.", "quotes": [
        {"speaker": "other", "text": "Still on for Saturday?", "at": "2026-10-15T18:41:00+09:00"},
        {"speaker": "person", "text": "Yes!", "at": "2026-10-14T21:10:00+09:00"}]}


def test_the_prep_note_loses_bee_s_ids_and_older_reads_become_what_it_looked_up(tmp_path):
    """The page's withoutIds rules, moved here; prep reads stored as "tool {json}" text are read too."""
    note = ("Talked with Dana (conversation 10785664) about the hike. Todo suggestion 11628862 is open; facts "
            "(#8812, #8813) need checking. See #12345 too.")
    prep = {"t": 1, "type": "call_prep", "note": note, "error": None,
            "reads": ['search_bee {"query": "hike"}', 'bee_day {"date": "2026-10-14"}',
                      'bee_conversation {"conversation_id": 5121}', 'search_bee {"query": "hike"}', "nonsense"]}
    record(tmp_path, [INBOUND, prep, say(2, "Hi."), CLOSED, ENDED])
    body = view(tmp_path)
    assert body["prep"] == {
        "note": "Talked with Dana about the hike. Todo suggestion is open; facts need checking. See too.",
        "looked_up": [{"type": "search", "query": "hike"}, {"type": "day", "date": "2026-10-14"},
                      {"type": "conversation", "summary": None, "at": None}],
        "from_bee": True}


def test_a_read_naming_a_conversation_by_a_list_or_object_is_shown_like_one_without_an_id(tmp_path):
    """Records are read as they are: a conversation id that is neither a number nor a string (an older or broken
    read) does not fail the view; the read shows like one without an id, by what it says."""
    older = ['bee_conversation {"conversation_id": [1, 2]}', 'bee_conversation {"conversation_id": {"a": 1}}']
    stored = [{"type": "conversation", "id": [1, 2], "summary": "Hike plans", "at": None},
              {"type": "conversation", "id": {"a": 1}, "summary": "Dinner", "at": None}]
    for reads, shown in ((older, [None]), (stored, ["Hike plans", "Dinner"])):
        shutil.rmtree(tmp_path / "2026-10-15", ignore_errors=True)
        record(tmp_path, [INBOUND, {"t": 1, "type": "call_prep", "note": "Hi.", "reads": reads}, say(2, "Hi."),
                          CLOSED, ENDED])
        assert [e["summary"] for e in view(tmp_path)["prep"]["looked_up"]] == shown


def test_a_person_without_bee_has_a_prep_from_earlier_calls_only(tmp_path):
    prep = {"t": 1, "type": "call_prep", "note": "Last time they talked about the hike.", "reads": [], "bee": False}
    record(tmp_path, [INBOUND, prep, say(2, "Hi."), CLOSED, ENDED])
    assert view(tmp_path)["prep"] == {"note": "Last time they talked about the hike.", "looked_up": [],
                                      "from_bee": False}
    shutil.rmtree(tmp_path / "2026-10-15")
    record(tmp_path, [INBOUND, say(2, "Hi."), CLOSED, ENDED])
    assert view(tmp_path)["prep"] is None


# ---------- titles ----------

def test_the_title_is_the_summary_then_the_topic_then_the_person_s_first_sentence(tmp_path):
    long_first = "I " + "really " * 12 + "need help. And more."
    record(tmp_path, [INBOUND, say(2, "Hi."), hear(4, long_first), CLOSED, ENDED])
    call = view(tmp_path)["call"]
    assert call["title_source"] == "first_sentence" and len(call["title"]) <= 60
    assert call["title"].startswith("I really really") and call["title"].endswith("…")
    with (tmp_path / "2026-10-15" / "2000" / "events.jsonl").open("a") as f:
        f.write(json.dumps({"t": 65, "type": "call_titled", "title": "Asking for help"}) + "\n")
    call = view(tmp_path)["call"]
    assert (call["title"], call["title_source"]) == ("Asking for help", "summary")


def test_a_call_with_no_conversation_has_no_title(tmp_path):
    record(tmp_path, [INBOUND, say(2, "Hi, it's Bee Call."), CLOSED, ENDED])
    call = view(tmp_path)["call"]
    assert (call["title"], call["title_source"]) == (None, "none")


# ---------- the list ----------

def ring(room: str, at: str, topic: str | None = "Saturday hike") -> dict:
    moment = datetime.fromisoformat(at)
    return {"at": at, "room": room, "expires_at": (moment + timedelta(seconds=60)).isoformat(),
            "expires_epoch": int((moment + timedelta(seconds=60)).timestamp()), "topic": topic, "kind": None,
            "reason": "Because.", "plan": None}


def rings(workspace: Path, lines: list[dict], verdicts: list[dict] = ()) -> None:
    (workspace / "state").mkdir(parents=True, exist_ok=True)
    (workspace / "state" / "outbound.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines))
    (workspace / "meta").mkdir(parents=True, exist_ok=True)
    (workspace / "meta" / "events.jsonl").write_text("".join(json.dumps(v) + "\n" for v in verdicts))


def test_an_unanswered_ai_call_is_missed_or_declined_by_the_voice_server_s_verdict(tmp_path):
    """One source: the voice server's verdict in meta/events.jsonl; a decline after it changes nothing. A ring with
    neither a verdict nor a record reads as missed once it expired; one still ringing is not listed."""
    rings(tmp_path, [ring("call-matt-20261015-200000", "2026-10-15T20:00:00+09:00"),
                     ring("call-matt-20261015-200500", "2026-10-15T20:05:00+09:00"),
                     ring("call-matt-20261015-201000", "2026-10-15T20:10:00+09:00", topic=None),
                     ring("call-matt-20261015-205930", "2026-10-15T20:59:30+09:00")],
          [{"at": "2026-10-15T20:01:00+09:00", "kind": "call_declined", "room": "call-matt-20261015-200000"},
           {"at": "2026-10-15T20:01:01+09:00", "kind": "call_missed", "room": "call-matt-20261015-200000"},
           {"at": "2026-10-15T20:06:00+09:00", "kind": "call_missed", "room": "call-matt-20261015-200500"}])
    (tmp_path / "state" / "declined.jsonl").write_text(json.dumps({"room": "call-matt-20261015-200500"}) + "\n")
    calls = listed(tmp_path)["calls"]
    assert [(c["call_id"], c["state"], c["started_at"], c["topic"], c["title_source"]) for c in calls] == [
        ("call-matt-20261015-201000", "missed", "2026-10-15T20:10:00+09:00", "Your day", "topic"),
        ("call-matt-20261015-200500", "missed", "2026-10-15T20:05:00+09:00", "Saturday hike", "topic"),
        ("call-matt-20261015-200000", "declined", "2026-10-15T20:00:00+09:00", "Saturday hike", "topic")]
    assert call_view.call_view(tmp_path, "call-matt-20261015-200000", zone=SEOUL, language="en", now=EVENING) is None


def test_an_answered_ai_call_is_listed_once_by_its_record(tmp_path):
    rings(tmp_path, [ring("call-matt-1", "2026-10-15T19:59:50+09:00")])
    record(tmp_path, [{"t": 0.01, "type": "call", "direction": "outbound", "topic": None, "reason": None},
                      say(2, "Hi."), CLOSED, ENDED])
    (call,) = listed(tmp_path)["calls"]
    assert (call["call_id"], call["started_by"], call["state"], call["topic"]) == (
        "call-matt-1", "ai", "ended", "Saturday hike")


def test_a_second_record_in_one_room_is_a_call_of_its_own(tmp_path):
    record(tmp_path, [INBOUND, say(2, "Hi."), {"t": 30, "type": "session_closed", "reason": "job_shutdown"}])
    record(tmp_path, [INBOUND, say(2, "Hi again.")], folder="2026-10-15/2001", started="2026-10-15T20:01:10+09:00",
           age_s=5)
    calls = listed(tmp_path)["calls"]
    assert [(c["call_id"], c["state"]) for c in calls] == [("call-matt-1.2", "live"), ("call-matt-1", "dropped")]
    assert speech(view(tmp_path, "call-matt-1.2")) == [("ai", "Hi again.")]


def test_the_list_pages_by_its_cursor(tmp_path):
    for n in range(5):
        record(tmp_path, [INBOUND, CLOSED, ENDED], folder=f"2026-10-15/20{n:02d}", room=f"call-matt-{n}",
               started=f"2026-10-15T20:{n:02d}:00+09:00")
    first = listed(tmp_path, limit=2)
    assert [c["call_id"] for c in first["calls"]] == ["call-matt-4", "call-matt-3"] and first["more"]
    second = listed(tmp_path, limit=2, before=first["before"])
    assert [c["call_id"] for c in second["calls"]] == ["call-matt-2", "call-matt-1"] and second["more"]
    last = listed(tmp_path, limit=2, before=second["before"])
    assert ([c["call_id"] for c in last["calls"]], last["more"], last["before"]) == (["call-matt-0"], False, None)
    assert listed(tmp_path)["version"] == listed(tmp_path)["version"] != first["version"]
    for bad in ({"limit": 0}, {"limit": 51}, {"before": "not-a-cursor"}, {"before": "a" * 300}):
        with pytest.raises(BadRequest):
            call_view.call_list(tmp_path, zone=SEOUL, language="en", now=EVENING, **bad)


def test_continues_names_the_earlier_call_by_its_id(tmp_path):
    record(tmp_path, [INBOUND, say(2, "Hi."), CLOSED, ENDED])
    record(tmp_path, [{"t": 0.01, "type": "call", "direction": "inbound", "topic": None, "reason": None,
                       "continue_from": "2026-10-15/2000"}, CLOSED, ENDED],
           folder="2026-10-15/2100", room="call-matt-2", started="2026-10-15T21:00:00+09:00")
    record(tmp_path, [{"t": 0.01, "type": "call", "direction": "inbound", "topic": None, "reason": None,
                       "continue_from": "call-matt-2"}, CLOSED, ENDED],
           folder="2026-10-15/2200", room="call-matt-3", started="2026-10-15T22:00:00+09:00")
    found = {c["call_id"]: c["continues"] for c in listed(tmp_path, now=EVENING + timedelta(hours=2))["calls"]}
    assert found == {"call-matt-3": "call-matt-2", "call-matt-2": "call-matt-1", "call-matt-1": None}


def test_a_record_is_listed_once_its_first_line_is_whole(tmp_path):
    """The recorder makes a call's folder and file before it writes the call_started line: until that line is whole
    the record is not listed, so it never shows under a name it is about to lose. A whole first line without a
    usable room still names the record by its folder."""
    path = tmp_path / "2026-10-15" / "2000"
    path.mkdir(parents=True)
    first = {"t": 0.0, "type": "call_started", "room": "call-matt-1", "started_at": STARTED}
    for begun in ("", json.dumps(first)[:30]):
        (path / "events.jsonl").write_text(begun)
        os.utime(path / "events.jsonl", (WALL - 5, WALL - 5))
        assert listed(tmp_path)["calls"] == [] and call_view.call_view(
            tmp_path, "rec-2026-10-15-2000", zone=SEOUL, language="en", now=EVENING, wall=WALL) is None
    (path / "events.jsonl").write_text(json.dumps(first) + "\n")
    os.utime(path / "events.jsonl", (WALL - 5, WALL - 5))
    assert [c["call_id"] for c in listed(tmp_path)["calls"]] == ["call-matt-1"]
    roomless = tmp_path / "2026-10-14" / "2000"
    roomless.mkdir(parents=True)
    (roomless / "events.jsonl").write_text(json.dumps({"t": 0.0, "type": "call_started"}) + "\n")
    assert [c["call_id"] for c in listed(tmp_path)["calls"]] == ["call-matt-1", "rec-2026-10-14-2000"]
    (roomless / "events.jsonl").write_text(json.dumps({"t": 0.0, "type": "call_started", "room": ["r"]}) + "\n")
    assert [c["call_id"] for c in listed(tmp_path)["calls"]] == ["call-matt-1", "rec-2026-10-14-2000"]
    assert view(tmp_path, "rec-2026-10-14-2000")["call"]["call_id"] == "rec-2026-10-14-2000"


def test_the_changes_stamp_sees_a_new_record_begin_in_a_day_that_has_calls(tmp_path):
    """/changes rebuilds when files_stamp changes: a call that begins in a day folder that already has calls shows
    as its file appears and as its first line lands, not only at the next timed rebuild."""
    record(tmp_path, [INBOUND, CLOSED, ENDED])
    path = tmp_path / "2026-10-15" / "2100"
    path.mkdir()
    made = call_view.files_stamp(tmp_path, [])
    (path / "events.jsonl").write_text("")
    opened = call_view.files_stamp(tmp_path, [])
    (path / "events.jsonl").write_text(json.dumps({"t": 0.0, "type": "call_started", "room": "call-matt-2"}) + "\n")
    begun = call_view.files_stamp(tmp_path, [])
    assert made != opened != begun


def test_a_line_still_being_written_is_left_out(tmp_path):
    path = record(tmp_path, [INBOUND, say(2, "Hi.")], age_s=5)
    with (path / "events.jsonl").open("a") as f:
        f.write('{"t": 3, "type": "user_transcript", "text": "Hel')
    assert speech(view(tmp_path)) == [("ai", "Hi.")]


def test_one_changes_rebuild_reads_the_records_and_ring_logs_once_however_many_calls_it_watches(tmp_path, monkeypatch):
    """The coordinator's review of 2026-10-15 (queued): each watched call's view scanned every record again and
    re-read state/outbound.jsonl and meta/events.jsonl, so a rebuild of 200 calls touched in the last ten minutes
    (the title backfill) took about 4 s on the token server's event loop. One rebuild now takes one look."""
    for n in range(30):
        record(tmp_path, [INBOUND, say(2, f"Call {n}."), CLOSED, ENDED], folder=f"2026-10-15/20{n:02d}",
               room=f"call-matt-{n}", started=f"2026-10-15T20:{n:02d}:00+09:00", age_s=5)
    looked = {"call_ids": 0, "_rings": 0}
    for name in looked:
        real = getattr(call_view, name)

        def counted(*args, _real=real, _name=name, **kwargs):
            looked[_name] += 1
            return _real(*args, **kwargs)

        monkeypatch.setattr(call_view, name, counted)
    versions, folders = call_view.changes(tmp_path, zone=SEOUL, language="en", now=EVENING, wall=WALL)
    assert len(folders) == 30 and len(versions) == 31 and looked == {"call_ids": 1, "_rings": 1}
    one = call_view.call_view(tmp_path, "call-matt-7", zone=SEOUL, language="en", now=EVENING, wall=WALL)
    assert versions["/calls/call-matt-7"] == one["version"]  # the same answer GET gives
    assert versions["/calls"] == call_view.call_list(tmp_path, zone=SEOUL, language="en", now=EVENING,
                                                      wall=WALL)["version"]


def test_a_removal_brought_back_shows_what_it_did_and_what_came_back(tmp_path):
    """Review of 2026-10-18, finding 2: the fact made again after a delete showed only as the delete "undone" (the
    testers' app labels that "뺌"). Now the delete's card keeps what it did, and what came back is a card of its own
    (fact_create, written: "넣음"). "빼 줘" takes what came back out again, so it carries the hint; the delete never
    does."""
    writes = [bee("w1", "applying", "2026-10-15T20:00:10+09:00", item={"kind": "todo_create", "text": "Call mom"},
                  **ASKED), bee("w1", "ok", "2026-10-15T20:00:11+09:00"),
              bee("w2", "applying", "2026-10-15T20:00:20+09:00", item={"kind": "fact_delete", "fact_id": 7,
                                                                       "text": "Bonus is 15%"}, **ASKED),
              bee("w2", "ok", "2026-10-15T20:00:21+09:00"),
              bee("w3", "applying", "2026-10-15T20:00:30+09:00", item={"kind": "fact_create", "text": "Bonus is 15%",
                                                                       "restores": "w2"}, **ASKED),
              bee("w3", "ok", "2026-10-15T20:00:31+09:00")]
    record(tmp_path, [INBOUND, say(2, "Hi.")], writes=writes, age_s=5)
    found = [(i["id"], i["kind"], i["state"], i["undoable"]) for i in view(tmp_path)["items"] if i["type"] == "bee_write"]
    assert found == [("write-w1", "todo_create", "written", False), ("write-w2", "fact_delete", "written", False),
                     ("write-w3", "fact_create", "written", True)]


def test_a_take_back_bee_did_not_answer_shows_as_unknown(tmp_path):
    writes = [bee("w1", "applying", "2026-10-15T20:00:10+09:00", item={"kind": "todo_create", "text": "Call mom"},
                  **ASKED), bee("w1", "ok", "2026-10-15T20:00:11+09:00"),
              bee("w2", "applying", "2026-10-15T20:00:20+09:00", item={"kind": "todo_create", "text": "Note the bug"},
                  **ASKED), bee("w2", "ok", "2026-10-15T20:00:21+09:00"),
              bee("w2", "undo_unknown", "2026-10-15T20:00:40+09:00", error="timed out")]
    record(tmp_path, [INBOUND, say(2, "Hi.")], writes=writes, age_s=5)
    found = [(i["id"], i["state"], i["undoable"]) for i in view(tmp_path)["items"] if i["type"] == "bee_write"]
    assert found == [("write-w1", "written", True), ("write-w2", "unknown", False)]


def test_a_withdrawn_write_and_one_that_cannot_be_taken_back_show_as_what_they_are(tmp_path):
    """Third review of 2026-10-18 (its view probe): a failed save the person took back reads failed and is not
    retried; one whose take-back cannot be done here is in Bee but carries no hint; an older to-do in Bee still
    does."""
    t = "2026-10-15T20:00:{:02d}+09:00".format
    todo = lambda text: {"kind": "todo_create", "text": text}  # noqa: E731
    writes = [
        bee("w1", "applying", t(10), item=todo("Call mom"), **ASKED), bee("w1", "ok", t(11)),
        bee("w2", "applying", t(12), item=todo("Note the bug"), **ASKED), bee("w2", "failed", t(13), error="503"),
        bee("w2", "withdrawn", t(14)),
        bee("w3", "applying", t(15), item={"kind": "todo_accept", "suggestion_id": 34, "text": "Ask about bonus"},
            **ASKED), bee("w3", "ok", t(16), response={"id": 34}), bee("w3", "undo_impossible", t(17), error="x"),
        bee("w4", "applying", t(18), item={"kind": "fact_delete", "fact_id": 7, "text": "Bonus is 15%"},
            before={"id": 7, "text": "Bonus is 15%"}, **ASKED), bee("w4", "ok", t(19)),
        bee("w5", "applying", t(20), item={"kind": "fact_create", "text": "Bonus is 15%", "restores": "w4"}, **ASKED),
        bee("w5", "failed", t(21), error="503"), bee("w5", "withdrawn", t(22)),
    ]
    record(tmp_path / "live", [INBOUND, say(2, "Hi.")], writes=writes, age_s=5)
    found = [(i["id"], i["state"], i["will_retry"], i["undoable"]) for i in view(tmp_path / "live")["items"]
             if i["type"] == "bee_write"]
    assert found == [("write-w1", "written", False, True), ("write-w2", "failed", False, False),
                     ("write-w3", "written", False, False), ("write-w4", "written", False, False),
                     ("write-w5", "failed", False, False)]


def test_one_card_carries_the_take_it_out_hint_the_newest_change_in_bee(tmp_path):
    """Fourth review of 2026-10-18: every to-do of a live call carried the app's fixed hint, so a bare "빼 줘" could
    point at none. Only the newest change that saying so takes out carries it: here what a bring-back made."""
    t = "2026-10-15T20:00:{:02d}+09:00".format
    todo = lambda text: {"kind": "todo_create", "text": text}  # noqa: E731
    writes = [bee("w1", "applying", t(10), item=todo("A"), **ASKED), bee("w1", "ok", t(11)),
              bee("w2", "applying", t(12), item=todo("B"), **ASKED), bee("w2", "ok", t(13)),
              bee("w3", "applying", t(14), item={"kind": "fact_delete", "fact_id": 7, "text": "C"}, **ASKED),
              bee("w3", "ok", t(15)),
              bee("w4", "applying", t(16), item={"kind": "fact_create", "text": "C", "restores": "w3"}, **ASKED),
              bee("w4", "ok", t(17))]
    record(tmp_path, [INBOUND, say(2, "Hi.")], writes=writes, age_s=5)
    assert [(i["id"], i["undoable"]) for i in view(tmp_path)["items"] if i["type"] == "bee_write"] == [
        ("write-w1", False), ("write-w2", False), ("write-w3", False), ("write-w4", True)]
