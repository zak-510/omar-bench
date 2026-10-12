#!/bin/bash
# Runs attempts unattended, one at a time: every arm on every task. Copies results to the
# bucket after each attempt, stops early when something is wrong, and stops the machine
# 30 minutes after it ends (`sudo shutdown -c` cancels). Log: ~/queue.log
#
#   STUDY=mine tmux new -d -s queue infra/ec2/queue.sh
#   STUDY=mine TASKS="fix-git prove-plus-comm" K=5 tmux new -d -s queue infra/ec2/queue.sh
: "${STUDY:?set STUDY: the name these attempts are reported under}"
cd "$(dirname "$0")/../.." || exit 1
exec >> ~/queue.log 2>&1
SUITE=${SUITE:-terminal-bench-2}
TASKS=${TASKS:-fix-git}
ARMS=${ARMS:-omar-solo solo omar-plan-build-check}
MODEL=${MODEL:-haiku}
K=${K:-1}
now() { date -u +'%a %H:%M:%S'; }
newest() { ls -t results/attempts/*.jsonl 2>/dev/null | head -1; }
row() {   # newest ledger row: outcome, error, requests, wall seconds, and the API error behind a stall
  python3 - "$(newest)" <<'PY'
import json, pathlib, sys
r = [json.loads(l) for l in open(sys.argv[1])][-1]
stall = "-"
agent = pathlib.Path("runs") / r["trial_dir"] / "agent"
if (agent / "omar-stall.txt").exists():
    stall = "unknown"
    name = (agent / "omar-stall.txt").read_text().split()[-1].split("/sessions/projects/")[-1]
    try:
        for line in open(agent / "sessions/projects" / name, errors="replace"):
            if '"isApiErrorMessage":true' in line:
                stall = json.loads(line).get("error") or "unknown"
    except OSError:
        pass
print(r["outcome"], r["error_type"] or "-", r["tokens"]["requests"], int(r["wall_secs"]), stall)
PY
}
limits() {   # plan use after the newest solo attempt: five-hour, weekly, five-hour reset (epoch)
  f=$(ls -t runs/*-solo-*/jobs/*/*/agent/claude-code.txt 2>/dev/null | head -1)
  [ -n "$f" ] && python3 - "$f" <<'PY'
import json, sys
last = None
for line in open(sys.argv[1], errors="replace"):
    if '"rate_limit_event"' in line:
        try: last = json.loads(line)["rate_limit_info"]
        except (ValueError, KeyError): pass
if last:
    w = last.get("unifiedWindows", {}); five = w.get("five_hour") or {}; seven = w.get("seven_day") or {}
    print(five.get("utilization", 0), seven.get("utilization", 0), int(five.get("resetsAt") or 0), last.get("status"))
PY
}
for t in $TASKS; do for a in $ARMS; do
  echo "$(now) start $a on $t"
  before=$(newest)
  uv run omar-bench run "$SUITE" --arm "$a" --model "$MODEL" --study "$STUDY" -k "$K" --tasks "*$t" > ~/queue-"$t"-"$a".log 2>&1
  rc=$?
  if [ "$(newest)" = "$before" ]; then echo "$(now) no ledger row from that run (exit $rc): stopping the queue"; break 2; fi
  set -- $(row); echo "$(now) end   $a on $t: outcome $1, error $2, requests $3, ${4}s, stall cause $5"
  sh infra/ec2/sync.sh > ~/sync.log 2>&1 || echo "$(now) sync reported a problem; see ~/sync.log"
  # Raised within ten minutes: a broken setup or a spent plan, not an attempt at the task.
  if [ "$2" != "-" ] && [ "$4" -lt 600 ]; then echo "$(now) raised within ten minutes: stopping the queue"; break 2; fi
  # A turn that died on anything but the output cap (a usage limit, say) would die again.
  if [ "$5" != "-" ] && [ "$5" != "max_output_tokens" ]; then echo "$(now) a turn died on '$5': stopping the queue"; break 2; fi
  if [ "$a" = solo ]; then
    set -- $(limits); echo "$(now) plan use: five-hour $1, week $2, status $4"
    if python3 -c "import sys; sys.exit(0 if float('${2:-0}') >= 0.85 else 1)"; then echo "$(now) the week is at $2: stopping the queue to leave room"; break 2; fi
    if python3 -c "import sys; sys.exit(0 if float('${1:-0}') >= 0.80 else 1)"; then
      wait=$(( ${3:-0} - $(date +%s) + 120 )); [ "$wait" -gt 0 ] && [ "$wait" -lt 19000 ] && { echo "$(now) five-hour window at $1: waiting ${wait}s for its reset"; sleep "$wait"; }
    fi
  fi
done; done
uv run omar-bench report --study "$STUDY" > ~/queue-report.md 2>&1
sh infra/ec2/sync.sh > ~/sync.log 2>&1 || echo "$(now) sync reported a problem; see ~/sync.log"
aws s3 cp ~/queue.log "s3://$(cat /etc/omar-bench/evidence-bucket)/queue-logs/$STUDY/queue.log" --only-show-errors
aws s3 cp ~/queue-report.md "s3://$(cat /etc/omar-bench/evidence-bucket)/queue-logs/$STUDY/report.md" --only-show-errors
echo "$(now) queue done; machine stops in 30 minutes"
sudo shutdown -c 2>/dev/null; sudo shutdown -h +30
