# How a call runs

This page follows one call from the ring to the hang-up. [between-calls.md](between-calls.md) covers how a call gets offered or booked, [bee.md](bee.md) covers what the call reads from Bee and writes to it, and [models-and-jobs.md](models-and-jobs.md) covers the models and background jobs.

## Who starts a call

A call starts in one of three ways.

- The user taps the call button in the app.
- The user said yes to an offer, either "now" or at the suggested time ([between-calls.md](between-calls.md)).
- A call the user booked comes due. The user can book one out loud during a call, or ask Bee for one in a voice memo or a conversation.

The AI never rings without the user's yes, and it never calls back on its own to continue a call.

## Joining the room and ringing

Every call happens in a LiveKit room. The voice server (`voice/agent.py`) joins only the rooms it is dispatched to, as the agent named `bee-call` (`LIVEKIT_AGENT_NAME` changes the name). For a call the user starts, the token the app gets from `GET /token` dispatches it. For a call the AI starts, `voice/dispatch.py` dispatches it with metadata that says why the AI is calling, and the voice server waits up to 60 seconds for the phone to answer.

The phone rings in one of two ways. An open app hears the call on the token server's `/ring` stream. A closed or locked app gets an Apple VoIP push (`voice/voip_push.py`), and the app reports the call to CallKit at once, as iOS requires for every VoIP push. A ring that already rang or expired is reported and ended at once. The app sends its VoIP token to the token server (`POST /voip`) whenever it reaches it. Once the server has the token, the app's home screen says calls come even with the app closed.

If the user declines the call while it rings, the app sends `POST /calls/decline`. The voice server stops waiting within about a second and records the call as declined, not missed, and `/ring` never replays it.

## The call prep

Before the AI picks up, it writes itself a call prep note (`voice/prep.py`). There is no fixed bundle of Bee reads. The prep starts from these.

- Who calls whom, and why.
- The meta agent's notes ([between-calls.md](between-calls.md)).
- The person's last three calls, each with its title, when it was, how it ended and its last few lines, built from their records (`last_calls` in `voice/continuation.py`).
- The conversation or voice memo behind the call. This is the only Bee read before the prep's model starts.
- What changed in Bee since the person's last call began. It comes from the watcher's saved batches, with no Bee read at all (`changes_since` in `beelink/context.py`).

The prep's model writes the call's opening line first. It may then make one round of up to three reads with the Bee read tools, and then it writes the note. The note carries what this call must know, compacted from what came before. That covers where things stand and how the last call ended, what the user has been up to, what they want from calls like this one, what this call could learn about them, and what to check, with Bee's ids. It leaves out what the call will not need.

Alongside the prep, the voice server reads what Bee already has (its newest facts and to-dos, with their ids) and lists it for the call AI after the note. [bee.md](bee.md) explains why.

## Picking up

When the user starts the call, they hear ringing from the voice server while the prep runs. The AI picks up when its opening is ready, which is within 6 seconds of the phone joining the room, or else with a fixed greeting. The ringing stops when the voice starts, and at most 8 seconds after the phone joined. The note usually lands during the opening. The first reply waits up to 2 seconds for a note that is still being written. Without a note, the call AI gets the plain Bee context instead.

When the AI starts the call, `voice/outbound.py` makes the prep before the phone rings, so the AI speaks as soon as the phone answers. The Bee read the voice server makes meanwhile only feeds the call's record, unless the call has no prep note. `voice/outbound.py` picks the prep's model from `COORDINATOR_LLM` the same way the voice server does, so set it (and `COORDINATOR_MODEL`) the same way for both processes.

The app shows the call as connected only when the voice server sets the participant attribute `bee_call.state` to `answered`. A caller who hangs up before the AI's first words, or whose phone never joins the room within 30 seconds, leaves no call record and does not wake the meta agent. The prep stops, and only what the call cost is kept (`state/spend.jsonl` in the person's folder). A phone that only lost its network has not hung up. LiveKit keeps the call open for it to come back.

## During the call

Amazon Transcribe turns the caller's speech into text, the call AI answers, and ElevenLabs or Amazon Polly speaks. LiveKit's turn detector decides when the caller has finished, and the caller can interrupt the AI. A transcript too quiet to be the caller, such as the AI's own voice leaking back into the microphone, is kept out of the conversation (`voice/speech_gate.py`).

Besides talking, the call AI has these tools (`voice/coordinator.py`).

| Tool | What it does |
|---|---|
| the Bee read tools | read Bee during the call ([bee.md](bee.md)) |
| `save_to_bee`, `organize_bee`, `undo_bee_write` | change Bee after the user's yes, or take a change back ([bee.md](bee.md)) |
| `search_workspace` | look up what the user and the AI said in earlier calls |
| `delegate`, `job_status` | start a background job, or check on the running ones ([models-and-jobs.md](models-and-jobs.md)) |
| `book_call`, `cancel_call` | book a call back for a time the user asked for or agreed to, or call off the one booked in this call ([between-calls.md](between-calls.md)) |
| `end_call` | hang up after the user's goodbye |

## Continuing a call

An ended call in the app has "Continue this call". The app asks for a token with that call's id (`GET /token?continue=<call_id>`, where the id is the call's LiveKit room). The older form, `continue=<date>/<time>`, still works. The voice server then starts a new call that knows what was said in the earlier one, what its background jobs found, and which jobs stopped unfinished (`voice/continuation.py`). The opening picks up from there. Nothing restarts by itself. A stopped job is only mentioned, and the user decides whether to run it again.

## Language

Each person's `language` setting picks their call language, `en` (the default) or `ko` (`voice/language.py`). The app changes it with `PUT /settings`. The language sets what Amazon Transcribe listens for, the voices to choose from, the language the call AI speaks and the prep's opening line. The rest of the instructions stay in English. LiveKit's turn detector follows the transcript's language. A call stays in one language, since Transcribe listens for one language per stream. To try Korean without a phone, set the person's language with `uv run python -m voice.people set alex language '"ko"'`, then run `uv run python -m voice.fake_caller --language ko`, which gives the test caller a Korean voice.

## Voices

Each person picks the AI's voice in the app, from the voice catalog in `voice/voice_catalog.json` (setting `voice`, read as a call starts). The catalog lists each call language's voices in the order the app shows them, the default first. Each entry has an id that does not depend on the provider (`ko-sian`), the voice's name, one word for how it sounds in Korean and in English, its ElevenLabs voice, a sample the app plays, and speech tuning that only the server sees (`stability` so far). The samples are in `voice/voice_samples/`, named by the hash of their content. The token server's `GET /voices` lists the voices and serves the samples to anyone. A person's `voice` setting holds a catalog id of their call language, or null for the default. A change of language alone resets it, unless it fits the new language.

ElevenLabs needs `ELEVENLABS_API_KEY` with the Text to Speech permission. `ELEVENLABS_MODEL` is `eleven_v4_turbo` when unset, the most expressive of ElevenLabs' fast models. Measured from Seoul, it reaches its first sound about as quickly as `eleven_flash_v2_5`, in 0.15 to 0.24 seconds against 0.8 to 1.1 seconds for Polly. `ELEVENLABS_STABILITY` (0 to 1) makes a voice livelier when lower, for voices whose tuning does not set it.

A machine without the ElevenLabs key, or with `TTS=polly`, speaks every call with Amazon Polly. The voice is Ruth for English and Seoyeon for Korean, or `POLLY_VOICE`. The call's `coordinator` event says which voice spoke (`voice/voices.py`).

To compare voices, `uv run python -m probes.voice_samples` has real call lines read by the Polly voices and your ElevenLabs voices, on the path a call uses. It writes a page to listen to them side by side, with each sample's time to first sound. `--list` shows your ElevenLabs voices, which needs the Voices read permission.

## Noise cancellation

Before speech recognition, turn detection and interruptions hear the caller's audio, LiveKit Cloud's enhanced noise cancellation cleans it (`livekit-plugins-noise-cancellation`, with Krisp's models, on LiveKit Cloud only). Each person has a switch in the app (setting `noise_cancellation`, on by default), read once as a call starts, so a call keeps the value it started with.

`NOISE_CANCELLATION` names the model a switched-on call uses. `bvc`, the default, takes out other people's voices as well as noise. `nc` takes out noise only. `off` turns the filter off for everyone, whatever the switches say. LiveKit's gain control stays on after the filter (LiveKit would otherwise turn it off), so the speech gate's floor still holds. The filter loads when the voice server starts. If it cannot load, calls run without it, and an `error` line says why. Each call's `coordinator` event names the filter it used (`bvc`, `nc` or `off`), and `audio/user.wav` keeps the audio as the voice server heard it, cleaned when the filter is on.

To turn it off for everyone on the server, run `uv run python scripts/deploy_server.py secrets --set NOISE_CANCELLATION=off`, which shows what it would set, then the same with `--yes`. The services read the new setting after `uv run python scripts/deploy_server.py ship --yes`. On a Mac, set it in `.env.local` and restart the voice server.

## How the AI talks

Everything the call AI writes is spoken aloud in a real phone call. Its instructions (`WHO_YOU_ARE` and `HOW_TO_TALK` in `voice/coordinator.py`, with a Korean line in `voice/language.py`) start from the purpose of every call. The AI is there to come to know the user and to help them know themselves, curious and never an interviewer. From there, one principle applies, which is to talk the way a person talks on the phone. The instructions give examples from real calls.

- Short turns, and stop when the user talks.
- A casual line gets a casual answer, not a summary. A call needs no task, and the user may call just to chat.
- No name said again and again.
- Nothing that only works on a screen, and no lists read out.
- No wrapping up a call the user still wants, while a goodbye in any words ends it ("좋은 하루 되세요", "응, 들어가", "That's it, thanks").
- Only words to say, with no notes to itself and no tool names.

Conversational behavior lives in these instructions, not in code. For example, `end_call` always goes through when the AI calls it. A check of the user's words in code would refuse polite goodbyes it does not know, and since a background job outlives its call, a wrong hang-up loses no work. Once `end_call` has run, nothing more is said, even a reply to job news that arrived during the goodbye.

The prep greets the user without their name and writes its note calling them "the user". Neither the prep note nor the meta agent's notes call the user he or she, and they guess nothing the user has not said, such as a gender from a name.

With an ElevenLabs v4 voice, the call AI may write `[laughs]`, `[sighs]` and `[exhales]`, sparingly and at most one in a reply, only where a person on the phone would make that sound (`SPEECH_RULE_WITH_TAGS` in `voice/coordinator.py`). Without such a voice it is told to write none. The speech filter keeps the tags ElevenLabs documents for its v3 and v4 models (`KNOWN` in `voice/audio_tags.py`) and drops other bracketed asides. A voice that cannot perform the tags never receives them. The captions, the conversation's history, the call record and the call view never show them. The model log keeps each reply as written.

## After the call

When a call ends, the voice server writes `call_ended` into the record and wakes the meta agent with a `call_ended` event. One Claude Haiku 5.5 request, at most 5 seconds and only when the person spoke, writes the call's one-line title (`voice/titles.py`). Bee writes that failed during the call are tried once more ([bee.md](bee.md)). A background job still running goes on for up to 30 minutes and reports to the person by notification ([models-and-jobs.md](models-and-jobs.md)).
