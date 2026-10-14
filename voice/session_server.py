"""Live session screen: a small web server for watching a call as it happens.

The voice server writes each call into workspace/<date>/<time>/ (see
voice/recorder.py). This server only reads those folders. It serves a static
page (web/) that shows one call: why the AI called, the turns, background jobs
and what will be saved to Bee.

Run: uv run python voice/session_server.py [--workspace DIR] [--port 8788]
Open the printed LAN URL on the iPhone (same Wi-Fi) or http://localhost:8788/.

GET /                                          the page (web/index.html)
GET /static/<file>                             files under web/
GET /api/sessions                              sessions, newest first
GET /api/sessions/<date>/<time>/events         Server-Sent Events: every line of events.jsonl
                                               (SSE id = line number, so a reconnect resumes),
                                               then new lines as they are written, until call_ended
GET /api/sessions/<date>/<time>/files/<path>   one file inside that session folder, never audio/ or inputs/
GET /api/meta                                  the meta agent's session (newest first) and its notes

Development only: anyone on the same network can read transcripts and job files. Raw call audio
(audio/) and raw Bee data (inputs/, which carries places and coordinates) are never served.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from aiohttp import web

REPO = Path(__file__).resolve().parent.parent
WEB_DIR = REPO / "web"
DEFAULT_WORKSPACE = REPO / "workspace"
DEFAULT_PORT = 8788

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
TIME_RE = re.compile(r"(\d{4})(?:-(\d+))?")
POLL_SECONDS = 0.2
KEEPALIVE_SECONDS = 15.0
LIVE_SECONDS = 120.0  # no new event for this long: the list stops calling the session live
PREVIEW_CHARS = 160
PLAIN_TEXT = {".md", ".txt", ".log", ".csv", ".jsonl"}  # shown as text instead of downloaded
PAGE_CSP = "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'"
# Job files are made from outside content; a sandboxed response cannot run script on this origin.
FILE_HEADERS = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "sandbox"}
# Host names a request may use. Anything else is refused, so a web page cannot point its own
# domain at this Mac (DNS rebinding) and read calls from a browser on the same network.
LOCAL_SUFFIXES = (".local", ".localhost", ".lan", ".home.arpa", ".internal")
PRIVATE_DIRS = ("audio", "inputs")  # raw call audio and raw Bee data stay off the network


def parse_event(raw: bytes) -> dict[str, Any] | None:
    try:
        event = json.loads(raw)
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


# ---------- session list ----------

def read_summary(events: Path) -> dict[str, Any]:
    """What the session list needs, from one pass over events.jsonl."""
    s: dict[str, Any] = {"started_at": None, "room": None, "ended": False, "closed": False,
                         "end_t": None, "last_t": 0.0, "why": None, "first_user": None}
    with events.open("rb") as f:
        for raw in f:
            event = parse_event(raw) if raw.endswith(b"\n") else None  # skip a line still being written
            if event is None:
                continue
            t = number(event.get("t"))
            if t is not None:
                s["last_t"] = max(s["last_t"], t)
            kind = event.get("type")
            if kind == "call_started":
                s["started_at"], s["room"] = event.get("started_at"), event.get("room")
            elif kind in ("session_closed", "call_ended"):
                s["closed"] = True
                s["ended"] = s["ended"] or kind == "call_ended"
                if s["end_t"] is None:
                    s["end_t"] = t
            elif kind == "why_calling" and not s["why"] and isinstance(event.get("summary"), str):
                s["why"] = event["summary"]
            elif (kind == "message" and event.get("role") == "user" and not s["first_user"]
                  and isinstance(event.get("text"), str)):
                s["first_user"] = event["text"]
    return s


def child_dirs(parent: Path, pattern: re.Pattern[str]) -> list[Path]:
    try:
        return [p for p in parent.iterdir() if pattern.fullmatch(p.name) and p.is_dir()]
    except OSError:
        return []


def sort_key(session_id: str) -> tuple[str, str, int]:
    date, clock = session_id.split("/")
    m = TIME_RE.fullmatch(clock)
    return date, m.group(1), int(m.group(2) or 1)


class SessionIndex:
    """Summaries of the session folders; a folder is re-read only when its events.jsonl changes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}

    def sessions(self) -> list[dict[str, Any]]:
        found = []
        now = time.time()
        root = self.root.resolve()
        for date_dir in child_dirs(self.root, DATE_RE):
            for folder in child_dirs(date_dir, TIME_RE):
                events = folder / "events.jsonl"
                try:
                    if not folder.resolve().is_relative_to(root):
                        continue  # a symlink out of the workspace; the other endpoints refuse it too
                    st = events.stat()
                    stamp = (st.st_mtime_ns, st.st_size)
                    session_id = f"{date_dir.name}/{folder.name}"
                    cached = self._cache.get(session_id)
                    if cached is None or cached[0] != stamp:
                        cached = (stamp, read_summary(events))
                        self._cache[session_id] = cached
                except OSError:
                    continue
                s = cached[1]
                hhmm = TIME_RE.fullmatch(folder.name).group(1)
                preview = s["why"] or s["first_user"]
                found.append({
                    "id": session_id,
                    "started_at": s["started_at"] or f"{date_dir.name}T{hhmm[:2]}:{hhmm[2:]}:00",
                    "room": s["room"],
                    "ended": s["ended"],
                    "live": not s["closed"] and now - st.st_mtime < LIVE_SECONDS,
                    "duration": round(s["end_t"] if s["end_t"] is not None else s["last_t"], 1),
                    "preview": preview[:PREVIEW_CHARS] if preview else None,
                })
        found.sort(key=lambda item: sort_key(item["id"]), reverse=True)
        return found


# ---------- events stream ----------

class LineTail:
    """Complete lines appended to a file since the previous read, numbered from 1."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.offset = 0
        self.count = 0
        self._partial = b""

    def read_new(self) -> list[tuple[int, str, dict[str, Any]]]:
        try:
            with self.path.open("rb") as f:
                f.seek(self.offset)
                chunk = f.read()
        except FileNotFoundError:
            return []
        self.offset += len(chunk)
        *lines, self._partial = (self._partial + chunk).split(b"\n")
        out = []
        for raw in lines:
            self.count += 1
            event = parse_event(raw)
            if event is not None:  # a broken line keeps its number but is not sent
                out.append((self.count, raw.decode("utf-8", "replace").strip().replace("\r", " "), event))
        return out


def frames(lines: list[tuple[int, str, dict[str, Any]]]) -> bytes:
    return "".join(f"id: {n}\ndata: {text}\n\n" for n, text, _ in lines).encode()


def has_end(lines: list[tuple[int, str, dict[str, Any]]]) -> bool:
    return any(event.get("type") == "call_ended" for _, _, event in lines)


def resume_point(request: web.Request) -> int:
    value = request.headers.get("Last-Event-ID", "").strip()
    return int(value) if value.isascii() and value.isdigit() else 0


async def stream_events(request: web.Request) -> web.StreamResponse:
    folder = session_folder(request)
    path = folder / "events.jsonl"
    after = resume_point(request)
    tail = LineTail(path)
    backlog = tail.read_new()
    ended = has_end(backlog)
    fresh = [line for line in backlog if line[0] > after]
    if after and ended and not fresh:
        return web.Response(status=204)  # resumed after the end: 204 tells EventSource to stop

    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                           "X-Accel-Buffering": "no"})
    await response.prepare(request)
    stopping = request.app[STOPPING]
    try:
        await response.write(b"retry: 2000\n\n" + frames(fresh))
        try:
            idle = max(0.0, time.time() - path.stat().st_mtime)
        except OSError:
            idle = 0.0
        ready = json.dumps({"lines": tail.count, "idle_s": round(idle, 1)})
        await response.write(f"event: ready\ndata: {ready}\n\n".encode())
        last_write = time.monotonic()
        while not ended and not stopping.is_set():
            await asyncio.sleep(POLL_SECONDS)
            if request.transport is None or request.transport.is_closing():
                break
            new = [line for line in tail.read_new() if line[0] > after]
            if new:
                await response.write(frames(new))
                ended = has_end(new)
                last_write = time.monotonic()
            elif time.monotonic() - last_write >= KEEPALIVE_SECONDS:
                await response.write(b": keepalive\n\n")
                last_write = time.monotonic()
    except ConnectionResetError:
        pass  # the page went away
    return response


# ---------- files ----------

def session_folder(request: web.Request) -> Path:
    date, clock = request.match_info["date"], request.match_info["time"]
    if not DATE_RE.fullmatch(date) or not TIME_RE.fullmatch(clock):
        raise web.HTTPNotFound()
    root = request.app[WORKSPACE]
    folder = root / date / clock
    if not folder.is_dir() or not folder.resolve().is_relative_to(root.resolve()):
        raise web.HTTPNotFound()
    return folder


def resolve_inside(folder: Path, rel: str) -> Path:
    """The real path of `rel` inside the session folder, or an HTTP error."""
    parts = rel.split("/")
    if (rel.startswith("/") or "\\" in rel or "\x00" in rel
            or any(part in ("", ".", "..") for part in parts)):
        raise web.HTTPForbidden(text="path not allowed\n")
    if parts[0].casefold() in PRIVATE_DIRS:
        raise web.HTTPForbidden(text=f"{parts[0]} is never served\n")
    base = folder.resolve()
    try:
        target = (base / rel).resolve(strict=True)
    except (OSError, RuntimeError):
        raise web.HTTPNotFound() from None
    if target == base or not target.is_relative_to(base):
        raise web.HTTPForbidden(text="path leaves the session folder\n")
    first = base / target.relative_to(base).parts[0]  # catches a symlink into audio/ and AUDIO/ on macOS
    for name in PRIVATE_DIRS:
        private = base / name
        if first.name.casefold() == name or (private.exists() and os.path.samefile(first, private)):
            raise web.HTTPForbidden(text=f"{name} is never served\n")
    if not target.is_file():
        raise web.HTTPNotFound()
    return target


async def session_file(request: web.Request) -> web.FileResponse:
    target = resolve_inside(session_folder(request), request.match_info["path"])
    headers = dict(FILE_HEADERS)
    if target.suffix.lower() in PLAIN_TEXT:
        headers["Content-Type"] = "text/plain; charset=utf-8"
    return web.FileResponse(target, headers=headers)


# ---------- app ----------

async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html",
                            headers={"Cache-Control": "no-cache", "Content-Security-Policy": PAGE_CSP})


async def list_sessions(request: web.Request) -> web.Response:
    return web.json_response(request.app[INDEX].sessions(), headers={"Cache-Control": "no-store"})


async def no_cache_static(request: web.Request, response: web.StreamResponse) -> None:
    if request.path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")  # page edits show up on reload


async def stop_streams(app: web.Application) -> None:
    app[STOPPING].set()  # open event streams end within one poll, so Ctrl+C does not hang


def local_host(name: str | None) -> bool:
    if not name:
        return False
    try:
        ipaddress.ip_address(name.strip("[]"))
        return True  # an IP literal never comes from DNS rebinding
    except ValueError:
        pass
    name = name.lower().rstrip(".")
    return name == "localhost" or name.endswith(LOCAL_SUFFIXES) or name == socket.gethostname().lower()


@web.middleware
async def local_hosts_only(request: web.Request, handler):
    if not local_host(request.url.host):
        raise web.HTTPMisdirectedRequest(text="unknown host name; use this Mac's IP address or .local name\n")
    return await handler(request)


WORKSPACE = web.AppKey("workspace", Path)
INDEX = web.AppKey("index", SessionIndex)
STOPPING = web.AppKey("stopping", asyncio.Event)


META_ENTRIES = 100  # the newest entries the timeline shows (arbitrary)


async def meta_session(request: web.Request) -> web.Response:
    """What the meta agent saw and decided between calls, as it wrote it (meta/session.py). Read here directly,
    so this server stays a standalone script."""
    folder = request.app[WORKSPACE] / "meta"
    entries = []
    try:
        lines = (folder / "session.jsonl").read_text(errors="replace").splitlines()[-META_ENTRIES:]
    except OSError:
        lines = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    try:
        state = (folder / "state.md").read_text(errors="replace").strip() or None
    except OSError:
        state = None
    return web.json_response({"entries": list(reversed(entries)), "state": state},
                             dumps=lambda data: json.dumps(data, ensure_ascii=False, default=str))


def make_app(workspace: Path) -> web.Application:
    app = web.Application(middlewares=[local_hosts_only])
    app[WORKSPACE] = workspace
    app[INDEX] = SessionIndex(workspace)
    app[STOPPING] = asyncio.Event()
    app.on_shutdown.append(stop_streams)
    app.on_response_prepare.append(no_cache_static)
    app.router.add_get("/", index)
    app.router.add_get("/api/sessions", list_sessions)
    app.router.add_get("/api/sessions/{date}/{time}/events", stream_events, allow_head=False)
    app.router.add_get("/api/sessions/{date}/{time}/files/{path:.+}", session_file)
    app.router.add_get("/api/meta", meta_session)
    app.router.add_static("/static/", WEB_DIR)
    return app


def lan_addresses() -> list[tuple[str, str]]:
    """(interface, IPv4) pairs a phone on the same Wi-Fi can reach. Tunnels such as a VPN are left out,
    because the default route can point at one and its address is not reachable from the phone."""
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    found, iface = [], ""
    for line in out.splitlines():
        if line and not line[0].isspace():
            iface = line.split(":", 1)[0]
        m = re.match(r"\s+inet (\d+\.\d+\.\d+\.\d+)\s", line)
        if m and iface.startswith(("en", "bridge")) and ipaddress.ip_address(m.group(1)).is_private:
            found.append((iface, m.group(1)))
    if not found:  # not macOS, or no Wi-Fi: fall back to the address of the default route
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            try:
                s.connect(("10.255.255.255", 1))
                found.append(("default route", s.getsockname()[0]))
            except OSError:
                pass
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the live session screen.")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                        help="folder that holds <date>/<time>/ session folders (default: workspace/)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    workspace = args.workspace.expanduser().resolve()
    print(f"session screen on http://localhost:{args.port}/ (this Mac); from the iPhone on the same Wi-Fi:", flush=True)
    for iface, ip in lan_addresses():
        print(f"  http://{ip}:{args.port}/  ({iface})", flush=True)
    if socket.gethostname().endswith(".local"):
        print(f"  http://{socket.gethostname()}:{args.port}/", flush=True)
    print(f"reading sessions from {workspace}" + ("" if workspace.is_dir() else " (missing for now)"), flush=True)
    web.run_app(make_app(workspace), host=args.host, port=args.port, print=None, shutdown_timeout=5)


if __name__ == "__main__":
    main()
