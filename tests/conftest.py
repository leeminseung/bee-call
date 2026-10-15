"""Every test starts from the same ground, whatever .env.local holds on this Mac."""

from pathlib import Path

import pytest

from beelink import cli

FAKE_BEE = Path(__file__).parent / "fakebee" / "bee"


@pytest.fixture(autouse=True)
def plain_environment(monkeypatch, tmp_path):
    # The modules load .env.local when imported; these would change who a request is and where data lives.
    for name in ("BEE_CALL_DEV_PERSON", "BEE_CALL_PUBLIC_HOST", "BEE_CALL_DATA", "BEE_CALL_CLOCK_FILE", "APNS_URL"):
        monkeypatch.delenv(name, raising=False)
    # Bee calls fail unless someone's login is chosen; tests that do not choose use the fake CLI as this machine.
    cli.use_machine_bee()
    # Never the real `bee`: on this Mac it is signed in to the user's own Bee account. A test that sets up the fake
    # itself (its own log, fixtures) overrides these.
    monkeypatch.setenv("BEE_BIN", str(FAKE_BEE))
    monkeypatch.setenv("FAKE_BEE_LOG", str(tmp_path / "fake-bee-calls.jsonl"))
