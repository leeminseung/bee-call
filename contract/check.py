"""Checks the app contract: every schema is valid draft 2020-12, every example is valid against its schema (the schema
whose name is the longest prefix of the example's name), and bodies the contract forbids are rejected.

Run from anywhere: uv run --with jsonschema python check.py
"""

import copy
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

root = Path(__file__).resolve().parent
schemas = {p.stem: json.loads(p.read_text()) for p in sorted((root / "schemas").glob("*.json"))}
registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in schemas.values())
problems = 0

for schema in schemas.values():
    Draft202012Validator.check_schema(schema)

examples = sorted((root / "examples").glob("*.json"))
for path in examples:
    candidates = [s for s in schemas if s != "defs" and (path.stem == s or path.stem.startswith(s + "-"))]
    if not candidates:
        print(f"NO SCHEMA  {path.name}")
        problems += 1
        continue
    name = max(candidates, key=len)
    errors = list(Draft202012Validator(schemas[name], registry=registry).iter_errors(json.loads(path.read_text())))
    if errors:
        problems += 1
        print(f"INVALID    {path.name} against {name}.json")
        for error in errors[:10]:
            print(f"   at {'/'.join(map(str, error.absolute_path)) or '(root)'}: {error.message[:300]}")
    else:
        print(f"valid      {path.name} against {name}.json")
for name in schemas:
    if name != "defs" and not any(p.stem == name or p.stem.startswith(name + "-") for p in examples):
        print(f"NO EXAMPLE {name}.json")
        problems += 1


def example(name):
    return json.loads((root / "examples" / f"{name}.json").read_text())


def valid(schema, body):
    return Draft202012Validator(schemas[schema], registry=registry).is_valid(body)


cases = []


def case(label, schema, base, mutate):
    body = copy.deepcopy(example(base))
    mutate(body)
    cases.append((label, valid(schema, body)))


case("call list: duration_s renamed to duration", "calls", "calls",
     lambda b: b["calls"][0].__setitem__("duration", b["calls"][0].pop("duration_s")))
case("call list: an extra field on an entry", "calls", "calls", lambda b: b["calls"][0].__setitem__("preview", "x"))
case("call list: more true, before null", "calls", "calls", lambda b: b.__setitem__("before", None))
case("call list: missed call with a duration", "calls", "calls", lambda b: b["calls"][5].__setitem__("duration_s", 10))
case("call list: ended call without a duration", "calls", "calls", lambda b: b["calls"][0].__setitem__("duration_s", None))
case("call list: title_source none with a title", "calls", "calls", lambda b: b["calls"][2].__setitem__("title", "x"))
case("call list: title null with source summary", "calls", "calls", lambda b: b["calls"][0].__setitem__("title", None))
case("call list: a person's call with a topic", "calls", "calls", lambda b: b["calls"][0].__setitem__("topic", "x"))
case("call list: time without an offset", "calls", "calls",
     lambda b: b["calls"][0].__setitem__("started_at", "2026-10-15T14:22:13"))
case("call list: time in UTC with Z", "calls", "calls",
     lambda b: b["calls"][0].__setitem__("started_at", "2026-10-15T05:22:13Z"))
case("call list: a speech item among results", "calls", "calls",
     lambda b: b["calls"][0]["results"].append(example("call-view")["items"][0]))
case("call view: speaker named me", "call-view", "call-view", lambda b: b["items"][1].__setitem__("speaker", "me"))
case("call view: an unknown item type", "call-view", "call-view",
     lambda b: b["items"].append({"type": "error", "id": "e-1", "at": "2026-10-15T01:23:08+09:00"}))
case("call view: the summary carries results", "call-view", "call-view", lambda b: b["call"].__setitem__("results", []))
case("call view: a person's call with why_calling", "call-view", "call-view",
     lambda b: b.__setitem__("why_calling", example("call-view-ai")["why_calling"]))
case("call view: a running job with a duration", "call-view", "call-view-live",
     lambda b: b["items"][-1].__setitem__("duration_s", 70))
case("call view: a done job with a failure", "call-view", "call-view",
     lambda b: [i for i in b["items"] if i["type"] == "job"][0].__setitem__("failure", "error"))
case("call view: a chart without image_url", "call-view", "call-view",
     lambda b: [i for i in b["items"] if i["type"] == "job"][0]["files"][0].__setitem__("image_url", None))
case("call view: a report with a title", "call-view", "call-view",
     lambda b: [i for i in b["items"] if i["type"] == "job"][0]["files"][1].__setitem__("title", "x"))
case("call view: undoable write that is undone", "call-view", "call-view-live",
     lambda b: [i for i in b["items"] if i["type"] == "bee_write"][0].__setitem__("state", "undone"))
case("call view: will_retry on a written write", "call-view", "call-view",
     lambda b: [i for i in b["items"] if i["type"] == "bee_write"][0].__setitem__("will_retry", True))
case("call view: a person's speech marked interrupted", "call-view", "call-view",
     lambda b: b["items"][1].__setitem__("interrupted", True))
case("call view: empty speech text", "call-view", "call-view", lambda b: b["items"][0].__setitem__("text", ""))
case("call view: captions_included missing ai", "call-view", "call-view", lambda b: b["captions_included"].pop("ai"))
case("call view: a view of a missed call", "call-view", "call-view-ai", lambda b: b["call"].update(
    {"state": "missed", "duration_s": None, "title_source": "topic", "title": "x"}))
case("call view: why_calling repeats the topic", "call-view", "call-view-ai",
     lambda b: b["why_calling"].__setitem__("topic", "x"))
case("call view: quote speaker me", "call-view", "call-view-ai",
     lambda b: b["why_calling"]["conversation"]["quotes"][0].__setitem__("speaker", "me"))
case("offers: call_at_epoch instead of call_at", "offers", "offers",
     lambda b: b["offers"][0].__setitem__("call_at_epoch", b["offers"][0].pop("call_at")))
case("offers: two questions asked at once", "offers", "offers",
     lambda b: b["offers"].append(dict(b["offers"][1], id="20261015-150000-0000")))
case("offers: a booked call without call_at", "offers", "offers", lambda b: b["offers"][0].__setitem__("call_at", None))
case("offers: a declined offer in the open list", "offers", "offers",
     lambda b: b["offers"][0].__setitem__("state", "declined"))
case("offers: an asked offer with call_at", "offers", "offers",
     lambda b: b["offers"][1].__setitem__("call_at", "2026-10-15T20:30:00+09:00"))
case("voices: two defaults", "voices", "voices", lambda b: b["voices"][1].__setitem__("default", True))
case("voices: four voices", "voices", "voices", lambda b: b["voices"].pop())
case("voices: character in one language only", "voices", "voices", lambda b: b["voices"][0]["character"].pop("en"))
case("me: unknown language", "me", "me", lambda b: b["settings"].__setitem__("language", "ja"))
case("me: a provider voice id instead of a catalog id", "me", "me",
     lambda b: b["settings"].__setitem__("voice", "5n5gqmaQi9Ewevrz7bOS"))
case("change: a path outside the contract", "change", "change", lambda b: b.__setitem__("path", "/me"))
case("ring: expires_at missing", "ring", "ring", lambda b: b.pop("expires_at"))
case("call list: an AI call without a topic", "calls", "calls", lambda b: b["calls"][3].__setitem__("topic", None))
case("offers: a null topic", "offers", "offers", lambda b: b["offers"][0].__setitem__("topic", None))
case("call view: Bee's conversation summary called title", "call-view", "call-view",
     lambda b: b["prep"]["looked_up"].__setitem__(0, {"type": "conversation", "title": "x", "at": None}))
case("call view: a caption count that is not whole", "call-view", "call-view",
     lambda b: b["captions_included"].__setitem__("person", 8.5))

for label, accepted in cases:
    if accepted:
        problems += 1
        print(f"ACCEPTED   a forbidden body: {label}")
print(f"{len(examples)} examples, {len(schemas)} schemas, {len(cases)} forbidden bodies; {problems} problems")
sys.exit(1 if problems else 0)
