"""Rings the iPhone through Apple's VoIP push (PushKit), so an AI call rings even when the app is closed or
the phone is locked.

The app registers for VoIP pushes at launch and sends its device token to the token server, which keeps
it in workspace/state/voip_token.json. When the AI places a call, voice/dispatch.py hands the ring line
(room, topic, expires_at) to ring(), which sends it to Apple's push service (APNs) over HTTP/2. On the
phone, PushKit wakes the app and it reports the call to CallKit at once. An open app also hears the ring
on the token server's /ring stream; it rings once per room either way.

APNs takes a provider token, a JWT signed (ES256) with the .p8 key from the Apple Developer account. Set
in .env.local:
  APNS_KEY_PATH   the .p8 file, kept outside this repository (it is read only to sign)
  APNS_KEY_ID     the key's 10-character id
  APNS_TEAM_ID    the team id of the Apple Developer account
  APNS_BUNDLE_ID  the app's bundle id (com.leeminseung.beecall when unset)
Without them, or before the app has registered, ring() does nothing and only an open app rings.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import jwt

log = logging.getLogger("bee-call.voip")

TOKEN_FILE = Path("state") / "voip_token.json"  # under the workspace; written by the token server
APNS_HOSTS = {"development": "https://api.sandbox.push.apple.com", "production": "https://api.push.apple.com"}
DEFAULT_BUNDLE_ID = "com.leeminseung.beecall"
TOKEN_REFRESH_S = 40 * 60  # APNs takes a provider token for up to an hour and refuses one renewed too often
DEVICE_TOKEN = re.compile(r"[0-9a-f]{64,200}")
GONE = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}  # this token will never work again


class PushError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApnsConfig:
    key_path: Path
    key_id: str
    team_id: str
    bundle_id: str = DEFAULT_BUNDLE_ID

    @classmethod
    def from_env(cls) -> "ApnsConfig | None":
        path, key_id, team_id = (os.environ.get(k) for k in ("APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID"))
        if not (path and key_id and team_id):
            return None
        return cls(Path(path).expanduser(), key_id, team_id, os.environ.get("APNS_BUNDLE_ID") or DEFAULT_BUNDLE_ID)


_tokens: dict[ApnsConfig, tuple[float, str]] = {}


def provider_token(config: ApnsConfig, now: float | None = None) -> str:
    """The JWT APNs wants in the authorization header, reused for TOKEN_REFRESH_S."""
    now = time.time() if now is None else now
    made = _tokens.get(config)
    if made and now - made[0] < TOKEN_REFRESH_S:
        return made[1]
    token = jwt.encode({"iss": config.team_id, "iat": int(now)}, config.key_path.read_text(), algorithm="ES256",
                       headers={"kid": config.key_id})
    _tokens[config] = (now, token)
    return token


def save_device_token(workspace: Path, token: str, environment: str) -> None:
    """Called by the token server when the app sends the token PushKit gave it."""
    token = token.strip().lower()
    if not DEVICE_TOKEN.fullmatch(token):
        raise ValueError("not a device token")
    if environment not in APNS_HOSTS:
        raise ValueError(f"environment must be one of {', '.join(APNS_HOSTS)}")
    path = workspace / TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"token": token, "environment": environment,
                                "at": datetime.now().isoformat(timespec="seconds")}) + "\n")


def load_device_token(workspace: Path) -> dict[str, str] | None:
    try:
        data = json.loads((workspace / TOKEN_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not DEVICE_TOKEN.fullmatch(str(data.get("token", ""))) \
            or data.get("environment") not in APNS_HOSTS:
        return None
    return data


async def ring(line: dict[str, Any], workspace: Path, config: ApnsConfig | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Sends the VoIP push for an AI call's ring line. Returns APNs' id for the push, or None when VoIP push
    is not set up (no key or no registered phone). Raises PushError when APNs refuses it."""
    config = config or ApnsConfig.from_env()
    device = load_device_token(workspace)
    if config is None or device is None:
        return None
    headers = {"authorization": f"bearer {provider_token(config)}", "apns-topic": f"{config.bundle_id}.voip",
               "apns-push-type": "voip", "apns-priority": "10"}
    try:  # a push that cannot arrive before the voice server gives up is dropped by APNs
        headers["apns-expiration"] = str(int(datetime.fromisoformat(line["expires_at"]).timestamp()))
    except (KeyError, TypeError, ValueError):
        pass
    payload = {key: line.get(key) for key in ("room", "topic", "expires_at")}
    url = f"{APNS_HOSTS[device['environment']]}/3/device/{device['token']}"
    async with httpx.AsyncClient(http2=transport is None, transport=transport, timeout=10) as client:
        response = await client.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        return response.headers.get("apns-id")
    try:
        reason = response.json().get("reason", "")
    except ValueError:
        reason = response.text[:200]
    if reason in GONE:  # the app was deleted, or this token belongs to another build; wait for a new one
        (workspace / TOKEN_FILE).unlink(missing_ok=True)
    raise PushError(f"APNs answered {response.status_code} {reason}".strip())
