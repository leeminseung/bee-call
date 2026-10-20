# People, data and privacy

## People and sign-in

One server serves several people (`voice/people.py`). Every request, call and tool works inside one person's folder, so one person's calls, Bee data and notes never reach another person's AI or screen.

The app signs in with Apple (`SignInView.swift` and `Server.swift` in `ios/BeeCall/BeeCall/`). The token server checks the identity token (`voice/apple_signin.py`) and answers with its own sign-in token, which the app keeps in the Keychain and sends with every request. A sign-in lasts 180 days. After that the app signs out and asks the person to sign in with Apple again. A new person sees a six-character code and waits until whoever runs the server approves it.

```bash
uv run python -m voice.people waiting                   # sign-ins waiting, with their codes
uv run python -m voice.people approve 7F3C2A --as sam   # refused if sam is, or was, someone's id
uv run python -m voice.people list                      # approved people and their settings
uv run python -m voice.people set alex daily_budget_usd 2
uv run python -m voice.people remove alex               # takes access away, and the folder stays
uv run python -m voice.people session alex              # prints a new sign-in token, for tests and support
```

Add `--same-person` to `approve` for a second Apple id of the same person.

A phone's push tokens belong to the person signed in on it (`data/devices.json`, one owner per token). Signing out takes them away, another person who signs in on that phone takes them over, and `voice.people remove` drops them.

### Working on a Mac

On a Mac, `uv run python -m voice.people add alex` approves a person for use from the Mac alone, without Apple sign-in. With `BEE_CALL_DEV_PERSON=alex` in `.env.local`, requests made on the Mac itself act as that person when they carry the header `X-Bee-Call-Dev: 1`. A web page in a browser cannot send that header to another site without asking first, and the token server never says yes. The session screen only reads, so it lets its own page on the Mac in without the header. It does not read `.env.local`, so start it with the setting on its command line (`BEE_CALL_DEV_PERSON=alex uv run python -m voice.session_server`). Elsewhere, the session screen asks for a person's sign-in token, as a Bearer header or a `bee_call_session` cookie, and `voice.people session <id>` prints a new one. The setting is ignored on a server, where `BEE_CALL_PUBLIC_HOST` is set, and the deploy script never copies it there.

## Settings

Each person's `settings.json` holds their settings. The app changes all of them except `bee` and `daily_budget_usd` with `PUT /settings`. `bee` follows connecting and disconnecting Bee ([bee.md](bee.md)), and `voice.people set` changes any of them on the server.

| Setting | Default | What it is |
|---|---|---|
| `language` | `en` | the call language, `en` or `ko` ([calls.md](calls.md)) |
| `voice` | null | a voice catalog id of the call language, or null for its default |
| `call_model`, `worker_model` | null | the person's picks for the call AI and background jobs, or null for the catalog's defaults ([models-and-jobs.md](models-and-jobs.md)) |
| `noise_cancellation` | on | the noise filter on the person's audio in calls |
| `call_time` | `21:00` | the evening time an offer suggests |
| `record_on_server` | on | keep each call's raw audio in its folder |
| `record_on_phone` | off | the app records each call on the iPhone itself |
| `bee` | off | Bee is connected |
| `daily_budget_usd` | 3 | the day's spending limit, in USD |
| `time_zone` | `Asia/Seoul` | an IANA time zone name, which the app sends from the phone |

A person's times follow their `time_zone`. That covers the suggested call time, quiet hours, the day's notifications and budget, call folder dates, booked times, and the time the AI is told.

## The daily budget

Each person's day has a budget (`daily_budget_usd`, 3 USD by default). The spend is an estimate from the calls' minutes and model use, the background jobs and the meta agent (`voice/costs.py`). A spend equal to the budget counts as past it. Past the budget, the app's new calls are refused, and the voice server checks again when a call the user started begins. Agreed AI calls are not placed, background jobs do not start, and the meta agent waits for the next day. The budget is checked only before a call starts, so a call in progress is never cut. When a call the user agreed to cannot be placed, one plain notification says so ([between-calls.md](between-calls.md)).

## The data folder

Everything a server keeps is under its data root, `data/` in the folder it starts in, or `BEE_CALL_DATA`. Git ignores `data/`.

```
data/
  people/<id>/               one person's folder
    settings.json            their settings
    <date>/<time>/           one call's record, named by the person's clock when it began
    inbox/<date-time>/       a Bee change the watcher saved (changed.json)
    meta/                    the meta agent's inbox, session and notes
    state/                   offers, ring lines, pushes, the Bee cursor and model use outside calls
  bee/<id>/                  their Bee CLI login, once they connect Bee
  accounts.json              the Apple sign-in ids of approved and waiting people
  sessions.json              the app's sign-in tokens, kept only as hashes
  devices.json               each phone's push tokens, with their owner
  harness.json               the AgentCore harness for background jobs
```

Every call is kept as plain files in its folder.

| File | What it holds |
|---|---|
| `transcript.md` | what both sides said, with times and interruptions |
| `events.jsonl` | everything that happened, one JSON object per line ([README, Testing from outside](../README.md#testing-from-outside)) |
| `audio/` | the raw audio of both sides, only while the person keeps audio on the server |
| `inputs/` | the raw Bee data read for the call |
| `prep.md` | the call prep note |
| `jobs/` | one folder per background job ([models-and-jobs.md](models-and-jobs.md)) |
| `bee-writes.jsonl` | each Bee write, with the question that named Bee and the user's answer ([bee.md](bee.md)) |
| `usage.json` | what the call used, for the budget |
| `model-log.jsonl` | everything the call AI's model was given, never served |
| `claude-history.json` | exactly what was sent to Claude, never served |

A second record in the same minute gets the folder `<time>-2`. The person's Bee login sits outside their folder, in `data/bee/<id>/`, where the meta agent's file tools cannot reach it.

## What the app reads

The app draws what the token server builds. `contract/app-contract.md` names every endpoint and field the app uses, with a JSON Schema for each body and an example of each in `contract/` (`uv run -q python contract/check.py` checks them). The server tests write `contract/examples/` from the recorded calls in `tests/fixtures/sessions/`, so the examples are what the code answers.

`voice/call_view.py` is the one place that turns a person's call records into what the app shows.

- The call list (`GET /calls`), newest first, with what each folded call shows.
- One call as its thread (`GET /calls/<call_id>`), with the speech as bubbles, job and Bee write cards where they happened, why the AI called, and what it looked at in Bee before it picked up. A call's id is its LiveKit room.
- A call's files (`GET /calls/<call_id>/files/<path>`), by the session screen's rule, which never serves audio, raw Bee data or what the model was given.

The list, a call and the offers carry a version as their ETag and answer 304 when nothing changed. `GET /changes` (Server-Sent Events) says which of them changed, so an open app fetches only that. `GET /voices?language=ko` lists the voices to pick from.

While a call runs, its record shows signs of life every 30 seconds, so a quiet call never reads as dropped. After the call, the app shows its one-line title, and the charts and report pages of its jobs ([models-and-jobs.md](models-and-jobs.md)).

## Privacy

- Each person sees only their own calls. The token server and the session screen work in the signed-in person's folder, the voice server records into it, and the meta agent and Bee reads stay inside it.
- Raw call audio is kept in the call's folder while the person's `record_on_server` setting is on, which is the default. The setting is read again when the phone answers. With it off, or when the person's settings file cannot be read, only the transcript and the record are kept.
- With Save on this iPhone on (`record_on_phone`), the app records the call itself with LiveKit's `AudioMixRecorder`, the microphone and the AI's voice in one file. The recording goes into the Files app under On My iPhone, Bee Call, and never to the server.
- LiveKit's own session recording is off in code (`record=False`), whatever the LiveKit project's setting. Every call job tells LiveKit so as it starts, so a call that never gets a session (missed, declined, refused over budget, hung up while it rang) has none of its logs uploaded either.
- The session screen and the token server never serve raw audio (`audio/`), raw Bee data (`inputs/`) or what the model was given (`model-log.jsonl`, `claude-history.json`).
- Bee data is read through the official Bee CLI, with each person's own login.
- Nothing is written to Bee without the user's yes. That is a clear yes to a question that named the item, a clear request for the change, or a yes to the one question that offers to put what came up into Bee. After that yes, the AI adds the call's new items, or the meta agent adds what is worth keeping from the past few days, up to 15 items. The AI says what it wrote, and the user can take back any change made in the call ([bee.md](bee.md)).
- What the agents read in Bee, in call records and in a job's results is information to them, never instructions. Bee records other people's words, and a job reads web pages.
- Before real calls, turn off "use of your content for service improvement" for AWS AI services, so Amazon Transcribe does not keep the callers' voices.
