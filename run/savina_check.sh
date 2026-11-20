#!/bin/sh
# One trial of each Savina benchmark in each arm on Codex, reading the weekly
# usage meter between steps to measure what a trial costs. Never counted.
# Run on the machine from the repo root:
#   nohup sh run/savina_check.sh > results/pilot/savina_check.log 2>&1 &
set -u
cd "$(dirname "$0")/.."
. ./versions.env
. "$HOME/omar-rerun.env"
export RESULTS_DIR=$PWD/results/pilot/savina
mkdir -p "$RESULTS_DIR"
python3 run/codex_quota.py --guard 85 & GUARD=$!
trap 'kill $GUARD 2>/dev/null' EXIT
meter() { echo "=== $(date -u +%H:%M:%S) $1 | $(python3 run/codex_quota.py)"; }

meter "start"
python3 savina/sweep.py --bench banking --arm dsl --tier sol --trials 1 --timeout 3600
meter "after banking dsl"
python3 savina/sweep.py --bench philosophers --arm dsl --tier sol --trials 1 --timeout 3600
meter "after philosophers dsl"
python3 savina/sweep.py --bench banking --arm agent --tier sol --trials 1 --timeout 7200
meter "after banking agent"
python3 savina/sweep.py --bench philosophers --arm agent --tier sol --trials 1 --timeout 7200
meter "after philosophers agent"
