// The call record page (web/app.js) against recorded events.jsonl lines. Run: node --test web/test/
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { loadPage, fixture } = require("./harness");

const ECHO_CALL = "tests/fixtures/sessions/2026-10-10/2105/events.jsonl";
const HIKE_CALL = "tests/fixtures/sessions/2026-10-11/1930/events.jsonl"; // recorded before the "call" event existed
const AI_CALL = "web/test/fixtures/ai-call.jsonl"; // the AI called after a yes, with Bee writes during the call
const USER_CALL = "web/test/fixtures/user-call.jsonl"; // the person called; Bee had unconfirmed facts

test("a call's turns show in order, and the call ends", () => {
  const page = loadPage({ hash: "#/s/2026-10-10/2105" });
  assert.equal(page.stream.url, "api/sessions/2026-10-10/2105/events");
  page.feed(fixture(ECHO_CALL));
  const turns = page.all("turn").filter((turn) => !turn.classList.contains("pending"));
  assert.deepEqual(turns.map((turn) => [turn.classList.contains("user") ? "user" : "ai", page.text(turn.children[0])]), [
    ["ai", "AI said: Hi! This is the echo test. Say something and I'll repeat it."],
    ["user", "You said: Testing one two three. Can you hear me?"],
    ["ai", "AI said: You said: Testing one two three. Can you hear me?"],
  ]);
  assert.equal(page.byId("status-text").textContent, "Call ended");
});

test("an ended call offers Continue this call in the iPhone app, which posts the call's id", () => {
  const page = loadPage({ hash: "#/s/2026-10-10/2105", bridge: true });
  page.feed(fixture(ECHO_CALL));
  const button = page.one("continue-call");
  button.click();
  assert.deepEqual(page.posted, [{ continue: "2026-10-10/2105" }]);
});

test("a background job's card shows its steps, its result and its files", () => {
  const page = loadPage({ hash: "#/s/2026-10-11/1930" });
  page.feed(fixture(HIKE_CALL));
  const job = page.one("job");
  assert.match(page.text(job), /Compare Bukhansan and Gwanaksan/);
  assert.match(page.text(job), /Gwanaksan's Seoul National University route is the easier pick/);
  assert.match(page.text(job), /report\.md/);
});

// ---------- debug events (contract section 8) ----------

test("without ?debug=1 there is no debug toggle and no debug event, even if one was switched on before", () => {
  const page = loadPage({ hash: "#/s/2026-10-10/2105", prefs: { debug: "1" } });
  page.feed(fixture(ECHO_CALL));
  assert.equal(page.all("debug").length, 0);
  assert.ok(page.all("link-button").every((button) => button.hidden), "the toggle is hidden");
  assert.doesNotMatch(page.visibleText(), /debug events/);
});

test("with ?debug=1 the toggle shows, and turns the debug events on", () => {
  const page = loadPage({ hash: "#/s/2026-10-10/2105", search: "?debug=1" });
  page.feed(fixture(ECHO_CALL));
  const toggle = page.one("link-button");
  assert.equal(toggle.hidden, false);
  assert.equal(toggle.textContent, "Show debug events");
  assert.equal(page.all("debug").length, 0);
  toggle.click();
  assert.ok(page.all("debug").length > 0);
  assert.equal(toggle.textContent, "Hide debug events");
  assert.equal(page.store.get("beecall.debug"), "1");
});

// ---------- why the AI called (contract section 8) ----------

test("an AI call shows why it called: the call's topic and reason, and the Bee conversation behind it", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(fixture(AI_CALL));
  const card = page.one("why");
  assert.match(page.text(card), /^Why I'm calling/);
  assert.match(page.text(card), /Saturday hike with Dana/);
  assert.match(page.text(card), /You promised Dana a trail pick by tonight, and her knee is still sore\./);
  assert.match(page.text(card), /From your Bee conversation/);
  assert.match(page.text(card), /So are we still on for Saturday\?/);
});

test("an AI call with no Bee conversation, such as a booked call, still shows why it called", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([
    { t: 0, type: "call_started", room: "call-matt-20261015-210002", started_at: "2026-10-15T21:00:02" },
    { t: 0.01, type: "call", direction: "outbound", topic: "Dentist booking",
      reason: "In a call at 18:05, the user asked to be called back: Dentist booking" },
  ]);
  const card = page.one("why");
  assert.match(page.text(card), /Dentist booking/);
  assert.match(page.text(card), /asked to be called back/);
});

test("an AI call that came with neither topic nor reason still says it was asked for", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([{ t: 0, type: "call", direction: "outbound", topic: null, reason: null }]);
  assert.match(page.text(page.one("why")), /You said yes to this call\./);
});

test("a call the person started never shows why the AI called, even with unconfirmed facts in Bee", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/1830" });
  page.feed(fixture(USER_CALL));
  assert.equal(page.all("why").length, 0);
  assert.doesNotMatch(page.visibleText(), /Why I'm calling/);
});

test("a record from before the call event keeps its Bee conversation card", () => {
  const page = loadPage({ hash: "#/s/2026-10-11/1930" });
  page.feed(fixture(HIKE_CALL));
  const card = page.one("why");
  assert.match(page.text(card), /Planned a Saturday hike with Dana and promised to pick a trail by tonight\./);
  assert.match(page.text(card), /From your Bee conversation/);
});

// ---------- wording (contract section 8) ----------

test("when the records cannot be reached, no page says it is the Mac", async () => {
  for (const hash of ["#/", "#/calls", "#/meta"]) {
    const page = loadPage({ hash }); // every request fails, as with no network
    await page.settle();
    const note = page.visibleText(page.byId("view"));
    assert.match(note, /Can't reach the call records right now\. Retrying…/, hash);
    assert.doesNotMatch(note, /\bMac\b/, hash);
  }
});
