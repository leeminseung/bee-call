"""How the meta agent judges, with the real model: three made-up moments in a scratch workspace.

  1. Bee saved a 14-utterance conversation weighing two job offers (a decision with a deadline).
  2. Bee saved a two-line hello.
  3. A call ended where the user asked to sort out the offers later and mentioned a fact Bee got wrong.

Bee reads return canned text and notifications are not sent, so nothing touches the user's Bee or phone. The
model is the meta role's as COORDINATOR_LLM runs it (voice/model_catalog.py: with anthropic, Claude Sonnet 5.5 and GLM
5 standing by; with bedrock, COORDINATOR_MODEL or GLM 5 on Bedrock).

Run:  uv run python -m probes.meta_probe [--out DIR]
Cost: about 1-3 US cents on GLM 5, about 10-20 on Claude Sonnet 5.5 (three thinks of a few requests each).
"""

import argparse
import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from beelink import cli, reads
from meta.agent import Thinker
from meta.session import MetaSession, add_event

NOW = datetime.now(ZoneInfo("Asia/Seoul")).replace(hour=18, minute=40, second=0, microsecond=0)


def scratch_workspace(root: Path) -> Path:
    ws = root / "workspace"
    earlier = ws / NOW.strftime("%Y-%m-%d") / "1210"
    earlier.mkdir(parents=True)
    (earlier / "transcript.md").write_text(
        "# Call\n\n- 00:03.0 **AI:** Hi Minseung! What's on your mind?\n"
        "- 00:08.2 **User:** I got a second offer today. I need to decide by Friday.\n"
        "- 00:15.9 **AI:** Congratulations! Want me to compare them now?\n"
        "- 00:20.4 **User:** Not now, I'm heading into a meeting. Let's do it tonight.\n")
    talk = ws / "inbox" / NOW.strftime("%Y-%m-%d-1825")
    talk.mkdir(parents=True)
    (talk / "changed.json").write_text(json.dumps({"conversations": [{
        "id": 901, "state": "COMPLETED", "utterances_count": 14, "short_summary": "Weighing two job offers",
        "summary": "Minseung compared Offer A (salary 92k, 5% bonus) and Offer B (salary 88k, 15% bonus, remote). "
                   "He has to answer both by Friday."}], "journals": []}))
    hello = ws / "inbox" / NOW.strftime("%Y-%m-%d-1832")
    hello.mkdir(parents=True)
    (hello / "changed.json").write_text(json.dumps({"conversations": [{
        "id": 902, "state": "COMPLETED", "utterances_count": 2, "short_summary": "Quick hello"}], "journals": []}))
    call = ws / NOW.strftime("%Y-%m-%d") / "1835"
    call.mkdir(parents=True)
    (call / "transcript.md").write_text(
        "# Call\n\n- 00:03.0 **AI:** Hi Minseung, about the two offers. Got a minute?\n"
        "- 00:07.5 **User:** Only a minute. By the way, Offer B's bonus is ten percent, not fifteen.\n"
        "- 00:12.0 **AI:** Got it, ten percent. Should we go through them now?\n"
        "- 00:15.2 **User:** Later. I want a proper comparison of the take-home pay over three years.\n"
        "- 00:20.1 **AI:** Okay. Talk later!\n")
    return ws


async def canned_today() -> str:
    return ("Bee's context for today: a 14-utterance conversation weighing two job offers (Offer A and Offer B, "
            "answer due Friday); a quick hello; one unconfirmed fact: \"Offer B bonus is 15%\".")


async def canned_conversation(conversation_id: int) -> str:
    return ("[conversation 901] Weighing two job offers. Offer A: salary 92k, 5% bonus. Offer B: salary 88k, "
            "15% bonus, remote. Minseung: 'I have to answer both by Friday.'")


async def canned_search(query: str, limit: int = 3) -> str:
    return '[{"id": 901, "short_summary": "Weighing two job offers"}]'


async def canned_facts(limit: int = 20, timeout: float = 0) -> list[dict]:
    return [{"id": 5501, "text": "Offer B bonus is 15%"}]


async def canned_suggestions(limit: int = 20, timeout: float = 0) -> list[dict]:
    return [{"id": 6601, "text": "Reply to both offers by Friday"}]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, help="make the scratch workspace in a new folder under this one")
    args = parser.parse_args()
    load_dotenv(".env.local")
    reads.today, reads.conversation, reads.search = canned_today, canned_conversation, canned_search
    cli.unconfirmed_facts, cli.todo_suggestions = canned_facts, canned_suggestions
    root = Path(tempfile.mkdtemp(prefix="meta-probe-", dir=args.out))
    ws = scratch_workspace(root)
    sent = []

    async def send(offer, workspace, now):
        sent.append(offer.topic)
        return "notification sent (probe: not really)"

    thinker = Thinker(ws, send=send, bee=True)  # the canned Bee reads above stand in for a Bee user's
    moments = [
        ("bee_change", {"inbox": f"inbox/{NOW:%Y-%m-%d-1825}", "conversation_ids": [901]}, NOW),
        ("bee_change", {"inbox": f"inbox/{NOW:%Y-%m-%d-1832}", "conversation_ids": [902]}, NOW + timedelta(minutes=8)),
        ("call_ended", {"call": f"{NOW:%Y-%m-%d}/1835", "direction": "outbound"}, NOW + timedelta(minutes=12)),
    ]
    for kind, data, at in moments:
        add_event(ws, kind, now=at, **data)
        events = MetaSession(ws).take_events()
        entry = await thinker.think(events, at)
        print(f"\n=== {kind} at {at:%H:%M} ({entry['seconds']} s, {entry['model']})")
        for tool in entry["tools"]:
            print(f"  tool {tool['name']} {json.dumps(tool['input'], ensure_ascii=False)[:160]}")
        if entry.get("error"):
            print(f"  ERROR {entry['error']}")
        print(f"  decision: {entry['decision']}")
    print(f"\nnotes (state.md):\n{MetaSession(ws).state()}")
    print(f"offers asked: {sent}\nworkspace: {ws}")


if __name__ == "__main__":
    asyncio.run(main())
