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

Every call is kept as plain files under `workspace/<date>/<time>/`: `transcript.md`, `events.jsonl`, `audio/` (raw audio, never uploaded), `inputs/` (raw Bee data), `jobs/`, `bee-queue.jsonl`. `workspace/` is not in git.

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

The voice server joins only rooms it is dispatched to, as the agent `bee-call`. A call the user starts gets it through the app's token. A call the AI starts gets it from `voice/dispatch.py`, with metadata that says why the AI is calling; the voice server then waits up to 60 seconds for the phone to answer. Until VoIP push is set up (it needs a paid Apple Developer account), an open app hears about the AI's call on the token server's `/ring` stream.

Whether a Bee change is worth a call is decided by a rule-based stand-in for now (`beelink/planner.py`): a conversation with at least four utterances that ended within the last hour, or a new voice note; never during another call; at least 30 minutes after the last call, whoever started it. Each decision, call or not, is written to `plan.md` in the Bee inbox folder. The watcher saves its Bee cursor only after a change was handled, so a call that could not be placed is retried on the next catch-up. Try one decision without Bee events with `uv run python -m voice.outbound --once workspace/inbox/<folder> --dry-run --any-age`.

`COORDINATOR_LLM` picks what answers in the coordinator slot: `claude` (the product), or one of three test stand-ins:

- `fake`: the real Claude adapter with scripted replies and no network. It checks every request against the Messages API rules and checks that earlier turns were never edited, so you can test barge-in, tools and job results without Bedrock access. Say "test job" to start a background job.
- `nova`: Amazon Nova 2 Lite, a model that can call tools.
- `echo`: repeats you; say "test job" to exercise the background job flow.

In `claude` and `fake` mode each call also saves `claude-history.json`, exactly what was sent to the model.

`WORKER` picks what runs delegated jobs: `harness` is the AgentCore Harness worker (`worker/harness.py`; create it once with `uv run python scripts/deploy_worker.py --smoke`), `search` answers each job with one Amazon Nova Web Grounding search (`worker/web_search.py`), and `fake` (the default) pretends to work for a few seconds. The last two are test stand-ins. A web search costs about 0.03 USD; try one on its own with `uv run python -m worker.web_search "your question"`.

The harness runs the agent loop in AWS; when the agent calls one of its inline functions, the harness stops and hands the call to this Mac, which runs it and sends the result back in the same session. Result files go straight into the job folder on this Mac through `save_file` (text) and `save_chart` (numbers, drawn here as SVG), so the job folder has one writer. However a job ends, the user hears about it: a job that never relayed anything has its summary relayed, and a failed job is reported as a failure. Until Claude on Bedrock is available for the account, the harness uses Amazon Nova 2 Lite (`WORKER_MODEL` or `--model` changes it).

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
