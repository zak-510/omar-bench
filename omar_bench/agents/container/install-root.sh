#!/bin/sh
# Runs as root inside a task container: tmux, git and ps for the OMAR runtime, then
# the pinned `omar` and `omarc` release binaries. The agent CLI is installed
# separately, by Harbor's own integration, so every arm gets the same CLI.
#
# The 0.4.2 `omar` binary links glibc >= 2.34 and OpenSSL 3 (libssl.so.3), so it
# loads on Debian 12+ and Ubuntu 22.04+ and not on Alpine or anything older.
set -eu
: "${OMAR_REPO:?}" "${OMAR_VERSION:?}"
OB=/installed-agent/omar-bench
mkdir -p "$OB" && chmod 1777 "$OB"

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends tmux git curl ca-certificates tar procps >/dev/null
  # OpenSSL 3 runtime; the package was renamed in Ubuntu 24.04.
  apt-get install -y -qq libssl3 >/dev/null 2>&1 || apt-get install -y -qq libssl3t64 >/dev/null 2>&1 || true
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache tmux git curl ca-certificates tar procps >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q tmux git curl ca-certificates tar openssl-libs procps-ng >/dev/null
elif command -v yum >/dev/null 2>&1; then
  yum install -y -q tmux git curl ca-certificates tar openssl-libs procps-ng >/dev/null
else
  echo "omar-bench: no supported package manager for tmux, git and ps" >&2
  exit 1
fi

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  *) echo "omar-bench: unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac
TARBALL="omar-linux-$ARCH.tar.gz"
curl -fsSL "https://github.com/$OMAR_REPO/releases/download/v$OMAR_VERSION/$TARBALL" -o "/tmp/$TARBALL"
# The pin in bench.toml is the amd64 tarball's checksum.
if [ -n "${OMAR_SHA256:-}" ] && [ "$ARCH" = amd64 ]; then
  echo "$OMAR_SHA256  /tmp/$TARBALL" | sha256sum -c - >/dev/null
fi
tar xzf "/tmp/$TARBALL" -C /tmp
install "/tmp/omar-linux-$ARCH/omar" "/tmp/omar-linux-$ARCH/omarc" /usr/local/bin/

# Where an image with musl, an older glibc or no OpenSSL 3 fails, before any model is called.
omar --version
