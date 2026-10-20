# Models and background jobs

## The model catalog

`voice/model_catalog.py` is the one place that knows the models. For each role it lists the choices by the catalog's own ids (`haiku`, `sonnet`, `glm`). For each choice it names the provider and that provider's model id, the price the day's budget counts, how the role runs it (thinking, effort, output limit), and the model standing by. Each provider is reached through one small adapter in `voice/providers/`, for Anthropic's Messages API, Bedrock's Converse API and the AgentCore harness. Adding a provider means adding its adapter and its catalog entries. The person picks the call and worker models in the app, from `GET /models?role=call` and `GET /models?role=worker`. The server decides the others. The catalog lists only models that run.

| Role | Default | Other choices | Standing by |
|---|---|---|---|
| call (the call AI) | Claude Haiku 5.5, adaptive thinking, effort low | Claude Sonnet 5.5, thinking between tools, effort low | GLM 5 on Bedrock |
| worker (background jobs) | Claude Sonnet 5.5, adaptive thinking, effort high | GLM 5 in the AgentCore harness | GLM 5 in the AgentCore harness |
| meta (the meta agent) | Claude Sonnet 5.5, adaptive thinking, effort medium | none | GLM 5 on Bedrock |
| prep (the call prep) | Claude Haiku 5.5, no thinking | none | GLM 5 on Bedrock |
| title (the title after a call) | Claude Haiku 5.5, no thinking | none | none |

The call AI's Claude Haiku 5.5 thinks adaptively at effort low. In probe calls with thinking turned off, it said it would call back and then skipped booking the call in 3 of 6 calls (`probes/haiku_probe.py`). Claude Sonnet 5.5 does not accept thinking turned off, and "between tools" skips thinking before its first words.

## The call AI's modes

`COORDINATOR_LLM` picks what answers as the call AI, which the code calls the coordinator. The voice server, `voice/outbound.py` and the meta agent all read it (`voice/call_models.py`). When it is unset, it is `echo`.

- `anthropic` is the server's mode. Claude answers on Anthropic's own API, with the key in `ANTHROPIC_API_KEY`. Each person picks the model in the app (setting `call_model`, read as a call starts), and the catalog above says how it runs. `COORDINATOR_MODEL`, `COORDINATOR_THINKING` and `COORDINATOR_EFFORT` are not used in this mode. A call that finds one of them set logs it and lists it in its record's `coordinator` event (`settings_not_used`). The system prompt and the growing history are cached, and the record's `coordinator` event names the model.
- `claude` runs Claude on Amazon Bedrock through the same adapter. It uses Claude Sonnet 5.5, or the Bedrock model id in `COORDINATOR_MODEL`. Set `COORDINATOR_THINKING=disabled` for Claude Haiku 5.5. This needs Claude access on Bedrock in your AWS account.
- `bedrock` runs any Bedrock model through the Converse API, named by `COORDINATOR_MODEL` (GLM 5 when unset). `probes/coordinator_bench.py` compares them.
- `fake` is a test stand-in. It is the real Claude adapter with scripted replies and no network. It checks every request against the Messages API rules and checks that earlier turns were never edited, so barge-in, tools and job results can be tested. Say "test job" to start a background job.
- `nova` is a test stand-in that runs Amazon Nova 2 Lite, a model that can call tools.
- `echo` is a test stand-in that repeats the caller, for the audio path. Say "test job" to exercise the background job flow.

With `anthropic`, the prep runs on Claude Haiku 5.5 and the meta agent on Claude Sonnet 5.5, each with GLM 5 on Bedrock standing by. With `bedrock`, `nova` and `claude`, the prep, the meta agent and the title run on that mode's one Bedrock model. With `echo` and `fake` there is no prep, while the meta agent and the title run on the catalog's models.

## When Claude fails or declines

With `anthropic`, GLM 5 on Amazon Bedrock stands by (`voice/fallback_llm.py`). Without a key, or when the first request of a call fails (no network, a refused key, no credit left, a rate limit, an outage), GLM 5 answers that request and the rest of the call. The call's `events.jsonl` has a `coordinator_model` line with the model that answered, and why when it is not Claude. After the first answer the call keeps its model. A later failure is a failed turn, as with any model.

A decline is not a failure and never goes to GLM 5. When Claude declines a turn (`stop_reason` `refusal`), the caller hears one short line in the call's language and the call goes on (`claude_refusal` in the record). What came in with the declined request, such as the caller's words, a tool's result or the app's notes, is left out of later requests, so the same turn is not declined again and again.

## The model log

Every call saves `model-log.jsonl`, whatever the model. It has one line per request the call AI's model received, with the model that answered it, the whole chat as LiveKit gives it (the instructions, both sides, every tool call and its result), the tools offered, and what came back. When Claude answered (`anthropic`, `claude`, `fake`), the call also saves `claude-history.json`, exactly what was sent to Claude. Both stay on the server for debugging. They hold Bee data, so neither the token server nor the session screen ever serves them.

## Background jobs

The call AI hands slow work, such as research or calculations, to a background job with its `delegate` tool, which returns a job number at once (`worker/runner.py`). The job's news arrives later as a `<job-notification>` block, the way Claude Code tells its model about a background task. The block has the job number, its status, its task, and a summary, an error or the files it made. Workers relay the answer first and write the report after, so the user hears the answer while the report is still being written. However a job ends, the user hears about it. A job that never relayed anything ends with its summary, and a failed job with its error. The call AI can also check on running jobs with `job_status`.

Each job has a folder in the call's folder, `jobs/<NN>/`. It holds `request.md` (what the call AI asked for), the job's own log `events.jsonl`, the final `report.md`, its readable page `report.html`, and any files the job made.

`WORKER` picks what runs the jobs.

- `harness` is the server's mode. Each person's `worker_model` decides. `sonnet`, the default, is the Claude worker (`worker/claude.py`). `glm` is GLM 5 in an AgentCore harness (`worker/harness.py`), which `uv run python scripts/deploy_worker.py --smoke` creates once. A choice that is not set up on the machine, because there is no `ANTHROPIC_API_KEY` or no harness, hands its jobs to the other. With neither, each job fails and says why.
- `search` is a test stand-in that answers each job with one Amazon Nova Web Grounding search (`worker/web_search.py`), at about 0.03 USD a search. Try one on its own with `uv run python -m worker.web_search "your question"`.
- `fake`, the default, pretends to work for a few seconds.

### The Claude worker

Each job is one conversation with Claude Sonnet 5.5 on Anthropic's API, with thinking at effort high, in a loop that runs in the voice server's call process. Anthropic runs its server tools within a request. They are web search (10 USD per 1,000 searches), web fetch for reading a page or a PDF in full, and code execution in one container per job. The voice server runs the job's own tools between requests (`progress`, `relay_to_user`, `save_file` and `save_chart` in `worker/tools.py`). A file Claude leaves in `$OUTPUT_DIR` during code execution is saved into the job folder under a safe name that is not taken yet. It is deleted from Anthropic's Files API when the job ends, also when the end of its call cuts the job off.

Each request may write 32K tokens and has 15 minutes, or 5 minutes without a byte. A request that fails midway is tried again twice. A job may make 80 requests, 60 web searches and 60 page reads, and spend 1.50 USD. At any of these limits it is asked for its answer in one more request without tools. The conversation is cached as it grows. Every request's tokens, cache reads and writes, and searches go into the job's `usage.json` at once, so the day's budget counts a job that fails or stops as well as one that finishes.

A job Claude cannot run (a refused key, no credit left, an API still overloaded after the retries) that has produced nothing yet runs again on GLM 5 in the harness, the worker role's standby, when the harness is set up. The call's record says so (`job_standby`), the call AI hears it in the job's notifications, and the first attempt's use stays counted (`usage.1.json`). A job Claude declines is not handed over, just as the call AI's declines are not. `probes/claude_worker_probe.py` runs a real job, a Korean price research task, which took about 2 minutes and 0.35 to 0.52 USD and produced a report, a CSV and a chart.

### The AgentCore harness worker

The harness runs the agent loop in AWS, in an isolated microVM per session in Sydney. When the agent calls one of its inline functions, the harness stops and hands the call to the voice server, which runs it and sends the result back in the same session. The inline functions are the job's own tools and a web search through Nova Web Grounding (`us-east-1`). AgentCore's Code Interpreter, run by AWS, handles calculations. Result files go straight into the job folder through `save_file` (text) and `save_chart` (numbers, drawn on the server as SVG), so the job folder has one writer.

The harness runs GLM 5 (`WORKER_MODEL` or `--model` changes it), with reasoning effort high from the model catalog. A harness job may take up to 80 round trips through the voice server, each up to 30 minutes and 120 steps, and write a report of up to 32K tokens in one model call. GLM 5 on Bedrock holds 202,752 tokens of prompt and answer together, which leaves the job's conversation about 170K.

### What a job leaves

A chart a job draws gets two PNGs beside its SVG (`worker/chart.py`), since the app cannot show SVG. One is for light mode (`price.png`), and the other (`price-dark.png`) is the same drawing in the dark palette on a transparent background. A job's report gets a readable page beside it (`worker/report_page.py`), `report.html`, rendered when the job is done by a Python process of its own, since the call's process carries live audio. The page has its own light and dark styles, and the app opens it instead of the raw Markdown.

The report comes from a model that read web pages, so its page is inert. Raw HTML shows as text, a link goes only to an http, https or mailto address, a picture shows its words and loads nothing, and the page's own policy allows only its inline styles.

### Jobs after the call

A job outlives its call (`finish_jobs` in `voice/agent.py`). One still running when the call ends runs to its end in the call's process, for at most 30 more minutes (`AFTER_CALL_JOBS_S`), and then fails saying so. LiveKit is told to give the process that long (`shutdown_process_timeout`). The job's progress and end go into the call's record after `call_ended`, so the app shows its card running and then done, with its report, page and charts, where it finished, after the call's last words. The call AI hears nothing more. The person gets one plain notification per job, in the call's language ("Results are in: <title>", or that it could not finish), and tapping it opens the app. The meta agent gets a `job_ended` event. Since a job may run on after its call, a job does not start once the person's budget for the day is used up (`delegate` checks it, as each call's start does).

When the voice server is stopped, for a deploy or a restart, it marks the data root (`voice-server-stopping`) before LiveKit drains it. A job still running after its call then stops at once and fails with "the server restarted while the job ran", and the person is told it could not finish. So a stop waits only for the calls in progress. The next start clears the mark.

A running job touches its own log every 30 seconds. A job with no end whose log has been quiet for 2 minutes is not running any more (`JOB_STALE_S` in `worker/runner.py`), because it crashed or LiveKit killed its process. The call view shows it as failed. The voice server also fails such jobs on record when it starts and every 5 minutes after, at the time the log went quiet. A later call that continues the call hears whether its job is still running or stopped unfinished.

For records made before titles, dark charts or report pages existed, these commands fill in what is missing for one person.

```bash
uv run python -m voice.titles data/people/<id>         # titles the ended calls that have none
uv run python -m worker.chart data/people/<id>         # draws any missing chart PNG, light or dark
uv run python -m worker.report_page data/people/<id>   # renders the reports' pages
```
