#!/bin/sh
# Runs as root inside a task container: tmux, git and ps for Omnigent's terminals, then
# the pinned Omnigent from PyPI on a Python of its own. uv, that Python and Omnigent live
# under /opt/omnigent, so the task's own Python and node, the tests' included, are left
# as they were; only `omnigent` is added to PATH. The agent CLI is installed separately,
# by Harbor's own integration, so every arm gets the same CLI.
set -eu
: "${OMNIGENT_VERSION:?}"
OB=/installed-agent/omar-bench
mkdir -p "$OB" && chmod 1777 "$OB"

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends tmux git curl ca-certificates procps >/dev/null
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache tmux git curl ca-certificates procps >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q tmux git curl ca-certificates procps-ng >/dev/null
elif command -v yum >/dev/null 2>&1; then
  yum install -y -q tmux git curl ca-certificates procps-ng >/dev/null
else
  echo "omar-bench: no supported package manager for tmux, git and ps" >&2
  exit 1
fi

O=/opt/omnigent
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$O/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null
UV_TOOL_DIR="$O/tools" UV_TOOL_BIN_DIR="$O/bin" UV_PYTHON_INSTALL_DIR="$O/python" \
  "$O/bin/uv" tool install --python 3.12 "omnigent==$OMNIGENT_VERSION" >/dev/null
chmod -R a+rX "$O"
ln -sf "$O/bin/omnigent" /usr/local/bin/omnigent
# Where a Python of its own cannot run, before any model is called.
omnigent --version
