"""Who may use this server, and where each person's things live. One server serves several people (Matt,
2026-10-14): Matt with Bee, a friend without it.

Under the data root (BEE_CALL_DATA, default data/):

  people/<id>/        one person's workspace, with the folders a single workspace always had (calls by date,
                      inbox/, meta/, state/), plus
    settings.json     their call language, evening call time, recording switches and daily budget
  bee/<id>/           their Bee CLI login (BEE_CONFIG_DIR, beelink/cli.py), once they connected Bee. Outside
                      the workspace on purpose: the meta agent's file tools browse the workspace, and this
                      folder holds a credential
  accounts.json       Apple sign-in ids of approved people, and of those waiting for Matt's approval
  sessions.json       the sign-in tokens the app holds, kept only as hashes
  devices.json        each phone's push tokens (alert and VoIP), each with exactly one owner and the sign-in
                      that registered it; a token leaves with that sign-in, or moves when someone else registers it
  harness.json        the background worker's AgentCore harness (scripts/deploy_worker.py), for everyone

Every request, call and tool works inside one person's folder, so one person's calls, Bee data and notes never
reach another person's AI or screen.

Run: uv run python -m voice.people list | waiting | approve CODE --as ID | add ID | remove ID
                                  | set ID KEY VALUE | move WORKSPACE --to ID
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PERSON_ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
CALL_TIME = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
SESSION_DAYS = 180  # a sign-in lasts this long; then the app signs in with Apple again (arbitrary)
DEFAULT_BUDGET_USD = 3.0  # estimated spend a day before calls stop (arbitrary until per-minute costs are measured)
DEFAULT_TIME_ZONE = "Asia/Seoul"  # until the person's phone sends its own (Matt, 2026-10-15: the friend is in Korea too)
DEVICE_KINDS = ("alert", "voip")  # the app's two push tokens: plain notifications, and VoIP pushes that ring
# Where each person's push tokens were kept before devices.json; read once into it, then removed.
LEGACY_DEVICE_FILES = {"voip": Path("state") / "voip_token.json", "alert": Path("state") / "alert_token.json"}


def data_root() -> Path:
    return Path(os.environ.get("BEE_CALL_DATA") or "data")


def dev_person() -> str | None:
    """For this Mac only: the person a request from this machine without a sign-in token is, and whose folder
    a call without a person in its metadata uses (voice/agent.py console, probes). Never set it on a server."""
    person = (os.environ.get("BEE_CALL_DEV_PERSON") or "").strip()
    return person if PERSON_ID.fullmatch(person) else None


@dataclass
class Settings:
    language: str = "en"  # the call language, en or ko (voice/language.py)
    call_time: str = "21:00"  # the evening time an offer suggests (voice/offers.py)
    record_on_server: bool = True  # keep each call's raw audio in its folder (Matt, 2026-10-14)
    record_on_phone: bool = False  # the app records the call on the iPhone itself; the server only notes it
    daily_budget_usd: float = DEFAULT_BUDGET_USD
    bee: bool = False  # Bee is connected (POST /bee/connect)
    tts: str | None = None  # the voice, for Matt to change by hand; None means the language's default
    voice: str | None = None
    time_zone: str = DEFAULT_TIME_ZONE  # an IANA name; the app sends the phone's (contract section 1)

    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.time_zone)

    @classmethod
    def from_file(cls, path: Path) -> "Settings":
        """A person's settings.json. Never written yet: the defaults. Broken (unreadable, not an object, or without
        a valid record_on_server): the defaults too, so calls still work, but with call audio off, since the
        person may have turned it off. A broken value of another setting keeps that setting's default."""
        try:
            text = path.read_text()
        except FileNotFoundError:
            return cls()
        except OSError:
            return cls(record_on_server=False)
        try:
            data = json.loads(text)
        except ValueError:
            return cls(record_on_server=False)
        settings = cls(record_on_server=False)  # until the file itself says to keep audio
        for name, value in data.items() if isinstance(data, dict) else ():
            try:
                settings.change(name, value)
            except ValueError:
                pass  # an unknown or broken value keeps the default
        return settings

    def change(self, name: str, value: Any) -> None:
        """Sets one setting after checking it; ValueError for an unknown name or a bad value."""
        if name == "language":
            from voice.language import LANGUAGES
            if value not in LANGUAGES:
                raise ValueError(f"language must be one of {', '.join(LANGUAGES)}")
        elif name == "call_time":
            if not isinstance(value, str) or not CALL_TIME.fullmatch(value):
                raise ValueError("call_time must look like 21:00")
        elif name in ("record_on_server", "record_on_phone", "bee"):
            if not isinstance(value, bool):
                raise ValueError(f"{name} must be true or false")
        elif name == "daily_budget_usd":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                raise ValueError("daily_budget_usd must be a number from 0 to 100")
            value = float(value)
        elif name in ("tts", "voice"):
            if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[\w.-]{1,64}", value)):
                raise ValueError(f"{name} must be a short name or null")
        elif name == "time_zone":
            try:
                ZoneInfo(value)
            except (TypeError, ValueError, ZoneInfoNotFoundError):
                raise ValueError("time_zone must be an IANA time zone name, like Asia/Seoul")
        else:
            raise ValueError(f"unknown setting {name!r}")
        setattr(self, name, value)

    def voice_env(self) -> dict[str, str]:
        """The voice settings in the form voice/voices.py reads (TTS, ELEVENLABS_VOICE or POLLY_VOICE)."""
        env = {}
        if self.tts:
            env["TTS"] = self.tts
        if self.voice:
            env["ELEVENLABS_VOICE" if (self.tts or os.environ.get("TTS")) == "elevenlabs" else "POLLY_VOICE"] = self.voice
        return env


APP_SETTINGS = ("language", "call_time", "record_on_server", "record_on_phone", "time_zone")  # what the app may change


@dataclass(frozen=True)
class Person:
    """One approved person, as the code working for them needs them: whose folder, settings and phone. Made by
    People.person(); everything that acts for one person takes this rather than a bare id or folder."""
    people: "People"
    id: str

    @property
    def workspace(self) -> Path:
        return self.people.workspace(self.id)

    def settings(self) -> Settings:
        return self.people.settings(self.id)

    def now(self) -> datetime:
        """The one clock for the person's time: now, in their time zone (aware). Every "the user's time" comes from
        here: the suggested call time, quiet hours, the day's notifications and budget, the 30 minutes after a
        call, call folder dates, booked times, and the "It is now" lines the models read."""
        return datetime.now(self.settings().zone())

    def device(self, kind: str) -> dict[str, Any] | None:
        """This person's phone for one kind of push: {"token", "environment"}, or None."""
        return self.people.device(self.id, kind)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def waiting_code(apple_sub: str) -> str:
    """A short code the app shows while someone waits, so Matt knows whom he approves."""
    return hashlib.sha256(apple_sub.encode()).hexdigest()[:6].upper()


class People:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else data_root()

    # ---------- files ----------

    @contextmanager
    def _lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / ".lock", "w") as handle:  # flock is per open file: never nest these
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _read(self, name: str) -> dict[str, Any]:
        try:
            data = json.loads((self.root / name).read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, name: str, data: dict[str, Any]) -> None:
        path = self.root / name
        tmp = path.with_name(f".{name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.chmod(0o600)
        tmp.replace(path)  # readers never see half a file

    # ---------- people ----------

    def folder(self, person: str) -> Path:
        if not isinstance(person, str) or not PERSON_ID.fullmatch(person):
            raise ValueError(f"not a person id: {person!r}")
        return self.root / "people" / person

    def workspace(self, person: str) -> Path:
        folder = self.folder(person)
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def person(self, person: str) -> Person:
        self.folder(person)  # checks the id
        return Person(self, person)

    def bee_dir(self, person: str) -> Path:
        self.folder(person)  # checks the id
        return self.root / "bee" / person

    def ids(self) -> list[str]:
        """Everyone approved."""
        return sorted({entry["person"] for entry in self._read("accounts.json").get("people", {}).values()
                       if isinstance(entry, dict) and PERSON_ID.fullmatch(str(entry.get("person", "")))})

    def known(self, person: str) -> bool:
        return person in self.ids()

    def settings(self, person: str) -> Settings:
        return Settings.from_file(self.folder(person) / "settings.json")

    def save_settings(self, person: str, settings: Settings) -> None:
        folder = self.workspace(person)
        tmp = folder / f".settings.{os.getpid()}.{secrets.token_hex(3)}.tmp"
        tmp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=1))
        tmp.replace(folder / "settings.json")

    def change_settings(self, person: str, changes: dict[str, Any], allowed: tuple[str, ...] = APP_SETTINGS) -> Settings:
        """Checks every change first, so a bad one changes nothing. ValueError names the problem."""
        settings = self.settings(person)
        for name, value in changes.items():
            if name not in allowed:
                raise ValueError(f"{name!r} cannot be changed here")
            settings.change(name, value)
        self.save_settings(person, settings)
        return settings

    # ---------- accounts ----------

    def add(self, person: str, apple_sub: str | None = None, email: str | None = None) -> None:
        """Approves a person directly. Without an Apple id they can only be used from this Mac (dev_person)."""
        self.folder(person)  # checks the id
        with self._lock():
            accounts = self._read("accounts.json")
            accounts.setdefault("people", {})[apple_sub or f"local:{person}"] = {
                "person": person, "email": email, "approved_at": datetime.now().isoformat(timespec="seconds")}
            if apple_sub:
                accounts.setdefault("waiting", {}).pop(apple_sub, None)
            self._write("accounts.json", accounts)
        self.workspace(person)

    def sign_in(self, apple_sub: str, email: str | None, now: datetime) -> str | None:
        """The approved person for this Apple id; otherwise it is noted as waiting and None comes back."""
        with self._lock():
            accounts = self._read("accounts.json")
            entry = accounts.get("people", {}).get(apple_sub)
            if isinstance(entry, dict) and PERSON_ID.fullmatch(str(entry.get("person", ""))):
                return entry["person"]
            waiting = accounts.setdefault("waiting", {})
            if apple_sub not in waiting:
                waiting[apple_sub] = {"code": waiting_code(apple_sub), "email": email,
                                      "at": now.isoformat(timespec="seconds")}
                self._write("accounts.json", accounts)
            return None

    def waiting(self) -> list[dict[str, Any]]:
        return [dict(entry, sub=sub) for sub, entry in self._read("accounts.json").get("waiting", {}).items()
                if isinstance(entry, dict)]

    def approve(self, code: str, person: str) -> str:
        """Approves the waiting sign-in with this code as `person`. Returns its Apple id."""
        matches = [entry["sub"] for entry in self.waiting() if entry.get("code") == code.strip().upper()]
        if len(matches) != 1:
            raise ValueError(f"no single sign-in waits with code {code!r}")
        email = next(entry.get("email") for entry in self.waiting() if entry["sub"] == matches[0])
        self.add(person, apple_sub=matches[0], email=email)
        return matches[0]

    def remove(self, person: str) -> None:
        """Takes the person's access away: their accounts, sign-ins and phones go. Their folder stays."""
        self.folder(person)
        with self._lock():
            accounts = self._read("accounts.json")
            accounts["people"] = {sub: entry for sub, entry in accounts.get("people", {}).items()
                                  if not (isinstance(entry, dict) and entry.get("person") == person)}
            self._write("accounts.json", accounts)
            sessions = self._read("sessions.json")
            self._write("sessions.json", {h: s for h, s in sessions.items()
                                          if not (isinstance(s, dict) and s.get("person") == person)})
            self._drop_devices(lambda entry: entry.get("person") == person)

    # ---------- sign-in tokens ----------

    def new_session(self, person: str, now: datetime | None = None) -> str:
        if not self.known(person):
            raise ValueError(f"{person!r} is not approved")
        token = secrets.token_urlsafe(32)
        now = now or datetime.now()
        with self._lock():
            sessions = self._read("sessions.json")
            # Expired sign-ins are dropped whenever a new one is made.
            sessions = {h: s for h, s in sessions.items() if isinstance(s, dict) and self._fresh(s, now)}
            sessions[_hash(token)] = {"person": person, "created": now.isoformat(timespec="seconds")}
            self._write("sessions.json", sessions)
        return token

    @staticmethod
    def _fresh(session: dict[str, Any], now: datetime) -> bool:
        try:
            return now - datetime.fromisoformat(session["created"]) < timedelta(days=SESSION_DAYS)
        except (KeyError, TypeError, ValueError):
            return False

    def who(self, token: str | None, now: datetime | None = None) -> str | None:
        """The person a sign-in token belongs to, or None (unknown, expired, or the person was removed)."""
        if not token:
            return None
        session = self._read("sessions.json").get(_hash(token))
        if not isinstance(session, dict) or not self._fresh(session, now or datetime.now()):
            return None
        person = session.get("person")
        return person if isinstance(person, str) and self.known(person) else None

    def end_session(self, token: str) -> None:
        """Signing out: the sign-in ends, and the push tokens it registered go with it, so the phone it was on
        no longer gets this person's offers and calls."""
        with self._lock():
            sessions = self._read("sessions.json")
            if sessions.pop(_hash(token), None) is not None:
                self._write("sessions.json", sessions)
            self._drop_devices(lambda entry: entry.get("session") == _hash(token))

    # ---------- phones (push tokens) ----------

    def register_device(self, person: str, kind: str, token: str, environment: str,
                        session_token: str | None = None, now: datetime | None = None) -> None:
        """A phone signed in as `person` sent its push token (voice/voip_push.py checks its form). From now on the
        token is this person's alone: whoever held it before loses it at the same moment, and it replaces this
        person's earlier phone of that kind. It stays until the sign-in that sent it ends (None: a request made
        on this Mac without one)."""
        if kind not in DEVICE_KINDS:
            raise ValueError(f"kind must be one of {', '.join(DEVICE_KINDS)}")
        self.folder(person)
        with self._lock():
            devices = self._devices()
            mine = devices.setdefault(kind, {})
            for other in [t for t, entry in mine.items() if entry.get("person") == person]:
                del mine[other]
            mine[token] = {"person": person, "environment": environment,
                           "session": _hash(session_token) if session_token else None,
                           "at": (now or datetime.now()).isoformat(timespec="seconds")}
            self._write("devices.json", devices)

    def device(self, person: str, kind: str) -> dict[str, Any] | None:
        """The person's phone for this kind of push, {"token", "environment"}, or None."""
        with self._lock():
            devices = self._devices()
        for token, entry in devices.get(kind, {}).items():
            if entry.get("person") == person:
                return {"token": token, "environment": entry.get("environment")}
        return None

    def forget_device(self, kind: str, token: str) -> None:
        """Apple says the token will never work again (the app was deleted, or it belongs to another build)."""
        with self._lock():
            devices = self._devices()
            if devices.get(kind, {}).pop(token, None) is not None:
                self._write("devices.json", devices)

    def _drop_devices(self, matches) -> None:
        """Under the lock: removes every registered token whose entry `matches`."""
        devices = self._devices()
        changed = False
        for kind in DEVICE_KINDS:
            for token in [t for t, entry in devices.get(kind, {}).items() if matches(entry)]:
                del devices[kind][token]
                changed = True
        if changed:
            self._write("devices.json", devices)

    def _devices(self) -> dict[str, dict[str, dict[str, Any]]]:
        """Under the lock: devices.json. The first time, the tokens each person's folder kept before it are read
        in (the newest registration wins a token two folders hold) and their files removed."""
        if (self.root / "devices.json").exists():
            devices = self._read("devices.json")
            return {kind: {t: e for t, e in (devices.get(kind) or {}).items() if isinstance(e, dict)}
                    for kind in DEVICE_KINDS}
        devices: dict[str, dict[str, dict[str, Any]]] = {kind: {} for kind in DEVICE_KINDS}
        found = []
        for folder in sorted((self.root / "people").glob("*")) if (self.root / "people").is_dir() else []:
            if not PERSON_ID.fullmatch(folder.name):
                continue
            for kind, name in LEGACY_DEVICE_FILES.items():
                try:
                    data = json.loads((folder / name).read_text())
                    found.append((str(data["at"]), kind, str(data["token"]), folder.name, str(data["environment"]),
                                  folder / name))
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        for at, kind, token, person, environment, _ in sorted(found):  # oldest first: the newest owner stays
            for other in [t for t, entry in devices[kind].items() if entry["person"] == person]:
                del devices[kind][other]
            devices[kind][token] = {"person": person, "environment": environment, "session": None, "at": at}
        self._write("devices.json", devices)
        for *_, path in found:
            path.unlink(missing_ok=True)
        return devices


# ---------- command line, for Matt ----------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m voice.people", description="Who may use this server.")
    parser.add_argument("--data", type=Path, help="data root (default: BEE_CALL_DATA or data/)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="approved people and their settings")
    sub.add_parser("waiting", help="sign-ins waiting for approval")
    approve = sub.add_parser("approve", help="approve a waiting sign-in by the code the app shows")
    approve.add_argument("code")
    approve.add_argument("--as", dest="person", required=True, help="a short id for the person, like jiwoo")
    add = sub.add_parser("add", help="approve a person for use from this Mac only (no Apple sign-in)")
    add.add_argument("person")
    remove = sub.add_parser("remove", help="take a person's access away (their folder stays)")
    remove.add_argument("person")
    setting = sub.add_parser("set", help="change one setting, for example set jiwoo daily_budget_usd 2")
    setting.add_argument("person")
    setting.add_argument("name")
    setting.add_argument("value", help="JSON value: 2, true, \"ko\", null")
    session = sub.add_parser("session", help="print a new sign-in token for an approved person (tests, support)")
    session.add_argument("person")
    move = sub.add_parser("move", help="move a single-user workspace's contents into a person's folder")
    move.add_argument("workspace", type=Path)
    move.add_argument("--to", dest="person", required=True)
    args = parser.parse_args(argv)
    people = People(args.data)

    if args.command == "list":
        for person in people.ids():
            print(f"{person}: {json.dumps(asdict(people.settings(person)), ensure_ascii=False)}")
    elif args.command == "waiting":
        for entry in people.waiting():
            print(f"{entry['code']}  since {entry.get('at')}  {entry.get('email') or '(email hidden)'}")
    elif args.command == "approve":
        people.approve(args.code, args.person)
        print(f"approved as {args.person}")
    elif args.command == "add":
        people.add(args.person)
        print(f"added {args.person} (this Mac only until they sign in with Apple and you approve them)")
    elif args.command == "remove":
        people.remove(args.person)
        print(f"removed {args.person}; their folder is still at {people.folder(args.person)}")
    elif args.command == "set":
        try:
            value = json.loads(args.value)
        except ValueError:
            value = args.value
        settings = people.change_settings(args.person, {args.name: value},
                                          allowed=tuple(f.name for f in fields(Settings)))
        print(json.dumps(asdict(settings), ensure_ascii=False))
    elif args.command == "session":
        if not people.known(args.person):
            sys.exit(f"{args.person} is not approved")
        print(people.new_session(args.person))
    elif args.command == "move":
        target = people.workspace(args.person)
        source = args.workspace.resolve()
        if not source.is_dir():
            sys.exit(f"{source} is not a folder")
        clashes = [p.name for p in source.iterdir() if (target / p.name).exists()]
        if clashes:
            sys.exit(f"{target} already has {', '.join(sorted(clashes))}; nothing moved")
        # The background worker's harness is the machine's, not the person's (worker/harness.py reads it here).
        harness = source / "state" / "harness.json"
        if harness.exists() and not (people.root / "harness.json").exists():
            shutil.move(str(harness), people.root / "harness.json")
        for item in sorted(source.iterdir()):
            shutil.move(str(item), target / item.name)
        print(f"moved the contents of {source} into {target}")
        if not people.settings(args.person).bee:
            print(f"Bee is not connected for {args.person} yet: turn on Use Bee in the app, or run "
                  "curl -X POST http://localhost:8787/bee/connect -H 'X-Bee-Call-Dev: 1' on this Mac")


if __name__ == "__main__":
    main()
