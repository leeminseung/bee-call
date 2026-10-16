# Bee Call server on EC2

One EC2 instance in Sydney (`ap-southeast-2`) runs the four Bee Call processes behind Caddy, so the iPhone app works without a Mac on the same Wi-Fi. `scripts/deploy_server.py`, run on a Mac, creates the instance, gives it its settings, updates it and takes it down. Nothing logs in to the instance: every command reaches it through SSM Run Command.

## What runs where

| Part | Where | How it is reached |
|---|---|---|
| Token server (`voice/token_server.py`) | EC2, `bee-call-token.service`, port 8787 | `https://<hostname>/` through Caddy |
| Session screen (`voice/session_server.py`) | EC2, `bee-call-session.service`, 127.0.0.1:8788 | `https://<hostname>:8443/` through Caddy |
| Voice server (`voice/agent.py start`) | EC2, `bee-call-agent.service` | LiveKit Cloud dispatches calls to the worker named `bee-call` |
| Between calls (`voice/outbound.py`) | EC2, `bee-call-outbound.service` | outbound only: Bee CLI, Bedrock, APNs, LiveKit |
| Caddy 2.11.7 | EC2, `caddy.service`, ports 80, 443, 8443 | the internet |
| Settings and keys | SSM Parameter Store, `/bee-call/*` | read at boot by `bee-call-env.service` |
| Call media | LiveKit Cloud | the phone and the voice server connect to it directly |
| Background jobs | AgentCore Harness in Sydney, as before | the voice server invokes it |

Routing: port 443 goes to the token server and port 8443 to the session screen, both on one hostname with one Let's Encrypt certificate. Port 80 answers Let's Encrypt's HTTP challenge and redirects everything else to `https://<hostname>/`, whatever name the request used. Over HTTPS a request whose Host is not the name of its TLS connection gets 421, and both sites send HSTS (`Strict-Transport-Security`, one year). A new Caddyfile is checked with `caddy validate` before it replaces the one in use; if Caddy refuses it, the deploy stops with Caddy's message and the server keeps the old config. Caddy forwards every write at once (`flush_interval -1`), so the ring stream and a call's live events are not held back. HTTP/3 is off because the security group opens TCP only. The security group opens 80, 443 and 8443 and nothing else: the token server (8787) and the voice server's health check (8081) listen on every interface of the instance, since their code does not take an address, but nothing outside reaches them.

The four services run as the system user `bee-call` with `Restart=always`, log to journald, and get `TZ=Asia/Seoul` (the system clock is set to Seoul too). Each person's times follow their own `time_zone` setting, which their phone sends; Seoul time only reads back the times stored before that setting existed, which carry no zone. Their settings come from `/run/bee-call/env`, which `bee-call-env.service` writes at every boot from SSM (root only, mode 0600, in memory). The voice server keeps one idle job process (`num_idle_processes=1` in `voice/agent.py`); on stop it takes no new calls and lets calls in progress end, for up to 10 minutes; a background job still running after its call stops at once, fails on record and is notified as unfinished (`voice/agent.py` `STOPPING`).

On the instance:

| Path | What |
|---|---|
| `/opt/bee-call/app` | the repository (branch `main`) and its `.venv` |
| `/opt/bee-call/bin` | `uv`, `caddy` and `bee`, each pinned and checked by sha256 |
| `/opt/bee-call/python` | the Python that `.python-version` names, installed by uv |
| `/var/lib/bee-call` | the data root (`BEE_CALL_DATA`), also `bee-call`'s home |
| `/var/lib/caddy` | Caddy's certificates |
| `/run/bee-call/env`, `/run/bee-call/apns-key.p8` | settings and the APNs key, rewritten at every boot |
| `/run/bee-call/apns/` | the signed APNs token the services share (good for up to an hour), only in memory, readable by `bee-call` only |
| `/swapfile` | 2 GB of swap |

## Before the first deployment

These steps need you, once.

1. **Sign-in first.** Once `dns` has run, the token server and the session screen are on the public internet. Deploy only code where every request needs the app's sign-in; code without it would let anyone with the hostname start calls (which read Bee and spend AWS and LiveKit money) and read call records. The services expect that code: data under `BEE_CALL_DATA`, and the session screen accepting its public name from `BEE_CALL_PUBLIC_HOST`.
2. **Give the Mac its own agent name.** Add `LIVEKIT_AGENT_NAME=bee-call-dev` to the Mac's `.env.local` and restart its voice server. The server registers as `bee-call`; if both machines used one name, LiveKit would split calls between them. Run `voice.outbound` for a Bee account on one machine only, or both would react to the same Bee changes.
3. **IAM, with an administrator identity** (not `bee-call-dev`):
   ```bash
   aws iam create-role --role-name bee-call-server --assume-role-policy-document \
     '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
   aws iam put-role-policy --role-name bee-call-server --policy-name bee-call-server \
     --policy-document file://deploy/iam/server-role-policy.json
   aws iam attach-role-policy --role-name bee-call-server \
     --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
   aws iam create-instance-profile --instance-profile-name bee-call-server
   aws iam add-role-to-instance-profile --instance-profile-name bee-call-server --role-name bee-call-server
   aws iam create-policy --policy-name bee-call-deployer --policy-document file://deploy/iam/deployer-policy.json
   aws iam attach-user-policy --user-name bee-call-dev \
     --policy-arn arn:aws:iam::515927224114:policy/bee-call-deployer
   ```
   If the role and its instance profile exist already, skip `create-role`, `create-instance-profile` and `add-role-to-instance-profile`.
   `server-role-policy.json` allows Bedrock models, Nova Web Grounding (the worker's web search, which needs `bedrock:InvokeTool` besides the model), the AgentCore harness, Transcribe, Polly and reading `/bee-call/*`. The deploying user's permissions go in a managed policy, because IAM allows a user only 2,048 characters of inline policies.
4. **Buy the domain in Route 53.** Registering it there creates the hosted zone that `dns` writes to. The hostname can be the domain or a name under it.

## First deployment

Every command that changes AWS prints its plan first; add `--yes` to carry it out.

```bash
uv run python scripts/deploy_server.py deploy-key --yes
gh repo deploy-key add <the .pub file it printed> --repo leeminseung/bee-call --title bee-call-server
uv run python scripts/deploy_server.py secrets            # review: names and lengths only
uv run python scripts/deploy_server.py secrets --yes
aws ssm put-parameter --profile bee-call --region ap-southeast-2 --name /bee-call/apns-key \
  --type SecureString --value file://<path to AuthKey_XXXXXXXXXX.p8>
uv run python scripts/deploy_server.py up --yes           # t4g.medium unless --type t4g.small
uv run python scripts/deploy_server.py status             # repeat until "first boot: done"
uv run python scripts/deploy_server.py dns bee.example.com --yes
```

- `deploy-key` makes an ed25519 key pair in memory, keeps the private key as `/bee-call/github-deploy-key`, and writes the public key to a file. Adding it to GitHub is yours to do; `gh repo deploy-key add` makes a read-only key unless given `--allow-write`. The instance reads the private key into `/run` (memory) for each pull and removes it right after.
- `secrets` copies `.env.local` to `/bee-call/<NAME>` as SecureString, except what belongs to the Mac (`AWS_PROFILE`, `APNS_KEY_PATH`, `LIVEKIT_AGENT_NAME`, `BEE_CALL_DEV_PERSON` and a few more, listed when it runs). It adds the worker's ARN from the Mac's `data/harness.json` (or `workspace/state/harness.json` from before the per-person folders) and the server's defaults for settings the Mac passes on the command line: `COORDINATOR_LLM=anthropic` (Claude Haiku 5.5 on Anthropic's own API, with GLM 5 on Bedrock standing by) and `WORKER=harness`. The Anthropic key is `.env.local`'s `ANTHROPIC_API_KEY` and goes to `/bee-call/ANTHROPIC_API_KEY` like the rest; when it is neither in `.env.local` nor on the server, `secrets` says that every call will run on GLM 5. `--set NAME=VALUE` adds or overrides one, for example `--set CALL_LANGUAGE=ko`.
- `up` stops if the deploy key is not in SSM yet, because the first boot clones the repository with it. It creates the security group `bee-call-server` (TCP 80, 443 and 8443 from anywhere, no SSH), the instance (Amazon Linux 2023 arm64, IAM role `bee-call-server`, 30 GB encrypted gp3, IMDSv2 only, no key pair, `deploy/bootstrap.sh` as user data) and an Elastic IP, then waits until SSM reaches the instance. The first boot keeps installing for about five minutes; `logs boot` shows it.
- `dns` points an A record at the Elastic IP, waits until Route 53 serves it, stores the name as `/bee-call/BEE_CALL_PUBLIC_HOST`, and has the server configure Caddy for it. Caddy then gets the certificate; `logs caddy` shows it.

Then point the app at the server: the server address is `https://<hostname>` and the call records are at `https://<hostname>:8443/`. In the app with Sign in with Apple these are the Release build settings `BEE_CALL_SERVER` and `BEE_CALL_RECORDS`; a Debug build still pairs a typed address with `http://…:8788/` for the records, which suits a Mac but not this server.

## Updating

```bash
uv run python scripts/deploy_server.py ship --yes
```

`ship` runs `deploy/bootstrap.sh` on the instance: it pulls `main` from GitHub, and `deploy/install.sh` updates the pinned tools if their versions changed, runs `uv sync --locked`, installs the units and restarts the services: the settings first (an unreadable SSM stops the update there, with the services still running), then the token server, the session screen and the between-calls process, and the voice server last, after calls in progress have ended (up to 10 minutes). Until it is back, no new call can start. Push first: the instance deploys what is on GitHub. After `secrets --yes`, `ship --yes` makes the services read the new settings.

```bash
uv run python scripts/deploy_server.py status             # services, their memory, the voice server's jobs
uv run python scripts/deploy_server.py logs agent         # token, session, agent, outbound, caddy, env, boot
uv run python scripts/deploy_server.py people waiting     # sign-ins waiting for approval
uv run python scripts/deploy_server.py people approve CODE --as jiwoo
uv run python scripts/deploy_server.py down --yes
```

`people` runs `python -m voice.people` on the instance as `bee-call`, with the server's data root, so approving someone who signed in with Apple works as it does on a Mac.

`down` deletes the A record (so the name never points at an address that is no longer yours) and `/bee-call/BEE_CALL_PUBLIC_HOST`, terminates the instance, releases the Elastic IP and deletes the security group. The instance's disk goes with it, with every call record and Bee login on it. The other parameters stay for the next `up`; after it, run `dns` again for the new address.

## Instance size

Measured on an M1 Mac on 2026-10-14, with the four processes started the way the units start them (`voice.agent start`, the agent name `bee-call-memtest` so no real call could reach it, and a fake Bee that reports no changes), sampled every 10 s for 2 minutes, with no call in progress:

| Process | Resident (MiB) | Private (macOS footprint, MiB) |
|---|---|---|
| Voice server, main process | 235 | 180 |
| Voice server, idle job process | 298 | 215 |
| Voice server, multiprocessing resource tracker | 22 | 11 |
| Token server | 81 | 63 |
| Session screen | 49 | 35 |
| Between calls (`voice.outbound`) | 94 | 72 |
| **The four services** | **779** | **576** |
| Bee CLI `bee stream` (one per Bee account watched) | 60 | 27 |
| Bee CLI, one read (`today --context`, 1.5 s) | 61 | |

A call's own objects add about 10 MiB to its job process; the media streams were not measured, because that needs a real call. As soon as the idle job process takes a call, LiveKit starts another one (about 300 MiB), and the call's prep runs several Bee reads at once (about 60 MiB each while they last).

On Linux the job processes are forked from a forkserver that holds the shared libraries, so they share more pages than on macOS, and there is one more process. Adding Amazon Linux with the SSM agent (about 300 MiB), Caddy (about 40 MiB) and one Bee watcher, the server should idle near 1.3 GB and reach about 2 GB during one call. A t4g.small has 2 GiB, so it would be swapping mid-call, which a voice call cannot afford; a t4g.medium has 4 GiB and room for a second call. `up` therefore defaults to t4g.medium. `status` shows each service's memory on the real instance; check it during the first calls.

Price (on-demand, Sydney, AWS's public price data on 2026-10-14): t4g.medium 0.0424 USD an hour (about 31 USD a month), t4g.small 0.0212 USD. The Elastic IP's public IPv4 address and the 30 GB disk add a few dollars a month (rates not checked).

## Pinned downloads

`deploy/install.sh` checks each download against its sha256 before using it, and skips what is already installed with the right checksum.

| Tool | Version | Pinned |
|---|---|---|
| uv | 0.12.19 | the installer script (it checks uv's own archive by sha256) |
| Python | 3.13 (from `.python-version`; uv 0.12.19 installs 3.13.15) | checked by uv |
| Caddy | 2.11.7, `caddy_2.11.7_linux_arm64.tar.gz` from GitHub | the archive and the binary |
| Bee CLI | `@beeai/cli` 0.7.3 from npm, only `dist/platforms/linux-arm64/bee` | the npm tarball and the binary |

To move to a new version, download it, check it against what its publisher lists (Caddy's `checksums.txt`, npm's `integrity`, both SHA-512), compute the sha256 values and change them in `deploy/install.sh`.
