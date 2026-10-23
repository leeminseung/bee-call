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
        # Replaced whole, readable by its owner only, whatever mode an older file had: it is a credential.
        tmp = cache.with_name(f".{cache.name}.{os.getpid()}.tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({"kid": config.key_id, "iss": config.team_id, "iat": int(now), "token": token}))
        tmp.replace(cache)
    return token


def token_cache(person: Person) -> Path:
    """Where the provider token is shared: APNS_TOKEN_CACHE on a server (tmpfs under /run, deploy/config.py), so the
    credential never reaches the disk (BC-154); on a Mac, one file for every person under their data root."""
    configured = os.environ.get("APNS_TOKEN_CACHE", "").strip()
    return Path(configured) if configured else person.people.root / PROVIDER_TOKEN_FILE.name


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


PUSHES = Path("state") / "pushes.jsonl"  # in the person's folder: every push sent, or that would have been


def record_push(person: Person, kind: str, payload: dict[str, Any], *, sent: bool, status: int | None = None,
                apns_id: str | None = None, reason: str | None = None) -> None:
    """One line of the person's push record (docs/testing-from-outside.md): the push's type and payload, whether it
    went out, and APNs' answer. Never a device token or a credential. A record that cannot be written never stops
    a push."""
    line = {"at": person.now().isoformat(timespec="seconds"), "type": kind, "payload": payload, "sent": sent,
            "status": status, "apns_id": apns_id, "reason": reason}
    try:
        path = person.workspace / PUSHES
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        log.warning("could not record a %s push", kind, exc_info=True)


async def _send(kind: str, payload: dict[str, Any], person: Person, config: ApnsConfig | None,
                transport: httpx.AsyncBaseTransport | None, *, expires_epoch: int | None = None,
                collapse_id: str | None = None) -> str | None:
    config = config or ApnsConfig.from_env()
    device = load_device_token(person, kind)
    if config is None or device is None:
        why = "APNs is not configured" if config is None else f"the app has not sent its {kind} token"
        record_push(person, kind, payload, sent=False, reason=why)
        return None
    # One cache for every person: APNs counts token renewals per signing key, not per person.
    token = provider_token(config, cache=token_cache(person))
    topic = f"{config.bundle_id}.voip" if kind == "voip" else config.bundle_id
    headers = {"authorization": f"bearer {token}", "apns-topic": topic, "apns-push-type": kind,
               "apns-priority": "10"}
    if expires_epoch is not None:
        headers["apns-expiration"] = str(expires_epoch)
    if collapse_id:
        headers["apns-collapse-id"] = collapse_id[:64]  # a newer push with the same id replaces the older one
    # APNS_URL: a test receiver instead of Apple (docs/testing-from-outside.md; never on a server).
    base = os.environ.get("APNS_URL", "").strip().rstrip("/") or APNS_HOSTS[device["environment"]]
    url = f"{base}/3/device/{device['token']}"
    async with httpx.AsyncClient(http2=transport is None and base.startswith("https://"), transport=transport,
                                 timeout=10) as client:
        response = await client.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        apns_id = response.headers.get("apns-id")
        record_push(person, kind, payload, sent=True, status=200, apns_id=apns_id)
        return apns_id
    try:
        reason = response.json().get("reason", "")
    except ValueError:
        reason = response.text[:200]
    record_push(person, kind, payload, sent=False, status=response.status_code, reason=reason or None)
    if reason in GONE:  # the app was deleted, or this token belongs to another build; wait for a new one
        person.people.forget_device(kind, device["token"])
    raise PushError(f"APNs answered {response.status_code} {reason}".strip())
