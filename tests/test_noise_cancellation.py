"""Noise cancellation on the caller's audio (NOISE_CANCELLATION = bvc | nc | off, voice/agent.py): LiveKit Cloud's
enhanced noise cancellation (livekit-plugins-noise-cancellation) on what the voice server hears of the caller, before
speech recognition, turn detection and interruptions see it. Off until it is compared in a real call."""

import asyncio
import json

import pytest

from tests.fake_call import FakeCall, use_fakes
from tests.helpers import signed_in
from tests.test_voice_server import call_folders, events_of, quick  # noqa: F401 (quick is a fixture)
from voice import agent


@pytest.mark.parametrize("value, setting", [(None, "off"), ("", "off"), ("off", "off"), (" BVC ", "bvc"),
                                            ("nc", "nc")])
def test_the_setting_is_bvc_nc_or_off_and_off_when_unset(value, setting):
    env = {} if value is None else {"NOISE_CANCELLATION": value}
    assert agent.noise_setting(env) == setting


def test_any_other_setting_stops_the_voice_server_at_start():
    with pytest.raises(SystemExit, match="NOISE_CANCELLATION must be one of bvc, nc, off"):
        agent.noise_setting({"NOISE_CANCELLATION": "krisp"})


def test_bvc_and_nc_filter_the_caller_s_audio_and_off_leaves_it_as_it_was():
    from livekit.plugins import noise_cancellation

    bvc, nc, off = (agent.caller_audio(setting).audio_input for setting in ("bvc", "nc", "off"))
    assert bvc.noise_cancellation.options == noise_cancellation.BVC().options
    assert nc.noise_cancellation.options == noise_cancellation.NC().options
    assert off.noise_cancellation is None
    # LiveKit's own default for the gain: on without noise cancellation, as before; off with it, as the models
    # manage the level themselves (livekit/agents#7064).
    from livekit.agents.utils import is_given
    assert not any(is_given(audio.auto_gain_control) for audio in (bvc, nc, off))


@pytest.mark.parametrize("setting", ["bvc", "off"])
def test_a_call_hears_the_caller_through_the_setting_and_its_record_says_which(tmp_path, monkeypatch, quick,
                                                                               setting):
    me = signed_in(tmp_path)
    monkeypatch.setattr(agent, "NOISE_CANCELLATION", setting)
    call = FakeCall(me.people, {"direction": "inbound", "person": "matt"})
    use_fakes(monkeypatch, call)

    async def main():
        call.join()
        await agent.run_call(call, me.people)
        call.hang_up()
        await call.end()

    asyncio.run(main())
    filtered = call.session.started["room_options"].audio_input.noise_cancellation
    assert (filtered is not None) is (setting != "off")
    (folder,) = call_folders(me.workspace)
    (coordinator,) = [e for e in events_of(folder) if e["type"] == "coordinator"]
    assert coordinator["noise_cancellation"] == setting
    assert json.dumps(coordinator)  # the record stays plain JSON
