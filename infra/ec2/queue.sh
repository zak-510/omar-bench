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
row() {   # newest ledger row: outcome, error, requests, wall seconds, and what a stall was
  python3 - "$(newest)" <<'PY'
import json, pathlib, sys
r = [json.loads(l) for l in open(sys.argv[1])][-1]
stall = "-"
agent = pathlib.Path("runs") / r["trial_dir"] / "agent"
if (agent / "omar-stall.txt").exists():
    text = (agent / "omar-stall.txt").read_text()
    if "every agent's turn was over" in text:
        stall = "idle"   # one agent left its OMAR step open
    else:                # an API error; which one is in the transcript named last
        stall = "unknown"
        name = text.split()[-1].split("/sessions/projects/")[-1]
        try:
            for line in open(agent / "sessions/projects" / name, errors="replace"):
                if '"isApiErrorMessage":true' in line:
                    stall = json.loads(line).get("error") or "unknown"
        except OSError:
            pass
print(r["outcome"], r["error_type"] or "-", r["tokens"]["requests"], int(r["wall_secs"]), stall)
PY
}
limits() {   # plan use as of the newest attempt that logged it: five-hour, weekly, five-hour reset (epoch)
  python3 - "$MODEL" <<'PY'
import glob, json, os, sys, tomllib
backend = tomllib.load(open("bench.toml", "rb"))["models"][sys.argv[1]]["backend"]
def claude(line):   # Claude Code reports it only in the solo arm's output
    info = json.loads(line)["rate_limit_info"]; w = info.get("unifiedWindows", {})
    five, week = w.get("five_hour") or {}, w.get("seven_day") or {}
    return five.get("utilization", 0), week.get("utilization", 0), int(five.get("resetsAt") or 0), info.get("status")
def codex(line):    # Codex reports it in every rollout's token_count events
    rl = json.loads(line)["payload"]["rate_limits"]
    by = {w.get("window_minutes"): w for w in (rl.get("primary"), rl.get("secondary")) if w}
    five, week = by.get(300) or {}, by.get(10080) or {}
    return ((five.get("used_percent") or 0) / 100, (week.get("used_percent") or 0) / 100,
            int(five.get("resets_at") or 0), rl.get("rate_limit_reached_type") or "allowed")
if backend == "claude":
    logs, marker, read = glob.glob("runs/*-solo-*/jobs/*/*/agent/claude-code.txt"), '"rate_limit_event"', claude
else:
    logs = glob.glob("runs/*/jobs/*/*/agent/sessions/**/rollout-*.jsonl", recursive=True)
    marker, read = '"rate_limits"', codex
for path in sorted(logs, key=os.path.getmtime, reverse=True):
    last = None
    try:
        for line in open(path, errors="replace"):
            if marker in line:
                try: last = read(line)
                except (ValueError, KeyError, TypeError, AttributeError): pass
    except OSError:
        continue
    if last:
        print(*last)
        break
PY
}
for t in $TASKS; do for a in $ARMS; do
  # Before every attempt, from the newest log that reports plan use (an earlier run's, at first).
  set -- $(limits)
  if [ -n "${1:-}" ]; then
    echo "$(now) plan use: five-hour $1, week $2, status $4"
    if python3 -c "import sys; sys.exit(0 if float('$2') >= 0.85 else 1)"; then echo "$(now) the week is at $2: stopping the queue to leave room"; break 2; fi
    if python3 -c "import sys; sys.exit(0 if float('$1') >= 0.80 else 1)"; then
      wait=$(( $3 - $(date +%s) + 120 )); [ "$wait" -gt 0 ] && [ "$wait" -lt 19000 ] && { echo "$(now) five-hour window at $1: waiting ${wait}s for its reset"; sleep "$wait"; }
    fi
  fi
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
  # An agent that left its step open is the system under test, not the setup: go on.
  case "$5" in -|max_output_tokens|idle) ;; *) echo "$(now) a turn died on '$5': stopping the queue"; break 2;; esac
done; done
uv run omar-bench report --study "$STUDY" > ~/queue-report.md 2>&1
sh infra/ec2/sync.sh > ~/sync.log 2>&1 || echo "$(now) sync reported a problem; see ~/sync.log"
aws s3 cp ~/queue.log "s3://$(cat /etc/omar-bench/evidence-bucket)/queue-logs/$STUDY/queue.log" --only-show-errors
aws s3 cp ~/queue-report.md "s3://$(cat /etc/omar-bench/evidence-bucket)/queue-logs/$STUDY/report.md" --only-show-errors
echo "$(now) queue done; machine stops in 30 minutes"
sudo shutdown -c 2>/dev/null; sudo shutdown -h +30
