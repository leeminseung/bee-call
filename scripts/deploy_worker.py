"""Creates or updates the cloud worker: its IAM execution role and its AgentCore Harness (Sydney).

Run: uv run python scripts/deploy_worker.py              # role + harness, then wait until READY
     uv run python scripts/deploy_worker.py --smoke      # also run one tiny job and time the round trips
     uv run python scripts/deploy_worker.py --model global.anthropic.claude-opus-5-5

The AWS user needs the permissions in the project notes (AgentCore harness actions, and
creating and passing the one role named ROLE_NAME). The harness ARN is saved to
workspace/state/harness.json, where worker/harness.py reads it.
"""

import argparse
import asyncio
import json
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from worker.harness import (DEFAULT_MODEL, HARNESS_NAME, REGION, STATE_FILE, HarnessWorker,  # noqa: E402
                            harness_definition, update_request)
from worker.runner import Job  # noqa: E402
from voice.people import DEFAULT_TIME_ZONE  # noqa: E402

load_dotenv(".env.local")
ROLE_NAME = "bee-call-worker"

TRUST = {"Version": "2012-10-17", "Statement": [{
    "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole"}]}


def role_policy(account: str) -> dict:
    """The sample execution role policy from the AgentCore harness security docs, cut to what the worker uses."""
    arn = f"arn:aws:bedrock-agentcore:{REGION}:{account}"
    logs = f"arn:aws:logs:{REGION}:{account}:log-group"
    return {"Version": "2012-10-17", "Statement": [
        {"Sid": "BedrockModelInvocation", "Effect": "Allow",
         "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
         "Resource": ["arn:aws:bedrock:*::foundation-model/*", f"arn:aws:bedrock:*:{account}:inference-profile/*"]},
        {"Sid": "EcrPublicTokenAccess", "Effect": "Allow", "Action": ["ecr-public:GetAuthorizationToken"], "Resource": "*"},
        {"Sid": "StsForEcrPublicPull", "Effect": "Allow", "Action": ["sts:GetServiceBearerToken"], "Resource": "*"},
        {"Sid": "XRayTracingAccess", "Effect": "Allow", "Resource": "*", "Action": [
            "xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]},
        {"Sid": "CloudWatchLogsGroup", "Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:DescribeLogStreams"],
         "Resource": f"{logs}:/aws/bedrock-agentcore/runtimes/*"},
        {"Sid": "CloudWatchLogsDescribeGroups", "Effect": "Allow", "Action": ["logs:DescribeLogGroups"],
         "Resource": f"{logs}:*"},
        {"Sid": "CloudWatchLogsStream", "Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"{logs}:/aws/bedrock-agentcore/runtimes/*:log-stream:*"},
        {"Sid": "CloudWatchLogsPutResourcePolicy", "Effect": "Allow", "Action": ["logs:PutResourcePolicy"], "Resource": "*"},
        {"Sid": "CloudWatchMetricsPublish", "Effect": "Allow", "Resource": "*", "Action": "cloudwatch:PutMetricData",
         "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}}},
        {"Sid": "AgentCoreWorkloadIdentity", "Effect": "Allow",
         "Action": ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT"],
         "Resource": [f"{arn}:workload-identity-directory/default",
                      f"{arn}:workload-identity-directory/default/workload-identity/harness_{HARNESS_NAME}-*"]},
        {"Sid": "AgentCoreCodeInterpreterDefault", "Effect": "Allow", "Action": [
            "bedrock-agentcore:StartCodeInterpreterSession", "bedrock-agentcore:StopCodeInterpreterSession",
            "bedrock-agentcore:GetCodeInterpreterSession", "bedrock-agentcore:ListCodeInterpreterSessions",
            "bedrock-agentcore:InvokeCodeInterpreter"],
         "Resource": f"arn:aws:bedrock-agentcore:{REGION}:aws:code-interpreter/*"},
    ]}


def ensure_role(iam, account: str) -> str:
    try:
        role = iam.get_role(RoleName=ROLE_NAME)["Role"]
        iam.update_assume_role_policy(RoleName=ROLE_NAME, PolicyDocument=json.dumps(TRUST))
        fresh = False
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
        role = iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(TRUST),
                               Description="Execution role for the Bee Call worker harness")["Role"]
        fresh = True
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName=ROLE_NAME, PolicyDocument=json.dumps(role_policy(account)))
    if fresh:
        print("created role; waiting 15 s for IAM to propagate")
        time.sleep(15)
    return role["Arn"]


def ensure_harness(ctl, role_arn: str, model: str) -> dict:
    definition = harness_definition(role_arn, model)
    found = [h for h in ctl.list_harnesses()["harnesses"] if h["harnessName"] == HARNESS_NAME]
    if found and found[0]["status"] == "CREATE_FAILED":
        # A harness that never got created cannot be updated; remove it and start over.
        ctl.delete_harness(harnessId=found[0]["harnessId"])
        while any(h["harnessName"] == HARNESS_NAME for h in ctl.list_harnesses()["harnesses"]):
            time.sleep(5)
        found = []
    if found:
        harness = ctl.update_harness(**update_request(definition, found[0]["harnessId"]))["harness"]
    else:
        harness = ctl.create_harness(**definition)["harness"]
    while harness["status"] in ("CREATING", "UPDATING"):
        time.sleep(5)
        harness = ctl.get_harness(harnessId=harness["harnessId"])["harness"]
    if harness["status"] != "READY":
        raise SystemExit(f"harness is {harness['status']}: {harness.get('failureReason', '')}")
    return harness


async def smoke(arn: str) -> None:
    """One tiny job: checks the inline-function round trip and times it."""
    with tempfile.TemporaryDirectory() as tmp:
        job = Job(job_id="00", dir=Path(tmp), now=datetime.now(ZoneInfo(DEFAULT_TIME_ZONE)),
                  task="Smoke test: call progress once with the text 'checking', then relay_to_user with 'The worker "
                       "is up.', then save_file report.md with one line, then finish.")
        steps: list[str] = []
        started = time.perf_counter()

        async def relay(text: str) -> None:
            steps.append(f"{time.perf_counter() - started:5.1f}s relay: {text}")

        summary = await HarnessWorker(arn).run(
            job, lambda text: steps.append(f"{time.perf_counter() - started:5.1f}s progress: {text}"), relay)
        print("\n".join(steps))
        print(f"{time.perf_counter() - started:5.1f}s done: {summary[:200]}")
        print("report.md:", (job.dir / "report.md").read_text()[:300])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    session = boto3.Session(profile_name="bee-call")
    account = session.client("sts").get_caller_identity()["Account"]
    role_arn = ensure_role(session.client("iam"), account)
    harness = ensure_harness(session.client("bedrock-agentcore-control", region_name=REGION), role_arn, args.model)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"harnessArn": harness["arn"], "harnessId": harness["harnessId"],
                                      "model": args.model, "version": harness.get("harnessVersion")}, indent=1))
    print(f"harness {harness['harnessName']} READY: {harness['arn']} (model {args.model})")
    if args.smoke:
        asyncio.run(smoke(harness["arn"]))


if __name__ == "__main__":
    main()
