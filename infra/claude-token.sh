#!/bin/sh
# Put a Claude Code token on the benchmark machines without it passing through
# anything else: make one with `claude setup-token`, copy it, then run
#   infra/claude-token.sh [infra/instance.env ...]
# (every machine in infra/instance*.env by default). It reads the token from
# the clipboard and stores it as ~/.claude-token (0600) on each machine.
set -eu
cd "$(dirname "$0")/.."
token=$(pbpaste | tr -d '[:space:]')
case "$token" in
    sk-ant-*) ;;
    *) echo "the clipboard does not hold a Claude token: copy the one claude setup-token printed, then type this command rather than pasting it" >&2; exit 1 ;;
esac
[ $# -gt 0 ] || set -- infra/instance*.env
for env in "$@"; do
    . "./$env"
    printf '%s' "$token" | ssh -i "$KEY" -o BatchMode=yes "ubuntu@$HOST" 'umask 077; cat > ~/.claude-token'
    echo "$env: token installed"
done
