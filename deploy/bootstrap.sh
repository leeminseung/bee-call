#!/bin/bash
# Bee Call server: the EC2 user data for Amazon Linux 2023 (arm64), and what every update runs again
# (scripts/deploy_server.py ship). It checks out the repository, or pulls the newest main, with the read-only
# deploy key kept in SSM Parameter Store, then hands over to deploy/install.sh from that checkout.
# Output of the first boot: /var/log/cloud-init-output.log.
set -euo pipefail
export HOME=${HOME:-/root}  # SSM Run Command starts scripts without one

REGION=ap-southeast-2
REPO=git@github.com:leeminseung/bee-call.git
APP=/opt/bee-call/app
KNOWN_HOSTS=/etc/bee-call/github-known-hosts

# The code uses this machine's local time for session folder names, quiet hours and call times; the users are
# in Korea. The services also get TZ=Asia/Seoul from their units.
timedatectl set-timezone Asia/Seoul
command -v git >/dev/null || dnf install -y git

install -d -m 0755 /etc/bee-call /opt/bee-call
# GitHub's SSH host keys as https://api.github.com/meta lists them, so the first connection trusts no one blindly.
cat > "$KNOWN_HOSTS" <<'EOF'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
EOF
# The deploy key is read from SSM on every run into /run (memory, mode 0600) and removed after the pull, so it is
# never on disk and replacing it in SSM rotates it.
KEY=$(mktemp -p /run bee-call-deploy-key.XXXXXX)
trap 'rm -f "$KEY"' EXIT
aws ssm get-parameter --region "$REGION" --name /bee-call/github-deploy-key --with-decryption \
    --query Parameter.Value --output text > "$KEY"
export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes -o UserKnownHostsFile=$KNOWN_HOSTS -o StrictHostKeyChecking=yes"

if [ -d "$APP/.git" ]; then
    git -C "$APP" pull --ff-only
else
    git clone --branch main "$REPO" "$APP"
fi
rm -f "$KEY"
exec "$APP/deploy/install.sh"
