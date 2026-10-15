"""Writes the Bee Call server's configuration from SSM Parameter Store. Runs on the EC2 instance, as root.

    python -m deploy.config env         /run/bee-call/env, the services' environment, from the parameters under
                                        /bee-call/ (bee-call-env.service runs it at boot, before the services),
                                        and the APNs key from /bee-call/apns-key
    python -m deploy.config caddyfile   /etc/caddy/Caddyfile for the hostname in /bee-call/BEE_CALL_PUBLIC_HOST,
                                        or none until scripts/deploy_server.py dns has set one

A parameter named like an environment variable (/bee-call/LIVEKIT_URL) becomes one. Lowercase names are files:
apns-key is written next to the environment for the services to read, github-deploy-key is only for
deploy/bootstrap.sh. /run is in memory, so no settings or keys are written to the instance's disk.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

PREFIX = "/bee-call/"
REGION = "ap-southeast-2"
RUN_DIR = Path("/run/bee-call")
ENV_FILE = RUN_DIR / "env"
APNS_KEY = RUN_DIR / "apns-key.p8"
# The signed APNs provider token the services share (voice/voip_push.py): a credential for up to an hour, so it is
# kept on tmpfs, never on the disk (BC-154), in a folder only the services' user can open.
APNS_TOKEN_DIR = RUN_DIR / "apns"
APNS_TOKEN_CACHE = APNS_TOKEN_DIR / "provider-token.json"
CADDYFILE = Path("/etc/caddy/Caddyfile")
CADDY_BIN = Path("/opt/bee-call/bin/caddy")  # where deploy/install.sh puts it
TEMPLATE = Path(__file__).with_name("Caddyfile.template")
HOST_PARAMETER = "BEE_CALL_PUBLIC_HOST"
SERVICE_GROUP = "bee-call"

ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
HOSTNAME = re.compile(r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])")
# Settings that never go to the server, with why: scripts/deploy_server.py keeps them on the Mac, and the server never
# takes them from SSM (one list, server review 19).
NOT_FOR_THE_SERVER = {
    "AWS_PROFILE": "this Mac's AWS profile; the server uses its instance role",
    "AWS_ACCESS_KEY_ID": "static AWS keys; the server uses its instance role",
    "AWS_SECRET_ACCESS_KEY": "static AWS keys; the server uses its instance role",
    "AWS_SESSION_TOKEN": "static AWS keys; the server uses its instance role",
    "APNS_KEY_PATH": "a path on this Mac; the key itself goes to /bee-call/apns-key",
    "APNS_TOKEN_CACHE": "the server's own place for it, set when its environment is written",
    "LIVEKIT_AGENT_NAME": "each machine registers its own; the server keeps bee-call",
    "BEE_CALL_DEV_PERSON": "lets requests in without signing in; never on a server",
    "BEE_CALL_DATA": "the server's data root is set in its units",
    "BEE_BIN": "the server's Bee CLI is set in its units",
    "TZ": "the units set the server's clock zone",
    "TOKEN_SERVER_PORT": "Caddy expects the token server on 8787",
    "BEE_CALL_CLOCK_FILE": "a test clock (voice/people.py clock_now); a server keeps the real one",
}
REFUSED = set(NOT_FOR_THE_SERVER)


def quoted(value: str) -> str:
    """A value the way systemd's EnvironmentFile= reads it back unchanged: in double quotes, with \\ " ` $ escaped."""
    return '"' + re.sub(r'([\\"`$])', r"\\\1", value) + '"'


def env_file(params: Mapping[str, str], apns_key: Path | None = None) -> tuple[str, dict[str, str]]:
    """The environment file for the services, and the parameters it leaves out, with why."""
    lines, skipped = ["# Written by deploy/config.py from SSM Parameter Store /bee-call/. Do not edit."], {}
    for name in sorted(params):
        value = params[name]
        if not ENV_NAME.fullmatch(name):
            continue  # a file, such as apns-key
        if name in REFUSED:
            skipped[name] = "never taken from SSM"
        elif any(ch in value for ch in "\n\r\0"):
            skipped[name] = "not one line"
        else:
            lines.append(f"{name}={quoted(value)}")
    if apns_key is not None:
        lines.append(f"APNS_KEY_PATH={quoted(str(apns_key))}")
        lines.append(f"APNS_TOKEN_CACHE={quoted(str(APNS_TOKEN_CACHE))}")
    return "\n".join(lines) + "\n", skipped


def caddyfile(hostname: str, template: str) -> str:
    """Caddy's config for the server's hostname. Raises ValueError for anything but a plain DNS name."""
    host = hostname.strip().lower().rstrip(".")
    if not HOSTNAME.fullmatch(host):
        raise ValueError(f"not a DNS name: {hostname!r}")
    return template.replace("__HOSTNAME__", host)


def ssm_client():
    import boto3  # here, so the tests import this module without AWS

    return boto3.client("ssm", region_name=REGION)


def parameters() -> dict[str, str]:
    """Every parameter directly under /bee-call/, decrypted, by its last name segment."""
    found = {}
    for page in ssm_client().get_paginator("get_parameters_by_path").paginate(Path=PREFIX, WithDecryption=True):
        for parameter in page["Parameters"]:
            found[parameter["Name"][len(PREFIX):]] = parameter["Value"]
    return found


def public_host() -> str:
    """The hostname scripts/deploy_server.py dns set, or "" before that."""
    ssm = ssm_client()
    try:
        return ssm.get_parameter(Name=PREFIX + HOST_PARAMETER)["Parameter"]["Value"].strip()
    except ssm.exceptions.ParameterNotFound:
        return ""


def write(path: Path, text: str, mode: int, group: str | None = None) -> None:
    """Replaces the file in one step, with its mode set before any content is in it."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    if group:
        shutil.chown(tmp, "root", group)
    tmp.replace(path)


def with_retries(read, tries: int = 10):
    """At boot the instance role or the network can lag behind this unit, so it keeps trying for a few minutes."""
    for attempt in range(1, tries + 1):
        try:
            return read()
        except Exception as exc:
            if attempt == tries:
                raise
            wait = min(60, 5 * attempt)
            print(f"reading SSM failed ({type(exc).__name__}: {exc}); trying again in {wait} s", flush=True)
            time.sleep(wait)
    raise AssertionError("unreachable")


def write_env() -> None:
    params = with_retries(parameters)
    RUN_DIR.mkdir(exist_ok=True)
    shutil.chown(RUN_DIR, "root", SERVICE_GROUP)
    RUN_DIR.chmod(0o750)  # the services may enter it for the APNs key; only root reads the environment
    key = params.get("apns-key", "").strip()
    if key:
        write(APNS_KEY, key + "\n", 0o640, SERVICE_GROUP)
        APNS_TOKEN_DIR.mkdir(mode=0o700, exist_ok=True)
        shutil.chown(APNS_TOKEN_DIR, SERVICE_GROUP, SERVICE_GROUP)  # the services' user, who signs the token
        APNS_TOKEN_DIR.chmod(0o700)
    else:
        APNS_KEY.unlink(missing_ok=True)
    text, skipped = env_file(params, APNS_KEY if key else None)
    write(ENV_FILE, text, 0o600)
    names = sorted(n for n in params if ENV_NAME.fullmatch(n) and n not in skipped)
    print(f"wrote {ENV_FILE}: {', '.join(names) or 'no settings'}" + ("; APNs key" if key else "; no APNs key"))
    for name, why in skipped.items():
        print(f"left out {name}: {why}")


def write_caddyfile() -> None:
    hostname = with_retries(public_host)
    if not hostname:
        CADDYFILE.unlink(missing_ok=True)
        print(f"no {HOST_PARAMETER} yet: Caddy stays off until scripts/deploy_server.py dns sets one")
        return
    CADDYFILE.parent.mkdir(exist_ok=True)
    candidate = CADDYFILE.with_name(CADDYFILE.name + ".new")
    write(candidate, caddyfile(hostname, TEMPLATE.read_text()), 0o644)
    # Caddy checks the new config before it takes the working one's place: a mistake in it stops the deploy with
    # Caddy's own words, and the server keeps the config that works.
    checked = subprocess.run([str(CADDY_BIN), "validate", "--config", str(candidate), "--adapter", "caddyfile"],
                             capture_output=True, text=True, timeout=120)
    if checked.returncode != 0:
        candidate.unlink(missing_ok=True)
        why = (checked.stderr or checked.stdout).strip()[-2000:]
        raise RuntimeError(f"Caddy refused the new config, so {CADDYFILE} stays as it was: {why}")
    candidate.replace(CADDYFILE)
    print(f"wrote {CADDYFILE} for {hostname}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("what", choices=["env", "caddyfile"])
    args = parser.parse_args()
    try:
        write_env() if args.what == "env" else write_caddyfile()
    except Exception as exc:
        sys.exit(f"{args.what}: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
