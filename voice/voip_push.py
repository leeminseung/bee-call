"""Apple push for the iPhone app: the VoIP push (PushKit) that rings an AI call even when the app is closed or
the phone is locked, and the plain alert push that asks before a call (voice/offers.py).

The app registers for VoIP pushes at launch and sends its device token to the token server, which gives it
to the signed-in person in the server's registry of phones (voice/people.py devices.json): one owner per
token, gone when that sign-in ends. When the AI places a call, voice/dispatch.py hands the ring line
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

The alert push needs its own device token (the app sends it to POST /push, kept the same way) and goes to the
app's bundle id with push type "alert"; the same .p8 key signs both.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt

from voice.people import Person

log = logging.getLogger("bee-call.voip")

# The signed provider token, shared by the processes that place calls (voice.outbound, the fake caller): APNs
# answers TooManyProviderTokenUpdates to a team that renews it too often.
PROVIDER_TOKEN_FILE = Path("state") / "apns_provider_token.json"
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


def provider_token(config: ApnsConfig, now: float | None = None, cache: Path | None = None) -> str:
    """The JWT APNs wants in the authorization header, reused for TOKEN_REFRESH_S (across processes when
    `cache` names a file)."""
    now = time.time() if now is None else now
    try:
        made = json.loads(cache.read_text()) if cache else None
    except (OSError, ValueError):
        made = None
    if isinstance(made, dict) and made.get("kid") == config.key_id and made.get("iss") == config.team_id \
            and 0 <= now - float(made.get("iat", 0)) < TOKEN_REFRESH_S and isinstance(made.get("token"), str):
        return made["token"]
    token = jwt.encode({"iss": config.team_id, "iat": int(now)}, config.key_path.read_text(), algorithm="ES256",
                       headers={"kid": config.key_id})
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.touch(mode=0o600)
        cache.write_text(json.dumps({"kid": config.key_id, "iss": config.team_id, "iat": int(now), "token": token}))
    return token


def save_device_token(person: Person, token: str, environment: str, kind: str = "voip",
                      session_token: str | None = None) -> None:
    """Called by the token server when the app sends the token PushKit (voip) or iOS (alert) gave it: the token
    becomes this person's alone, until the sign-in `session_token` ends (voice/people.py register_device)."""
    token = token.strip().lower()
    if not DEVICE_TOKEN.fullmatch(token):
        raise ValueError("not a device token")
    if environment not in APNS_HOSTS:
        raise ValueError(f"environment must be one of {', '.join(APNS_HOSTS)}")
    person.people.register_device(person.id, kind, token, environment, session_token)


def load_device_token(person: Person, kind: str = "voip") -> dict[str, str] | None:
    data = person.device(kind)
    if not isinstance(data, dict) or not DEVICE_TOKEN.fullmatch(str(data.get("token", ""))) \
            or data.get("environment") not in APNS_HOSTS:
        return None
    return data


async def ring(line: dict[str, Any], person: Person, config: ApnsConfig | None = None,
               transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Sends the VoIP push for an AI call's ring line to the person's phone. Returns APNs' id for the push, or
    None when VoIP push is not set up (no key or no registered phone). Raises PushError when APNs refuses it."""
    payload = {key: line.get(key) for key in ("room", "topic", "expires_at", "expires_epoch")}
    # APNs drops a push that cannot arrive before the voice server gives up.
    expires = line.get("expires_epoch") if isinstance(line.get("expires_epoch"), int) else None
    return await _send("voip", payload, person, config, transport, expires_epoch=expires)


async def alert(payload: dict[str, Any], person: Person, *, expires_epoch: int | None = None,
                collapse_id: str | None = None, config: ApnsConfig | None = None,
                transport: httpx.AsyncBaseTransport | None = None) -> str | None:
    """Sends a plain notification (payload with "aps") to the person's phone. Returns APNs' id, or None when
    alert push is not set up (no key, or the app has not sent its alert token). Raises PushError when APNs
    refuses it."""
    return await _send("alert", payload, person, config, transport, expires_epoch=expires_epoch,
                       collapse_id=collapse_id)


async def _send(kind: str, payload: dict[str, Any], person: Person, config: ApnsConfig | None,
                transport: httpx.AsyncBaseTransport | None, *, expires_epoch: int | None = None,
                collapse_id: str | None = None) -> str | None:
    config = config or ApnsConfig.from_env()
    device = load_device_token(person, kind)
    if config is None or device is None:
        return None
    # One cache for every person: APNs counts token renewals per signing key, not per person.
    token = provider_token(config, cache=Path(os.environ.get("BEE_CALL_DATA") or "data") / PROVIDER_TOKEN_FILE.name)
    topic = f"{config.bundle_id}.voip" if kind == "voip" else config.bundle_id
    headers = {"authorization": f"bearer {token}", "apns-topic": topic, "apns-push-type": kind,
               "apns-priority": "10"}
    if expires_epoch is not None:
        headers["apns-expiration"] = str(expires_epoch)
    if collapse_id:
        headers["apns-collapse-id"] = collapse_id[:64]  # a newer push with the same id replaces the older one
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
        person.people.forget_device(kind, device["token"])
    raise PushError(f"APNs answered {response.status_code} {reason}".strip())
