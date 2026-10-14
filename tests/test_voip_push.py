"""VoIP push: the provider token, the app's device token, the push request, and the ring at call placement.

No network: APNs is an httpx mock transport, and the signing key is made up for the test.
"""

import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from beelink.planner import RulePlanner
from voice import voip_push
from voice.dispatch import place_call
from voice.outbound import saved_trigger
from voice.offers import OfferStore
from voice.people import People
from voice.voip_push import ApnsConfig, PushError, load_device_token, provider_token, save_device_token
from tests.test_outbound import inbox

DEVICE = "ab" * 32
LINE = {"at": "2026-10-13T21:00:00", "room": "call-20261013-210000", "expires_at": "2026-10-13T21:01:00",
        "expires_epoch": 1791892860, "topic": "Planned a Saturday hike with Dana", "kind": "conversation",
        "reason": "...", "plan": None}


KEY = ec.generate_private_key(ec.SECP256R1())  # made up for the tests


@pytest.fixture
def person(tmp_path):
    people = People(tmp_path / "data")
    people.add("matt")
    return people.person("matt")


@pytest.fixture
def config(tmp_path) -> ApnsConfig:
    path = tmp_path / "AuthKey_TEST123456.p8"  # the shape of Apple's key file
    path.write_bytes(KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    return ApnsConfig(path, "TEST123456", "TEAM123456")


def test_the_provider_token_is_signed_with_the_key_and_shared_for_a_while(config, tmp_path):
    cache = tmp_path / "state" / "apns_provider_token.json"
    first = provider_token(config, now=1_000_000, cache=cache)
    claims = jwt.decode(first, KEY.public_key(), algorithms=["ES256"])
    assert claims == {"iss": "TEAM123456", "iat": 1_000_000}
    assert jwt.get_unverified_header(first) == {"alg": "ES256", "kid": "TEST123456", "typ": "JWT"}
    assert oct(cache.stat().st_mode & 0o777) == "0o600"
    assert provider_token(config, now=1_000_000 + 60, cache=cache) == first  # another process, a minute later
    assert provider_token(config, now=1_000_000 + 41 * 60, cache=cache) != first


def test_the_device_token_is_checked_and_kept(person):
    with pytest.raises(ValueError):
        save_device_token(person, "not-hex", "development")
    with pytest.raises(ValueError):
        save_device_token(person, DEVICE, "staging")
    assert load_device_token(person) is None
    save_device_token(person, DEVICE.upper(), "development")
    assert load_device_token(person)["token"] == DEVICE


def push(person, config, status=200, body=b"", headers=None):
    requests = []

    def apns(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, content=body, headers=headers or {})

    result = asyncio.run(voip_push.ring(LINE, person, config, transport=httpx.MockTransport(apns)))
    return result, requests


def test_nothing_is_sent_until_the_key_and_the_phone_are_set_up(person, config, monkeypatch):
    for name in ("APNS_KEY_PATH", "APNS_KEY_ID", "APNS_TEAM_ID"):  # .env.local may hold the real ones
        monkeypatch.delenv(name, raising=False)

    def no_network(request):
        raise AssertionError("nothing should be sent")

    offline = httpx.MockTransport(no_network)
    assert asyncio.run(voip_push.ring(LINE, person, config, transport=offline)) is None  # no phone registered
    save_device_token(person, DEVICE, "development")
    assert asyncio.run(voip_push.ring(LINE, person, None, transport=offline)) is None  # no key in the environment


def test_the_push_carries_the_ring_to_the_registered_phone(person, config):
    save_device_token(person, DEVICE, "development")
    result, (request,) = push(person, config, headers={"apns-id": "1234-ABCD"})
    assert result == "1234-ABCD"
    assert str(request.url) == f"https://api.sandbox.push.apple.com/3/device/{DEVICE}"
    assert request.headers["apns-topic"] == "com.leeminseung.beecall.voip"
    assert request.headers["apns-push-type"] == "voip" and request.headers["apns-priority"] == "10"
    assert request.headers["authorization"].startswith("bearer ey")
    assert request.headers["apns-expiration"] == "1791892860"
    assert json.loads(request.content) == {"room": LINE["room"], "topic": LINE["topic"], "expires_at": LINE["expires_at"],
                                           "expires_epoch": 1791892860}


def test_a_token_apple_no_longer_knows_is_dropped(person, config):
    save_device_token(person, DEVICE, "production")
    with pytest.raises(PushError, match="410 Unregistered"):
        push(person, config, status=410, body=b'{"reason": "Unregistered"}')
    assert load_device_token(person) is None


def test_other_refusals_keep_the_token(person, config):
    save_device_token(person, DEVICE, "production")
    with pytest.raises(PushError, match="403 InvalidProviderToken"):
        push(person, config, status=403, body=b'{"reason": "InvalidProviderToken"}')
    assert load_device_token(person)


def test_the_token_server_keeps_the_app_s_voip_token(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from tests.helpers import signed_in
    from voice.token_server import make_app

    me = signed_in(tmp_path)

    async def main():
        app = make_app("wss://example", "APIkey", "s" * 32, me.people, room_exists=None)
        async with TestClient(TestServer(app), headers=me.headers) as client:
            bad = await client.post("/voip", json={"token": "xyz", "environment": "development"})
            good = await client.post("/voip", json={"token": DEVICE, "environment": "development"})
            return bad.status, good.status

    assert asyncio.run(main()) == (400, 204)
    assert load_device_token(me.person)["environment"] == "development"


def test_placing_a_call_pushes_its_ring_line_and_a_failed_push_does_not_stop_it(tmp_path, person, monkeypatch):
    pushed = []

    async def fake_ring(line, person):
        pushed.append(line)
        raise PushError("APNs answered 500")

    async def create_room(request):
        pass

    monkeypatch.setattr(voip_push, "ring", fake_ring)
    plan = RulePlanner(person.workspace).decide(saved_trigger(inbox(tmp_path)))
    store = OfferStore(person.workspace)
    store.answer(store.ask(plan, datetime.now()).id, "now", datetime.now())
    claim = store.claim(store.due(datetime.now())[0].id, datetime.now())
    room = asyncio.run(place_call(claim, person, lk=SimpleNamespace(room=SimpleNamespace(create_room=create_room))))
    (line,) = pushed
    assert line["room"] == room and line["topic"] == plan.topic
    written = json.loads((person.workspace / "state" / "outbound.jsonl").read_text().splitlines()[-1])
    assert written == line
