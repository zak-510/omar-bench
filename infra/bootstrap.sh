#!/bin/sh
# Install the pinned toolchain on the benchmark machine (Ubuntu 24.04, x86_64).
# Run on the machine from the repo root: sh infra/bootstrap.sh. Safe to rerun.
set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)
. ./versions.env
OPT=$HOME/opt
mkdir -p "$OPT" "$HOME/wd/sol" "$HOME/wd/stub" "$HOME/codex-home"

sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
    tmux git python3 build-essential curl jq xz-utils ca-certificates

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

# The DSL arm calls bin/omar-dsl and bin/omarc next to its sweep scripts.
DSLBIN=$ROOT/summer/probes/dsl/bin
mkdir -p "$DSLBIN"
ln -sfn "$OPT/omar-$OMAR_VERSION/omar" "$DSLBIN/omar-dsl"
ln -sfn "$OPT/omar-$OMAR_VERSION/omarc" "$DSLBIN/omarc"
ln -sfn "$OPT/omar-$OMAR_VERSION/lib" "$DSLBIN/lib"

# The agent arm's runner, built once with the pinned compiler.
command -v rustup >/dev/null 2>&1 || curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
rustup toolchain install "$RUST_VERSION" --profile minimal
rustup default "$RUST_VERSION"   # OMAR builds code reactions with plain `cargo`
(cd summer && cargo "+$RUST_VERSION" build --release --locked)

# Codex reads its model from CODEX_HOME; nothing else on the machine sets one.
cp infra/codex-config.toml "$HOME/codex-home/config.toml"
for tier in sol stub; do
    printf '[dashboard]\nsession_prefix = "dslprobe-"\n\n[agent]\ndefault_workdir = "%s"\n' \
        "$HOME/wd/$tier" > "summer/probes/dsl/config-$tier.toml"
done

cat > "$HOME/omar-rerun.env" <<EOF
export PATH=$OPT/node/bin:$HOME/.cargo/bin:$OPT/omar-$OMAR_VERSION:\$PATH
export CODEX_HOME=$HOME/codex-home
export OMAR_BIN=$OPT/omar-$OMAR_VERSION/omar
export OMAR_MASS_BIN=$ROOT/summer/target/release/omar-mass
export RESULTS_DIR=$ROOT/results
EOF
echo "done. next: . ~/omar-rerun.env && codex login --device-auth"
