# Bee Call friction log

I built Bee Call alone for the Amazon Build, Ship, Shape hackathon. It is an iPhone app and server in which an AI phones its user to talk through what their Bee recorded, reading Bee and writing facts and to-dos back through the Bee CLI. The server runs on AWS. I wore Bee as the Bee app on an Apple Watch, since the Bee wearable ships only in the US.

Each entry follows the hackathon rules' format, with the task attempted, the steps taken, the expected and actual results, the severity, the workaround and a suggestion. Severity means what the problem cost this project. Critical stopped the main path with no workaround. High blocked a feature until it was found or worked around. Medium cost hours, a design change, or data that an agent relies on. Low cost minutes.

Entries 1 to 5, 13, 14 and 16 to 18 are about the Bee app and the Bee CLI. Entries 6 to 12 and 15 are about AWS. On 2026-10-19 I read the Bee CLI 0.8.0 source, and entries 3, 4, 5, 13 and 16 still apply to it. Entry 6 still applies to my account. I did not recheck the others after the dates given.

## 1. Finding the official Bee iOS app (2026-09-28)

- **Tool**: Bee iOS app (App Store listing), Bee developer docs
- **Task attempted**: Install the official Bee app on an iPhone in South Korea, as the first step toward Developer Mode and the Bee CLI.
- **Steps taken**: Searched "Bee" in the App Store. Several apps with similar names came up (for example "Bee.ai" and "Bikii - Bee AI"). Looked for an App Store link on the Bee developer docs (the overview, Developer Mode and CLI pages) and on the hackathon Resources page, and found none. Found the link on the bee.computer home page.
- **Expected**: The Developer Mode instructions link straight to the App Store listing, and the listing is easy to recognize as the official Bee app.
- **Actual**: The docs ask for "the Bee iOS app" without a link. The official listing ("Bee - Your Personal AI", app ID 6480349491) shows the seller as "Bluush Inc", not Bee or Amazon, so it is hard to tell apart from look-alikes.
- **Severity**: Low. It cost a few minutes.
- **Workaround**: Took the App Store link from bee.computer and confirmed the same app ID in the Korean storefront.
- **Suggestion**: Put a direct App Store link, with the app name and seller name, on the Developer Mode page and under Bee on the hackathon Resources page.

## 2. Finding where to turn on Developer Mode (2026-09-29)

- **Tool**: Bee iOS app (Settings), Bee developer docs
- **Task attempted**: Turn on Developer Mode so the Bee CLI can connect to the account.
- **Steps taken**: Opened Settings in the Bee app and looked for a Developer Mode option. Found only Settings → About → App version, plus an "Enable Bee agent beta" option that asks for a beta code. Went back to the docs, which say to tap the Version row in Settings 5 times. A bee-cli GitHub issue (#13) showed other iOS and Android users stuck at the same step. Tapped "App version" under About 5 times, and the app confirmed that Developer Mode was on.
- **Expected**: A visible Developer Mode setting, or docs that give the full path and say which option to ignore.
- **Actual**: The switch is a hidden gesture. The docs leave out the About screen. The "Enable Bee agent beta" option sits nearby and asks for a code, so it is easy to mistake for the developer path.
- **Severity**: Medium. The CLI, MCP and Skill routes are all blocked until this works.
- **Workaround**: Tapped "App version" under Settings → About 5 times.
- **Suggestion**: Write the full path in the docs (Settings → About → App version), say that no beta code is needed for the CLI, and show a Developer Mode row once it is on.

## 3. Finishing `bee login --no-wait` after approval (2026-09-29)

- **Tool**: Bee CLI 0.7.3 (`@beeai/cli` from npm) on macOS (Apple Silicon)
- **Task attempted**: Log the CLI in from a coding agent's terminal, using the non-blocking login flow that the README recommends for agents.
- **Steps taken**: Ran `bee login --no-wait`, which printed an approval link valid for about 6 minutes. Approved the connection on the iPhone. Ran `bee status`, as the CLI output and the README suggest.
- **Expected**: `bee status` picks up the approved session and reports the account as logged in.
- **Actual**: `bee status` still said "Not logged in." Running `bee login` again resumed the same session and finished right away ("connected to the Bee account of …"). After that, `bee status` showed the verified account. The behavior is the same in 0.8.0.
- **Severity**: Medium. For a person at a terminal it costs one extra command, but an agent that only polls `bee status`, as the README suggests for agents, never sees the login finish.
- **Workaround**: Re-ran `bee login` after the approval.
- **Suggestion**: Either let `bee status` complete a pending approved session, or change the `--no-wait` message and README to say that `bee login` must be run again. I sent a pull request that does the first: https://github.com/bee-computer/bee-cli/pull/26

## 4. Listing pending facts with the Bee CLI (2026-10-09)

- **Tool**: Bee CLI 0.7.3 (`bee facts list`)
- **Task attempted**: See which facts Bee had already extracted from a recorded test conversation, to build on them.
- **Steps taken**: Ran `bee facts list --json`. It returned no facts. Ran `bee facts list --unconfirmed --json`, which returned three.
- **Expected**: `bee facts list` lists every fact, confirmed or not, or says that it only shows confirmed ones.
- **Actual**: The default list shows confirmed facts only, and nothing in the `--json` output says so (the text output has had a "Confirmed Facts" heading since 0.7.3). With no confirmed facts yet, it looks as if Bee had extracted nothing.
- **Severity**: Low. The `--unconfirmed` flag is in the README, but the empty default result is easy to misread as "no data".
- **Workaround**: Always query both lists, or use the unconfirmed filter explicitly.
- **Suggestion**: Show counts for both states in the default output (for example "0 confirmed, 3 pending, use --unconfirmed to list them"), or add `--all`. It would also help agents if each fact carried the id of the conversation it came from.

## 5. A conversation that never closes, and `bee now` that misses new speech (2026-10-09)

- **Tool**: Bee app on Apple Watch, Bee CLI 0.7.3 (`bee now`, `bee changed`, `bee conversations get`)
- **Task attempted**: Record a two-minute test on the watch and read it back from the CLI right after.
- **Steps taken**: Started recording on the watch and spoke for about two minutes. Ran `bee now --json`, which covers the last ten hours. It returned no conversations. Ran `bee changed --json`, which reported one changed conversation. Opened it with `bee conversations get`.
- **Expected**: The new speech appears as a new conversation, and `bee now` lists it.
- **Actual**: The speech was appended to a conversation that had started eleven days earlier and was still in the CAPTURING state. I do not know why it stayed open that long. Because `bee now` filters conversations by their start time, which I confirmed in the CLI's source, it showed nothing, even though the conversation had new utterances from minutes before.
- **Severity**: Medium. An agent that polls `bee now`, as the "Agentic Now" docs suggest, silently misses fresh speech. Conversation boundaries also stop matching real-world events.
- **Workaround**: Poll `bee changed` with a cursor, then split utterances into sessions by their own timestamps.
- **Suggestion**: Close a capture after a long gap, and make `bee now` include conversations with recent utterances, not only recent starts.

## 6. A new AWS account is locked to Sydney, and the error never says so (2026-10-11)

- **Tool**: AWS sign-up ("new AWS experience"), Amazon Transcribe, Amazon Polly, Bedrock AgentCore Gateway
- **Task attempted**: Call Amazon Transcribe streaming and Amazon Polly from a fresh account in us-east-1, where the other AWS services I planned to use (Bedrock, AgentCore Gateway Web Search) run.
- **Steps taken**: Signed up, upgraded to the Paid plan, created an IAM user with AmazonTranscribeFullAccess and AmazonPollyFullAccess, and called `polly:DescribeVoices` and `transcribe:ListVocabularies` in us-east-1. Both failed. Read the AWS Account Management docs and found that the new sign-up assigns each project a Region from the contact address country (South Korea maps to ap-southeast-2) and blocks other Regions with AWS-managed SCPs.
- **Expected**: Either a Region choice during sign-up, or an error that names the Region restriction and the selected Region.
- **Actual**: `AccessDeniedException ... with an explicit deny in a service control policy`. Nothing in the console flow or the error said the account is pinned to Sydney. In us-east-1 only Bedrock inference and global services are allowed, so the AgentCore Gateway Web Search tool (us-east-1 only) cannot be used at all from this account.
- **Severity**: High. It blocked speech-to-text and text-to-speech until found, and it removed a planned AgentCore feature.
- **Workaround**: Moved Transcribe, Polly and the AgentCore harness that runs background jobs to ap-southeast-2. Background jobs search the web through Amazon Nova Web Grounding instead, a Bedrock call that the lock allows in us-east-1.
- **Suggestion**: Show the assigned Region on the sign-up summary and the console home, let builders pick it, and make the SCP denial message say "this project is limited to ap-southeast-2".

## 7. Bedrock blocked for a while right after upgrading to the Paid plan (2026-10-12)

- **Tool**: Amazon Bedrock (Converse and InvokeModel), AWS account verification
- **Task attempted**: Make the first Claude call on Bedrock after upgrading the new account to the Paid plan.
- **Steps taken**: Upgraded to the Paid plan on 2026-10-11. Around midnight Korean time, at the start of 2026-10-12, submitted the Anthropic first-time-use form with `PutUseCaseForModelAccess`, then called Claude Sonnet 5.5 through Converse and through the Anthropic SDK's Bedrock client.
- **Expected**: The call succeeds, or the console says the account is still being verified and roughly how long that takes.
- **Actual**: Every call returned `Your account is currently being verified. Verification normally takes less than 2 hours.` Nothing in the Bedrock console showed this state beforehand. It cleared within about two hours.
- **Severity**: Medium. It looks like a permissions bug until you read the exact message, and I suspect it also caused the missing agreement in entry 8.
- **Workaround**: Waited.
- **Suggestion**: Show the verification state and an estimate on the Billing or Bedrock page, and say which services are held back meanwhile.

## 8. Claude stays "not available for this account" even when every check says yes (2026-10-12)

- **Tool**: Amazon Bedrock model access for Anthropic Claude Sonnet 5.5
- **Task attempted**: Use Claude Sonnet 5.5 as the model that runs the phone conversation, from a new account in ap-southeast-2.
- **Steps taken**: Submitted the Anthropic first-time-use form. The automatic subscription never completed (`agreementAvailability` stayed `NOT_AVAILABLE`). I suspect this is because my first call came during account verification (entry 7). So I created the agreement explicitly with `CreateFoundationModelAgreement`. It went from `PENDING` to `AVAILABLE`. `GetFoundationModelAvailability` then reported `AUTHORIZED`, entitlement `AVAILABLE` and region `AVAILABLE`. Called the model through the `au.` and `global.` inference profiles from ap-southeast-2 and the `us.` and `global.` profiles from us-east-1, on both Converse and InvokeModel. Amazon Nova 2 Lite worked from the same account.
- **Expected**: With the form submitted, the agreement available and the availability API all green, the call succeeds.
- **Actual**: Every Claude call returned `anthropic.claude-sonnet-5-5 is not available for this account ... contact AWS Sales`. The availability API gave no sign of this extra account-level gate, and there is no self-service way to request it.
- **Severity**: Critical. The product's main model is unusable until someone at AWS acts.
- **Workaround**: Moved Claude to Anthropic's own API and kept GLM 5 on Bedrock as the model standing by. I opened a support case about it on 2026-10-12 (entry 11) and had no answer before I switched.
- **Others hit the same wall**: AWS re:Post has several reports of the same symptom, with every access check green and only the runtime call refused, for example [Claude Opus 5](https://repost.aws/questions/QU73Bi640YS5yHWmeEB1N3wA/claude-opus-5-returns-not-available-for-this-account-despite-available-agreement-entitlement-authorization-and-region), [Claude Fable 5 and Sonnet 5](https://repost.aws/questions/QU_-WCZSBLQyyYo2VBbkwrqA/amazon-bedrock-http-403-model-is-not-available-for-this-account-when-invoking-claude-fable-5-claude-sonnet-5-claude-opus-4-7-and-claude-opus-4-8) and [a stored use case declaration](https://repost.aws/questions/QUnPrQ8R5KSWqkgYg0guyhHw/anthropic-models-stay-not-available-for-this-account-although-the-use-case-declaration-is-stored). None had an official answer when I found them.
- **Not only Claude**: The same account gets the same refusal for Z.ai GLM 5.3 and xAI Grok 4.7, again with every availability field green, while Kimi K3, GLM 5, Kimi K2.5, DeepSeek V3.2 and MiniMax M2.5 answer normally (2026-10-12, one short call each in ap-southeast-2). Nothing in the console or the API says which models an account can call.
- **Suggestion**: Make `GetFoundationModelAvailability` report the account-level gate, retry the automatic subscription after verification finishes, and offer a request form (for example in Service Quotas) instead of "contact AWS Sales".

## 9. `aws configure` asks to change coding-agent settings in the middle of entering keys (2026-10-11)

- **Tool**: AWS CLI v2 (2.37.12), `aws configure`
- **Task attempted**: Store an IAM user's access keys under a named profile.
- **Steps taken**: Ran `aws configure --profile bee-call` and entered the keys.
- **Expected**: Only the four credential and Region questions.
- **Actual**: An extra prompt, "Configure AWS skills and the AWS MCP server for your AI coding agent(s)? [y/n/never]", came between the key questions. A first-time user does not expect a credentials command to offer changes to their coding-agent setup.
- **Severity**: Low.
- **Workaround**: Answered `never`.
- **Suggestion**: Keep this to `aws configure agent-toolkit`, or say in the prompt exactly which files it will change.

## 10. The documented permissions for creating an AgentCore Harness are incomplete (2026-10-12)

- **Tool**: Amazon Bedrock AgentCore Harness (`CreateHarness`), IAM
- **Task attempted**: Give a least-privilege IAM user exactly the permissions the harness security page lists for `CreateHarness`, then create a harness from a script.
- **Steps taken**: Granted the actions in the "Required IAM permissions for callers" table (`CreateHarness`, `CreateAgentRuntime`, `CreateMemory`, plus get, list, update, delete and invoke), scoped to the account in ap-southeast-2. Created the execution role, then called `CreateHarness`.
- **Expected**: The harness reaches READY.
- **Actual**: `CreateHarness` returned a harness that went to CREATE_FAILED with "not authorized to perform: bedrock-agentcore:CreateAgentRuntimeEndpoint". The table does not list that action for `CreateHarness`. The request itself was accepted, so the missing permission only shows up after the harness is created, in its failure reason.
- **Severity**: Medium. Each missing permission costs a round trip to whoever manages IAM, and leaves a failed harness to clean up.
- **Workaround**: Allowed `bedrock-agentcore:*` for the account in that Region instead of the listed actions. The next attempt failed the same way on a second unlisted permission: "Failed creating service linked role", so the caller also needs `iam:CreateServiceLinkedRole` for the AgentCore service the first time a harness is created in an account. That took a third round of widening the user's permissions.
- **Suggestion**: Add `bedrock-agentcore:CreateAgentRuntimeEndpoint` and, for the first harness in an account, `iam:CreateServiceLinkedRole` for the AgentCore service-linked role to the table, or check the caller's permissions when `CreateHarness` is called and refuse it there. The minimal set is not confirmed, since the setup that worked allowed `bedrock-agentcore:*`.
- **Outcome**: With `bedrock-agentcore:*` and `iam:CreateServiceLinkedRole`, the third attempt reached READY and a test job ran end to end (2026-10-12).

## 11. Opening a support case from a new-experience account goes through an AI chat first (2026-10-12)

- **Tool**: AWS Support Center (Basic support), console signed in as the account's `AccountFullAccessRole`
- **Task attempted**: Open an "Account and billing" case about the Bedrock model access in entry 8, on 2026-10-12.
- **Steps taken**: Clicked "Create case" in Support Center. It opened the "Support interaction" AI chat instead of the case form. Sent the problem description, waited for the AI answer ("I can't make a recommendation with the details provided"), then chose "Create case" under that answer, which opened the form inside the chat panel.
- **Expected**: A case form reachable directly, and the new case listed in case history.
- **Actual**: The only path to the form was through the AI answer. After the case was created (the console showed its ID and opened the case page), the case history list still showed "No cases" several minutes later.
- **Severity**: Low. A user who is told by an error message to "contact AWS" has to discover the chat route, and may think the case was lost when the list stays empty.
- **Workaround**: Open the case directly from the confirmation (case details page with its display ID).
- **Suggestion**: Keep a direct "Create case" link for account and billing questions, and show newly created cases in the list right away.

## 12. An AgentCore Harness with the built-in Code Interpreter fails on Kimi K3 with an opaque error (2026-10-12)

- **Tool**: Amazon Bedrock AgentCore Harness (`UpdateHarness`, `InvokeHarness`), model `global.moonshotai.kimi-k3` in ap-southeast-2
- **Task attempted**: Switch the harness model from Amazon Nova 2 Lite to Kimi K3, keeping the same system prompt, five inline functions and the built-in `agentcore_code_interpreter` tool.
- **Steps taken**: Updated the harness (it reached READY), then ran a one-step test job with `InvokeHarness`.
- **Expected**: The job runs as it did with Nova 2 Lite, or the update is refused if the model cannot use one of the tools.
- **Actual**: The first model call failed with `runtimeClientError ... validationException ... The model returned the following errors: Mantle streaming error for requestId ...: The task request was rejected by the target`. Calling Kimi K3 directly with Converse, the same system prompt and the same inline functions worked. Removing only the code interpreter from the harness made the job succeed. The error does not name the tool, and the harness accepted the configuration.
- **Severity**: Medium. It took four experiments to find which part of the request the model rejected.
- **Workaround**: Leave the code interpreter off for Kimi K3 only. GLM 5, the model the harness runs now, works with it attached.
- **Suggestion**: Validate the tool set against the chosen model when the harness is created or updated, and name the rejected tool or field in the runtime error.

## 13. A deleted fact or a dismissed suggestion cannot be brought back (2026-10-18)

- **Tool**: Bee CLI 0.7.3 (`bee facts delete`, `bee todos dismiss-suggestion`)
- **Task attempted**: Let a user take back any change the voice assistant made to Bee during a call, including deleting a wrong fact and turning down a suggested to-do. The assistant changes Bee only after the user agrees, but people misspeak and speech recognition mishears, so every change needs a way back.
- **Steps taken**: Looked for a restore, undelete or trash command in `bee facts --help` and `bee todos --help`. Facts offer list, search, get, create, update, confirm and delete. To-dos offer list, get, create, update, complete, delete, suggestions, accept-suggestion and dismiss-suggestion.
- **Expected**: A way to restore a deleted fact or a dismissed suggestion, such as a restore command or a short recovery window. Every other change (create, update, confirm, complete, accept) can be reversed with the commands that exist.
- **Actual**: Both changes are permanent. No command brings back a deleted fact or a dismissed suggestion.
- **Severity**: Medium. Because these two changes could not be reversed, I first guarded them with much stricter confirmation logic than the rest. That logic misjudged real yeses and made the assistant ask the same question twice.
- **Workaround**: My server keeps the item's text before changing it. Taking back a delete creates a new confirmed fact with the same text. Taking back a dismissal creates an open to-do with the suggestion's text. The fact gets a new id, and since `facts create` takes only text, its tags are not carried over. The suggestion comes back as a to-do, not as a suggestion.
- **Suggestion**: Add `bee facts restore <id>` and `bee todos restore-suggestion <id>`, or keep deleted items recoverable for a short time. Returning the full deleted object from `delete`, together with a `facts create` that accepts tags, would let a client restore a fact with its tags.

## 14. The wearer's own words labeled "Unknown" (2026-10-09)

- **Tool**: Bee app on Apple Watch (speaker labels in transcripts), Bee CLI 0.7.3 (`bee conversations transcript`)
- **Task attempted**: Use the speaker label on each transcript line to tell the wearer's words from other people's, so that an agent acts only on what the user asked for and keeps other people's words out of the user's facts.
- **Steps taken**: Recorded a conversation alone in a quiet room, with nothing playing, and spoke 17 lines. Read the transcript with `bee conversations transcript <id> --json`.
- **Expected**: All 17 lines carry the wearer's label.
- **Actual**: In this one recording, 3 of the 17 lines were labeled "Unknown", the label Bee also gives other people. I have not checked whether the app offers a voice setup that would help.
- **Severity**: Medium. Labels that can be wrong for the wearer cannot keep other people's words out of the user's memory, and an agent cannot tell whether a request in an "Unknown" line came from the user.
- **Workaround**: The agent that reads new Bee records between calls acts on no request in a line labeled "Unknown". A real request that Bee mislabels is ignored, so that nobody else's words are acted on.
- **Suggestion**: Let the wearer enroll their voice, or give each line a confidence for the wearer's label, so that an agent can choose its own threshold.

## 15. `UpdateHarness` wants some fields wrapped in `optionalValue` (2026-10-12)

- **Tool**: Amazon Bedrock AgentCore Harness (`UpdateHarness`) through boto3 and botocore 1.43.106
- **Task attempted**: Change an existing harness from the deploy script, such as its memory setting, without deleting and recreating it.
- **Steps taken**: Called `UpdateHarness` with the same field shapes that `CreateHarness` accepts.
- **Expected**: The update takes the same shapes as the create call.
- **Actual**: Every redeploy failed until three fields, `memory`, `environmentArtifact` and `authorizerConfiguration`, were wrapped one more level as `{"optionalValue": ...}`. `CreateHarness` takes the same three fields unwrapped. I learned the wrapped shape from botocore's parameter validation errors.
- **Severity**: Low. It cost several failed deploys.
- **Workaround**: The deploy script wraps those fields in `{"optionalValue": ...}`.
- **Suggestion**: Accept plain values in `UpdateHarness` as `CreateHarness` does, or point out the difference in the API reference for both calls.

## 16. The `bee stream --json` examples leave out the event name (2026-10-20)

- **Tool**: Bee CLI 0.7.3 (`bee stream --json`) and its README
- **Task attempted**: Have a server process react to new Bee records as they happen, by reading `bee stream --json` and acting on each event by its name.
- **Steps taken**: Read the README's list of stream events, where every payload is shown without an event name (for example `{"conversation": {...}}`), and the CLI source, where `--json` prints only each event's data. Concluded that a `--json` line could not be matched to its event, and switched the server to `bee stream --agent`. Then captured real `--json` lines on the server.
- **Expected**: The documented payloads match what the stream sends.
- **Actual**: In the one real event I captured, a conversation event, the line had both a `conversation` and a `type` field, so the name was there after all. I did not record the value of `type`. The README examples leave `type` out. The server's original parser, which also matched on `type`, had worked, and my change rested on the examples.
- **Severity**: Low. It cost a needless change and a correction, and it can mislead anyone writing an agent against the stream.
- **Workaround**: The server now reads `bee stream --agent`, whose lines always start with `Event <name>`, and still accepts JSON lines that carry `type`.
- **Suggestion**: Show the full `--json` line in the README, `type` included, or print each line as `{"event": ..., "data": ...}`.

## 17. A login check can wait with no time limit (2026-10-20)

- **Tool**: Bee CLI 0.8.0, built from source with pull request #26 from entry 3 applied. The request that hung is the pairing request in `sources/commands/auth/appPairingRequest.ts`, which that pull request does not change.
- **Task attempted**: Finish a `bee login --no-wait` login from a script that runs `bee status` every few seconds until the approval comes through.
- **Steps taken**: Started the login and ran `bee status` every 6 seconds. Each check answered "pending" within 1 to 2 seconds. The link was approved in the Bee app while one check was in flight.
- **Expected**: Each check answers within seconds, or gives up after a time limit so that the next one can run.
- **Actual**: The check sent around the time of the approval got no answer for more than four minutes while its connection stayed open. No later check ran, the link expired, and the approval was lost. This happened once in two tries, and the second try finished the login at once.
- **Severity**: Medium. A user's approval was thrown away. `bee login` polls with the same request, so it can hang the same way, although I have not seen stock `bee login` hang.
- **Workaround**: My script stops any check that takes longer than 20 seconds and sends a new one.
- **Suggestion**: Give the pairing request a time limit, and stop its retry loop once the caller has given up. Another developer's open pull request, https://github.com/bee-computer/bee-cli/pull/18, does the second part.

## 18. Short watch recordings show as "Processing", then disappear (2026-10-21)

- **Tool**: Bee app on Apple Watch and iPhone, Bee CLI 0.7.3 (`bee changed`, `bee now`, `bee journals list`)
- **Task attempted**: Leave a quick spoken note from the watch for an agent to pick up, the kind of note a person makes on a walk.
- **Steps taken**: Recorded notes of a few seconds on the watch, once with Start and Stop and once by pressing and holding after "Tap to activate". Checked Recent conversations in the iPhone app and the CLI's change feed.
- **Expected**: A short note is kept as a conversation or a voice note, or the watch says that it was too short to keep.
- **Actual**: The pressed-and-held note showed in Recent conversations as "Processing", then disappeared with no message. The Start and Stop note never appeared in the app. Neither note ever appeared in `bee changed`, `bee now` or `bee journals list`. A 48-minute conversation the same day was kept and summarized normally. The day before, a conversation that stayed "CAPTURING" for more than six hours also vanished, and `bee conversations get` later answered "Conversation not found". I do not know whether it held speech, so I cannot tell whether anything was lost.
- **Severity**: Medium. Short notes are the natural use of a watch, an agent never sees them, and nothing tells the user that they were dropped.
- **Workaround**: Speak for longer, and check the app for a finished summary before relying on a note.
- **Suggestion**: Keep short recordings, for example as voice notes, or say on the watch and in the app that a recording was dropped and why, with the minimum length documented.
