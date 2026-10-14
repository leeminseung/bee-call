"""The AWS credentials every process uses: a named profile on a Mac, the instance role on the EC2 server.

AWS_PROFILE wins when it is set. Otherwise the bee-call profile, if this machine's AWS config has it (the Mac),
and otherwise boto3's default chain, which on EC2 finds the instance role and renews its keys before they expire.
"""

import os
from collections.abc import Iterable, Mapping

import boto3

PROFILE = "bee-call"


def aws_profile(env: Mapping[str, str] = os.environ, profiles: Iterable[str] | None = None) -> str | None:
    """The profile to use, or None for the default chain. `profiles` defaults to the ones in ~/.aws."""
    named = env.get("AWS_PROFILE", "").strip()
    if named:
        return named
    if profiles is None:
        profiles = boto3.Session().available_profiles
    return PROFILE if PROFILE in profiles else None


def aws_session(**kwargs) -> boto3.Session:
    return boto3.Session(profile_name=aws_profile(), **kwargs)
