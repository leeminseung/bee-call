"""Every test starts from the same ground, whatever .env.local holds on this Mac."""

import pytest

from beelink import cli


@pytest.fixture(autouse=True)
def plain_environment(monkeypatch):
    # The modules load .env.local when imported; these would change who a request is and where data lives.
    for name in ("BEE_CALL_DEV_PERSON", "BEE_CALL_PUBLIC_HOST", "BEE_CALL_DATA"):
        monkeypatch.delenv(name, raising=False)
    # Bee calls fail unless someone's login is chosen; tests that do not choose use the fake CLI as this machine.
    cli.use_machine_bee()
