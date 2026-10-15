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

// ---------- Bee writes during the call (contract sections 6 and 8) ----------

const beeItems = (page) => page.all("bee-item").map((item) => ({
  state: item.dataset.state,
  kind: page.text(item.children.find((c) => c.classList.contains("bee-kind"))),
  text: page.text(item.children.find((c) => c.classList.contains("bee-text"))),
  status: page.text(item.children.find((c) => c.classList.contains("bee-status"))),
  rest: page.text(item),
}));

test("each Bee write shows what was written, the question and the answer, and its result", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(fixture(AI_CALL));
  const card = page.one("bee");
  assert.match(page.text(card), /^Saved in Bee/);
  const [todo, fact] = beeItems(page);
  assert.equal(beeItems(page).length, 2);
  assert.deepEqual([todo.kind, todo.text], ["New to-do", "Text Dana the trail plan"]);
  assert.match(todo.rest, /Asked: “Shall I put "Text Dana the trail plan" in Bee as a to-do\?” · You said: “Yes please\.”/);
  assert.deepEqual([fact.kind, fact.text, fact.state], ["Update fact", "Dana prefers easy routes while her knee heals", "failed"]);
  assert.equal(fact.status, "Not saved yet");
  assert.match(fact.rest, /Bee Call tries again after the call\./);
});

test("a write the person took back in the call shows as taken out", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(fixture(AI_CALL));
  const [todo] = beeItems(page);
  assert.equal(todo.state, "undone");
  assert.equal(todo.status, "Taken out");
  assert.equal(page.text(page.one("bee").children[0]), "Saved in Bee 0 of 1 saved");
});

test("a saved write counts as saved", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([{ t: 3, type: "bee_write", kind: "todo_create", text: "Note the bug", question: "Bee에 할 일로 넣을까요?",
    answer: "응", result: "ok" }]);
  const [todo] = beeItems(page);
  assert.equal(todo.state, "saved");
  assert.equal(todo.status, "Saved");
  assert.match(todo.rest, /Asked: “Bee에 할 일로 넣을까요\?” · You said: “응”/);
  assert.equal(page.text(page.one("bee").children[0]), "Saved in Bee 1 saved");
});

test("taking out with no item named takes out the last saved write", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([
    { t: 3, type: "bee_write", kind: "todo_create", text: "First", question: "q1", answer: "yes", result: "ok" },
    { t: 5, type: "bee_write", kind: "todo_create", text: "Second", question: "q2", answer: "yes", result: "ok" },
    { t: 6, type: "bee_write_undone" },
  ]);
  assert.deepEqual(beeItems(page).map((item) => [item.text, item.state]), [["First", "saved"], ["Second", "undone"]]);
});

test("without keys, only a retry after the call updates the failed write with the same kind and text", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([
    { t: 3, type: "bee_write", kind: "fact_update", text: "Dana likes easy trails", question: "q", answer: "yes",
      result: "failed" },
    { t: 30, type: "bee_write", kind: "fact_update", text: "Dana likes easy trails", result: "ok", retried: true },
    { t: 40, type: "bee_write", kind: "fact_update", text: "Dana likes easy trails", question: "q2", answer: "yes",
      result: "ok" },
  ]);
  assert.deepEqual(beeItems(page).map((item) => [item.text, item.state]),
    [["Dana likes easy trails", "saved"], ["Dana likes easy trails", "saved"]]);
});

test("two writes without text, one failed and one saved, stay two rows with their own questions", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([
    { t: 3, type: "bee_write", key: "w1", kind: "fact_confirm", text: "", item: { kind: "fact_confirm", fact_id: 1 },
      question: "Shall I mark fact 1 as right in Bee?", answer: "yes", result: "failed" },
    { t: 6, type: "bee_write", key: "w2", kind: "fact_confirm", text: "", item: { kind: "fact_confirm", fact_id: 2 },
      question: "Shall I mark fact 2 as right in Bee?", answer: "yes", result: "ok" },
  ]);
  const [first, second] = beeItems(page);
  assert.equal(beeItems(page).length, 2);
  assert.deepEqual([first.text, first.state], ["Fact #1", "failed"]);
  assert.match(first.rest, /fact 1/);
  assert.deepEqual([second.text, second.state], ["Fact #2", "saved"]);
  assert.match(second.rest, /fact 2/);
});

test("a write of a kind the page does not know is still shown", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([{ t: 3, type: "bee_write", kind: "memory_note", text: "Likes window seats", question: "q", answer: "sure",
    result: "ok" }]);
  assert.equal(beeItems(page)[0].kind, "Memory note");
});

test("an older record's writes after the call keep their To save in Bee card", () => {
  const page = loadPage({ hash: "#/s/2026-10-11/1930" });
  page.feed(fixture(HIKE_CALL));
  const card = page.one("bee");
  assert.match(page.text(card), /^To save in Bee 2 of 2 saved/);
  assert.deepEqual(beeItems(page).map((item) => item.status), ["Saved", "Saved"]);
});

// ---------- the person's speech, live (contract section 8) ----------

// The person's bubbles as shown, in order: confirmed turns, then the unconfirmed one when it shows on its own.
const userBubbles = (page) => page.all("turn")
  .filter((turn) => turn.classList.contains("user") && !turn.hidden)
  .map((turn) => page.text(turn.children[0]).replace(/^You (said|are saying): /, ""));

test("speech not yet confirmed shows as its own bubble while the AI spoke last", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(fixture(AI_CALL).slice(0, 10)); // up to the AI's opening line
  page.live([{ t: 6.0, type: "user_transcript", text: "Yes, now is fine." }]);
  assert.deepEqual(userBubbles(page), ["Yes, now is fine."]);
  assert.equal(page.one("pending").hidden, false);
});

test("the person's consecutive turns before the AI's next turn read as one bubble", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(fixture(AI_CALL));
  assert.deepEqual(userBubbles(page), [
    "Yes, now is fine. Dana says her knee is better, but she still wants an easy route.",
    "Yes please.",
    "Actually, take that out.",
    "Sure.",
  ]);
});

test("live, the person's next words show in the same bubble until the AI speaks", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(fixture(AI_CALL).slice(0, 12)); // the AI's opening, then "Yes, now is fine." confirmed
  page.live([{ t: 7.1, type: "user_transcript", text: "Dana says her knee is better," }]);
  assert.deepEqual(userBubbles(page), ["Yes, now is fine. Dana says her knee is better,"]);
  assert.equal(page.one("pending").hidden, true, "no second bubble for the unconfirmed words");
  page.live([{ t: 7.4, type: "message", role: "user", text: "Dana says her knee is better," }]);
  assert.deepEqual(userBubbles(page), ["Yes, now is fine. Dana says her knee is better,"]);
  assert.equal(page.all("pending-text").length, 0);
});

test("words the AI's turn cuts into start the next bubble after it", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(fixture(AI_CALL).slice(0, 12));
  page.live([
    { t: 7.1, type: "user_transcript", text: "Wait," },
    { t: 7.2, type: "message", role: "assistant", text: "Shall I", interrupted: true },
  ]);
  assert.deepEqual(userBubbles(page), ["Yes, now is fine.", "Wait,"]);
  page.live([{ t: 7.4, type: "message", role: "user", text: "Wait, one more thing." }]);
  assert.deepEqual(userBubbles(page), ["Yes, now is fine.", "Wait, one more thing."]);
  assert.equal(page.one("pending").hidden, true);
});

test("words left unconfirmed when the call ends join the person's last bubble", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([
    ...fixture(AI_CALL).slice(0, 12),
    { t: 7.1, type: "user_transcript", text: "bye then." },
    { t: 8, type: "call_ended" },
  ]);
  assert.deepEqual(userBubbles(page), ["Yes, now is fine. bye then."]);
});

// The server records each write under a key ("w1"); a retry after the call and an undo carry the same key.

test("a take-out names its write by key", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([
    { t: 3, type: "bee_write", key: "w1", kind: "todo_create", text: "Same", question: "q1", answer: "yes", result: "ok" },
    { t: 5, type: "bee_write", key: "w2", kind: "todo_create", text: "Same", question: "q2", answer: "yes", result: "ok" },
    { t: 6, type: "bee_write_undone", key: "w1", kind: "todo_create", text: "Same" },
  ]);
  assert.deepEqual(beeItems(page).map((item) => item.state), ["undone", "saved"]);
});

test("the retry after the call updates its write by key, and a second failure promises nothing more", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([
    { t: 3, type: "bee_write", key: "w1", kind: "fact_update", text: "A", question: "q", answer: "yes", result: "failed" },
    { t: 4, type: "bee_write", key: "w2", kind: "todo_create", text: "B", question: "q", answer: "yes", result: "failed" },
    { t: 30, type: "bee_write", key: "w1", kind: "fact_update", text: "A", result: "ok", retried: true },
    { t: 31, type: "bee_write", key: "w2", kind: "todo_create", text: "B", result: "failed", retried: true },
  ]);
  const [fact, todo] = beeItems(page);
  assert.equal(beeItems(page).length, 2);
  assert.deepEqual([fact.state, fact.status], ["saved", "Saved"]);
  assert.deepEqual([todo.state, todo.status], ["failed", "Not saved"]);
  assert.match(todo.rest, /Bee Call tried again after the call, and it did not go through\./);
  assert.doesNotMatch(todo.rest, /tries again/);
});

test("a write with no text of its own is named by its Bee item", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([{ t: 3, type: "bee_write", key: "w1", kind: "fact_confirm", text: null,
    item: { kind: "fact_confirm", fact_id: 8812 }, question: "Bee에 이 사실을 확인할까요?", answer: "응", result: "ok" }]);
  assert.deepEqual([beeItems(page)[0].kind, beeItems(page)[0].text], ["Confirm fact", "Fact #8812"]);
});

test("a write Bee may have taken without saying so asks the person to check Bee and promises no retry", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([{ t: 3, type: "bee_write", key: "w1", kind: "todo_create", text: "Note the bug", question: "q",
    answer: "yes", result: "failed", error: "timed out", may_have_gone_in: true }]);
  const [todo] = beeItems(page);
  assert.deepEqual([todo.state, todo.status], ["unknown", "Check Bee"]);
  assert.match(todo.rest, /may have gone in/);
  assert.doesNotMatch(todo.rest, /tries again/);
});

test("between calls, a missed and a declined AI call read as words, not as event names", async () => {
  const meta = { state: "", entries: [{ at: "2026-10-15T21:05:00+09:00", events: [
    { kind: "call_missed", topic: "Saturday hike" }, { kind: "call_declined", topic: "Saturday hike" }] }] };
  const page = loadPage({ hash: "#/meta", fetch: async () => ({ ok: true, json: async () => meta }) });
  await page.settle();
  const text = page.visibleText(page.byId("view"));
  assert.match(text, /You did not answer an AI call about “Saturday hike”/);
  assert.match(text, /You declined an AI call about “Saturday hike”/);
  assert.doesNotMatch(text, /call_missed|call_declined/);
});

// ---------- what a normal record leaves out (BC-034, BC-149; the 10/15 rule: the conversation, why the AI
// called, and its jobs; everything else only in a Debug build's debug view) ----------

/** A Debug build's page with the debug view switched on. */
const debugView = (hash) => loadPage({ hash, search: "?debug=1", prefs: { debug: "1" } });

test("the note the AI wrote before picking up shows only in the debug view", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(fixture(AI_CALL));
  assert.equal(page.all("prep").length, 0);
  assert.doesNotMatch(page.visibleText(), /Before picking up/);
  const debug = debugView("#/s/2026-10-15/2100");
  debug.feed(fixture(AI_CALL));
  assert.match(debug.text(debug.one("prep")), /Before picking up Dana's knee is still sore/);
});

test("without a prep note, the debug view does not claim the call started with Bee's context", () => {
  const page = debugView("#/s/2026-10-15/1830");
  page.feed([
    { t: 0, type: "call", direction: "inbound", topic: null, reason: null },
    { t: 1, type: "call_prep", seconds: 0.4, reads: [], note: null, error: "the person does not use Bee" },
  ]);
  const card = page.text(page.one("prep"));
  assert.match(card, /No prep note/);
  assert.doesNotMatch(card, /Bee context/);
});

test("the LiveKit room's name shows only in the debug view, and a normal record has no footer", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(fixture(AI_CALL));
  assert.doesNotMatch(page.visibleText(), /call-matt-20261015-210002/);
  assert.equal(page.one("session-foot").hidden, true);
  const debug = debugView("#/s/2026-10-15/2100");
  debug.feed(fixture(AI_CALL));
  assert.match(debug.visibleText(debug.one("session-foot")), /Room call-matt-20261015-210002/);
});

test("a failed background job says so in plain words; its raw error only in the debug view", () => {
  const raw = "no harness ARN: run scripts/deploy_worker.py first";
  const events = [
    { t: 1, type: "job_started", job_id: 1, task: "Compare Bukhansan and Gwanaksan" },
    { t: 9, type: "job_failed", job_id: 1, error: raw },
  ];
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(events);
  const job = page.text(page.one("job"));
  assert.match(job, /The job could not finish\./);
  assert.doesNotMatch(page.visibleText(), /harness ARN/);
  const debug = debugView("#/s/2026-10-15/2100");
  debug.feed(events);
  assert.match(debug.text(debug.one("job")), /no harness ARN: run scripts\/deploy_worker\.py first/);
});

test("an error reads in plain words, and the raw error is not one tap away outside the debug view", () => {
  const raw = "bee_conversation: HTTP 502 from https://api.bee.computer";
  const error = { t: 2, type: "error", source: "bee_context", error: raw };
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed([error]);
  const row = page.one("error");
  assert.equal(page.text(row.children[1]), "Some of your Bee data could not be read before the call.");
  row.children[1].click();
  assert.doesNotMatch(page.visibleText(), /502/);
  const debug = debugView("#/s/2026-10-15/2100");
  debug.feed([error]);
  assert.match(debug.visibleText(debug.one("error")), /HTTP 502/);
});

/** A fetch that answers every request with this JSON body. */
const answering = (body, status = 200) => async () => ({
  ok: status >= 200 && status < 300, status, json: async () => body, text: async () => JSON.stringify(body),
});

const metaRun = {
  entries: [{
    at: "2026-10-15T19:02:00", events: [{ kind: "bee_change", inbox: "inbox/1902" }],
    decision: "Dana's hike plan changed; worth a short call tonight.",
    error: "ThrottlingException: Rate exceeded (Bedrock)",
    actions: [
      { tool: "offer_call", topic: "Saturday hike", result: "notification sent: About “Saturday hike”: free for a quick call?" },
      { tool: "search_bee", result: "[{\"id\": 5121, \"score\": 0.82}]" },
      { tool: "write_state", result: "notes updated" },
    ],
    tools: [{ name: "bee_today", input: {}, result: "{\"todos\": []}" }],
    model: "claude-sonnet-5-5", seconds: 12.3,
  }],
  state: "Dana: easy trails only.",
};

test("between calls, a normal page shows what Bee Call noticed and did, not its raw tool calls or model", async () => {
  const page = loadPage({ hash: "#/meta", fetch: answering(metaRun) });
  await page.settle();
  const shown = page.visibleText(page.byId("view"));
  assert.match(shown, /Dana's hike plan changed; worth a short call tonight\./);
  assert.match(shown, /Asked you about a call: “Saturday hike”\./);
  assert.match(shown, /Updated what it keeps track of/);
  assert.match(shown, /It could not finish this time\./);
  for (const internal of [/ThrottlingException/, /claude-sonnet-5-5/, /What it looked at/, /search_bee/, /bee_today/,
    /notification sent/, /12\.3 s/]) {
    assert.doesNotMatch(shown, internal);
  }
});

test("between calls, the debug view still shows the raw tool calls, the model and the error", async () => {
  const page = loadPage({ hash: "#/meta", search: "?debug=1", prefs: { debug: "1" }, fetch: answering(metaRun) });
  await page.settle();
  const shown = page.visibleText(page.byId("view"));
  for (const internal of [/ThrottlingException/, /claude-sonnet-5-5/, /What it looked at \(1\)/, /search_bee/,
    /notification sent/]) {
    assert.match(shown, internal);
  }
});

/** One time the meta agent looked, with only what a normal page shows. */
const looked = (at, decision) => ({ at, events: [{ kind: "bee_change", inbox: "inbox/x" }], decision });

/** A fetch for Between calls: the newest page, and pages before a given time (BC-054). */
function metaPages(pages) {
  const asked = [];
  const fetch = async (url) => {
    asked.push(url);
    const before = /[?&]before=([^&]+)/.exec(url);
    return answering(pages[before ? decodeURIComponent(before[1]) : ""])();
  };
  return { fetch, asked };
}

test("between calls, earlier times it looked load below when the server says there are more (BC-054)", async () => {
  const pages = {
    "": { entries: [looked("2026-10-15T21:05:00+09:00", "Newest look.")], state: "", more: true },
    "2026-10-15T21:05:00+09:00": { entries: [looked("2026-10-14T08:00:00+09:00", "An older look.")], more: false },
  };
  const server = metaPages(pages);
  const page = loadPage({ hash: "#/meta", fetch: server.fetch });
  await page.settle();
  assert.doesNotMatch(page.visibleText(page.byId("view")), /An older look\./);
  page.one("meta-older").click();
  await page.settle();
  const shown = page.visibleText(page.byId("view"));
  assert.match(shown, /Newest look\.[\s\S]*An older look\./);
  assert.equal(page.has("meta-older"), false, "nothing older is left");
  assert.deepEqual(server.asked.slice(-1), ["api/meta?before=2026-10-15T21%3A05%3A00%2B09%3A00"]);

  // The next poll brings a new look; the older ones stay below it.
  pages[""] = { entries: [looked("2026-10-15T22:00:00+09:00", "A new look."), looked("2026-10-15T21:05:00+09:00", "Newest look.")],
    state: "", more: true };
  page.tick();
  await page.settle();
  assert.match(page.visibleText(page.byId("view")), /A new look\.[\s\S]*Newest look\.[\s\S]*An older look\./);
});

test("between calls, a server that does not say there are more shows no button (BC-054)", async () => {
  const page = loadPage({ hash: "#/meta", fetch: answering({ entries: [looked("2026-10-15T21:05:00+09:00", "A look.")] }) });
  await page.settle();
  assert.equal(page.has("meta-older"), false);
});

test("between calls, earlier times that cannot be loaded say so and can be tried again (BC-054)", async () => {
  let failing = true;
  const fetch = async (url) => (url.includes("before=") && failing
    ? answering("Bad Gateway", 502)()
    : answering(url.includes("before=") ? { entries: [looked("2026-10-14T08:00:00+09:00", "An older look.")] }
      : { entries: [looked("2026-10-15T21:05:00+09:00", "A look.")], more: true })());
  const page = loadPage({ hash: "#/meta", fetch });
  await page.settle();
  page.one("meta-older").click();
  await page.settle();
  assert.match(page.text(page.one("meta-older")), /Couldn't load earlier ones\. Try again\./);
  failing = false;
  page.one("meta-older").click();
  await page.settle();
  assert.match(page.visibleText(page.byId("view")), /An older look\./);
});

// ---------- when the records cannot be read (review finding 7) ----------

test("a 401 says the sign-in ended, on every page that reads the records", async () => {
  for (const hash of ["#/", "#/calls", "#/meta"]) {
    const page = loadPage({ hash, fetch: answering("sign in first\n", 401) });
    await page.settle();
    const note = page.visibleText(page.byId("view"));
    assert.match(note, /Your sign-in ended\. Open Bee Call and sign in again\./, hash);
    assert.doesNotMatch(note, /Can't reach/, hash);
  }
});

test("a server error says the records are not available, with its code", async () => {
  const page = loadPage({ hash: "#/calls", fetch: answering("Bad Gateway", 502) });
  await page.settle();
  assert.match(page.visibleText(page.byId("view")), /The call records are not available right now \(error 502\)\. Retrying…/);
});

// ---------- the live stream after a server restart (review finding 7) ----------

/** A fetch that gives these answers in turn, then the last one again and again. */
const inTurn = (...answers) => {
  let next = 0;
  return async (...args) => answers[Math.min(next++, answers.length - 1)](...args);
};
const listed = (...ids) => answering(ids.map((id) => ({ id, started_at: "2026-10-15T21:00:02", live: true })));

test("a stream the server refuses while it restarts reopens, and the call is rebuilt from its backlog", async () => {
  const lines = fixture(AI_CALL);
  const page = loadPage({ hash: "#/s/2026-10-15/2100",
    fetch: inTurn(answering("Bad Gateway", 502), listed("2026-10-15/2100")) });
  page.feed(lines.slice(0, 12)); // a live call, mid-way
  page.stream.refuse(); // Caddy's 502 while the session server restarts
  assert.equal(page.byId("status-text").textContent, "Reconnecting…");
  await page.settle(); // the list answers 502 too: wait and ask again
  assert.equal(page.streams.length, 1);
  page.tick();
  await page.settle(); // now the call is listed again
  assert.equal(page.streams.length, 2, "a new stream");
  page.feed(lines.slice(0, 20)); // the new stream sends the whole backlog, and more
  assert.equal(userBubbles(page)[0], "Yes, now is fine. Dana says her knee is better, but she still wants an easy route.");
  assert.equal(page.all("speech").filter((s) => /Is now still good/.test(page.text(s))).length, 1, "nothing twice");
});

test("a stream refused again and again while the call is listed is tried again after a pause, not at once", async () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100", fetch: listed("2026-10-15/2100") });
  page.feed(fixture(AI_CALL).slice(0, 12));
  page.stream.refuse(); // the first refusal after lines came: reopened at once
  await page.settle();
  assert.equal(page.streams.length, 2);
  page.stream.refuse(); // refused before it sent anything, such as a 500 before the stream starts
  await page.settle();
  assert.equal(page.streams.length, 2, "not at network speed");
  page.tick();
  await page.settle();
  assert.equal(page.streams.length, 3, "after the pause");
  page.stream.refuse();
  await page.settle();
  assert.equal(page.streams.length, 3);
});

test("a dropped connection is left to the browser, which resumes by itself", () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100" });
  page.feed(fixture(AI_CALL).slice(0, 12));
  page.stream.drop();
  assert.equal(page.byId("status-text").textContent, "Reconnecting…");
  assert.equal(page.streams.length, 1);
});

test("the stream of an ended call is not reopened", async () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100", fetch: listed("2026-10-15/2100") });
  page.feed(fixture(AI_CALL));
  page.stream.refuse(); // the 204 a finished stream answers
  await page.settle();
  page.tick();
  await page.settle();
  assert.equal(page.streams.length, 1);
  assert.equal(page.byId("status-text").textContent, "Call ended");
});

test("a call the server does not have says it could not be loaded", async () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2359", fetch: listed("2026-10-15/2100") });
  page.stream.refuse(); // 404
  await page.settle();
  assert.match(page.visibleText(page.byId("view")), /This call could not be loaded\./);
  assert.equal(page.streams.length, 1);
});

test("a sign-in that ended while watching says so and stops", async () => {
  const page = loadPage({ hash: "#/s/2026-10-15/2100", fetch: answering("sign in first\n", 401) });
  page.feed(fixture(AI_CALL).slice(0, 12));
  page.stream.refuse();
  await page.settle();
  page.tick();
  await page.settle();
  assert.equal(page.streams.length, 1);
  assert.equal(page.byId("status-text").textContent, "Your sign-in ended. Open Bee Call and sign in again.");
});
