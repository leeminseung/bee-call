"""Running on the EC2 server as well as on a Mac: the agent name each machine registers under, and the AWS
credentials each process finds.

No network: AWS config files are written for the test, and credentials are made up.
"""

from types import SimpleNamespace

from voice import agent
from voice.aws_session import aws_profile, aws_session
from voice.dispatch import AGENT_NAME, agent_name


def test_each_machine_names_its_voice_server_and_the_server_keeps_the_default():
    assert agent_name({}) == "bee-call"
    assert agent_name({"LIVEKIT_AGENT_NAME": ""}) == "bee-call"
    assert agent_name({"LIVEKIT_AGENT_NAME": " bee-call-dev "}) == "bee-call-dev"


def test_the_voice_server_registers_under_the_name_calls_are_dispatched_to():
    assert agent.server._agent_name == AGENT_NAME  # LiveKit keeps the name the rtc_session decorator was given


def test_aws_profile_is_named_on_a_mac_and_left_to_the_default_chain_on_ec2():
    assert aws_profile({"AWS_PROFILE": "other"}, profiles=["bee-call"]) == "other"
    assert aws_profile({}, profiles=["default", "bee-call"]) == "bee-call"  # a Mac without AWS_PROFILE set
    assert aws_profile({"AWS_PROFILE": " "}, profiles=[]) is None  # EC2: no ~/.aws, the instance role
    assert aws_profile({}, profiles=["default"]) is None


def test_aws_session_reads_the_profiles_this_machine_has(tmp_path, monkeypatch):
    config = tmp_path / "config"
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "credentials"))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    config.write_text("[profile bee-call]\nregion = ap-southeast-2\n")
    assert aws_session().profile_name == "bee-call"
    config.write_text("[default]\nregion = ap-southeast-2\n")
    assert aws_session().profile_name == "default"  # boto3's name for the default chain


def test_transcribe_gets_the_keys_as_they_are_at_the_start_of_each_call():
    keys = iter([("AKIA1", "secret1", "token1"), ("ASIA2", "secret2", "token2")])

    class Renewing:  # like an instance role's credentials, which botocore renews before they expire
        def get_frozen_credentials(self):
            access, secret, token = next(keys)
            return SimpleNamespace(access_key=access, secret_key=secret, token=token)

    session = SimpleNamespace(get_credentials=lambda: Renewing())
    first, second = agent.transcribe_credentials(session), agent.transcribe_credentials(session)
    assert (first.access_key_id, first.session_token) == ("AKIA1", "token1")
    assert (second.access_key_id, second.secret_access_key, second.session_token) == ("ASIA2", "secret2", "token2")
