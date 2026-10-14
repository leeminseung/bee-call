"""The Bee Call server on one EC2 instance in Sydney: create it, give it its settings, update it, take it down.

Run from this Mac with the AWS profile bee-call; deploy/README.md has the order of the first deployment.
    uv run python scripts/deploy_server.py deploy-key                  a read-only GitHub deploy key, kept in SSM
    uv run python scripts/deploy_server.py secrets [--set NAME=VALUE]  .env.local's settings to SSM /bee-call/
    uv run python scripts/deploy_server.py up [--type t4g.medium]      security group, instance, Elastic IP
    uv run python scripts/deploy_server.py dns bee.example.com         A record to the Elastic IP, then HTTPS
    uv run python scripts/deploy_server.py ship                        pull main, uv sync, restart
    uv run python scripts/deploy_server.py status
    uv run python scripts/deploy_server.py logs agent [--lines 300]    token, session, agent, outbound, caddy, env, boot
    uv run python scripts/deploy_server.py down                        terminate, release the IP, delete the rest

A command that changes AWS prints what it would do, and does it only with --yes. Settings and keys are shown by
name and length, never by value. Commands on the server go through SSM Run Command; the instance has no SSH.
"""

import argparse
import base64
import json
import sys
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from botocore.exceptions import ClientError, WaiterError
from dotenv import dotenv_values

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from deploy.config import ENV_NAME, HOST_PARAMETER, HOSTNAME, PREFIX, REFUSED  # noqa: E402
from voice.aws_session import aws_session  # noqa: E402

REGION = "ap-southeast-2"
NAME = "bee-call-server"  # the instance's Name tag, its security group, and its IAM role and instance profile
TAGS = [{"Key": "Name", "Value": NAME}, {"Key": "Project", "Value": "bee-call"}]
INSTANCE_TYPES = ("t4g.small", "t4g.medium")
DEFAULT_TYPE = "t4g.medium"  # measured: see deploy/README.md
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
PORTS = (80, 443, 8443)
DISK_GB = 30
GITHUB_REPO = "leeminseung/bee-call"
DEPLOY_KEY = "github-deploy-key"
ENV_LOCAL = REPO / ".env.local"
HARNESS_STATE = REPO / "workspace" / "state" / "harness.json"  # written by scripts/deploy_worker.py
BOOTSTRAP = REPO / "deploy" / "bootstrap.sh"
UNITS = {"token": "bee-call-token", "session": "bee-call-session", "agent": "bee-call-agent",
         "outbound": "bee-call-outbound", "caddy": "caddy", "env": "bee-call-env"}

# .env.local settings that stay on this Mac, with why.
MAC_ONLY = {
    "AWS_PROFILE": "this Mac's AWS profile; the server uses its instance role",
    "AWS_ACCESS_KEY_ID": "static AWS keys; the server uses its instance role",
    "AWS_SECRET_ACCESS_KEY": "static AWS keys; the server uses its instance role",
    "AWS_SESSION_TOKEN": "static AWS keys; the server uses its instance role",
    "APNS_KEY_PATH": "a path on this Mac; the key itself goes to /bee-call/apns-key",
    "LIVEKIT_AGENT_NAME": "each machine registers its own; the server keeps bee-call",
    "BEE_CALL_DEV_PERSON": "lets requests in without signing in; never on a server",
    "BEE_CALL_DATA": "the server's data root is set in its units",
    "BEE_BIN": "the server's Bee CLI is set in its units",
    "TOKEN_SERVER_PORT": "Caddy expects the token server on 8787",
    HOST_PARAMETER: "set by dns",
}
# Settings this Mac passes on the command line rather than in .env.local, as the server should run them:
# GLM 5 on Bedrock in the coordinator slot, and the AgentCore worker (only when its ARN is known).
SERVER_DEFAULTS = {"COORDINATOR_LLM": "bedrock", "WORKER": "harness"}

STATUS_SCRIPT = r"""
echo "first boot: $(cloud-init status 2>/dev/null | sed -n 's/^status: //p')"
echo "code: $(git -C /opt/bee-call/app log -1 --format='%h %s (%cr)' 2>/dev/null || echo 'not checked out yet')"
for unit in bee-call-env bee-call-token bee-call-session bee-call-agent bee-call-outbound caddy; do
    state=$(systemctl show -p ActiveState --value "$unit")
    sub=$(systemctl show -p SubState --value "$unit")
    mem=$(systemctl show -p MemoryCurrent --value "$unit")
    case "$mem" in ''|'[not set]'|infinity|18446744073709551615) mem='';; *) mem="$((mem / 1048576)) MiB";; esac
    printf '  %-18s %-9s %-8s %s\n' "$unit" "$state" "$sub" "$mem"
done
echo "voice server: $(curl -s --max-time 3 http://127.0.0.1:8081/worker || echo 'no answer on :8081')"
free -m | awk 'NR==2 {printf "memory: %s MiB used of %s, %s available\n", $3, $2, $7} NR==3 {printf "swap: %s MiB used of %s\n", $3, $2}'
df -h / | awk 'NR==2 {print "disk: " $3 " used of " $2}'
"""

HOSTNAME_SCRIPT = """
set -euo pipefail
cd /opt/bee-call/app
.venv/bin/python -m deploy.config env
systemctl restart bee-call-token bee-call-session
deploy/install.sh caddy
"""


# ---------- pure parts (tested) ----------

def server_settings(local: Mapping[str, str | None], harness_arn: str | None = None,
                    extra: Mapping[str, str] | None = None) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    """What `secrets` writes, as name -> (value, where it came from), and the names it leaves out, with why:
    .env.local except this Mac's own settings, the worker's ARN, the server defaults, and --set."""
    chosen: dict[str, tuple[str, str]] = {}
    skipped: dict[str, str] = {}
    for name, value in local.items():
        if name in MAC_ONLY:
            skipped[name] = MAC_ONLY[name]
        elif not ENV_NAME.fullmatch(name):
            skipped[name] = "not an environment variable name"
        elif not (value or "").strip():
            skipped[name] = "empty"
        else:
            chosen[name] = (value, ".env.local")
    if harness_arn and "WORKER_HARNESS_ARN" not in chosen:
        chosen["WORKER_HARNESS_ARN"] = (harness_arn, "workspace/state/harness.json")
    for name, value in SERVER_DEFAULTS.items():
        if name == "WORKER" and "WORKER_HARNESS_ARN" not in chosen:
            continue  # the harness worker cannot start without its ARN; the server then runs the fake worker
        chosen.setdefault(name, (value, "server default"))
    for name, value in (extra or {}).items():
        chosen[name] = (value, "--set")
    return chosen, skipped


def parse_sets(pairs: list[str]) -> dict[str, str]:
    found = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not ENV_NAME.fullmatch(name):
            raise SystemExit(f"--set wants NAME=VALUE with an upper-case NAME, not {name!r}")
        if name in REFUSED or name in MAC_ONLY:
            raise SystemExit(f"{name} is not set on the server through SSM")
        found[name] = value
    return found


def zone_for(host: str, zones: list[str]) -> str | None:
    """The most specific hosted zone name that contains `host`."""
    matches = [z for z in (zone.rstrip(".") for zone in zones) if host == z or host.endswith("." + z)]
    return max(matches, key=len) if matches else None


def remote_commands(script: str, chunk: int = 2000) -> list[str]:
    """Run Command lines that rebuild `script` on the instance and run it with bash. It travels as base64, so no
    shell reads or expands it on the way. Run Command keeps only the first 24,000 characters of output, so the
    script's output is cut to its last 23,000 first: the end of a long run says how it ended."""
    encoded = base64.b64encode(script.encode()).decode()
    lines = ['f=$(mktemp)']
    lines += [f"printf '%s' '{encoded[i:i + chunk]}' >> \"$f\"" for i in range(0, len(encoded), chunk)]
    lines.append('base64 -d < "$f" > "$f.sh" && bash "$f.sh" > "$f.out" 2>&1; rc=$?; tail -c 23000 "$f.out"; '
                 'rm -f "$f" "$f.sh" "$f.out"; exit $rc')
    return lines


# ---------- AWS ----------

def session():
    return aws_session(region_name=REGION)


def confirm(plan: list[str], yes: bool) -> bool:
    print("Plan:")
    for step in plan:
        print(f"  - {step}")
    if not yes:
        print("Nothing changed. Run again with --yes to do this.")
    return yes


def find_instance(ec2) -> dict | None:
    reservations = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [NAME]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"]
    found = [i for r in reservations for i in r["Instances"]]
    if len(found) > 1:
        raise SystemExit(f"more than one instance is tagged {NAME}: {', '.join(i['InstanceId'] for i in found)}")
    return found[0] if found else None


def running_instance(ec2) -> str:
    instance = find_instance(ec2)
    if instance is None:
        raise SystemExit("no Bee Call server: up creates one")
    if instance["State"]["Name"] != "running":
        raise SystemExit(f"{instance['InstanceId']} is {instance['State']['Name']}, not running")
    return instance["InstanceId"]


def find_address(ec2) -> dict | None:
    found = ec2.describe_addresses(Filters=[{"Name": "tag:Name", "Values": [NAME]}])["Addresses"]
    return found[0] if found else None


def default_vpc(ec2) -> str:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "is-default", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise SystemExit(f"{REGION} has no default VPC")
    return vpcs[0]["VpcId"]


def find_group(ec2, vpc: str) -> str | None:
    groups = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [NAME]},
                                                   {"Name": "vpc-id", "Values": [vpc]}])["SecurityGroups"]
    return groups[0]["GroupId"] if groups else None


def parameter_names(ssm) -> set[str]:
    """The names directly under /bee-call/, without reading any value."""
    names = set()
    for page in ssm.get_paginator("describe_parameters").paginate(
            ParameterFilters=[{"Key": "Path", "Option": "OneLevel", "Values": [PREFIX.rstrip("/")]}]):
        names.update(p["Name"][len(PREFIX):] for p in page["Parameters"])
    return names


def public_host(ssm) -> str | None:
    try:
        return ssm.get_parameter(Name=PREFIX + HOST_PARAMETER)["Parameter"]["Value"]  # a hostname, not a secret
    except ssm.exceptions.ParameterNotFound:
        return None


def run_on_server(ssm, instance_id: str, script: str, timeout: int = 300, comment: str = "Bee Call") -> str:
    """Runs a bash script as root on the instance through SSM Run Command and returns what it printed (its last
    23,000 characters). Exits if the script fails."""
    command = ssm.send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript", Comment=comment,
                               Parameters={"commands": remote_commands(script), "executionTimeout": [str(timeout)]})
    command_id = command["Command"]["CommandId"]
    deadline = time.time() + timeout + 120
    while True:
        time.sleep(3)
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
        except ssm.exceptions.InvocationDoesNotExist:
            continue  # not registered yet
        if result["Status"] not in ("Pending", "InProgress", "Delayed"):
            break
        if time.time() > deadline:
            raise SystemExit(f"no answer from {instance_id} after {timeout} s (command {command_id})")
    output = result["StandardOutputContent"] + result["StandardErrorContent"]
    if result["Status"] != "Success":
        print(output)
        raise SystemExit(f"failed on the server: {result['Status']} (exit {result['ResponseCode']})")
    return output


# ---------- commands ----------

def cmd_deploy_key(args) -> None:
    ssm = session().client("ssm")
    replacing = DEPLOY_KEY in parameter_names(ssm)
    public_path = Path(tempfile.gettempdir()) / f"{NAME}-deploy-key.pub"
    plan = ["make an ed25519 key pair in memory",
            f"{PREFIX}{DEPLOY_KEY}: the private key, SecureString" +
            (" (replaces the one there; remove the old key on GitHub afterwards)" if replacing else ""),
            f"write the public key to {public_path}, for you to add to GitHub as a read-only deploy key"]
    if not confirm(plan, args.yes):
        return
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.PEM, PrivateFormat.OpenSSH, NoEncryption()).decode()
    public = key.public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
    ssm.put_parameter(Name=PREFIX + DEPLOY_KEY, Value=private, Type="SecureString", Overwrite=True)
    public_path.write_text(f"{public} {NAME}\n")
    print(f"kept the private key in {PREFIX}{DEPLOY_KEY}. Now add the public key to GitHub (read-only, the default):")
    print(f"  gh repo deploy-key add {public_path} --repo {GITHUB_REPO} --title {NAME}")


def cmd_secrets(args) -> None:
    local = dotenv_values(ENV_LOCAL) if ENV_LOCAL.exists() else {}
    harness = json.loads(HARNESS_STATE.read_text()).get("harnessArn") if HARNESS_STATE.exists() else None
    chosen, skipped = server_settings(local, harness, parse_sets(args.set))
    ssm = session().client("ssm")
    existing = parameter_names(ssm)
    plan = [f"{PREFIX}{name}: SecureString, {len(value)} characters, from {source}"
            + (" (replaces the one there)" if name in existing else "")
            for name, (value, source) in sorted(chosen.items())]
    for name, why in sorted(skipped.items()):
        print(f"stays on this Mac: {name} ({why})")
    others = sorted(n for n in existing if ENV_NAME.fullmatch(n) and n not in chosen and n != HOST_PARAMETER)
    if others:
        print(f"already on the server and left as they are: {', '.join(others)}")
    if "WORKER_HARNESS_ARN" not in chosen and "WORKER_HARNESS_ARN" not in existing:
        print("no worker ARN (workspace/state/harness.json): the server runs the fake worker until WORKER and "
              "WORKER_HARNESS_ARN are set")
    if "apns-key" not in existing:
        print("no APNs key on the server yet, so no pushes from it. Upload the .p8 file yourself:")
        print(f"  aws ssm put-parameter --profile bee-call --region {REGION} --name {PREFIX}apns-key "
              "--type SecureString --value file://<path to AuthKey_XXXXXXXXXX.p8>")
    if not confirm(plan, args.yes):
        return
    for name, (value, _) in sorted(chosen.items()):
        ssm.put_parameter(Name=PREFIX + name, Value=value, Type="SecureString", Overwrite=True)
    print(f"wrote {len(chosen)} settings. The server reads them when its services restart: ship --yes")


def cmd_up(args) -> None:
    s = session()
    ec2, ssm = s.client("ec2"), s.client("ssm")
    existing = find_instance(ec2)
    if existing:
        print(f"already up: {existing['InstanceId']} ({existing['State']['Name']}). ship updates it, down removes it.")
        return
    names = parameter_names(ssm)
    if DEPLOY_KEY not in names:
        raise SystemExit("no deploy key in SSM: run deploy-key first (the first boot clones the repository with it)")
    vpc = default_vpc(ec2)
    group = find_group(ec2, vpc)
    address = find_address(ec2)
    ami = ssm.get_parameter(Name=AMI_PARAMETER)["Parameter"]["Value"]
    image = ec2.describe_images(ImageIds=[ami])["Images"][0]
    plan = [f"use security group {group}" if group else
            f"security group {NAME} in {vpc}: TCP {', '.join(map(str, PORTS))} open to anyone; no SSH",
            f"instance {args.type} from {image['Name']} ({ami}) with the IAM role {NAME}, a {DISK_GB} GB encrypted "
            "gp3 disk, IMDSv2 only, no SSH key, and deploy/bootstrap.sh as user data",
            f"use Elastic IP {address['PublicIp']}" if address else "a new Elastic IP for it",
            "wait until SSM Run Command can reach it"]
    missing = [n for n in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET") if n not in names]
    if missing:
        print(f"note: {', '.join(missing)} not in SSM yet; the services keep restarting until secrets and ship")
    if not confirm(plan, args.yes):
        return
    if not group:
        group = ec2.create_security_group(GroupName=NAME, VpcId=vpc, Description="Bee Call server: HTTPS and ACME",
                                          TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}])["GroupId"]
        ec2.authorize_security_group_ingress(GroupId=group, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": port, "ToPort": port,
             "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "Bee Call"}]} for port in PORTS])
        print(f"security group {group}")
    instance_id = ec2.run_instances(
        ImageId=ami, InstanceType=args.type, MinCount=1, MaxCount=1, SecurityGroupIds=[group],
        IamInstanceProfile={"Name": NAME}, UserData=BOOTSTRAP.read_text(),
        MetadataOptions={"HttpTokens": "required", "HttpEndpoint": "enabled", "HttpPutResponseHopLimit": 1},
        BlockDeviceMappings=[{"DeviceName": image["RootDeviceName"], "Ebs": {
            "VolumeSize": DISK_GB, "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}],
        TagSpecifications=[{"ResourceType": "instance", "Tags": TAGS}, {"ResourceType": "volume", "Tags": TAGS}],
    )["Instances"][0]["InstanceId"]
    print(f"instance {instance_id}; waiting for it to run")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    if not address:
        address = ec2.allocate_address(Domain="vpc", TagSpecifications=[{"ResourceType": "elastic-ip", "Tags": TAGS}])
    ec2.associate_address(InstanceId=instance_id, AllocationId=address["AllocationId"])
    print(f"Elastic IP {address['PublicIp']}; waiting for SSM to reach it")
    deadline = time.time() + 900
    while time.time() < deadline:
        found = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
        if any(i.get("PingStatus") == "Online" for i in found["InstanceInformationList"]):
            break
        time.sleep(10)
    else:
        raise SystemExit("SSM did not reach the instance in 15 minutes: check its role has "
                         "AmazonSSMManagedInstanceCore, then status")
    print(f"up: {instance_id} at {address['PublicIp']}. The first boot goes on installing for about five minutes;")
    print("status shows when it is done, logs boot shows its output. Next: dns <hostname> --yes")


def cmd_dns(args) -> None:
    host = args.hostname.strip().lower().rstrip(".")
    if not HOSTNAME.fullmatch(host):
        raise SystemExit(f"not a DNS name: {args.hostname!r}")
    s = session()
    ec2, ssm, route53 = s.client("ec2"), s.client("ssm"), s.client("route53")
    instance_id = running_instance(ec2)
    address = find_address(ec2)
    if address is None:
        raise SystemExit("no Elastic IP: up makes one")
    zones = {z["Name"].rstrip("."): z["Id"].split("/")[-1]
             for page in route53.get_paginator("list_hosted_zones").paginate() for z in page["HostedZones"]
             if not z.get("Config", {}).get("PrivateZone")}
    zone = zone_for(host, list(zones))
    if zone is None:
        raise SystemExit(f"no public Route 53 hosted zone holds {host}. Registering the domain in Route 53 makes one.")
    current = current_record(route53, zones[zone], host)
    plan = [f"Route 53 zone {zone}: A record {host} -> {address['PublicIp']}, TTL 300"
            + (f" (now {', '.join(r['Value'] for r in current.get('ResourceRecords', [])) or 'an alias'})"
               if current else " (new)"),
            f"{PREFIX}{HOST_PARAMETER} = {host}",
            f"on {instance_id}: read the settings again, restart the token and session servers, and configure Caddy "
            "for the name; Caddy then gets a Let's Encrypt certificate"]
    if not confirm(plan, args.yes):
        return
    change = route53.change_resource_record_sets(HostedZoneId=zones[zone], ChangeBatch={
        "Comment": "Bee Call server", "Changes": [{"Action": "UPSERT", "ResourceRecordSet": {
            "Name": host, "Type": "A", "TTL": 300, "ResourceRecords": [{"Value": address["PublicIp"]}]}}]})
    print("waiting for Route 53 to serve the record")
    route53.get_waiter("resource_record_sets_changed").wait(Id=change["ChangeInfo"]["Id"])
    ssm.put_parameter(Name=PREFIX + HOST_PARAMETER, Value=host, Type="String", Overwrite=True)
    print(run_on_server(ssm, instance_id, HOSTNAME_SCRIPT, comment="Bee Call hostname"))
    print(f"token server: https://{host}/token   session screen: https://{host}:8443/")
    print("the certificate takes a minute or so; logs caddy shows it")


def current_record(route53, zone_id: str, host: str) -> dict | None:
    found = route53.list_resource_record_sets(HostedZoneId=zone_id, StartRecordName=host, StartRecordType="A",
                                              MaxItems="1")["ResourceRecordSets"]
    return next((r for r in found if r["Name"].rstrip(".") == host and r["Type"] == "A"), None)


def cmd_ship(args) -> None:
    s = session()
    instance_id = running_instance(s.client("ec2"))
    plan = [f"on {instance_id}: pull main from GitHub, update the pinned tools if they changed, uv sync, install "
            "the units, restart the services (the voice server first lets calls in progress end, up to 10 minutes)"]
    if not confirm(plan, args.yes):
        return
    print(run_on_server(s.client("ssm"), instance_id, BOOTSTRAP.read_text(), timeout=1800, comment="Bee Call ship"))


def cmd_status(args) -> None:
    s = session()
    ec2, ssm = s.client("ec2"), s.client("ssm")
    instance = find_instance(ec2)
    if instance is None:
        print("no Bee Call server: up creates one")
        return
    instance_id, state = instance["InstanceId"], instance["State"]["Name"]
    address = find_address(ec2)
    print(f"instance: {instance_id} {instance['InstanceType']} {state} in "
          f"{instance['Placement']['AvailabilityZone']}, IP {address['PublicIp'] if address else 'none'}")
    print(f"hostname: {public_host(ssm) or 'none yet (dns sets it)'}")
    found = ssm.describe_instance_information(Filters=[{"Key": "InstanceIds", "Values": [instance_id]}])
    ping = next((i["PingStatus"] for i in found["InstanceInformationList"]), "not registered")
    print(f"SSM: {ping}")
    if state == "running" and ping == "Online":
        print(run_on_server(ssm, instance_id, STATUS_SCRIPT, timeout=60, comment="Bee Call status"))


def cmd_logs(args) -> None:
    s = session()
    instance_id = running_instance(s.client("ec2"))
    if args.service == "boot":
        script = f"tail -n {args.lines} /var/log/cloud-init-output.log"
    else:
        script = f"journalctl -u {UNITS[args.service]} -n {args.lines} --no-pager -o short-iso"
    print(run_on_server(s.client("ssm"), instance_id, script, timeout=60, comment="Bee Call logs"))


def cmd_down(args) -> None:
    s = session()
    ec2, ssm, route53 = s.client("ec2"), s.client("ssm"), s.client("route53")
    instance = find_instance(ec2)
    address = find_address(ec2)
    group = find_group(ec2, default_vpc(ec2))
    host = public_host(ssm)
    record = zone_id = None
    if host and address:
        zones = {z["Name"].rstrip("."): z["Id"].split("/")[-1]
                 for page in route53.get_paginator("list_hosted_zones").paginate() for z in page["HostedZones"]}
        zone = zone_for(host, list(zones))
        if zone:
            zone_id = zones[zone]
            found = current_record(route53, zone_id, host)
            if found and [r["Value"] for r in found.get("ResourceRecords", [])] == [address["PublicIp"]]:
                record = found
    plan = []
    if record:
        plan.append(f"delete the A record {host} -> {address['PublicIp']}, so the name never points at an IP "
                    "that is no longer ours")
    if host:
        plan.append(f"delete {PREFIX}{HOST_PARAMETER} ({host}); dns sets it again after the next up")
    if instance:
        plan.append(f"terminate {instance['InstanceId']}: its disk goes with it, with every call record, Bee login "
                    "and certificate on it")
    if address:
        plan.append(f"release Elastic IP {address['PublicIp']}")
    if group:
        plan.append(f"delete security group {group}")
    if not plan:
        print("nothing to take down")
        return
    plan.append("keep the other SSM parameters (settings and keys) for the next up")
    if not confirm(plan, args.yes):
        return
    if record:
        route53.change_resource_record_sets(HostedZoneId=zone_id, ChangeBatch={
            "Changes": [{"Action": "DELETE", "ResourceRecordSet": record}]})
    if host:
        ssm.delete_parameter(Name=PREFIX + HOST_PARAMETER)
    if instance:
        ec2.terminate_instances(InstanceIds=[instance["InstanceId"]])
        print(f"terminating {instance['InstanceId']}")
        ec2.get_waiter("instance_terminated").wait(InstanceIds=[instance["InstanceId"]])
    if address:
        ec2.release_address(AllocationId=address["AllocationId"])
    if group:
        for attempt in range(12):  # the instance's network interface can take a moment to let go of the group
            try:
                ec2.delete_security_group(GroupId=group)
                break
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "DependencyViolation" or attempt == 11:
                    raise
                time.sleep(10)
    print("down")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    changes = argparse.ArgumentParser(add_help=False)
    changes.add_argument("--yes", action="store_true", help="do it; without it, only print the plan")
    sub.add_parser("deploy-key", parents=[changes], help="a read-only GitHub deploy key, kept in SSM")
    p = sub.add_parser("secrets", parents=[changes], help=".env.local's settings to SSM /bee-call/")
    p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", help="also this setting")
    p = sub.add_parser("up", parents=[changes], help="security group, instance, Elastic IP")
    p.add_argument("--type", choices=INSTANCE_TYPES, default=DEFAULT_TYPE)
    p = sub.add_parser("dns", parents=[changes], help="A record to the Elastic IP, then HTTPS for that name")
    p.add_argument("hostname")
    sub.add_parser("ship", parents=[changes], help="pull main, uv sync, restart the services")
    sub.add_parser("status", help="the instance, its services and their memory")
    p = sub.add_parser("logs", help="a service's journal, or the first boot's output")
    p.add_argument("service", choices=[*UNITS, "boot"])
    p.add_argument("--lines", type=int, default=200)
    sub.add_parser("down", parents=[changes], help="terminate, release the IP, delete the group and the A record")
    args = parser.parse_args()
    command = {"deploy-key": cmd_deploy_key, "secrets": cmd_secrets, "up": cmd_up, "dns": cmd_dns,
               "ship": cmd_ship, "status": cmd_status, "logs": cmd_logs, "down": cmd_down}[args.command]
    try:
        command(args)
    except (ClientError, WaiterError) as exc:
        sys.exit(f"AWS refused or failed: {exc}")


if __name__ == "__main__":
    main()
