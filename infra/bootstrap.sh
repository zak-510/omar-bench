#!/bin/sh
# Install the pinned toolchain on the benchmark machine (Ubuntu 24.04, x86_64).
# Run on the machine from the repo root: sh infra/bootstrap.sh. Safe to rerun.
set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)
. ./versions.env
OPT=$HOME/opt
mkdir -p "$OPT" "$HOME/wd/sol" "$HOME/wd/terra" "$HOME/wd/luna" "$HOME/wd/stub" "$HOME/wd/haiku55/.claude" "$HOME/codex-home"

sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    tmux git python3 build-essential curl jq xz-utils ca-certificates

# 8 GiB of swap: Codex agents peak near 1 GB each while starting, so six
# starting at once can briefly outgrow an 8 GiB machine.
if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
    sudo fallocate -l 8G /swapfile && sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

fetch() { # url sha256 file
    curl -fsSL "$1" -o "$3"
    echo "$2  $3" | sha256sum -c - >/dev/null || { echo "checksum mismatch: $1" >&2; exit 1; }
}

if [ ! -x "$OPT/node/bin/node" ]; then
    fetch "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" "$NODE_SHA256" /tmp/node.tar.xz
    mkdir -p "$OPT/node" && tar -xJf /tmp/node.tar.xz -C "$OPT/node" --strip-components 1
fi
PATH=$OPT/node/bin:$HOME/.cargo/bin:$PATH
npm install -g --prefix "$OPT/node" "@openai/codex@$CODEX_CLI_VERSION" "@anthropic-ai/claude-code@$CLAUDE_CODE_VERSION"

if [ ! -x "$OPT/omar-$OMAR_VERSION/omar" ]; then
    fetch "https://github.com/omar-os/omar/releases/download/v$OMAR_VERSION/omar-linux-amd64.tar.gz" "$OMAR_SHA256" /tmp/omar.tgz
    mkdir -p "$OPT/omar-$OMAR_VERSION" && tar -xzf /tmp/omar.tgz -C "$OPT/omar-$OMAR_VERSION" --strip-components 1
fi

# Both DSL arms call local/bin/omar-dsl and local/bin/omarc, with a config per tier.
LOCAL=$ROOT/local
mkdir -p "$LOCAL/bin"
ln -sfn "$OPT/omar-$OMAR_VERSION/omar" "$LOCAL/bin/omar-dsl"
ln -sfn "$OPT/omar-$OMAR_VERSION/omarc" "$LOCAL/bin/omarc"
ln -sfn "$OPT/omar-$OMAR_VERSION/lib" "$LOCAL/bin/lib"

# The agent arm's runner, built once with the pinned compiler.
command -v rustup >/dev/null 2>&1 || curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
rustup toolchain install "$RUST_VERSION" --profile minimal
rustup default "$RUST_VERSION"   # OMAR builds code reactions with plain `cargo`
(cd runner && cargo "+$RUST_VERSION" build --release --locked)

# Codex reads its model from CODEX_HOME (infra/codex-model.sh sets another
# than CODEX_MODEL, and the scheduler checks it). Claude Code reads one from the
# folder it starts in, but OMAR starts each dsl-arm agent in a folder of its
# own, where the workdir's setting is not seen: so the model is also pinned
# for every Claude Code session on the machine. Every trial reads back the
# model its agents ran on, and is voided if it was another.
cp infra/codex-config.toml "$HOME/codex-home/config.toml"
printf '{"model": "%s"}\n' "$CLAUDE_MODEL" > "$HOME/wd/haiku55/.claude/settings.json"
sh infra/claude-model.sh
for tier in sol terra luna stub haiku55; do
    printf '[dashboard]\nsession_prefix = "dslprobe-"\n\n[agent]\ndefault_workdir = "%s"\n' \
        "$HOME/wd/$tier" > "$LOCAL/config-$tier.toml"
done

cat > "$HOME/omar-rerun.env" <<EOF
export PATH=$OPT/node/bin:$HOME/.cargo/bin:$OPT/omar-$OMAR_VERSION:\$PATH
export CODEX_HOME=$HOME/codex-home
export OMAR_BIN=$OPT/omar-$OMAR_VERSION/omar
export OMAR_MASS_BIN=$ROOT/runner/target/release/omar-mass
export RESULTS_DIR=$ROOT/results
export DISABLE_AUTOUPDATER=1   # keep Claude Code at the pinned version
if [ -f $HOME/.claude-token ]; then export CLAUDE_CODE_OAUTH_TOKEN=\$(cat $HOME/.claude-token); fi
EOF
echo "done. next: . ~/omar-rerun.env && codex login --device-auth"
