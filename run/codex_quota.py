#!/usr/bin/env python3
"""Read Codex's weekly usage, as Codex's app server reports it.

  python3 run/codex_quota.py              the current reading
  python3 run/codex_quota.py --guard 85   stop the runs once the weekly limit
                                          reaches 85%, checking every minute

The reading is account-wide (any Codex use counts), in whole percents, and
costs no model call. Session logs are no use for this: agents started by the
runtime do not all keep one.
"""
import json
import subprocess
import sys
import time


def reading(timeout=30):
    """(used percent, resets at) for the weekly limit, or None if Codex did not say."""
    try:
        p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None
    try:
        for m in ({"method": "initialize", "id": 0, "params": {"clientInfo": {"name": "omar-bench", "version": "0"}}},
                  {"method": "initialized"}, {"method": "account/rateLimits/read", "id": 1}):
            p.stdin.write(json.dumps(m) + "\n")
        p.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 1:
                primary = ((msg.get("result") or {}).get("rateLimits") or {}).get("primary") or {}
                return (primary["usedPercent"], primary.get("resetsAt")) if "usedPercent" in primary else None
        return None
    finally:
        p.kill()


def main():
    if "--guard" not in sys.argv:
        print(json.dumps(reading()))
        return
    limit = float(sys.argv[sys.argv.index("--guard") + 1])
    while True:
        now = reading()
        if now and now[0] >= limit:
            print(f"{time.strftime('%H:%M:%S')} weekly Codex use {now[0]}% >= {limit}%: stopping runs", flush=True)
            for pattern in ("[s]weep.py", "[r]un/schedule.py"):
                subprocess.run(["pkill", "-f", pattern])
            return
        time.sleep(60)


if __name__ == "__main__":
    main()
