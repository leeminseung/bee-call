# Bee Call

An AI that phones you about your day. It reads what your Bee wearable picked up, calls your iPhone, talks with you in English over AirPods, hands slow work (research, calculations) to a background agent, tells you the result in two or three sentences, and saves the facts and todos you agree to back into Bee.

Status: work in progress for the Amazon "Build, Ship, Shape" hackathon (Bee track).

## How it fits together

| Part | Where it runs | What it does |
|---|---|---|
| iPhone app (`ios/BeeCall`) | your iPhone | Rings and takes calls with CallKit, joins the LiveKit room, shows live captions and the session screen |
| Voice server (`voice/`) | this Mac | LiveKit Agents: Amazon Transcribe (speech to text), the coordinator model, Amazon Polly (text to speech), turn-taking and barge-in; records every call |
| Coordinator | Amazon Bedrock | Claude Sonnet 5.5, the voice you talk to (`voice/claude_llm.py`) |
| Worker (`worker/`) | AWS: Bedrock AgentCore Harness in Sydney | Background jobs, one folder per job; this Mac serves the harness's inline functions (progress, relay to the user, web search, saving result files) |
| Bee link (`beelink/`) | this Mac | Watches Bee for new conversations, saves raw context before a call, writes confirmed facts and todos after it |
| AI-started calls (`voice/outbound.py`, `voice/dispatch.py`, `beelink/planner.py`) | this Mac + LiveKit Cloud | Decides whether a Bee change is worth a call, writes `plan.md`, opens the call room and rings the app |
| Session screen (`web/`, `voice/session_server.py`) | this Mac | Live view of a call: transcript, why the AI called, job cards |
| Token server (`voice/token_server.py`) | this Mac | Hands LiveKit room tokens to the app, and tells an open app when the AI is calling |

Every call is kept as plain files under `workspace/<date>/<time>/`: `transcript.md`, `events.jsonl`, `audio/` (raw audio, never uploaded), `inputs/` (raw Bee data), `prep.md` (the call prep note), `jobs/`, `bee-queue.jsonl`. `workspace/` is not in git.

## Setup

Requirements: macOS, [uv](https://docs.astral.sh/uv/), Xcode 26 or later for the app, the [Bee CLI](https://www.npmjs.com/package/@beeai/cli) logged in to your Bee account, a [LiveKit Cloud](https://cloud.livekit.io) project, and an AWS account with Bedrock, Transcribe and Polly.

```bash
uv sync
brew install livekit-cli && lk cloud auth      # link your LiveKit project
lk app env -w                                   # writes LiveKit values into .env.local
aws configure --profile bee-call                # AWS keys stay in ~/.aws, never in this repo
python3 scripts/set_secret.py ELEVENLABS_API_KEY   # optional; input is hidden
```

`.env.local` holds `AWS_PROFILE`, `AWS_REGION` (Transcribe and Polly), `BEDROCK_REGION` (Claude) and the LiveKit values; see `.env.example`. Turn off "use of your content for service improvement" for AWS AI services before real calls, so Transcribe does not keep your voice.

## Run

```bash
uv run python -m voice.token_server                       # tokens for the app (port 8787)
COORDINATOR_LLM=claude uv run python -m voice.agent dev   # the voice server
uv run python voice/session_server.py                     # the session screen (port 8788)
uv run python -m voice.outbound                           # watch Bee and let the AI call when it is worth it
uv run python -m beelink.writer workspace/<date>/<time>   # after a call: dry run of Bee writes; add --apply to write
```

The voice server joins only rooms it is dispatched to, as the agent `bee-call`. A call the user starts gets it through the app's token. A call the AI starts gets it from `voice/dispatch.py`, with metadata that says why the AI is calling; the voice server then waits up to 60 seconds for the phone to answer. The phone rings in two ways: an open app hears the call on the token server's `/ring` stream, and a closed or locked one gets an Apple VoIP push (`voice/voip_push.py`). For the push, create an APNs key in the Apple Developer account (Certificates, Identifiers & Profiles, Keys, with Apple Push Notifications service), keep the `.p8` file outside the repository, and set `APNS_KEY_PATH`, `APNS_KEY_ID` and `APNS_TEAM_ID` in `.env.local`. The app registers its push token with the token server (`POST /voip`) whenever it reaches it, and shows "also while the app is closed" once that worked. On a push, the app reports the call to CallKit at once, as iOS requires for every VoIP push, and a ring that already rang or expired is reported and ended.

Before the AI picks up, it reads the user's Bee data and writes itself a call prep note (`voice/prep.py`). The voice server reads Bee all at once (today's context, recent conversations, unconfirmed facts, todo suggestions, the conversations behind the call), and the coordinator's own model starts from that: it writes its opening line first, may take one round of up to three more Bee reads (a conversation in full, a search, another day's summary), then writes the note. For a call the user starts, the caller hears ringing from the voice server meanwhile, and the AI picks up when its opening is ready, at most about 8 seconds in; the note usually lands during the opening, and the first reply waits up to 2 seconds for one still being written. The app shows the call as connected only when the voice server sets the participant attribute `bee_call.state` to `answered`. For a call the AI starts, `voice/outbound.py` prepares before the phone rings, so the user never waits. Without a note, the coordinator gets the plain Bee context instead. During the call it can read more with its `bee_today`, `bee_conversation` and `search_bee` tools. `voice/outbound.py` prepares with the model the voice server uses, so set `COORDINATOR_LLM` and `COORDINATOR_MODEL` the same way for both (GLM 5 when unset).

An ended call's record in the app has "Continue this call". The app asks the token server for a pass with that record's id (`/token?continue=<date>/<time>`), and the voice server starts the new call knowing what was said in the earlier one, what its background jobs found, and which jobs stopped unfinished when it ended (`voice/continuation.py`); the opening picks up from there. Nothing restarts by itself: a stopped job is only mentioned, and the user decides whether to run it again. The AI never calls back on its own to continue a call.

Whether a Bee change is worth a call is decided by a rule-based stand-in for now (`beelink/planner.py`): a conversation with at least four utterances that ended within the last hour, or a new voice note; never during another call; at least 30 minutes after the last call or the last question, whoever started it. Each decision, call or not, is written to `plan.md` in the Bee inbox folder. The watcher saves its Bee cursor only after a change was handled, so a call that could not be placed is retried on the next catch-up.

Between calls, a meta agent decides what to propose (`meta/agent.py`). It wakes only for events, which any process appends to `workspace/meta/events.jsonl`: a Bee change (from `voice/outbound.py`'s watcher), a call that ended (the voice server), the user's answer to an offer (the token server), and a booked call ten minutes away. It looks at what changed with read-only tools, `list_files`, `read_file` and `grep` over the workspace (never outside it, never push tokens or audio) and the Bee read tools, and then it may offer a call, rewrite its notes (`workspace/meta/state.md`: open threads, the agenda for the next call, what the user turned down, what they prefer), or do nothing. It only proposes: calls wait for the user's yes, and Bee changes go on the agenda for a call. Notifications are capped at four a day and held between 23:00 and 08:00. Every time it thinks, the meta session (`workspace/meta/session.jsonl`) keeps the events, every tool it used and its own words, and the session screen shows them as they are under "Between calls". Each call's prep reads the notes. `uv run python -m probes.meta_probe` shows how the model judges three made-up moments in a scratch workspace. `voice/outbound.py --rules` uses the old rule-based planner instead.

The AI never rings on its own: it asks first (`voice/offers.py`). A call decision becomes an offer, sent as a plain notification such as "About “Planned a Saturday hike”: free for a 5-minute call at 9:00 PM today?", with three buttons: at the suggested time, now, or not now. It shows on the lock screen and on Apple Watch, and the app's first screen lists open offers too. A button sends the answer to the token server (`POST /offers/<id>`) even while the app is closed, and `voice/outbound.py` places the call when the user tapped "now", when the accepted time comes (prepared 30 seconds early), or at a time the user booked out loud in a call (the coordinator's `book_call` tool; that call continues the call it was booked in). The suggested time is `CALL_TIME` (21:00 when unset; tomorrow when today's is less than ten minutes away). The app registers its notification token with `POST /push`. Try one offer without Bee events with `uv run python -m voice.outbound --once workspace/inbox/<folder> --any-age --no-cooldown`: it asks, then waits for the answer and places the call; `--direct` calls without asking (tests only).

`CALL_LANGUAGE` picks the call language: `en` (the default, and the demo) or `ko` (`voice/language.py`). It sets what Amazon Transcribe listens for, the Amazon Polly voice (Ruth or Seoyeon), the language the coordinator speaks and the prep's opening line; the rest of the instructions stay in English, and LiveKit's turn detector follows the transcript's language. One call is in one language, since Transcribe listens for one language per stream. Start `voice/outbound.py` with the same setting, and try Korean without a phone with `uv run python -m voice.fake_caller --language ko`.

`TTS` picks the AI's voice: `polly` (the default: Ruth, or Seoyeon for Korean) or `elevenlabs` (`voice/voices.py`). ElevenLabs needs `ELEVENLABS_API_KEY` with the Text to Speech permission and a voice from the account's My Voices list (`ELEVENLABS_VOICE`, a voice id, overrides the language's default). Each call language has a default ElevenLabs voice (Sarah for English, Sian for Korean, `voice/language.py`), and `ELEVENLABS_MODEL` is `eleven_v4_turbo` when unset: the most expressive of ElevenLabs' fast models and, measured from Seoul, about as quick to its first sound as `eleven_flash_v2_5` (0.15-0.24 s against Polly's 0.8-1.1 s). `ELEVENLABS_STABILITY` (0 to 1) makes the voice livelier when lower. To compare voices, `uv run python -m probes.voice_samples` has real call lines read by the Polly voices and your ElevenLabs voices, on the path a call uses, and writes a page to listen to them side by side with each sample's time to first sound (`--list` shows your ElevenLabs voices, with the Voices read permission).

`COORDINATOR_LLM` picks what answers in the coordinator slot: `claude` (the product), or one of three test stand-ins:

- `fake`: the real Claude adapter with scripted replies and no network. It checks every request against the Messages API rules and checks that earlier turns were never edited, so you can test barge-in, tools and job results without Bedrock access. Say "test job" to start a background job.
- `nova`: Amazon Nova 2 Lite, a model that can call tools.
- `echo`: repeats you; say "test job" to exercise the background job flow.

In `claude` and `fake` mode each call also saves `claude-history.json`, exactly what was sent to the model.

`WORKER` picks what runs delegated jobs: `harness` is the AgentCore Harness worker (`worker/harness.py`; create it once with `uv run python scripts/deploy_worker.py --smoke`), `search` answers each job with one Amazon Nova Web Grounding search (`worker/web_search.py`), and `fake` (the default) pretends to work for a few seconds. The last two are test stand-ins. A web search costs about 0.03 USD; try one on its own with `uv run python -m worker.web_search "your question"`.

The harness runs the agent loop in AWS; when the agent calls one of its inline functions, the harness stops and hands the call to this Mac, which runs it and sends the result back in the same session. Result files go straight into the job folder on this Mac through `save_file` (text) and `save_chart` (numbers, drawn here as SVG), so the job folder has one writer. The coordinator hears about a job the way Claude Code tells its model about a background task: `delegate` returns a job number at once, and the job's news arrives later as a `<job-notification>` block (job number, status, task, and a summary, an error or the files it made). The answer the worker relays comes while the job still runs, so the user hears it before the report is written, and the job's end follows with its files. However a job ends, the user hears about it: a job that never relayed anything ends with its summary, and a failed job with its error. The coordinator can also check on running jobs with its `job_status` tool. Until Claude on Bedrock is available for the account, the harness uses GLM 5 (`WORKER_MODEL` or `--model` changes it).

### Testing without a phone

```bash
uv run python -m voice.fake_caller                        # Polly plays a caller into a new room
uv run python -m voice.fake_caller --outbound workspace/inbox/<folder>   # the AI calls about that Bee change; the fake caller answers
uv run python -m voice.fake_caller --line "Hello" --line "Please start a test job."
# per-line pauses: cut the AI off after 3 s, pause 0.7 s mid-thought, then wait 10 s
uv run python -m voice.fake_caller --line "Hi there" --line "Wait, one thing." --line "I got an offer," --line "and I'm not sure." --gap 3,10,0.7,10
uv run pytest
```

### iPhone app

Open `ios/BeeCall/BeeCall.xcodeproj`, pick your team under Signing, run on a device on the same Wi-Fi as the Mac, and set the token server URL in the app to `http://<Mac LAN IP>:8787/token`.

## Privacy

Raw call audio stays on this Mac. Transcripts and job files are the only things meant for cloud storage. The session screen serves transcripts and job files to your local network so the phone can show them, but never raw audio (`audio/`) or raw Bee data (`inputs/`); it has no login, so run it only on a network you trust. Bee data is read through the official Bee CLI, and nothing is written to Bee unless you agreed to it out loud during the call and the writer runs with `--apply`.

## License

MIT. See `LICENSE`.
