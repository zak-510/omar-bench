#!/bin/sh
# Copy run evidence and the ledger to the stack's versioned bucket. Run from the repo root.
# The instance role can write and read but not delete, so this only ever adds.
set -eu
BUCKET=${EVIDENCE_BUCKET:-$(cat /etc/omar-bench/evidence-bucket)}
# The ledger first: while a run is in flight its session logs are not readable by this
# user, which fails the copy of runs/ and must not hold the ledger back.
aws s3 sync results "s3://$BUCKET/results" --only-show-errors
# aws copies what it can and exits non-zero for a file it cannot read. An agent's CLI
# still running in the container after a timeout can leave one, owned by root.
# A link in a trial points into the container it came from, gone by now: it is not
# followed (two such, from 2026-10-30, failed every sync after them).
aws s3 sync runs "s3://$BUCKET/runs" --only-show-errors --no-follow-symlinks || {
  echo "copied the rest; not copied, unreadable by $(id -un):" >&2
  find runs ! -readable >&2
  echo "fix with: sudo chown -R $(id -un) runs   then run this again" >&2
  exit 1
}
echo "synced to s3://$BUCKET"
