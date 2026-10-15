"""Read-only file tools for the meta agent: list, read and grep inside the workspace.

The workspace is plain files (each call's transcript.md, events.jsonl, prep.md and job files; Bee batches under
inbox/; the meta session itself), so three familiar tools let the agent find what it needs, including the raw
record of a call when a summary is not enough. They cannot leave the workspace (no symlinks followed out of it),
cannot write, and skip what is not for reading: push tokens and keys, audio, hidden files, and a call's model log
and Claude history. Bee's locations
(addresses, coordinates) are blanked in what they return, since the timeline shows tool results on the LAN.
A pattern gets a time budget, so a pattern that backtracks without end cannot stall the process.
"""

import fnmatch
import re
import time
from pathlib import Path

import regex

MAX_READ_CHARS = 12000  # one read_file answer (arbitrary: about 3K tokens)
MAX_LIST = 200
MAX_MATCHES = 50
MAX_FILE_BYTES = 5_000_000
GREP_SECONDS = 3.0  # a whole grep (arbitrary)
LINE_SECONDS = 0.05  # one line against the pattern (arbitrary)
# Secrets (push tokens, keys, a Bee CLI login: token-prod, pairing-prod.json), audio, half-written files, and a call's
# private debugging records (voice/session_server.py PRIVATE): the model log repeats the whole conversation in every
# request, so it would fill every search, and the timeline shows what the meta agent reads.
HIDDEN = ("*token*", "pairing-*", "proxy-*", "*.p8", "*.wav", "*.mp3", "*.m4a", "*.tmp", ".*",
          "model-log.jsonl", "claude-history.json", "fake-claude.jsonl")
LOCATION = re.compile(r'"(primary_location|location|address|latitude|longitude|lat|lng|lon)"\s*:\s*'
                      r'(\{[^{}]*\}|"[^"]*"|-?\d+(?:\.\d+)?)', re.IGNORECASE)


class FileError(ValueError):
    pass


def _root(workspace: Path) -> Path:
    return workspace.resolve()


def _inside(workspace: Path, relative: str) -> Path:
    root = _root(workspace)
    path = (root / (relative or ".")).resolve()
    if path != root and root not in path.parents:
        raise FileError(f"{relative!r} is outside the workspace")
    return path


def _readable(path: Path, root: Path) -> bool:
    """Inside the workspace once links are resolved, and no part of the path, as named or as resolved, is hidden
    or secret ("..", dot-folders, token files, audio)."""
    try:
        resolved, named = path.resolve(), path.absolute()
    except OSError:
        return False
    if resolved != root and root not in resolved.parents:
        return False  # a symlink pointing out of the workspace
    names: list[str] = []
    for form in (named, resolved):
        try:
            names += list(form.relative_to(root).parts)
        except ValueError:
            pass
    # In lower case: a Mac's disk finds "VOIP_TOKEN.json" under "voip_token.json" (BC-049).
    return not any(fnmatch.fnmatchcase(name.lower(), pattern) for name in names for pattern in HIDDEN)


def redact(text: str) -> str:
    return LOCATION.sub(lambda m: f'"{m.group(1)}": "[hidden]"', text)


def list_files(workspace: Path, path: str = ".", pattern: str = "*") -> str:
    """Files and folders under `path` (one level), newest first, with sizes."""
    root = _root(workspace)
    folder = _inside(workspace, path)
    if not folder.is_dir() or not _readable(root / (path or "."), root):
        raise FileError(f"{path!r} is not a folder that can be listed")
    entries = [p for p in folder.iterdir() if _readable(p, root) and fnmatch.fnmatch(p.name, pattern)]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    lines = [f"{p.relative_to(root)}/" if p.is_dir() else f"{p.relative_to(root)}  ({p.stat().st_size} bytes)"
             for p in entries[:MAX_LIST]]
    more = f"\n... and {len(entries) - MAX_LIST} more" if len(entries) > MAX_LIST else ""
    return "\n".join(lines) + more if lines else "(empty)"


def read_file(workspace: Path, path: str, start_line: int = 1, max_lines: int = 200) -> str:
    """Lines start_line.. of a text file, numbered."""
    root = _root(workspace)
    file = _inside(workspace, path)
    if not file.is_file() or not _readable(root / path, root):
        raise FileError(f"{path!r} is not a file that can be read")
    if file.stat().st_size > MAX_FILE_BYTES:
        raise FileError(f"{path!r} is too large; use grep on it")
    lines = file.read_text(errors="replace").splitlines()
    start = max(1, int(start_line))
    chosen = lines[start - 1: start - 1 + max(1, min(int(max_lines), 1000))]
    text = redact("\n".join(f"{start + i}: {line}" for i, line in enumerate(chosen)))
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + "\n... (cut; read on with a later start_line)"
    rest = len(lines) - (start - 1 + len(chosen))
    return (text or "(no lines there)") + (f"\n({rest} more lines)" if rest > 0 else "")


def grep(workspace: Path, pattern: str, path: str = ".", glob: str = "*") -> str:
    """Lines matching a regular expression (case-insensitive) in files under `path` whose names match `glob`."""
    try:
        compiled = regex.compile(pattern, regex.IGNORECASE)
    except regex.error as exc:
        raise FileError(f"bad pattern: {exc}") from None
    root = _root(workspace)
    start = _inside(workspace, path)
    files = [start] if start.is_file() else sorted(start.rglob("*"), key=lambda p: str(p), reverse=True)
    deadline = time.monotonic() + GREP_SECONDS
    matches: list[str] = []
    for file in files:
        if file.is_symlink() or not file.is_file() or not _readable(file, root) \
                or not fnmatch.fnmatch(file.name, glob) or file.stat().st_size > MAX_FILE_BYTES:
            continue
        for number, line in enumerate(file.read_text(errors="replace").splitlines(), 1):
            if time.monotonic() > deadline:
                return "\n".join(matches + ["... (stopped: the search took too long; narrow the pattern or the path)"])
            try:
                found = compiled.search(line, timeout=LINE_SECONDS)
            except TimeoutError:
                return "\n".join(matches + ["... (stopped: this pattern is too slow; use a simpler one)"])
            if found:
                matches.append(redact(f"{file.relative_to(root)}:{number}: {line.strip()[:300]}"))
                if len(matches) >= MAX_MATCHES:
                    return "\n".join(matches) + "\n... (more matches; narrow the pattern or the path)"
    return "\n".join(matches) or "No matches."
