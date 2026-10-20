# Between calls

Between calls, `voice/outbound.py` runs three loops for each approved person. They are the Bee watcher for people who use Bee ([bee.md](bee.md)), the meta agent, and the offers together with the calls the user agreed to. Start it next to the voice server and the token server with `COORDINATOR_LLM=anthropic uv run python -m voice.outbound`.

## The meta agent

Each person has a meta agent (`meta/agent.py`) that decides what to propose between calls. It sees only that person's folder and their Bee. It wakes only for events, which any process appends to `data/people/<id>/meta/events.jsonl`.

| Event | Comes from |
|---|---|
| `bee_change` | the Bee watcher, when Bee has something new |
| `call_ended` | the voice server, when a call ends |
| `call_missed`, `call_declined` | the voice server, when the user missed or declined an AI call while it rang |
| `offer_answered` | the token server, when the user answers an offer |
| `booked_soon` | `voice/outbound.py`, ten minutes before a booked call |
| `job_ended` | the voice server, when a background job ends after its call |
| `organize_bee` | the voice server, when the user said yes in a call to organizing the past days in Bee |
| `bee_disconnected` | the token server, when the person turns Bee off |

When it wakes, the meta agent looks at what changed with read-only tools. `list_files`, `read_file` and `grep` work over the person's folder, never outside it, and never on push tokens or audio. People who use Bee also get the Bee read tools. Then it may offer a call (`offer_call`), book one the user asked for (`book_call`), rewrite its notes, or do nothing.

Its notes are `meta/state.md` in the person's folder. They hold open threads, the agenda for the next call, what the user turned down, what they prefer, what Bee keeps that is worth bringing up, and what is worth getting to know about the user next. Each call's prep reads them first. The notes never call the user he or she, also in the lines kept from earlier notes.

The meta agent only proposes. Calls wait for the user's yes, and Bee changes go on the agenda for a call. The one exception is an organize run the user said yes to in a call, which only adds to Bee ([bee.md](bee.md)).

One think is one conversation with the model catalog's meta role, Claude Sonnet 5.5 with GLM 5 on Bedrock standing by, with at most 8 model requests. Every think goes into the meta session (`meta/session.jsonl`), with the events it took, every tool it used and its own words. The session screen shows them as they are on its "Between calls" page. The inbox moves on only after a think that worked. A think that failed, for example because the model was unreachable, is tried again with the same events after 30 seconds, then 1, 2 and 5 minutes, then every 15 minutes, and is never given up.

`uv run python -m probes.meta_probe` shows how the model judges three made-up moments in a scratch workspace. `META_AGENT=rules` runs the meta agent without a model, for tests ([README, Testing from outside](../README.md#testing-from-outside)).

## What the meta agent sees of Bee

The meta agent sees each new voice memo whole, as Bee has it when it thinks (`bee journals get`, the finished text and Bee's reply to it). For each changed conversation it sees the lines spoken since the last change that showed it that conversation (`bee conversations get`), with their times. Bee names the wearer and marks other speakers "Unknown", though it sometimes marks the wearer "Unknown" too. Lines Bee heard during a Bee Call call are left out, since the watch records through the calls. A note recorded with Start and Stop in the Apple Watch Bee app arrives as a conversation, since that app has no voice memos.

## Calls the user asks for

Whatever the input, a voice memo or a conversation, the user asking Bee Call to call them counts as their yes. Whether an input asks for a call is the model's reading, guided by its instructions. A request counts when the wearer asks the assistant to call them. Words said to another person ("나중에 전화해" to a friend) do not count, and neither does a time mentioned without asking.

For a call at a time ("오늘 밤 9시에 전화해서 같이 정리하자", or "call me at 9 tonight and we'll sort it out"), the meta agent books it with `book_call`. It gives the user's words for the call, which are kept with the offer and never checked. For a conversation, it also gives the line that asks, by the number it was shown with (L1, L2, and so on). The code holds a booking to these rules.

- The named line must have been shown to the agent, have a time within the last 12 hours, and not be a line Bee marks "Unknown". Such a line is not taken as the user's request, even when it may be their words, and the agent is told to book nothing and offer nothing because of it.
- The call's time must fall within the coming week.
- One memo or conversation books at most one call.
- Nothing is booked from an input recorded more than 12 hours before, from one Bee gives no recording time for, or from Bee's recording of a Bee Call call.
- An input asking for a time within 10 minutes of a call already booked joins that call, which then reads it too, with no second notification.

The person gets one plain notification ("I'll call you at 9:00 PM: <topic>. You can call it off in the app."), which quiet hours do not hold. A booked call is pinned on the app's home screen, where the user can call it off. The booked call opens from the input that asked for it, so the prep reads the memo and Bee's reply, or the conversation, first. For a call now or soon, the meta agent offers one with `offer_call` instead.

## Offers

The AI never rings on its own. It asks first (`voice/offers.py`), and it asks about one Bee conversation at most once, also after the user declined. A decision to call becomes an offer, sent as a plain notification such as "About “Planned a Saturday hike”: free for a quick call at 9:00 PM today?". It has three buttons, "At the suggested time", "Call me now" and "Not now". The notification shows on the lock screen and on Apple Watch, and the app's home screen shows open offers too. A button sends the answer to the token server (`POST /offers/<id>`) even while the app is closed. The app registers its notification token with `POST /push`.

`voice/outbound.py` places the call in three cases.

- The user tapped "Call me now" on the notification, or "Now" on the offer in the app.
- The accepted time came. The call is prepared 30 seconds early, so it rings on time.
- A time the user booked out loud in a call came. The call AI's `book_call` is meant only for a call the user asked for or agreed to, which is the call AI's judgment. The user's words for it are kept in the record, a time like "in ten minutes" counts from when it was said, and the AI reads the booked time back. That call continues the call it was booked in.

The suggested time is the person's `call_time` setting, 21:00 by default, or tomorrow's when today's is less than ten minutes away. A call the user said yes to can be called off (`POST /offers/<id>` with `decline`) until its room is being made. The offer is checked once more right before that, and nothing rings for a call that was called off.

Offer notifications follow these limits.

- None go out during a call.
- They wait 30 minutes after a call ends (answered, missed or declined) and 30 minutes after the last offer notification.
- They are held between 23:00 and 08:00 in the person's time zone. A held offer shows in the app's list at once, and its notification goes out once it may.
- At most four go out a day. A fifth offer shows only in the app's list.

Whenever a call the user agreed to cannot be placed, because the day's budget is used up, it is more than 10 minutes late, or placing it failed, a plain notification without buttons says so, once, in the person's language. For example, "Couldn't place your 9:00 PM call: today's budget is used up."

To make an offer by hand, run `uv run python -m voice.offers ask <id> --topic "..." --reason "..."` ([README, Make the first call](../README.md#make-the-first-call)). To replay one saved Bee change, run `uv run python -m voice.outbound --once data/people/<id>/inbox/<folder> --person <id>`. The meta agent thinks about that change once, and for any offer it makes, the command waits for the answer and places the call.

## When Bee is turned off

When the person turns Bee off (`POST /bee/disconnect` while Bee was on, or `voice.people set <id> bee false`), the switch is marked in `meta/bee-off.json` before Bee is off, and the meta agent is woken with `bee_disconnected`. Its notes may hold what came from Bee, such as facts, Bee ids, insights and what Bee recorded. Until it has rewritten them without that, no call's prep gets them, and each think until then is told to rewrite them. This is the one time a person without Bee is told about Bee. The person's loops, started again with Bee off, also think once as they start, in case the loop that ran while Bee was on took the event.

From the switch on, the meta agent's file tools also hide what came from Bee in the person's folder. That covers `inbox/`, `bee-writes.jsonl`, each call's `prep.md` and `events.jsonl`, and the agent's own inbox and session, while the calls' transcripts and jobs stay visible. In its own inbox and session it sees only its entries since the switch until the notes are rewritten, and afterwards only those after the think that rewrote them. A Bee change from before the switch is not looked at, nor is the switch event itself once Bee is connected again.

## The rule-based planner

`beelink/planner.py` is an older rule-based stand-in for the meta agent. It offers a call for a conversation with at least four utterances that ended within the last hour, or for a new voice note. It never offers during another call, and waits at least 30 minutes after the last call or the last question.

Two commands use it. `uv run python -m voice.outbound --rules` lets it decide instead of the meta agent, and writes each decision to `plan.md` in the inbox folder, call or not. With `--once <folder>` it decides about one saved Bee change, `--any-age` and `--no-cooldown` relax its age and spacing rules, and `--dry-run` writes plans but never asks or calls. `uv run python -m voice.fake_caller --outbound data/people/<id>/inbox/<folder>` lets the AI start a test call about a saved Bee change when the planner says yes. No phone rings, and the fake caller answers.
