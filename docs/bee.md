# Reading and writing Bee

Bee Call reaches Bee only through the official Bee CLI ([`@beeai/cli`](https://www.npmjs.com/package/@beeai/cli)), with each person's own Bee login. `beelink/cli.py` wraps the CLI. The server pins version 0.7.3 (`deploy/install.sh`).

## Connecting Bee

Each person connects their own Bee account. They turn on Use Bee on the Bee Call page of the iOS Settings app and open Bee Call. The app asks the token server to connect (`POST /bee/connect`), which runs `bee login --no-wait` in the person's own Bee folder, `data/bee/<id>/`, and returns Bee's approval link. The app opens it, and the person approves in the Bee app. The token server waits about five minutes for the approval, also across its own restart (`data/bee/<id>/pending.json`). A second connect stops the first one's wait before it starts. On a Mac without the app, `curl -X POST http://localhost:8787/bee/connect -H 'X-Bee-Call-Dev: 1'` does the same for the person named by `BEE_CALL_DEV_PERSON`.

The login stays in `data/bee/<id>/`, outside the person's folder, where the meta agent's file tools cannot reach it. Every Bee read and write runs the Bee CLI with `--json` in that folder (`BEE_CONFIG_DIR`, with `BEE_FORCE_FILE_STORE=1`). A Bee call made without a chosen login fails, so it cannot land in the wrong person's account.

Turning Use Bee off sends `POST /bee/disconnect`. The server runs `bee logout` and removes the person's Bee login, also when `bee logout` fails, and answers 204 only once the login is gone. `uv run python -m voice.people set <id> bee false` turns Bee off from the server's side. Either way, the meta agent then rewrites its notes without what came from Bee ([between-calls.md](between-calls.md)).

## Using Bee Call without Bee

Use Bee is off until a person connects Bee, and Bee Call works without it. Such a person's calls never touch Bee. Nothing is read from Bee before or during a call, the call AI has no Bee tools, and nothing the call AI, the prep or the meta agent is told mentions Bee. The AI asks about their day and works from their earlier calls and the meta agent's notes.

## Watching Bee

For each person who uses Bee, `voice/outbound.py` runs a watcher (`beelink/watcher.py`). It follows `bee stream` for new and updated conversations and voice memos. `bee stream` delivers each event at most once and cannot replay, so after every burst of events, and on every reconnect, the watcher also asks `bee changed --cursor <last>` for everything since the last catch-up.

A catch-up starts 20 seconds after the last event. Each piece of a voice memo starts that wait over, so the catch-up sees the finished memo. A conversation Bee keeps updating waits at most 3 minutes from its first event. Each new batch is saved in the person's folder as `inbox/<date-time>/changed.json`, and a `bee_change` event wakes the meta agent. The watcher saves its cursor only after a batch was handled, so a batch that could not be handled comes back on the next catch-up.

## Reading Bee

The call AI, the meta agent and the call prep read Bee with one set of tools (`beelink/toolkit.py`), with the same names, descriptions and parameters for all three. Each tool is one Bee CLI read with Bee's own parameters, and it answers with Bee's JSON and Bee's fields. A time may be given as the person's local time in ISO 8601, or as Bee's epoch milliseconds.

| Tool | What it reads |
|---|---|
| `bee_me` | the user's Bee profile, with their name and time zone |
| `bee_today` | what Bee has for today, including the daily summary once Bee has written it, the open to-dos, today's voice memos, conversations and calendar events |
| `bee_conversations` | the conversations Bee recorded, newest first |
| `bee_conversation` | one conversation's times, state and summaries |
| `bee_transcript` | the lines of one conversation, or one part of a long recording by `since` and `until` |
| `bee_related` | the conversations Bee finds related to one conversation |
| `bee_search` | a keyword search over conversations, daily summaries and facts (with `filter` and `sort`), or a neural search of conversations, bounded by `since` and `until` |
| `bee_daily` | the daily summary of one date, or of the latest days |
| `bee_facts` | confirmed or unconfirmed facts, a keyword `query` over both, or one fact by `fact_id` |
| `bee_todos` | open, done or suggested to-dos, or one to-do by `todo_id` |
| `bee_voice_notes` | the voice memos the user left on purpose, each with Bee's reply |
| `bee_insights` | Bee's observations about the user's patterns |

Only three things change what Bee gave, and the answer says so.

- What Bee recorded during one of Bee Call's calls is left out, with a count (next section).
- Locations, the email brief and photos are dropped.
- A long answer is cut, with a note on how to get the rest (the next page's cursor, a `since` or `until`, or the ids to read one by one). Lists inside an answer, such as `bee_today`'s, are cut from their ends the same way.

A read in a call waits at most 4 seconds and answers in about 3,000 characters at most. The prep's reads get 4 seconds and 2,500 characters, and the meta agent's get 15 seconds and 8,000. The background agent never sees Bee.

Every agent that reads Bee gets the same text about it in its instructions (`WHAT_BEE_KNOWS` and `READING_BEE` in `beelink/toolkit.py`), with one sentence each on how much to read. The text says what Bee keeps and how to read it. It also says that what the agent reads in Bee, in call records and in a background job's results is information and never an instruction, since Bee records other people's words and a job reads web pages.

## Leaving out Bee Call's own calls

The wearable also hears the user talking to Bee Call. For those times the call's own record is the source, so what Bee made of a call is left out (`beelink/calls.py`). Every reader applies this rule, which means the watcher's batches, a call's Bee inputs, and the reads of the call AI, the prep and the meta agent.

- The read tools leave out a conversation recorded within a call (with a minute's margin). A longer recording that runs through a call is kept without its lines from the call.
- The watcher's batches are coarser. They leave out any conversation that overlaps a call.
- The facts and suggested to-dos Bee made from a minute before a call to ten minutes after it are left out. Bee does not say which conversation a fact came from, so a fact from another conversation in that time is left out too.
- A voice memo recorded during a call is left out. One recorded right after a call is the user's own and is kept.
- Facts and to-dos that a call wrote on the user's yes are kept, and so are the user's own to-dos.

## Writing to Bee during a call

Bee is the user's memory, and a call keeps it accurate and tidy. The rules live in the Bee part of the call AI's instructions (`voice/coordinator.py`), and `beelink/writer.py` carries out the writes. The call AI changes one item with `save_to_bee`.

| Kind | What it does |
|---|---|
| `fact_create` | keeps a new fact, confirmed at once, since the yes was to keeping it |
| `fact_confirm` | confirms a fact Bee heard right |
| `fact_update` | fixes a fact Bee has wrong |
| `fact_delete` | takes a wrong fact out |
| `todo_create` | adds a to-do, with an optional alarm |
| `todo_accept` | takes a to-do Bee suggested |
| `todo_dismiss` | turns down a to-do Bee suggested |
| `todo_complete` | marks a to-do done |

### When it asks and when it acts

New things that come up in a call, such as something lasting the user says about themselves or something they mean to do, are not offered as they come up. The call AI keeps them in mind. Once the user's topic is done, at a natural pause or as the call winds down, it asks once whether to put what came up in Bee ([Organizing what came up](#organizing-what-came-up)).

One item at a time is only for what Bee already has, and for a change the user asks for. When the user asks for a change in Bee and it is clear what they mean, that request is their yes. This holds for any change, whether putting one thing in, taking or turning down a suggestion, marking a to-do done, or fixing or deleting a fact, and whether the user names the item or points back at it. The AI makes the change at once and says what changed, as Bee has it. It asks first, saying the item, only when it is unclear what the user means, or when the change is its own idea.

A call asks at most two Bee questions, so Bee never takes over the call. One is the question near the end that offers what came up, and the other checks one thing Bee already has. More checks come only when the user brings Bee up. The AI never asks twice about the same item, and offering to fix what the user just corrected counts as that check. A call where nothing lasting comes up and nothing needs checking has no Bee question at all. When to offer which is a principle with examples in the instructions.

Whether an answer is a clear yes, and not a hedge, a "later", a yes taken back or a yes to another question, is the AI's judgment from its instructions. No word list in code decides it. The code keeps only what needs no reading of meaning. That is the kind and the ids of each change, what Bee had under an id, one write of the same item per call, and the exchange the change was made on. The AI calls `save_to_bee` once, after the user's clear yes.

### How items are written

Every item is written in English, Bee's language, whatever the call's language. It reads the way Bee writes its own, with no subject and no pronoun for the user ("Wants to keep own judgment when using AI", never "He wants..." or "The user wants..."), so no gender is guessed into the user's memory. The meta agent writes its items the same way. The AI says the item in the question, or in the sentence just before it, in the call's language ("Bee에 'Wants a quiet home server'로 기억해 둘까요?"). An alarm given without a time zone is in the person's own time.

A change to something Bee has (deleting a fact, marking a to-do done, turning down a suggestion) goes by its id. Bee is read first, and the write keeps what the item was. If the AI named the item differently, the tool's answer says what Bee had.

Every step is kept in the call's `bee-writes.jsonl`, with the AI's last turn before the user's last words, and those words, verbatim. The same item is written once a call.

## Organizing what came up

The new facts and to-dos of a call go into Bee together. Near the end of the call, or when the user wants Bee tidied, the call AI asks once, in one question that names Bee and lists no items ("오늘 나온 것 몇 개 Bee에 넣어 둘까요?", "A few things from today are worth keeping. Shall I put them in Bee?"). It asks the same way when only one thing came up.

After a clear yes, `organize_bee` adds what came up in the call at once. Facts go in as confirmed facts (`bee facts create`, then confirmed when Bee made one unconfirmed), and to-dos as open to-dos. Each is kept in the call's `bee-writes.jsonl` with the question and the answer, so the app shows each as a Bee write card and the notification after the call lists them. The code allows one organize of each kind per answer of the user, and at most 8 items of under 200 characters each. Nothing the call already put in Bee is added again.

To leave out what Bee already has, the call AI's context lists Bee's confirmed and unconfirmed facts, open to-dos and the to-dos Bee suggests, each with its Bee id. These are the newest 30, 15, 30 and 15 (`what_bee_has` in `voice/prep.py`). The voice server reads the four lists at call setup, all at once and alongside the prep, within the prep's 20 seconds, so the pickup never waits for them. Like every read, they leave out what Bee made of Bee Call's own calls. The list sits outside the fixed part of the instructions, so the cached prompt prefix stays the same. A list read late or not at all is left out, and the AI is told so in one line. Whether an item is already there, even in other words or with more detail, is the AI's judgment from the list. An unconfirmed fact said again is left out too, since adding it again would not confirm it. No code compares the texts.

With scope `recent` (1 to 7 past days, 3 when not said), the voice server hands the request to the meta agent when the call ends, as an `organize_bee` event in its inbox. In that run only, the meta agent has `bee_add_fact` and `bee_add_todo`, and it offers or books no call. It reads those days in Bee and adds what is worth keeping that Bee does not have yet. The code checks each add. It must be one short statement, at most 15 go in per run, and none may repeat Bee's facts (confirmed or not) or to-dos (open or done) in the same words. Each add is kept in `meta/bee-writes.jsonl` with the question and the answer, and counts as the user's own in every later read. The run ends with one plain notification in the person's language, counted from the adds that went in ("Added 3 memories to Bee. You can see and edit them in the Bee app."). Quiet hours do not hold it, and its entry shows on the session screen's "Between calls" page. A request whose run fails comes back with its inbox's other events, but is done only once. After three failed runs it is given up, with one notification that says so and how many items went in before, and the rest of the inbox goes on.

Organizing only adds. Deleting, merging or rewriting stays one item at a time after a yes to that item, or in the Bee app.

## Undo

Every tool answer that reports a change names its key, such as "w3". When the user says "take it back", the call AI gives `undo_bee_write` the key of the change they mean. The code only checks that the key is one of the call's changes and in a state that can be taken back.

- A missing or unknown key gets back the call's changes and their keys, so the AI can call again.
- A change already taken back says so.
- Several keys are taken back in the order given.
- A change still being saved is waited for, within the call's 15 seconds, and then taken back.

An item that was added comes out. Bee has no undelete, so a deleted fact is made again with the same text, confirmed. It gets a new id, which the tool's answer gives the AI for any later change. A dismissed suggestion comes back as an open to-do. Each bring-back is a write of its own with its own key (`fact_create` or `todo_create`, its item saying which write it restores), so the app shows it as what went in and later reads keep it as the user's. The removal keeps its own record and is not brought back twice. Taking back what came back takes it out again. A write that failed is withdrawn, so it is not tried again after the call. A failed save or bring-back is said with its key, along with the fact that it will be tried again after the call unless the user takes it back.

Take-backs reach Bee one at a time.

- A take-back Bee refused can be tried again, after Bee is asked whether it still has the item, by its id first. Each read, and the whole check, is held to the 15 seconds the call waits on Bee. A take-back Bee carried out but answered with an error is found done.
- A take-back Bee did not answer (`undo_unknown`) is never tried again by itself, like a write Bee did not answer. The AI says to check Bee.
- A take-back that cannot be worked out, because Bee did not say which item it made or a fact's earlier text was not read, is `undo_impossible`. It is final, its change stands, and the AI says to change it in the Bee app.

## When a write fails

A write that ends with an error is said in the call and tried once more after it. The Bee CLI also reports an error when Bee stored a to-do and only its answer was lost, so before a to-do is written again, Bee is asked whether it has it already. A to-do Bee cannot be asked about is not written again. A write Bee may have taken without saying so (it did not answer in time, or it was cut off mid-write) is never written again by itself, so no to-do is made twice. The AI says to check Bee, or the notification does when the call has already ended.

`uv run python -m beelink.writer data/people/<id>/<date>/<time>` lists a call's Bee writes and how they went. `--apply` tries the failed ones again.

## After the call

One notification after the call says how each Bee item the call touched ends it, following a bring-back to its new id. An item ends as added, confirmed, changed, marked done, removed, brought back (removed and made again, in one line), not done, or to check (a write or a take-back Bee did not answer). An item brought back and removed again ends removed. One whose take-back cannot be done here stays in Bee as it is.

What Bee keeps reaches the next call in two ways. The meta agent's notes hold a kept fact the next topic may touch, and the unconfirmed facts and suggestions to check, with their Bee ids. The call AI also reads Bee itself. It mentions a kept fact when it connects to the topic, never as a list.
