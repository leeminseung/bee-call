"""Running on the EC2 server as well as on a Mac: the agent name each machine registers under, the AWS credentials
each process finds, and the deployment's own parts (the services' environment file, Caddy's config, the settings
copied to SSM, the commands sent through Run Command, the systemd units).

No network: AWS config files are written for the test, and credentials and settings are made up.
"""

import base64
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.config import TEMPLATE, caddyfile, env_file, quoted
from scripts import deploy_server
from scripts.deploy_server import (BOOTSTRAP, harness_on_this_mac, parse_sets, remote_commands, server_settings,
                                   zone_for)
from voice import agent
from voice.aws_session import aws_profile, aws_session
from voice.dispatch import AGENT_NAME, agent_name

UNITS = Path(__file__).parent.parent / "deploy" / "systemd"
SERVICES = ["bee-call-token", "bee-call-session", "bee-call-agent", "bee-call-outbound"]


def systemd_env(text: str) -> dict[str, str]:
    """Reads KEY="value" lines the way systemd 252's EnvironmentFile= does: inside double quotes a backslash
    before one of " \\ ` $ gives that character, and before anything else stays as written."""
    found = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, rest = line.partition("=")
        assert rest.startswith('"') and rest.endswith('"'), line
        value, chars, i = [], rest[1:-1], 0
        while i < len(chars):
            if chars[i] == "\\" and i + 1 < len(chars):
                value.append(chars[i + 1] if chars[i + 1] in '"\\`$' else chars[i:i + 2])
                i += 2
            else:
                assert chars[i] != '"', f"an unescaped quote ends the value early: {line}"
                value.append(chars[i])
                i += 1
        found[key] = "".join(value)
    return found


# ---------- the agent name and AWS credentials ----------

def test_each_machine_names_its_voice_server_and_the_server_keeps_the_default():
    assert agent_name({}) == "bee-call"
    assert agent_name({"LIVEKIT_AGENT_NAME": ""}) == "bee-call"
    assert agent_name({"LIVEKIT_AGENT_NAME": " bee-call-dev "}) == "bee-call-dev"


def test_the_voice_server_registers_under_the_name_calls_are_dispatched_to():
    assert agent.server._agent_name == AGENT_NAME  # LiveKit keeps the name the rtc_session decorator was given
    assert agent.server._num_idle_processes == 1  # one warm job process, not one per CPU as `start` would keep


def test_aws_profile_is_named_on_a_mac_and_left_to_the_default_chain_on_ec2():
    assert aws_profile({"AWS_PROFILE": "other"}, profiles=["bee-call"]) == "other"
    assert aws_profile({}, profiles=["default", "bee-call"]) == "bee-call"  # a Mac without AWS_PROFILE set
    assert aws_profile({"AWS_PROFILE": " "}, profiles=[]) is None  # EC2: no ~/.aws, the instance role
    assert aws_profile({}, profiles=["default"]) is None


def test_aws_session_reads_the_profiles_this_machine_has(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    config.write_text("[profile bee-call]\nregion = ap-southeast-2\n")
    assert aws_session().profile_name == "bee-call"
    config.write_text("[default]\nregion = ap-southeast-2\n")
    assert aws_session().profile_name == "default"  # boto3's name for the default chain


def test_transcribe_gets_the_keys_as_they_are_at_the_start_of_each_call():
    keys = iter([("AKIA1", "secret1", "token1"), ("ASIA2", "secret2", "token2")])

    class Renewing:  # like an instance role's credentials, which botocore renews before they expire
        def get_frozen_credentials(self):
            access, secret, token = next(keys)
            return SimpleNamespace(access_key=access, secret_key=secret, token=token)

    session = SimpleNamespace(get_credentials=lambda: Renewing())
    first, second = agent.transcribe_credentials(session), agent.transcribe_credentials(session)
    assert (first.access_key_id, first.session_token) == ("AKIA1", "token1")
    assert (second.access_key_id, second.secret_access_key, second.session_token) == ("ASIA2", "secret2", "token2")
    # On EC2 there is no profile: the plugin reads the instance role itself, afresh for every stream it opens.
    assert agent.transcribe_credentials(None) is None


# ---------- the services' environment, from SSM ----------

def test_the_env_file_gives_the_services_exactly_the_values_in_ssm():
    params = {"LIVEKIT_URL": "wss://bee-call.livekit.cloud", "ELEVENLABS_API_KEY": 'sk_a"b\\c$HOME`id`',
              "CALL_TIME": "  21:00 # evening", "NOTE": "it's é", "apns-key": "-----BEGIN PRIVATE KEY-----\nMIGT\n",
              "github-deploy-key": "-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl\n"}
    text, skipped = env_file(params, Path("/run/bee-call/apns-key.p8"))
    assert systemd_env(text) == {"CALL_TIME": "  21:00 # evening", "ELEVENLABS_API_KEY": 'sk_a"b\\c$HOME`id`',
                                 "LIVEKIT_URL": "wss://bee-call.livekit.cloud", "NOTE": "it's é",
                                 "APNS_KEY_PATH": "/run/bee-call/apns-key.p8"}
    assert "PRIVATE KEY" not in text and skipped == {}  # the keys are files, never environment variables
    assert quoted("$x") == '"\\$x"'


def test_the_server_never_takes_a_profile_static_keys_the_dev_person_or_what_its_units_set_from_ssm():
    params = {"AWS_PROFILE": "bee-call", "AWS_ACCESS_KEY_ID": "AKIA", "BEE_CALL_DEV_PERSON": "matt",
              "APNS_KEY_PATH": "/Users/me/AuthKey.p8", "LIVEKIT_AGENT_NAME": "bee-call-dev", "BEE_CALL_DATA": "data",
              "TWO_LINES": "a\nb", "TTS": "elevenlabs"}
    text, skipped = env_file(params)
    assert systemd_env(text) == {"TTS": "elevenlabs"}
    assert set(skipped) == {"AWS_PROFILE", "AWS_ACCESS_KEY_ID", "BEE_CALL_DEV_PERSON", "APNS_KEY_PATH",
                            "LIVEKIT_AGENT_NAME", "BEE_CALL_DATA", "TWO_LINES"}


# ---------- Caddy ----------

def caddy_sites(text: str) -> dict[str, str]:
    """Each site block of a Caddyfile by its address, with its body."""
    return {m.group(1): m.group(2) for m in re.finditer(r"^(\S+) \{\n(.*?)^\}", text, re.MULTILINE | re.DOTALL)}


def test_caddy_serves_the_token_server_on_443_and_the_session_screen_on_8443():
    text = caddyfile(" Bee.Example.com. ", TEMPLATE.read_text())
    assert "__HOSTNAME__" not in text
    sites = caddy_sites(text)
    for address, upstream in (("bee.example.com", "127.0.0.1:8787"), ("bee.example.com:8443", "127.0.0.1:8788")):
        assert re.search(rf"\treverse_proxy {re.escape(upstream)} \{{\n\t\tflush_interval -1 ", sites[address])
    assert "protocols h1 h2" in text  # HTTP/3 needs UDP, which the security group does not open


def test_caddy_answers_only_for_its_own_name_and_keeps_browsers_on_https():
    """Black-box probe 1, 2 and 7 (2026-10-15): port 80 redirected to whatever Host the request named; a Host that
    is not the server's own got Caddy's empty 200 over HTTPS; neither site sent HSTS."""
    text = caddyfile("bee.example.com", TEMPLATE.read_text())
    sites = caddy_sites(text)
    assert "auto_https disable_redirects" in text  # Caddy's own redirect copies the request's Host
    assert sites["http://"].strip() == "redir https://bee.example.com{uri} permanent"  # every name to this one
    assert "strict_sni_host on" in text  # a Host other than the TLS name gets 421 Misdirected Request
    for address in ("bee.example.com", "bee.example.com:8443"):
        assert 'header Strict-Transport-Security "max-age=31536000"' in sites[address], address


def test_a_caddyfile_that_caddy_refuses_never_replaces_the_working_one(tmp_path, monkeypatch, capsys):
    """The new config is checked with Caddy itself before it takes the old one's place, so a mistake in it stops
    the deploy with Caddy's words and leaves the server on the config that works."""
    from deploy import config

    working = tmp_path / "Caddyfile"
    working.write_text("the config that works\n")
    caddy = tmp_path / "caddy"
    monkeypatch.setattr(config, "CADDYFILE", working)
    monkeypatch.setattr(config, "CADDY_BIN", caddy)
    monkeypatch.setattr(config, "public_host", lambda: "bee.example.com")
    monkeypatch.setattr(config, "write", lambda path, text, mode, group=None: path.write_text(text))
    caddy.write_text("#!/bin/sh\necho 'Error: adapting config using caddyfile: unrecognized directive' >&2\nexit 1\n")
    caddy.chmod(0o755)
    with pytest.raises(RuntimeError, match="unrecognized directive"):
        config.write_caddyfile()
    assert working.read_text() == "the config that works\n" and list(tmp_path.glob("Caddyfile.*")) == []
    caddy.write_text('#!/bin/sh\n[ "$1" = validate ] && [ "$5" = caddyfile ] && grep -q bee.example.com "$3"\n')
    config.write_caddyfile()
    assert "redir https://bee.example.com{uri}" in working.read_text()


@pytest.mark.parametrize("name", ["", "localhost", "1.2.3.4", "bee.example.com:443", "*.example.com",
                                  "bee example.com", "bee.example.com {\n}", "-bee.example.com", "bee..example.com"])
def test_caddy_takes_only_a_plain_dns_name(name):
    with pytest.raises(ValueError):
        caddyfile(name, TEMPLATE.read_text())


# ---------- scripts/deploy_server.py ----------

def test_secrets_copy_this_macs_settings_except_its_own():
    local = {"LIVEKIT_URL": "wss://bee-call.livekit.cloud", "LIVEKIT_API_SECRET": "s3cret", "TTS": "elevenlabs",
             "AWS_PROFILE": "bee-call", "APNS_KEY_PATH": "/Users/me/AuthKey.p8", "LIVEKIT_AGENT_NAME": "bee-call-dev",
             "BEE_CALL_DEV_PERSON": "matt", "ELEVENLABS_VOICE": "", "COORDINATOR_LLM": "claude"}
    harness = ("arn:aws:bedrock-agentcore:ap-southeast-2:1:harness/w", "data/harness.json")
    chosen, skipped = server_settings(local, harness, extra={"CALL_LANGUAGE": "ko"})
    assert {name: value for name, (value, _) in chosen.items()} == {
        "LIVEKIT_URL": "wss://bee-call.livekit.cloud", "LIVEKIT_API_SECRET": "s3cret", "TTS": "elevenlabs",
        "COORDINATOR_LLM": "claude", "WORKER_HARNESS_ARN": "arn:aws:bedrock-agentcore:ap-southeast-2:1:harness/w",
        "WORKER": "harness", "CALL_LANGUAGE": "ko"}
    assert chosen["WORKER"][1] == "server default" and chosen["COORDINATOR_LLM"][1] == ".env.local"
    assert set(skipped) == {"AWS_PROFILE", "APNS_KEY_PATH", "LIVEKIT_AGENT_NAME", "BEE_CALL_DEV_PERSON",
                            "ELEVENLABS_VOICE"}


def test_without_the_workers_arn_the_server_keeps_the_fake_worker():
    chosen, _ = server_settings({})
    assert {name: value for name, (value, _) in chosen.items()} == {"COORDINATOR_LLM": "anthropic"}


def test_the_server_runs_claude_haiku_on_anthropic_s_api_with_the_key_from_this_mac():
    """The call AI on the server is Claude Haiku 5.5 on Anthropic's own API (2026-10-15); its key goes to SSM like
    the other settings of .env.local. Without it on either side, secrets says every call will run on GLM 5."""
    from scripts.deploy_server import coordinator_note

    chosen, skipped = server_settings({"ANTHROPIC_API_KEY": "sk-ant-test"})
    assert chosen["COORDINATOR_LLM"] == ("anthropic", "server default")
    assert chosen["ANTHROPIC_API_KEY"] == ("sk-ant-test", ".env.local") and not skipped
    assert coordinator_note(chosen, existing=set()) is None
    assert coordinator_note(server_settings({})[0], existing={"ANTHROPIC_API_KEY"}) is None  # already on the server
    note = coordinator_note(server_settings({})[0], existing=set())
    assert "ANTHROPIC_API_KEY" in note and "GLM 5" in note
    assert coordinator_note(server_settings({"COORDINATOR_LLM": "bedrock"})[0], existing=set()) is None


def test_the_workers_arn_is_found_in_the_macs_data_root(tmp_path):
    (tmp_path / "harness.json").write_text('{"harnessArn": "arn:aws:bedrock-agentcore:ap-southeast-2:1:harness/w"}')
    assert harness_on_this_mac({"BEE_CALL_DATA": str(tmp_path)}) == (
        "arn:aws:bedrock-agentcore:ap-southeast-2:1:harness/w", f"{tmp_path}/harness.json")


@pytest.mark.parametrize("pair", ["AWS_PROFILE=bee-call", "BEE_CALL_DEV_PERSON=matt", "lower=x", "NOEQUALS"])
def test_set_refuses_what_the_server_must_not_take(pair):
    with pytest.raises(SystemExit):
        parse_sets([pair])


def test_dns_picks_the_hosted_zone_that_holds_the_name():
    assert zone_for("bee.example.com", ["example.com.", "other.org."]) == "example.com"
    assert zone_for("bee.example.com", ["example.com.", "bee.example.com."]) == "bee.example.com"
    assert zone_for("example.com", ["example.com."]) == "example.com"
    assert zone_for("notexample.com", ["example.com."]) is None


def test_run_command_rebuilds_the_script_on_the_server_unchanged():
    script = "set -e\necho \"it's $((40 + 2)) \\$HOME\"\nexit 3\n"
    lines = remote_commands(script, chunk=8)
    chunks = [re.fullmatch(r"printf '%s' '([A-Za-z0-9+/=]+)' >> \"\$f\"", line).group(1) for line in lines[1:-1]]
    assert base64.b64decode("".join(chunks)).decode() == script and max(map(len, chunks)) == 8
    run = subprocess.run(["sh", "-c", "\n".join(lines)], capture_output=True, text=True)  # as Run Command does
    assert (run.returncode, run.stdout) == (3, "it's 42 $HOME\n")


def test_run_command_keeps_the_end_of_a_long_output():
    lines = remote_commands("printf 'x%.0s' $(seq 30000); echo; echo finished >&2\n")
    run = subprocess.run(["sh", "-c", "\n".join(lines)], capture_output=True, text=True)
    assert run.returncode == 0 and len(run.stdout) == 23000 and run.stdout.endswith("x\nfinished\n")


class FakeSsm:
    """Run Command as boto3 shows it: the invocation is unknown at first, then in progress, then done."""

    class exceptions:
        class InvocationDoesNotExist(Exception):
            pass

    def __init__(self, final: dict):
        self.answers = [None, {"Status": "InProgress"}, final]
        self.sent = None

    def send_command(self, **kwargs):
        self.sent = kwargs
        return {"Command": {"CommandId": "c-1"}}

    def get_command_invocation(self, CommandId, InstanceId):
        answer = self.answers.pop(0)
        if answer is None:
            raise self.exceptions.InvocationDoesNotExist()
        return answer


def test_run_command_waits_for_the_server_and_stops_on_a_failure(monkeypatch):
    monkeypatch.setattr(deploy_server.time, "sleep", lambda s: None)
    done = {"Status": "Success", "ResponseCode": 0, "StandardOutputContent": "ok\n", "StandardErrorContent": ""}
    ssm = FakeSsm(done)
    assert deploy_server.run_on_server(ssm, "i-1", "echo ok", timeout=60) == "ok\n"
    assert ssm.sent["DocumentName"] == "AWS-RunShellScript" and ssm.sent["Parameters"]["executionTimeout"] == ["60"]
    failed = {"Status": "Failed", "ResponseCode": 1, "StandardOutputContent": "uv: lock out of date\n",
              "StandardErrorContent": ""}
    with pytest.raises(SystemExit):
        deploy_server.run_on_server(FakeSsm(failed), "i-1", "exit 1")


def test_the_user_data_fits_ec2s_limit_and_hands_over_to_the_checkout():
    text = BOOTSTRAP.read_text()
    assert text.startswith("#!/bin/bash\n") and len(text.encode()) < 16 * 1024
    assert text.rstrip().endswith('exec "$APP/deploy/install.sh"')


# ---------- systemd units ----------

def unit(name: str) -> list[str]:
    return [line.strip() for line in (UNITS / f"{name}.service").read_text().splitlines()]


@pytest.mark.parametrize("name", SERVICES)
def test_every_service_runs_unprivileged_in_seoul_time_with_the_settings_from_ssm(name):
    lines = unit(name)
    for expected in ["User=bee-call", "Restart=always", "EnvironmentFile=/run/bee-call/env",
                     "After=bee-call-env.service network-online.target", "Environment=TZ=Asia/Seoul",
                     "Environment=BEE_CALL_DATA=/var/lib/bee-call", "WorkingDirectory=/opt/bee-call/app"]:
        assert expected in lines, f"{name}: {expected}"
    assert next(line for line in lines if line.startswith("ExecStart=")).startswith(
        "ExecStart=/opt/bee-call/app/.venv/bin/python -m ")
    # Wants, not Requires: a failed settings read must not keep a service down, since Restart= never retries a
    # failed dependency. The settings unit tries again by itself instead.
    assert not any(line.startswith(("Requires=", "BindsTo=")) for line in lines)
    assert "Restart=on-failure" in unit("bee-call-env")


def test_the_voice_server_finishes_calls_before_it_stops_and_registers_as_bee_call():
    lines = unit("bee-call-agent")
    assert "ExecStart=/opt/bee-call/app/.venv/bin/python -m voice.agent start" in lines
    assert {"KillMode=mixed", "TimeoutStopSec=10min", "Environment=LIVEKIT_AGENT_NAME=bee-call"} <= set(lines)
