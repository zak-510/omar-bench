"""Follows Omnigent's Polly session to its end, over the local server's API.

    python omnigent_watch.py API LOGS        (run.sh runs it with Omnigent's own Python)

Polly's lead hands work to sub-agents, which run on their own and wake it when they
finish, so no one turn's end is the attempt's. It is over when the lead and every
sub-agent are idle with nothing pending and stay so for OB_SWARM_SETTLE_SECS: exit 0. Or
when nothing has moved, in any session or any agent's log, for OB_DEAD_TURN_MINS (a
session parked on an approval nobody will give, say): exit 70, said as run.sh's
stalled() says it, so the queue reads it the same way. Or when the lead's last turn
failed, on a provider error for one: exit 70 with the error as given, which Harbor names
as it does for `solo`. No session within OB_OMNIGENT_START_SECS: exit 71.

Leaves beside the logs what the attempt cost (omnigent-usage.json: the lead's session,
whose usage Omnigent reports for its whole subtree) and every session's record and
transcript (omnigent-sessions.json), and prints the lead's last message as the answer.
Standard library only: it runs in whatever container the task brings.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
import urllib.request

SETTLE = int(os.environ.get("OB_SWARM_SETTLE_SECS", "60"))
QUIET = int(os.environ.get("OB_DEAD_TURN_MINS", "15")) * 60
WATCH = int(os.environ.get("OB_WATCH_SECS", "30"))
START = int(os.environ.get("OB_OMNIGENT_START_SECS", "600"))
# Where the agents' own logs are written: a session can be at work for a while (a long
# build) without Omnigent recording anything new.
LOGS_GLOBS = ("~/.omnigent/**/*.jsonl", "/tmp/omnigent-*/**/*.jsonl", "{logs}/sessions/**/*.jsonl")


def get(api: str, path: str):
    with urllib.request.urlopen(api + path, timeout=30) as response:
        return json.load(response)


def root(api: str) -> dict | None:
    for session in get(api, "/v1/sessions")["data"]:
        if session.get("agent_name") == "polly" and not session.get("parent_session_id"):
            return session
    return None


def tree(api: str, root_id: str) -> list[dict]:
    """The lead's session and every session under it."""
    out, todo = [], [root_id]
    while todo:
        sid = todo.pop()
        out.append(get(api, f"/v1/sessions/{sid}"))
        todo += [c["id"] for c in get(api, f"/v1/sessions/{sid}/child_sessions?limit=100")["data"]]
    return out


def settled(session: dict) -> bool:
    return (session.get("status") == "idle" and not session.get("pending_inputs")
            and not session.get("pending_elicitations"))


def last_activity(sessions: list[dict], logs: str) -> float:
    times = [float(s.get("updated_at") or 0) for s in sessions]
    for pattern in LOGS_GLOBS:
        for path in glob.glob(os.path.expanduser(pattern.format(logs=logs)), recursive=True):
            try:
                times.append(os.path.getmtime(path))
            except OSError:
                pass
    return max(times, default=0.0)


def answer(api: str, root_id: str) -> str:
    """The lead's last message."""
    items = get(api, f"/v1/sessions/{root_id}/items?limit=100&order=desc")["data"]
    for item in items:
        if item.get("type") == "message" and item.get("role") == "assistant":
            return "".join(part.get("text", "") for part in item.get("content") or [])
    return ""


def keep(api: str, sessions: list[dict], logs: str) -> None:
    """What the attempt cost, and every session's record and transcript."""
    lead = sessions[0]
    with open(os.path.join(logs, "omnigent-usage.json"), "w") as f:
        json.dump(lead.get("usage_by_model") or {}, f, indent=2)
    records = []
    for s in sessions:
        try:
            items = get(api, f"/v1/sessions/{s['id']}/items?limit=1000")["data"]
        except OSError:
            items = None
        records.append({"session": s, "items": items})
    with open(os.path.join(logs, "omnigent-sessions.json"), "w") as f:
        json.dump(records, f, indent=2)


def main(api: str, logs: str) -> int:
    started = time.time()
    lead = None
    while lead is None:
        try:
            lead = root(api)
        except OSError:
            pass  # the server is still starting
        if lead is None:
            if time.time() - started >= START:
                print(f"omar-bench: no Polly session within {START} s")
                return 71
            time.sleep(2)
    idle_since, code, why = None, 0, ""
    while True:
        try:
            sessions = tree(api, lead["id"])
        except OSError as e:  # the server went away: the run cannot go on
            return stall(logs, f"Omnigent's server stopped answering ({e})")
        now = time.time()
        if all(settled(s) for s in sessions):
            idle_since = idle_since or now
            if now - idle_since >= SETTLE:
                break
        else:
            idle_since = None
        quiet = now - last_activity(sessions, logs)
        if quiet >= QUIET:
            code = 70
            asked = sum(len(s.get("pending_elicitations") or []) for s in sessions)
            why = ("every agent's turn was over or parked, and nothing had moved for "
                   f"{QUIET // 60} minutes, while a session was still "
                   + (f"waiting on {asked} approval(s) nobody will give" if asked else "not idle"))
            break
        time.sleep(WATCH)
    keep(api, sessions, logs)
    print("Output t.answer = " + json.dumps(answer(api, lead["id"])))
    failed = sessions[0].get("last_task_error")
    if failed:
        # As the CLI gave it, so Harbor names it (a model at capacity, a usage limit).
        return stall(logs, "the lead's last turn failed:\n"
                     + (failed if isinstance(failed, str) else json.dumps(failed)))
    return stall(logs, why) if code else 0


def stall(logs: str, why: str) -> int:
    """Why the attempt ended early, where the queue reads it, and on stdout for Harbor."""
    text = f"omar-bench ended this attempt: {why}\n"
    with open(os.path.join(logs, "omar-stall.txt"), "w") as f:
        f.write(text)
    print(text, end="")
    return 70


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
