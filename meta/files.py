"""Read-only file tools for the meta agent: list, read and grep inside the workspace.

The workspace is plain files (each call's transcript.md, events.jsonl, prep.md and job files; Bee batches under
inbox/; the meta session itself), so three familiar tools let the agent find what it needs, including the raw
record of a call when a summary is not enough. They cannot leave the workspace, cannot write, and skip what is not
for reading: push tokens and keys, and audio.
"""

import fnmatch
import re
from pathlib import Path

MAX_READ_CHARS = 12000  # one read_file answer (arbitrary: about 3K tokens)
MAX_LIST = 200
MAX_MATCHES = 50
MAX_FILE_BYTES = 5_000_000
HIDDEN = ("*token*.json", "*.p8", "*.wav", "*.mp3", "*.m4a", "*.tmp", ".*")  # secrets, audio, half-written files


class FileError(ValueError):
    pass


def _inside(workspace: Path, relative: str) -> Path:
    root = workspace.resolve()
    path = (root / (relative or ".")).resolve()
    if path != root and root not in path.parents:
        raise FileError(f"{relative!r} is outside the workspace")
    return path


def _hidden(path: Path) -> bool:
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in HIDDEN)


def list_files(workspace: Path, path: str = ".", pattern: str = "*") -> str:
    """Files and folders under `path` (one level), newest first, with sizes."""
    folder = _inside(workspace, path)
    if not folder.is_dir():
        raise FileError(f"{path!r} is not a folder")
    entries = [p for p in folder.iterdir() if not _hidden(p) and fnmatch.fnmatch(p.name, pattern)]
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    root = workspace.resolve()
    lines = [f"{p.relative_to(root)}/" if p.is_dir() else f"{p.relative_to(root)}  ({p.stat().st_size} bytes)"
             for p in entries[:MAX_LIST]]
    more = f"\n... and {len(entries) - MAX_LIST} more" if len(entries) > MAX_LIST else ""
    return "\n".join(lines) + more if lines else "(empty)"


def read_file(workspace: Path, path: str, start_line: int = 1, max_lines: int = 200) -> str:
    """Lines start_line.. of a text file, numbered."""
    file = _inside(workspace, path)
    if not file.is_file() or _hidden(file):
        raise FileError(f"{path!r} is not a file that can be read")
    if file.stat().st_size > MAX_FILE_BYTES:
        raise FileError(f"{path!r} is too large; use grep on it")
    lines = file.read_text(errors="replace").splitlines()
    start = max(1, int(start_line))
    chosen = lines[start - 1: start - 1 + max(1, min(int(max_lines), 1000))]
    text = "\n".join(f"{start + i}: {line}" for i, line in enumerate(chosen))
    if len(text) > MAX_READ_CHARS:
        text = text[:MAX_READ_CHARS] + "\n... (cut; read on with a later start_line)"
    rest = len(lines) - (start - 1 + len(chosen))
    return (text or "(no lines there)") + (f"\n({rest} more lines)" if rest > 0 else "")


def grep(workspace: Path, pattern: str, path: str = ".", glob: str = "*") -> str:
    """Lines matching a regular expression (case-insensitive) in files under `path` whose names match `glob`."""
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise FileError(f"bad pattern: {exc}") from None
    start = _inside(workspace, path)
    files = [start] if start.is_file() else sorted(start.rglob("*"), key=lambda p: str(p), reverse=True)
    root = workspace.resolve()
    matches: list[str] = []
    for file in files:
        if not file.is_file() or _hidden(file) or not fnmatch.fnmatch(file.name, glob) \
                or any(part.startswith(".") for part in file.relative_to(root).parts) \
                or file.stat().st_size > MAX_FILE_BYTES:
            continue
        for number, line in enumerate(file.read_text(errors="replace").splitlines(), 1):
            if regex.search(line):
                matches.append(f"{file.relative_to(root)}:{number}: {line.strip()[:300]}")
                if len(matches) >= MAX_MATCHES:
                    return "\n".join(matches) + "\n... (more matches; narrow the pattern or the path)"
    return "\n".join(matches) or "No matches."
