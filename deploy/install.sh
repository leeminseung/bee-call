#!/bin/bash
# Installs or updates everything the Bee Call server runs, from this checkout. deploy/bootstrap.sh runs it as root
# at first boot and on every update; running it again changes only what is out of date.
#   deploy/install.sh          tools, Python packages, systemd units; restarts the services
#   deploy/install.sh caddy    only Caddy's config, after scripts/deploy_server.py dns set a hostname
set -euo pipefail
export HOME=${HOME:-/root}

APP=$(cd "$(dirname "$0")/.." && pwd)
BIN=/opt/bee-call/bin
DATA=/var/lib/bee-call
SERVICES=(bee-call-env bee-call-token bee-call-session bee-call-agent bee-call-outbound)

# Every download is pinned to a version and checked against its sha256 before use.
UV_VERSION=0.12.19
UV_INSTALLER_SHA256=61b349611f1b6e1ba33645f30c36da5287df2609dd7af8605d96a031435eb35b  # it checks uv's own sha256
CADDY_VERSION=2.11.7
CADDY_TARBALL_SHA256=d8fc6d179a5d283028a472a5618564f6ad8a86fed513e64f032b3b0b7cc45e42
CADDY_SHA256=0019d864a2cdee41c3675cb2a26185833c5d9540a48f5a6729142c0413461b60
BEE_VERSION=0.7.3  # @beeai/cli on npm; only its native linux-arm64 binary is used, so no node
BEE_TARBALL_SHA256=1895f0a97ac306c15a0733bc6db6fabefcce77ca05181a76fa7f138a3f8f39c4
BEE_SHA256=f8a19d7ee7a54a218d87eb5dbee139eec2d8aa50173478860001c56f1a6ff17a

check() {  # sha256 file: stops the script unless the file has exactly that content
    echo "$1  $2" | sha256sum --check --quiet -
}

installed() {  # sha256 file: whether the file is already there with that content
    [ -f "$2" ] && echo "$1  $2" | sha256sum --check --status -
}

download() {  # url sha256 file
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o "$3" "$1"  # also through a dropped connection
    check "$2" "$3"
}

caddy_config() {
    "$APP/.venv/bin/python" -m deploy.config caddyfile
    if [ -f /etc/caddy/Caddyfile ]; then
        systemctl enable caddy
        systemctl reload-or-restart caddy
    else
        systemctl disable --now caddy 2>/dev/null || true
    fi
}

cd "$APP"
if [ "${1:-}" = caddy ]; then
    caddy_config
    exit 0
fi

# The four services run as bee-call, whose home is the data root (BEE_CALL_DATA); Caddy runs as caddy, whose
# home keeps the certificates. Neither can log in.
getent passwd bee-call >/dev/null || useradd --system --home-dir "$DATA" --shell /sbin/nologin bee-call
getent passwd caddy >/dev/null || useradd --system --home-dir /var/lib/caddy --shell /sbin/nologin caddy
install -d -o bee-call -g bee-call -m 0750 "$DATA"
install -d -o caddy -g caddy -m 0750 /var/lib/caddy
install -d -m 0755 "$BIN" /etc/caddy

# 2 GB of swap: a burst of memory then slows the box down instead of the kernel killing a service mid-call.
if [ -z "$(swapon --show=NAME --noheadings)" ]; then
    if [ ! -f /swapfile ]; then
        fallocate -l 2G /swapfile
        chmod 600 /swapfile
        mkswap /swapfile
    fi
    swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap defaults 0 0' >> /etc/fstab
fi

tmp=$(mktemp -d -p /var/tmp)  # on disk: /tmp is memory, and the npm tarball is 170 MB
trap 'rm -rf "$tmp"' EXIT

if [ "$("$BIN/uv" --version 2>/dev/null | cut -d' ' -f2)" != "$UV_VERSION" ]; then
    download "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-installer.sh" \
        "$UV_INSTALLER_SHA256" "$tmp/uv-installer.sh"
    for attempt in 1 2 3; do  # it downloads uv itself, without retries
        UV_UNMANAGED_INSTALL="$BIN" sh "$tmp/uv-installer.sh" && break
        [ "$attempt" = 3 ] && exit 1
        sleep 10
    done
fi

if ! installed "$CADDY_SHA256" "$BIN/caddy"; then
    release="https://github.com/caddyserver/caddy/releases/download/v$CADDY_VERSION"
    download "$release/caddy_${CADDY_VERSION}_linux_arm64.tar.gz" "$CADDY_TARBALL_SHA256" "$tmp/caddy.tar.gz"
    tar -xzf "$tmp/caddy.tar.gz" -C "$tmp" caddy
    check "$CADDY_SHA256" "$tmp/caddy"
    install -m 0755 "$tmp/caddy" "$BIN/caddy"
fi

if ! installed "$BEE_SHA256" "$BIN/bee"; then
    download "https://registry.npmjs.org/@beeai/cli/-/cli-$BEE_VERSION.tgz" "$BEE_TARBALL_SHA256" "$tmp/bee.tgz"
    tar -xzf "$tmp/bee.tgz" -C "$tmp" package/dist/platforms/linux-arm64/bee
    check "$BEE_SHA256" "$tmp/package/dist/platforms/linux-arm64/bee"
    install -m 0755 "$tmp/package/dist/platforms/linux-arm64/bee" "$BIN/bee"
fi

# Python (the version in .python-version) and the locked packages. The interpreter goes under /opt, not root's
# home, so the services' user can run the virtualenv that links to it.
export UV_PYTHON_INSTALL_DIR=/opt/bee-call/python UV_CACHE_DIR=/var/cache/bee-call-uv UV_COMPILE_BYTECODE=1
"$BIN/uv" sync --locked --no-dev --no-progress

install -m 0644 deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable "${SERVICES[@]}"
# The settings first (this stops the update if SSM cannot be read), then the quick services, and the voice server
# last: it lets calls in progress end first (up to 10 minutes), while the app can already reach the others.
systemctl restart bee-call-env
systemctl restart bee-call-token bee-call-session bee-call-outbound
echo "restarting the voice server; calls in progress end first (up to 10 minutes)"
systemctl restart bee-call-agent
caddy_config
echo "running $(git -C "$APP" log -1 --format='%h %s')"
