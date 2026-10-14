"""Shared test helpers."""

from pathlib import Path
from types import SimpleNamespace

from voice.people import People


def signed_in(root: Path, person: str = "matt") -> SimpleNamespace:
    """An approved person with a sign-in token, in a data root under `root`."""
    people = People(root / "data")
    people.add(person)
    token = people.new_session(person)
    return SimpleNamespace(people=people, person=person, token=token, workspace=people.workspace(person),
                           headers={"Authorization": f"Bearer {token}"})
