"""Is Claude on Bedrock open for this account yet? Prints lines ready to paste into a support case.

One tiny InvokeModel call (16 output tokens at most) per inference profile, plus the
read-only availability check. A refused call is not billed.

Run: uv run python probes/claude_access_check.py
"""

import datetime
import json
import os

import boto3
import botocore
from dotenv import load_dotenv

load_dotenv(".env.local")
PROFILE = os.environ.get("AWS_PROFILE", "bee-call")
REGION = "ap-southeast-2"
MODEL = "anthropic.claude-sonnet-5-5"
PROFILES = ["au.anthropic.claude-sonnet-5-5", "global.anthropic.claude-sonnet-5-5"]


def main() -> None:
    session = boto3.Session(profile_name=PROFILE)
    avail = session.client("bedrock", region_name=REGION).get_foundation_model_availability(modelId=MODEL)
    print(f"GetFoundationModelAvailability {MODEL} in {REGION}: agreementAvailability "
          f"{avail['agreementAvailability']['status']}, authorizationStatus {avail['authorizationStatus']}, "
          f"entitlementAvailability {avail['entitlementAvailability']}, regionAvailability {avail['regionAvailability']}")
    runtime = session.client("bedrock-runtime", region_name=REGION)
    body = json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 16,
                       "messages": [{"role": "user", "content": "Say ok."}]})
    for profile in PROFILES:
        at = datetime.datetime.now(datetime.timezone.utc).strftime("%B %-d, %Y at %H:%M:%S UTC")
        try:
            runtime.invoke_model(modelId=profile, body=body)
            print(f"ACCESS OPEN: {profile} answered. No support case needed for it.")
        except botocore.exceptions.ClientError as exc:
            err = exc.response["Error"]
            print(f"- Still failing on {at}, inference profile {profile}, request ID "
                  f"{exc.response['ResponseMetadata'].get('RequestId')} ({err['Code']}: {err['Message'][:60]}...)")


if __name__ == "__main__":
    main()
