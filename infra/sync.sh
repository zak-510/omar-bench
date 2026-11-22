#!/bin/sh
# Copy the repo to the benchmark machine, or bring its results back.
# Usage: infra/sync.sh push | pull | ssh   (ENV_FILE picks another machine)
set -eu
cd "$(dirname "$0")/.."
. "${ENV_FILE:-infra/instance.env}"
SSH="ssh -i $KEY -o StrictHostKeyChecking=accept-new"
case "${1:-}" in
    push) # What the machine runs: the commit, its tag, and whether the tree differs from it.
          { git rev-parse HEAD; git describe --tags --exact-match 2>/dev/null | sed 's/^/tag:/'
            [ -z "$(git status --porcelain)" ] || echo dirty; } | tr '\n' ' ' > REVISION
          rsync -az -e "$SSH" --delete \
              --exclude .git --exclude results --exclude runner/target --exclude local \
              ./ "ubuntu@$HOST:omar-bench/" ;;
    pull) rsync -az -e "$SSH" "ubuntu@$HOST:omar-bench/results/" results/ ;;
    ssh)  exec $SSH "ubuntu@$HOST" ;;
    *)    echo "usage: $0 push|pull|ssh" >&2; exit 2 ;;
esac
