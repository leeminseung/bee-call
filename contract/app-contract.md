# Bee Call app contract for design D

Draft, 2026-10-15. Where this file and the code disagree, this file wins. Where it is silent, today's behaviour
stays.

Design D draws a call as a thread: speech bubbles, job cards, Bee write cards and a "why I'm calling" card. Today
the web record page (`web/app.js`) builds those from the raw lines of a call's `events.jsonl`. If the iPhone app
built them again in Swift, the two copies would drift, as the booked time already did (the app read
`call_at_epoch` while the server wrote `call_epoch`, so a booked call's time never showed). So the server builds
one call view and the app only draws it. This file names every endpoint, field and value the app uses for D, once.

In this folder:
- `schemas/*.json`: JSON Schema (draft 2020-12) for every response body. `schemas/defs.json` holds the shared
  definitions. Each concept has one definition there.
- `examples/*.json`: one body per response (three for the call view), written from the real content of the D
  mockups. An example belongs to the schema whose name is the longest prefix of its own name.
- `check.py`: `uv run --with jsonschema python check.py` checks that every schema is valid, every example is valid
  against its schema, and 43 bodies the contract forbids (renamed fields, epochs, times without an offset, a running
  job with a duration, two open questions...) are rejected.

The plan of 2026-10-15 (DECISIONS, "D 확정과 구현 계획 승인"): the server's tests regenerate these examples from
real code and check every answer against the schemas, and the app's tests decode the same example files.

**Compatibility.** As in `fix-contract.md`: the server is deployed before the D build reaches phones, so it keeps
serving the build on phones now. Existing requests keep their meaning and existing response fields stay. Fields
kept only for that build are marked `deprecated` in the schemas, the D build never reads them, and they go once no
phone runs a pre-D build.

## 0. Conventions

- **Where.** Every endpoint is on the token server, the one origin the app already talks to through
  `ServerClient` (Release `https://call.mslee.dev`, Debug the Mac on port 8787). The records server (port 8443 on
  the server, 8788 on a Mac) stays for the Mac's web page. The app no longer uses it.
- **Who.** Every request carries `Authorization: Bearer <sign-in token>`, except `GET /auth/nonce`,
  `POST /auth/apple` and the voice samples. Every answer is the signed-in person's own: another person's call is
  404, never 403. A 401 means the sign-in ended, and the app signs out as it does today.
- **Bodies.** JSON, UTF-8, snake_case keys. Every key a schema requires is always present. A value that does not
  apply is `null`, never left out. Booleans and arrays are never null.
- **Times.** ISO 8601 to the second, with the UTC offset of the person's time zone: `2026-10-15T01:23:05+09:00`.
  Never an epoch, never a time without its offset. A calendar day is `2026-10-15`.
- **Durations.** Whole seconds, in fields whose names end in `_s`.
- **Ids.** Opaque strings, stable for the life of the record. A call's id comes from its LiveKit room name
  (section 1). An item's id is unique within its call and does not change when the item grows.
- **Counts.** Whole numbers of things (`marks.bee_writes`, `captions_included`), never durations.
- **Enumerations.** The values are exactly those in the schemas. The app decodes a value it does not know without
  failing, and skips what it cannot draw, so a later server can add one without breaking an older build.
- **Errors.** A status code and a one-line plain-text reason, as today. The app decides by the code.
- **Caching and versions.** Every token server answer says `Cache-Control: no-store` (black-box probe, 2026-10-15),
  so nothing goes to a disk cache. `/calls`, `/calls/{call_id}` and `/offers` carry a `version` in the body and the
  same value as their `ETag`. The app keeps the last body in memory and asks again with
  `If-None-Match: "<version>"`, and the server answers 304 with no body when nothing changed. A version changes
  when anything the body shows changes, and never because a running timer advanced: a running time is given as
  its start, and the app counts. Some changes come from the clock alone (a call with no sign of life turns
  `dropped`, an unanswered ring turns `missed`). The server works these out when it builds a body, never from a
  cache, and `/changes` announces them (section 3).
- **Paths in bodies.** `url`, `page_url`, `image_url`, `image_url_dark` and `sample_url` are paths on the token server (`/calls/.../files/...`).
  The app joins them to the server address and loads them with its sign-in, so images go through an authorized
  request, not a plain `AsyncImage`.

## 1. The call list: `GET /calls`

The home thread: every call, newest first, each with what its folded section shows.

Request: `GET /calls?limit=20&before=<cursor>`. `limit` is 1 to 50, 20 when left out. `before` is the `before` of
the previous page. Leave it out for the newest calls.

Response 200, `schemas/calls.json`, example `examples/calls.json`:

| Field | Type | Meaning |
|---|---|---|
| `calls` | array of call entries | Newest first. |
| `more` | bool | Older calls exist. |
| `before` | string or null | Pass as `?before=` for the next older page. Opaque. Null when `more` is false. |
| `version` | string | See Conventions. `/changes` announces the first page as the path `/calls`. |

Errors: 400 for a bad `limit` or `before`, 401.

Listed: every call with a record (live, ended, dropped) and every AI call that rang without being answered (missed,
declined). Not listed: a call the person hung up while it still rang, which keeps no record (BC-027), and an AI call
ringing right now, which appears once it is answered, missed or declined. Whether an unanswered AI call was missed
or declined has one source, the voice server's verdict (`call_missed` or `call_declined` in `meta/events.jsonl`,
with its room). A decline that arrives after that verdict changes nothing. A ring with neither a verdict nor a
record reads as missed once its `expires_at` has passed (a voice server that never picked the call up).

**A call entry** is the call summary below, which the call view shares, plus `results`.

| Field | Type | Meaning and rule |
|---|---|---|
| `call_id` | string | The call's LiveKit room name, the same string as `room` in `GET /token` and in a ring, so the app knows its own call's id the moment it joins. If one room ever has a second record (a voice server restarted during the call), that record is a call of its own, `<room>.2` (then `.3`), so job folders and write keys, which each record numbers from 1, never collide. During its own call the app shows the newest call whose id is its room or begins with its room and a dot. |
| `started_at` | time | When the record began, which is when the AI picks up. For a missed or declined call, when it began to ring. |
| `started_by` | `person` or `ai` | Who placed the call ("내가 건 전화", "Bee Call이 건 전화"). |
| `topic` | string or null | An AI call's topic, never null for one: what it asked about, or what the person booked out loud, or else a line in the person's language ("새 음성 메모 이야기", "오늘 하루 이야기") made by the function that writes the offer notification, so the app has no fallback of its own. Null for a call the person placed. |
| `title` | string or null | The call's one line. Null exactly when `title_source` is `none`. |
| `title_source` | enum | Where the title came from, best first: `summary` (one line a model wrote after the call, only when the person spoke), `topic` (an AI call's topic, until a summary exists), `first_sentence` (the person's first sentence, at most 60 characters, until a summary exists), `none` (nothing yet: a live call before the person spoke, or a call with no conversation, which D shows as "대화 없음"). |
| `state` | enum | `live`, `ended` ("정상 종료"), `dropped`, `missed` (the AI's call rang 60 s unanswered) or `declined` (the person declined it while it rang). How a record decides between the first three is in the table below. |
| `duration_s` | int or null | How long the call lasted: to its close, or for a dropped call without a close, to its last line. Null while live, and for a missed or declined call. |
| `continues` | call id or null | The earlier call this one picks up from: "이 통화 이어서 하기", or a call booked during that call. |
| `marks` | object | `report`, `table`, `chart` (bool: a finished job made a file of that kind) and `bee_writes` (int: how many of the call's Bee writes are in Bee now). For the home row's summary and its VoiceOver label. |
| `results` | array | The call's jobs and Bee writes, in thread order, as the same item objects with the same ids as in the call view (2.4). The folded section draws these without fetching each call's view. |

How a record's `state` is decided. The record's `session_closed` line names LiveKit's close reason. LiveKit closes
a session as `participant_disconnected` only when the person left on purpose (their app hung up, or the room was
deleted). It waits out a lost network, and when the room then closes under the voice server the reason is
`job_shutdown`, the same as a stopped voice server.

| What the record shows | `state` |
|---|---|
| `session_closed` with `user_initiated` (the AI said goodbye and ended the call), `participant_disconnected` (the person hung up) or `task_completed` | `ended` |
| `session_closed` with `job_shutdown` (a lost network, or a stopped voice server) or `error` | `dropped` |
| `call_ended` without any `session_closed` (the session never closed normally, for example it failed to start) | `dropped` |
| No `session_closed` and no `call_ended`, and no sign of life for 120 s | `dropped` |
| No `session_closed` and no `call_ended`, with a sign of life in the last 120 s | `live` |

A record shows life by new lines, and by the voice server refreshing its file's time every 30 s while the call runs.
A call writes lines only when something happens, so two quiet minutes must not read as dropped (section 10).

## 2. The call view: `GET /calls/{call_id}`

One call as its thread: the expanded section on the home thread, the thread during the call, and the record sheet
after it.

Request: `GET /calls/{call_id}`, with `If-None-Match` when the app holds a version.

Response 200, `schemas/call-view.json`. Examples: `call-view.json` (the 01:23 call after it ended),
`call-view-live.json` (the same call at 3:41, while the person speaks and a job runs), `call-view-ai.json` (a call
the AI placed).

| Field | Meaning |
|---|---|
| `call` | The call summary: the call entry's fields of section 1, without `results`. |
| `why_calling` | 2.2. Null for a call the person placed. |
| `prep` | 2.3. Null when the record has no prep. |
| `items` | 2.4, in drawing order (2.5). |
| `captions_included` | `{person, ai}`: how many caption lines of each speaker the items already hold (section 3). |
| `version` | See Conventions. |

Errors: 304, 401, and 404 for a call this person does not have, for a missed or declined call (it has no record),
and for a call whose record has not begun yet (a call the person placed is recorded once the AI picks up).

### 2.1 How the call ended

The thread's last line ("통화 끝 · 7분 40초") and the sheet's header ("오늘 오전 1:23 · 내가 건 전화 · 7분 40초 ·
정상 종료") come from `call.state` and `call.duration_s`. There is no end item.

### 2.2 `why_calling`: the card of a call the AI placed

The card's title is `call.topic`.

| Field | Type | Meaning |
|---|---|---|
| `reason` | string or null | Why the AI called, in the person's language (open question 2). |
| `conversation` | object or null | The Bee conversation behind the call: `summary` (Bee's short summary, or null) and `quotes`, its last lines oldest first, each `{speaker: person or other, text, at}`. |

### 2.3 `prep`: "통화 전에 Bee에서 본 것"

| Field | Type | Meaning |
|---|---|---|
| `note` | string or null | The note the AI wrote for itself before it picked up, with Bee's ids taken out (the page's `withoutIds` rules move to the server). Null when it wrote none. |
| `looked_up` | array | What it read, without repeats, in the order read: the conversation behind an AI call, the day's Bee summary, and each extra read the prep chose. Each is `{type: conversation, summary, at}` (Bee's short summary, named as in `why_calling.conversation.summary`, and when the conversation began), `{type: day, date}` or `{type: search, query}`. The app writes "오늘 요약" for a day that is today. The eight recent conversations every call reads are not listed. |
| `from_bee` | bool | The call read the person's Bee. False for a person without Bee, whose note comes from earlier calls only. The app then leaves "Bee" out of the card's title. |

### 2.4 Items

Every item has `type`, `id` and `at` (when it began, or for a finished job when it finished).

**`speech`**: one bubble.

| Field | Meaning |
|---|---|
| `speaker` | `person` or `ai`. |
| `text` | A run of one speaker's speech with nothing else between, its pieces joined with spaces. The person's speech is their final transcripts, so the bubble reads as the captions did: "주로 파이썬이고 / 머신러닝도 조금 해요. / 도서관이나 카페에서 일할 때가 많아요." arrive as three pieces and make one bubble. The AI's speech is what it said aloud, cut where the person cut in. An empty piece adds nothing. |
| `interrupted` | The person cut the AI off at the end of this run. Always false for the person. |

**`job`**: a background job.

| Field | Meaning |
|---|---|
| `title` | A few words in the call's language: "코딩용 맥북 에어와 프로 한국 가격 비교" (open question 5). |
| `state` | `running`, `done` or `failed`. |
| `started_at` | When it started. The app counts "1분 10초째" from it. |
| `duration_s` | Once finished, how long it took ("1분 27초 걸림"). Null while running. |
| `step` | While running, the latest step, in the call's language ("애플 한국 가격표 읽는 중"). Else null. |
| `summary` | Once done, what it found, in a paragraph, in the call's language. Else null. |
| `files` | What it made: charts first, then the report, tables, others, by name within a kind. Each is `{name, kind, url, page_url, title, image_url, image_url_dark}`. `name` is the file's real name, which the worker keeps to ASCII (`report.md`, `comparison.csv`), so the app may label a chip by its `kind` ("보고서", "표", "차트") rather than by its name. `kind` is `report` (report.md, with `page_url`, the report rendered as a readable page in HTML with light and dark styles, which the app opens instead of the raw text; null for a report from before pages existed), `table` (.csv, .tsv), `chart` (a chart the worker drew) or `other`. A chart has `image_url`, a PNG for the thumbnail and the card, `image_url_dark`, the same chart drawn for dark mode (transparent background, the dark chart palette; null for a chart drawn before dark charts existed, which the app then shows on a white plate), and its `title` in the call's language ("한국 가격(원), 에어와 프로, 세 가지 구성") when known. Other kinds have both null. The worker's own bookkeeping (request.md, events.jsonl, usage.json) is never listed. |
| `failure` | When failed: `call_ended` (the call ended first) or `error` (anything else, with the error's words kept in the server log). Else null. A job that a call outlived without an end (a dropped call) reads as failed with `call_ended`, its `at` and `duration_s` taken from the call's end. |

The step, the summary and a chart's title are in the call's language only once the worker is told that language
(section 10). Today it writes them in English.

**`bee_write`**: a fact or to-do written to Bee after the person's spoken yes. Built from the call's
`bee-writes.jsonl`, the writer's own record of every step, not from the `bee_write` events: an undo that failed and
a write cut off by a stopped process appear only there.

| Field | Meaning |
|---|---|
| `kind` | `todo_create`, `todo_accept`, `fact_confirm` or `fact_update`. |
| `text` | The item as the AI quoted it when it asked: "토요일 촬영 장소 정하기". Null only in records from before 2026-10-15. |
| `state` | `written`: in Bee, "넣음" (the writer's `ok`, or `undo_failed`). `undone`: taken out with "빼 줘". `unknown`: Bee may or may not have it, "Bee에서 확인해 주세요", never tried again (the writer's `unknown`, or `applying` left by a stopped process). `failed`: not in Bee. |
| `will_retry` | A failed write of a live call: it is tried once more right after the call. False once the call is over. |
| `undoable` | Saying "빼 줘" now would take this write out. In a live call, the last write whose status is `ok`, as the call AI's undo picks it (a write whose undo failed is skipped). The app shows "빼려면 '빼 줘'라고 말하세요" on it. |

### 2.5 Order

Items come in the order to draw. Each sits where it began: a speech run where its first words began (an AI reply
where its voice started, so a line the person cut into comes before the words that cut it), a Bee write where it
was written, a finished job where it finished ("통화 중에 끝난 작업은 대화 속 도착한 자리에"). A running job comes
last, docked at the bottom of the live call. A card between two pieces of speech splits the run in two. The app does
not sort.

### 2.6 Files: `GET /calls/{call_id}/files/{path}`

What `url` and `image_url` point at. Content-Type by kind: `text/plain; charset=utf-8` for .md, .csv and .txt,
`image/png`, and `image/svg+xml` with a sandbox CSP as today. Errors: 403 for anything under `audio/` or `inputs/`,
the model log and the Claude history (never served, BC-095) or a path that leaves the call's folder, 404 for
anything else not there, and 401. A finished job's files never change, so the app may keep an image in memory by
its URL.

## 3. Live updates during a call

**Recommendation: a "changed" signal, after which the app fetches the view again.**

`GET /changes` is a Server-Sent Events stream for the signed-in person. Its one event is `changed`, with data
`{path, version}` (`schemas/change.json`): the resource at `path` now has `version`. Paths are `/calls` (the first
page of the list), `/offers` and `/calls/{call_id}`. On connect the server sends one `changed` for `/calls`, for
`/offers` and for each call that is live or ended in the last ten minutes (a title and Bee retries still arrive
after the end). After that it sends one whenever a version changes, at most one per path every 250 ms. Besides
watching the files, it rebuilds every live call and every ring still waiting at least every 30 s, so a change the
clock alone makes (`live` to `dropped`, a ring to `missed`) is announced too. A keepalive comment goes out every
15 s, and the app treats 40 s of silence as a dead stream and reconnects, as with `/ring`. There is no
`Last-Event-ID`: the versions sent on connect bring the app up to date.

The app keeps the stream open while it is on screen or in a call. When a changed path is on screen and its version
differs from the one drawn, it fetches the path with `If-None-Match`. Otherwise it only notes the version. During its
own call the app fetches `GET /calls/{room}` directly: 404 until the record begins, then `/changes` announces it.
After a voice server restart in the room, the call continues as `<room>.2` (section 1), which `/changes` announces
as a new live call.

Why this way rather than patches:
- One way to get a body. First load, a reconnect and a live update are the same GET, so the app holds no merge
  state that could drift from the server's.
- Nothing is lost on a reconnect or to a slow phone: the next fetch returns the whole current view.
- The stream is tiny and the same for every resource. The ring stream stays as it is, and the raw event stream
  (`/api/sessions/.../events` on the records server) stays for the Mac page's debug view only.
- The cost is small at this scale, one person and one call at a time. A ten-minute call watched in the thread the
  whole time makes about 150 to 200 fetches of 5 to 15 KB, 1 to 2 MB in all and less with gzip. Patches would save
  that at the price of an ordering and gap protocol in Swift.

**Captions and the view.** LiveKit's captions reach the phone within a fraction of a second, and the view follows
a moment later. These rules keep a line from showing twice:
1. The call screen (the orb) draws captions only, as today: the line being spoken large, the other speaker's last
   line small.
2. The thread draws the view's items, then the caption lines the view does not hold yet, then any running job card.
3. The app numbers caption lines per speaker, 1, 2, 3..., in the order they first arrive in the room. A line is one
   LiveKit segment, which for the person runs from the first word to the final transcript and for the AI is one
   spoken reply. The view's `captions_included` says how many lines of each speaker its items hold. The thread draws
   a caption line only while its number is greater than that count. A line still being spoken has its number too,
   so the person's growing words come from the caption and hand over to the view as soon as the server has them.
4. A caption line joins the last item drawn (running jobs aside) when that item is a speech run of the same
   speaker. Otherwise it starts a new bubble after the items.
5. Caption lines are drawn in the thread only while the app is in that call and the call is `live`. Once it ends,
   the view alone draws it.
6. When the numbers may no longer line up, the thread draws the view only for the rest of that call (the orb screen
   keeps its captions). That is when the phone's room reconnects (lines sent meanwhile are lost), when the voice
   server leaves the room or a new one joins it (a restart: a reply cut off mid-voice was captioned but never
   recorded), and when the voice server reports its own reconnect by changing its participant attribute
   `bee_call.captions` (captions are not sent while it is away, while the transcripts it hears are still recorded).

This holds because, while one connection lasts, the server counts the same lines the phone receives. The person's
lines are the final transcripts (`user_transcript` events), one per caption line: the caption and the event come
from the same final transcript, after the same quiet-speech filter (the speech gate works before both), and empty
finals reach neither. The AI's lines are its spoken replies, one caption line each: a reply is recorded only when
words of it were heard, the server never splits a reply (no `FlushSentinel`), and the speech filter keeps markup out
of the voice. A server test pins these conditions (open question 6).

Worked example, `call-view-live.json`: at 3:41 the person has finished two pieces of "주로 파이썬이고 / 머신러닝도
조금 해요. / 도서관이나 카페에서 일할 때가 많아요." The view holds them (`captions_included.person` is 8), so the
phone draws its caption line 9, the third piece still growing, inside the same bubble, above the docked job card.

## 4. Offers and booked calls: `GET /offers`

**Decision: offers stay a list of their own, not thread items.** Their life (answer, call off, expire) is not a
call's, at most one question is open at a time, and booked calls are pinned above the thread rather than placed in
it. The app puts the open question into the thread at its `created_at`, among the calls ("오늘 오후 2:41 · Bee
Call이 물어봐요"), and pins each booked call ("오늘 저녁 7:00 전화 예정", "전화 취소"). An answered or expired
question leaves the list: a "later" answer comes back as booked, a "now" answer becomes the call itself (open
question 8).

Response 200, `schemas/offers.json`, example `examples/offers.json`: `offers` (open ones only, in the order they
were made) and `version`.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Such as `20261015-144102-a91f`. |
| `state` | enum | `asked`: waiting for an answer, with the buttons "8:30에" (later), "지금" (now) and "괜찮아요" (decline). `booked`: agreed for `call_at`, can be called off. `calling`: being placed now, can no longer be called off (the server's own `now` and `placing` states read as `calling`). |
| `topic` | string | What it is about, in the person's language. Never null: when the offer has no topic of its own, the server writes the line its notification uses ("새 음성 메모 이야기", "오늘 하루 이야기"), so the app keeps no fallback of its own. |
| `created_at` | time | When it was asked, or booked out loud in a call. |
| `suggested_at` | time or null | The time a question suggests. Null for a call booked out loud in a call. |
| `call_at` | time or null | When it rings, once agreed: the booked time the pinned row shows. Null while asked. |

Deprecated, for pre-D builds only: `suggested_epoch`, `call_epoch`, and `kind` and `source`, which those builds
used for their own topic fallback.

Answering: `POST /offers/{offer_id}` with `{"choice": "later" | "now" | "decline"}` returns 200 with
`{"offer": ...}` (`schemas/offer-answer.json`, state `booked`, `calling` or `declined`). "decline" also calls off a
booked call ("전화 취소") until its room is being made. Errors: 404 unknown offer (checked first), 400 another
choice, 409 closed or already answered, 401.

## 5. Voices: `GET /voices?language=ko`

The first run's second step and the settings sheet's voice list.

Response 200, `schemas/voices.json`, example `examples/voices.json`: `language` and `voices`, 1 to 12 entries in
the order to list them, the default first. The number differs by language.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | A catalog id such as `ko-harin`, the value `settings.voice` holds. The server maps it to its provider's voice, which the app never sees. |
| `name` | string | The voice's name as written in its language: "하린". D's names are placeholders until the voices are chosen (open question 9). |
| `character` | object | One word for how it sounds, `ko` and `en`: "친근함", "Friendly". Both, so the app's own language can differ from the call's. |
| `sample_url` | path | About five seconds of the voice, MP3. |
| `default` | bool | The language's default. Exactly one per language. |

Errors: 400 for a missing or unknown `language`, 401.

`GET /voices/samples/{file}` needs no sign-in: samples are the same for everyone and hold nothing personal, so
AVPlayer can stream them. It is the one token server answer that may be cached
(`Cache-Control: public, max-age=31536000, immutable`). A file's name carries its content hash, so a new sample
gets a new URL.

## 5b. Models: `GET /models?role=call` and `GET /models?role=worker`

The settings sheet's "통화 모델" and "일꾼 모델" lists. The server keeps one model catalog, the only place that knows
providers, their model ids, prices and how each role runs a model (section 10). The app shows only what this answer
gives, so a new model or a new provider (for example Nebius Token Factory later) is a server change, never an app
update.

Response 200, `schemas/models.json`, examples `examples/models-call.json` and `examples/models-worker.json`:
`role` and `models`, 1 to 12 choices, the default first.

| Field | Type | Meaning |
|---|---|---|
| `id` | string | A catalog id such as `haiku`, `sonnet` or `glm`: the value `settings.call_model` or `settings.worker_model` holds. Never a provider's own model id. |
| `name` | string | As shown: "Claude Haiku 5.5". |
| `maker` | string | Who makes or runs it, as shown: "Anthropic", "AWS". |
| `description` | object | One short line for this role, `ko` and `en`. |
| `default` | bool | The role's default. Exactly one per role. |

Errors: 400 for a missing or unknown `role`, 401. Today: call `haiku` (default) and `sonnet`; worker `sonnet`
(default: Claude Sonnet 5.5 on Anthropic's API with web search, page reading and code execution) and `glm` (GLM 5
on the AWS AgentCore worker). The meta agent and the call prep are not person settings: the server runs them on
`sonnet` and `haiku` with GLM 5 standing by.

## 6. Settings: `GET /me`, `PUT /settings`

`GET /me` returns 200 with `{person, settings, spent_today_usd, bee_connecting}` (`schemas/me.json`). `settings` is
the object in the table below. `bee_connecting` is true while a Bee approval waits in the Bee app.

`PUT /settings` takes any subset of the fields the app sends (table) and returns 200 with `{settings}`
(`schemas/settings.json`). Fields left out keep their values. 400 with the reason for an unknown field or a bad
value, for a `voice` that is not one of the language's voices, and for a `call_model` or `worker_model` that is not
one of its role's ids (a stored id the catalog no longer has reads as the role's default). A change of `language` without a `voice` in the
same request sets `voice` to null (the new language's default), unless the stored voice belongs to the new
language. 401.

Each setting has one owner after D:

| Field | Owner after D | The app sends | The app reads | The server uses it for |
|---|---|---|---|---|
| `language` | **In the app**: the first run and the settings sheet | `PUT` when the person picks it, never again from the Settings app | `/me` | The call language |
| `voice` | **In the app** | `PUT` when the person picks it | `/me`, with `/voices` for its name and character; null means the entry marked `default` | The AI's voice |
| `call_model` | **In the app**: the settings sheet, from `GET /models?role=call` | `PUT` when the person picks it | `/me` | The call AI's model |
| `worker_model` | **In the app**: the settings sheet, from `GET /models?role=worker` | `PUT` when the person picks it | `/me` | The background worker's model |
| `call_time` | **iOS Settings** | Same | Its own field | The time an offer suggests |
| `record_on_server` | **iOS Settings** | Same, and before joining an answered call (at most 2 s) | Its own switch | Keeping call audio |
| `record_on_phone` | **iOS Settings** | Same | Its own switch: the app records the call itself | Noted in the record |
| `time_zone` | **The phone** (system time zone) | Same | No | The person's clock |
| `use_bee` | **iOS Settings**: the person's wish | Never as a field: it drives `POST /bee/connect` or `/bee/disconnect` (`BeeSync`) | `/me`: `settings.bee` and `bee_connecting` | Nothing directly |
| `bee` | **The server**: Bee is connected | No | `/me`, for "Bee 연결됨" | Bee reads and writes |
| `daily_budget_usd` | **The server's operator** | No | No | The day's budget |

`language` leaves the Settings app because today every launch sent the Settings app's value and overwrote what the
server held (DECISIONS 2026-10-15: the friend's calls stayed in English until the iOS Settings value changed).
`tts` (the operator's provider switch) retires: the voice catalog names each voice's provider.

## 7. Endpoints D keeps using as they are

| Endpoint | 200 body | Errors and notes |
|---|---|---|
| `GET /auth/nonce` | `{nonce}`, `auth-nonce.json` | No sign-in needed. |
| `POST /auth/apple` `{identity_token, nonce}` | `{token, person}`, `auth-apple.json` | 403 `{waiting}` with the code to show (`auth-waiting.json`), 400, 401. No sign-in needed. |
| `POST /auth/signout` | 204 | |
| `GET /token` | `{url, token, room, identity}`, `token.json` | A new call. `?room=R&answer=1` answers the AI's call in room R. `?continue=<call_id>` is "이 통화 이어서 하기", for an `ended` or `dropped` call; the pre-D form `continue=<date>/<time>` stays accepted. 400 (not your room, or `continue` names no recorded call), 410 (the AI's call is over), 429 (today's budget is spent; the text says so). |
| `GET /ring` | Server-Sent Events, `event: ring`, data `ring.json` | D reads `room`, `topic` and `expires_at` only. The VoIP push carries the same `room`, `topic` and `expires_at`. |
| `POST /calls/decline` `{room}` | 204 | 400 for a room that is not this person's. |
| `POST /push`, `POST /voip` `{token, environment}` | 204 | 400. |
| `POST /bee/connect` | `{link}`, `bee-connect.json` | 502 when Bee cannot start a login. |
| `POST /bee/disconnect` | 204 | 502 when the login could not be removed. |

## 8. Where each part of D comes from

| D | Data |
|---|---|
| Home header "Bee 연결됨" | `/me`: `settings.bee`, `bee_connecting` |
| Pinned "오늘 저녁 7:00 전화 예정", "전화 취소" | `/offers` with state `booked`: `call_at`, `topic`; `POST /offers/{id}` decline |
| Folded call line "오늘 오전 1:23 · 내가 건 전화 · 7분 40초" | `started_at`, `started_by`, `duration_s` or `state` |
| Folded call title, "대화 없음" | `title`, `title_source` |
| Result cards under a folded call | `results` |
| "대화 펼치기" | `GET /calls/{call_id}`: `items` |
| The open question card and its three buttons | `/offers` with state `asked`: `created_at`, `topic`, `suggested_at`; `POST /offers/{id}` |
| Call screen: the line being spoken, the other's last line | LiveKit captions only |
| Call screen: "가격 비교 중 · 1:07" pill | The running job: `title`, `started_at` |
| "결과 도착 · 대화에서 보기" | A job id whose `state` went from `running` to `done` between two views |
| Bubbles in the thread during the call | `items` of type `speech`, then caption lines (section 3) |
| "Bee 할 일에 넣었어요" card and its hint | `bee_write`: `kind`, `text`, `state`, `undoable` |
| "작업 중 · 1분 10초째" card | `job` running: `title`, `started_at`, `step` |
| "방금 끝남 · 1분 27초 걸림" card | `job` done: `at`, `duration_s`, `title`, `summary`, `files` (chart `title` and `image_url`, chips) |
| Record sheet header and "정상 종료" | `call` |
| "통화 전에 Bee에서 본 것" | `prep` |
| "통화 끝 · 7분 40초" | `call.state`, `call.duration_s` |
| "이 통화 이어서 하기" | `GET /token?continue=<call_id>` |
| First run, step 1 (언어) | The app's two languages; `PUT /settings` `language` |
| First run, step 2 (목소리) and the settings sheet | `GET /voices`, `sample_url`; `PUT /settings` `voice` |

## 9. Open questions

1. **The prep card in the record.** D shows "통화 전에 Bee에서 본 것" in every record, while BC-034 and BC-149
   (2026-10-15) keep that card in the debug view only. Recommendation: D's decision replaces BC-034 and BC-149 for
   the app. Amend them, and let the web page follow the same view.
2. **Reasons written in English.** An AI call's `reason` comes from the meta agent and from `book_call` ("In a call
   at 14:27, the user asked to be called back: ..."), in English today, while D's card is Korean. Recommendation:
   write `topic` and `reason` in the person's language where they are made (the meta agent's instructions, and a
   per-language sentence in `book_call`), not translated by the view.
3. **A lost network reads as `dropped`, from the code only.** LiveKit's code closes a session as
   `participant_disconnected` only for a deliberate leave, and a phone that lost its network ends the call as
   `job_shutdown` once the room closes, so the table in section 1 shows it as `dropped`. No real record of a lost
   network has been checked. Recommendation: confirm it on one device call (airplane mode in the middle of a call)
   before the app relies on it. If the record differs, the voice server records the caller's disconnect reason.
4. **Chart images.** Charts are SVG, which iOS cannot show without a web view. Recommendation: the worker writes a
   PNG beside each chart as it saves it (one renderer, `worker/chart.py`) and keeps the chart's title. A one-off
   command renders PNGs for older charts.
5. **Job text in the call's language.** A job carries only the coordinator's long instruction (`task`), while D
   shows a few words. And the worker is never told the call's language (the coordinator is even told that job
   results may be in English), so steps, summaries and chart titles come out in English while D shows Korean.
   Recommendation: `delegate` takes a short `title`, which the call AI writes in the call's language, and the job
   request carries that language, which the worker's instructions apply to its progress lines, relay, summary and
   chart titles. Until then the server cuts the task to its first 40 characters for the title.
6. **Exactness of caption numbers.** Section 3 is exact while one connection lasts and LiveKit sends one caption
   line per final transcript and per spoken reply, which holds today. Rule 6 covers the breaks the code allows: the
   phone's reconnect, a voice server restart, and the voice server's own reconnect (which needs the new attribute).
   Recommendation: keep the numbers, pin the conditions with a server test (no `FlushSentinel` in replies, markup
   filtered from speech), and move to caption ids the voice server publishes itself only if LiveKit's behaviour
   changes.
7. **Who writes the title.** Recommendation: the voice server, after the call, beside the Bee retry and within
   5 s: one Claude Haiku 5.5 request over the transcript, in the call's language, only when the person spoke,
   recorded as an after-call `call_titled` event. Its cost goes to the spend log. A one-off command titles older
   calls. When it fails, the title falls back as in section 1.
8. **Answered questions in the thread.** D draws only the open question. Recommendation: an answered or expired
   question leaves the thread (a booked one moves to the pinned row, a "now" answer becomes the call). Revisit if
   the user wants a trace of questions declined.
9. **The voice catalog.** Which five voices per language, their names and the default are being chosen by ear.
   Recommendation: catalog ids stay independent of the provider (`ko-harin`), the catalog maps each to a provider
   voice, and today's hand-set `tts` and `voice` values move to the matching catalog id or to null.

### Decisions on the open questions (coordinator, 2026-10-15)

1. Follow D: the prep card shows in every record (titled without "Bee" when `from_bee` is false). BC-034 and BC-149
   are amended for the app and the page follows the same view.
2. Accepted: `topic` and `reason` are written in the person's language where they are made.
3. Accepted: confirm on one real device call (airplane mode in the middle of a call) before the app relies on it.
4. Accepted: our own chart code writes a PNG beside each SVG chart and keeps the chart's title; a one-off command
   renders older charts.
5. Accepted: `delegate(task, title)` and the job request carries the call's language; until then the first 40
   characters of the task.
6. Accepted: caption numbers, pinned by a server test.
7. Accepted with limits: the title request runs beside the Bee retry, never delays closing the record, and has a hard
   5 s limit; on any failure the title falls back as in section 1.
8. Accepted: an answered or expired question leaves the thread.
9. Accepted: provider-independent catalog ids. The user's final picks (2026-10-15, by ear over two rounds of
   samples), in listing order, default first. Korean: `ko-sian` Sian (5n5gqmaQi9Ewevrz7bOS, default, 차분함 / Calm),
   `ko-han` Han (8jHHF8rMqMlg8if2mOUe, 친근함 / Friendly), `ko-anna` Anna Kim (uyVNoMrnUku1dZyVEXwD, 다정함 / Tender),
   `ko-yooni` Yooni (n2fbxG88jqAoaVPUy3IG, 또렷함 / Clear), `ko-seulki` Seulki (ksaI0TCD9BstzEzlxj4q, 안정감 / Steady),
   `ko-kyle` Kyle (RU7aSi6lT4uQBXMLgDxK, 묵직함 / Deep), `ko-mrk` Mr. K (LKOcTG4J4tYTPR9DnLeM, 자연스러움 / Natural),
   `ko-jason` Jason (YBRudLRm83BV5Mazcr42, 잔잔함 / Soothing). English: `en-zara` Zara (jqcCZkN6Knx8BJ5TBdYR, default,
   따뜻함 / Warm), `en-amelia` Amelia (ZF6FPAbjXT4488VcRRnw, 생기 / Lively), `en-hope` Hope (uYXf8XasLslADfZ2MB4u,
   발랄함 / Bubbly), and two ElevenLabs default male voices as placeholders until the user picks men by ear: `en-will`
   Will (young, relaxed) and `en-chris` Chris (natural, down to earth). Display names are the voices' own names.
   Each catalog entry may also hold server-only speech tuning for that voice (voice settings such as speed and
   stability, and how it laughs: a tag or written laughter), empty for now and tuned by ear later; the app never
   sees it.

10. Models (the user, 2026-10-15): every model choice is provider-independent, so switching providers is easy and
   another provider's version can be added later (the user plans one with Nebius Token Factory for another
   hackathon). One server catalog holds every role: the call AI (`call_model`), the worker (`worker_model`), the
   meta agent (Claude Sonnet 5.5) and the call prep (Claude Haiku 5.5), with GLM 5 standing by for each. The worker's
   Claude runs in our own runner on Anthropic's API with Anthropic's web search, page reading and code execution
   tools and our own progress, relay, file and chart tools, thinking on and effort high; the AWS AgentCore worker
   stays selectable as `glm`. Both model settings move from iOS Settings into the app's settings sheet.

**Changing this contract.** The repository's `contract/` folder is the one copy. The server's tests regenerate
`examples/` from real code. Any change to a field name, type or meaning is logged in the server's progress notes and
told to the coordinator, who passes it to the app side before either side relies on it.

## 10. What changes where

**Server** (`bee-call-fix-server`):
- `voice/call_view.py`, new: the one place that turns a call's folder into its summary and view, and a person's
  folders, ring log (`state/outbound.jsonl`) and the voice server's ring verdicts (`call_missed`, `call_declined` in
  `meta/events.jsonl`) into the list. It holds every rule of sections 1 and 2: speech runs, job states, Bee write
  states from `bee-writes.jsonl`, titles and their fallbacks, call states, marks, order, `captions_included`, Bee
  ids taken out of the prep note, versions. Pure functions over the files. What the files say may be cached per
  folder by file stamps, as `SessionIndex` does, but liveness and missed rings are worked out against the clock on
  every build. Older records are read here and nowhere else: `bee_write_queued` and `bee_write_done`, records without
  `bee-writes.jsonl`, `user_transcript` or a `call` event, times without an offset, prep reads stored as
  "tool {json}".
- `voice/token_server.py`: routes `GET /calls`, `GET /calls/{call_id}`, `GET /calls/{call_id}/files/{path}`,
  `GET /changes` (with its 30 s rebuild of live calls and waiting rings), `GET /voices` and
  `GET /voices/samples/{file}` (open and cacheable); `version` and `If-None-Match` on `/offers`; `continue=` takes a
  call id; `PUT /settings` takes `voice`.
- `voice/call_folders.py`: takes the file rule (`resolve_inside`, `PRIVATE`) from `voice/session_server.py`, so both
  servers serve call files by one rule, and finds a record by its room.
- `voice/offers.py`: `Offer.view()` adds `created_at`, reports `now` and `placing` as `calling`, and fills an empty
  topic with the same function its notification uses, which also gives an AI call without a topic its line. The
  epochs, `kind` and `source` stay for pre-D builds.
- `voice/people.py`: `APP_SETTINGS` gains `voice` and `worker_model`, with the language rule of section 6; `tts` retires.
- A model catalog (`voice/model_catalog.py` or beside it as data), the one place for every model choice and role
  (decision 10), with one small adapter per provider (Anthropic Messages, Bedrock Converse, the AgentCore harness;
  later Nebius); `GET /models`; `voice/call_models.py`, the meta agent and the call prep read it; a Claude worker
  runner next to the harness one, chosen by `worker_model`.
- `voice/voices.py` and a voice catalog beside it with the sample files: the voices of decision 9, catalog ids, one
  default per language; `voice_choice` reads the person's catalog id.
- `voice/titles.py`, new: the after-call title (open question 7). `voice/agent.py` runs it in its shutdown, and
  changes its participant attribute `bee_call.captions` after its own room reconnects (section 3, rule 6).
- `voice/recorder.py`: refreshes the record file's time every 30 s while the call runs, so a quiet live call stays
  `live` (section 1). Today's 120 s rule in `voice/session_server.py` and `web/app.js` has the same gap: the
  current code writes no periodic lines (the per-second metrics lines of the 2026-10-11 fixture are not recorded).
- `voice/coordinator.py`: `delegate(task, title)`; `book_call` writes its reason in the person's language.
  `worker/runner.py`: `job_started` carries the title, and the job request carries the call's language.
- `worker/harness.py` and `worker/chart.py`: the worker writes progress lines, its relay, the summary and chart
  titles in the call's language; a PNG beside each SVG chart; the chart's title kept for the view.
- `voice/prep.py`: each read recorded as data (a conversation's id, Bee's short summary and start time; a day; a
  search's query) instead of "tool {json}" text. `beelink/context.py`: quote times as full times with an offset
  (today "HH:MM").
- `meta/agent.py`: an offer's topic and reason in the person's language.
- `voice/continuation.py`: finds the earlier call by call id as well as by date and time.
- Tests: `tests/test_call_view.py` builds the list and the three views from fixture records under
  `tests/fixtures/sessions/` and writes them over `examples/`, so the examples are the code's output; every
  endpoint's body is checked against `schemas/`; the cases of `web/test/page.test.js` that test interpretation
  move here (three pieces make one bubble, a write and its undo by key, a retried write, a write that may have gone
  in, the why card only for AI calls).

**App** (`bee-call-fix-app/ios/BeeCall/BeeCall`):
- New: one file of Codable types for every schema (`Contract.swift`); `CallStore.swift`, the only place that fetches
  `/calls` and `/calls/{call_id}` and keeps their versions; `ChangeStream.swift` for `/changes`, modelled on
  `RingStream.swift`; `Voices.swift` with the sample player; and the D screens: the home thread, a call's section
  (folded and expanded), the cards (job, Bee write, question), the call screen with its orb and the call bar with the
  zoom transition, the record sheet, the first run and the settings sheet. Tests decode every file in this folder's
  `examples/` with those types.
- Changed: `Captions.swift` (caption numbers per speaker and the rules of section 3); `LiveKitClient.swift` (tells
  that rule when the room reconnects, when the voice server leaves or joins, and when its `bee_call.captions`
  attribute changes; `passPath(continuing:)` sends a call id); `CallManager.swift` (`startCall(continuing:)` takes a
  call id); `OfferCenter.swift` and `BookedCall.swift` (`created_at`, `suggested_at`, `call_at`, no epochs, and no
  topic fallback of their own: `about()` goes); `RingStream.swift` (`expires_at` only); `SettingsSync.swift` (stops
  sending `language`); `Settings.bundle/Root.plist` and its strings (the Language and call model rows go; the settings sheet lists
both models from `GET /models`); `Server.swift`
  (`recordsURL` goes); `CallScreen.swift` (replaced by the home thread and the call screen); `BeeCallApp.swift` (the
  first run comes before the home).
- Removed: `CallRecordSheet.swift` and `RecordPagePolicy.swift`, the in-app web record sheet, with their tests.

**Web page, later** (`web/`): the records server can serve the same list and view to the page (with its cookie),
and the page can draw them instead of interpreting `events.jsonl`. `SessionPage.apply` and its helpers
(`addMessage`, `updateJob`, `beeWrite`, `renderWhy`, `renderPrep`, `withoutIds`) would go, and the drawing would
stay. Live updates would come from `/changes` or from polling the view. The debug view keeps the raw event stream.
`web/test/` would then test drawing from `examples/`. The "Between calls" page (`/api/meta`) is outside this
contract.
