"""Shared test helpers."""

import functools
from pathlib import Path
from types import SimpleNamespace

from voice.people import People


def signed_in(root: Path, person: str = "matt") -> SimpleNamespace:
    """An approved person with a sign-in token, in a data root under `root`."""
    people = People(root / "data")
    people.add(person)
    token = people.new_session(person)
    return SimpleNamespace(people=people, id=person, person=people.person(person), token=token,
                           workspace=people.workspace(person), headers={"Authorization": f"Bearer {token}"})


def fake_livekit(requests: list) -> SimpleNamespace:
    """LiveKit's room API, keeping each CreateRoomRequest it gets."""
    async def create_room(request):
        requests.append(request)

    return SimpleNamespace(room=SimpleNamespace(create_room=create_room))


def placing(requests: list):
    """The real place_call (voice/dispatch.py), with LiveKit faked: for call_due's `place`."""
    from voice.dispatch import place_call
    return functools.partial(place_call, lk=fake_livekit(requests))
