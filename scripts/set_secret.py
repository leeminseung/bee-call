"""Store one secret in .env.local without showing it on screen.

Usage: python3 scripts/set_secret.py NAME
"""

import getpass
import os
import pathlib
import re
import sys

ENV_FILE = pathlib.Path(__file__).resolve().parent.parent / ".env.local"


def main() -> None:
    if len(sys.argv) != 2 or not re.fullmatch(r"[A-Z][A-Z0-9_]*", sys.argv[1]):
        sys.exit("usage: python3 scripts/set_secret.py NAME")
    name = sys.argv[1]
    value = getpass.getpass(f"{name} (input is hidden): ").strip()
    if not value:
        sys.exit("nothing entered; .env.local unchanged")

    text = ENV_FILE.read_text() if ENV_FILE.exists() else ""
    line = f"{name}={value}"
    pattern = re.compile(rf"^{name}=.*$", re.MULTILINE)
    if pattern.search(text):
        text = pattern.sub(lambda _: line, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += line + "\n"

    ENV_FILE.write_text(text)
    os.chmod(ENV_FILE, 0o600)
    print(f"saved {name} to {ENV_FILE.name} ({len(value)} characters)")


if __name__ == "__main__":
    main()
