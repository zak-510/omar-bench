#!/usr/bin/env python3
"""Read Codex's weekly usage, as Codex's app server reports it.

  python3 run/codex_quota.py              the current reading
  python3 run/codex_quota.py --guard 98   stop the runs once the weekly limit
                                          reaches 98%, checking every minute

The reading is account-wide (any Codex use counts), in whole percents, and
costs no model call. Session logs are no use for this: agents started by the
runtime do not all keep one.
"""
import json
import os
import signal
import subprocess
import sys
import time


def reading(timeout=30):
    """(used percent, resets at) for the weekly limit, or None if Codex did not say."""
    s = state(timeout)
    return (s["used"], s["resets_at"]) if s else None


def state(timeout=30):
    """Weekly use and whether Codex is refusing work right now: {"used",
    "resets_at", "limited"}, or None if Codex did not say."""
    try:
        # In a process group of its own: `codex` is a launcher, and killing it
        # alone leaves the app server it started running on the machine.
        p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, start_new_session=True)
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
                result = msg.get("result") or {}
                limits = result.get("rateLimits") or {}
                primary = limits.get("primary") or {}
                if "usedPercent" not in primary:
                    return None
                limited = (result.get("ordinaryUsageAllowed") is False or bool(limits.get("rateLimitReachedType"))
                           or bool(limits.get("spendControlReached")))
                return {"used": primary["usedPercent"], "resets_at": primary.get("resetsAt"), "limited": limited}
        return None
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()
        p.wait()


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
