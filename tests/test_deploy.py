"""Running on the EC2 server as well as on a Mac: the agent name each machine registers under.

No network.
"""

from voice import agent
from voice.dispatch import AGENT_NAME, agent_name


def test_each_machine_names_its_voice_server_and_the_server_keeps_the_default():
    assert agent_name({}) == "bee-call"
    assert agent_name({"LIVEKIT_AGENT_NAME": ""}) == "bee-call"
    assert agent_name({"LIVEKIT_AGENT_NAME": " bee-call-dev "}) == "bee-call-dev"


def test_the_voice_server_registers_under_the_name_calls_are_dispatched_to():
    assert agent.server._agent_name == AGENT_NAME  # LiveKit keeps the name the rtc_session decorator was given
