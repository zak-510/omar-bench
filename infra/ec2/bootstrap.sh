#!/bin/bash
# Provision an Ubuntu 24.04 x86_64 host to run omar-bench. Run as root; safe to re-run.
# The stack's user-data calls this; on any other box: sudo bash infra/ec2/bootstrap.sh
set -euxo pipefail
REPO=$(cd "$(dirname "$0")/../.." && pwd)
RUN_AS=${SUDO_USER:-ubuntu}

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
# Harbor drives every task through `docker compose`, and Terminal-Bench's Dockerfiles
# need BuildKit; Ubuntu packages both apart from docker.io.
# nftables: Harbor's egress allowlist, which SWE Atlas refactoring tasks require.
apt-get install -y docker.io docker-compose-v2 docker-buildx tmux git jq curl unzip nftables nodejs npm
usermod -aG docker "$RUN_AS"
command -v aws >/dev/null || snap install aws-cli --classic

pin() {
  python3 -c 'import sys, tomllib
value = tomllib.load(open(sys.argv[1], "rb"))
for key in sys.argv[2].split("."):
    value = value[key]
print(value)' "$REPO/bench.toml" "$1"
}

# uv fetches each suite's pinned Harbor at run time.
sudo -u "$RUN_AS" -H bash -c 'command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh'

# The agent CLIs on the host are only for logging in. The ones that do the work
# are installed inside each task container, at the same pins.
CLAUDE_VERSION=$(pin cli.claude)
CODEX_VERSION=$(pin cli.codex)
sudo -u "$RUN_AS" -H bash -c "curl -fsSL https://claude.ai/install.sh | bash -s -- $CLAUDE_VERSION"
npm install -g "@openai/codex${CODEX_VERSION:+@$CODEX_VERSION}"

# SWE Atlas ships its tasks in its own repository, not on Harbor Hub.
ATLAS_REF=$(pin suites.swe-atlas-rf.ref)
if [ -n "$ATLAS_REF" ] && [ ! -d "$REPO/vendor/SWE-Atlas" ]; then
  sudo -u "$RUN_AS" -H git clone "https://github.com/$(pin suites.swe-atlas-rf.repo).git" \
    "$REPO/vendor/SWE-Atlas"
  sudo -u "$RUN_AS" -H git -C "$REPO/vendor/SWE-Atlas" checkout "$ATLAS_REF"
fi

# OMAR on the host: needed to compile topologies (`omar-bench doctor --topologies`).
# Skipped until pinned.
OMAR_VERSION=$(pin omar.candidate.version)
if [ -n "$OMAR_VERSION" ]; then
  OMAR_REPO=$(pin omar.candidate.repo)
  OMAR_SHA256=$(pin omar.candidate.sha256_linux_amd64)
  curl -fsSL "https://github.com/$OMAR_REPO/releases/download/v$OMAR_VERSION/omar-linux-amd64.tar.gz" \
    -o /tmp/omar-linux-amd64.tar.gz
  [ -z "$OMAR_SHA256" ] || echo "$OMAR_SHA256  /tmp/omar-linux-amd64.tar.gz" | sha256sum -c -
  tar xzf /tmp/omar-linux-amd64.tar.gz -C /tmp
  install /tmp/omar-linux-amd64/omar /tmp/omar-linux-amd64/omarc /usr/local/bin/
fi

echo "omar-bench bootstrap done"
