#!/usr/bin/env python3
"""Read Codex's weekly usage from the session logs on this machine.

  python3 run/codex_quota.py              latest reading and tokens used here
  python3 run/codex_quota.py --guard 85   stop the runs once the weekly limit
                                          reaches 85%, checking every minute

The reading is account-wide (any Codex use counts), in whole percents.
"""
import glob
import json
import os
import subprocess
import sys
import time

SESSIONS = os.path.join(os.environ.get("CODEX_HOME", os.path.expanduser("~/.codex")), "sessions")


def reading(newest=None):
    """(latest (time, percent, resets_at), tokens used here). With `newest`,
    read only that many most recently written session files."""
    latest, tokens = None, 0
    paths = sorted(glob.glob(os.path.join(SESSIONS, "**", "*.jsonl"), recursive=True),
                   key=os.path.getmtime, reverse=True)
    for path in paths[:newest]:
        prev = 0
        for line in open(path, errors="ignore"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            p = e.get("payload") or {}
            if p.get("type") != "token_count":
                continue
            total = ((p.get("info") or {}).get("total_token_usage") or {}).get("total_tokens", prev)
            tokens += total - prev
            prev = total
            primary = (p.get("rate_limits") or {}).get("primary") or {}
            if "used_percent" in primary and (latest is None or e["timestamp"] > latest[0]):
                latest = (e["timestamp"], primary["used_percent"], primary.get("resets_at"))
    return latest, tokens


def main():
    if "--guard" not in sys.argv:
        latest, tokens = reading()
        print(json.dumps({"latest": latest, "tokens_here": tokens}))
        return
    limit = float(sys.argv[sys.argv.index("--guard") + 1])
    while True:
        latest, _ = reading(newest=20)
        if latest and latest[1] >= limit:
            print(f"{time.strftime('%H:%M:%S')} weekly Codex use {latest[1]}% >= {limit}%: stopping runs", flush=True)
            for pattern in ("[s]weep.py", "[r]un/schedule.py", "[r]un/savina_check.sh"):
                subprocess.run(["pkill", "-f", pattern])
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
