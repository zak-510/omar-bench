"""Keep the benchmark machine as the pre-registration describes it: one trial
at a time, and nothing left over from the trial before.

A trial that is killed can leave its agents running. They hold memory, slow
every later trial on the machine, and in the end starve it, so both the
scheduler and the sweeps clear them, and the scheduler records what it found.
Only agents this benchmark started are touched: the runner's (OMAR agents named
`mass<run>-...`) and the dsl arm's (tmux sessions with the `dslprobe-` prefix
that infra/bootstrap.sh configures).
"""
import fcntl
import os
import re
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_SESSION = re.compile(r"omar-agent-\d+-mass[0-9a-f]{4}-|dslprobe-")
# What a usage limit, an outage or a sign-in failure looks like in an agent's
# terminal or in an error message.
PROVIDER = re.compile(
    r"usage limit|rate limit|hit your limit|limit reached|too many requests|overloaded|service unavailable|"
    r"api error|invalid api key|authentication_error|unauthorized|not logged in|please log in|credit balance",
    re.I)
# How long an agent has to be silent before the time limit is its doing: three
# reminders have gone unanswered by then.
SILENT_SECS = 360


def trouble(*, end="", silent_secs=None, evidence="", rc=0, stderr="", stopped=False):
    """Whether a run was stopped by the machinery around it rather than by
    what its agents did: "provider" (the model provider was limited, down or
    signed out), "runtime" (agents that did not start, an agent that never
    answered a delivered request, the runtime failing or exiting with an
    error), or "" (neither: the run is its arm's to answer for).

    No reply before the reply timeout is silence. At the time limit it is
    silence only if the agent had been quiet a while; otherwise the run was
    still moving and simply ran out of time."""
    silent = end == "no-reply" or (end == "deadline" and (silent_secs is None or silent_secs >= SILENT_SECS))
    if not (silent or stopped or end == "no-result" or rc not in (0, None, "0", "")):
        return ""
    return "provider" if PROVIDER.search(f"{evidence}\n{stderr}") else "runtime"
# An agent process older than this at the start of a trial was not started by
# it, nor by the scheduler's own short checks of the provider.
STALE_SECS = 300


def _out(cmd, timeout=60):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def sessions():
    """tmux sessions of agents this benchmark started."""
    names = _out(["tmux", "list-sessions", "-F", "#{session_name}"]).split()
    return [n for n in names if AGENT_SESSION.match(n)]


def stale_agents():
    """Agent CLI processes that were running before the coming trial."""
    count = 0
    for line in _out(["ps", "-eo", "etimes=,comm="]).splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip() in ("claude", "codex") and int(parts[0]) > STALE_SECS:
            count += 1
    return count


def clear():
    """End every agent an earlier trial left behind. Returns how many agent
    sessions were found and how many are left."""
    found = sessions()
    if found:
        runner = os.environ.get("OMAR_MASS_BIN", os.path.join(ROOT, "runner", "target", "release", "omar-mass"))
        if os.path.exists(runner):
            _out([runner, "teardown"], timeout=180)  # through OMAR, so its own records stay right
        for name in sessions():
            _out(["tmux", "kill-session", "-t", name])
        for _ in range(10):
            if not sessions():
                break
            time.sleep(1)
    return len(found), len(sessions())


def stop_dsl(omar_dsl, config, printed):
    """End the session of a dsl-arm run that failed or was killed, by the name
    OMAR printed when it started it. A run that ends well ends its own."""
    for name in re.findall(r"Started session (\S+?);", printed):
        _out([omar_dsl, "-c", config, "down", "-s", name])


def snapshot():
    """What the machine looks like before a trial: for the ledger."""
    mem = {}
    try:
        for line in open("/proc/meminfo"):
            key, value = line.split(":", 1)
            mem[key] = int(value.split()[0]) // 1024
        load = open("/proc/loadavg").read().split()[0]
    except OSError:
        load = ""
    return {"mem_available_mib": mem.get("MemAvailable", ""), "swap_used_mib":
            (mem["SwapTotal"] - mem["SwapFree"]) if "SwapTotal" in mem else "", "load1": load,
            "stale_agents": stale_agents()}


def lock(name="omar-bench"):
    """Hold this machine for one scheduler. Returns the open lock file, or None
    if another scheduler has it."""
    f = open(os.path.join(os.path.expanduser("~"), f".{name}.lock"), "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.write(str(os.getpid()))
    f.flush()
    return f
