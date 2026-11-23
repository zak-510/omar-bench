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
import json
import os
import re
import signal
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT_SESSION = re.compile(r"omar-agent-\d+-mass[0-9a-f]{4}-|dslprobe-")
# What a usage limit, an outage or a sign-in failure looks like in an agent's
# terminal or in an error message.
# None of these is a phrase an agent would write about its own task: "limit"
# and "overloaded" alone are not here, since a buffer can be both.
PROVIDER = re.compile(
    r"usage limit|rate limit|hit your [\w -]{0,12}limit|too many requests|overloaded_error|api error|"
    r"service unavailable|invalid api key|authentication_error|not logged in|please log in|credit balance",
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
# An agent process older than this at the start of a trial was not started by it.
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
    """Agents that were running before the coming trial: Claude Code
    processes, and Codex processes that OMAR started (their command line
    names OMAR's runtime folder). A bare `codex app-server` is not an agent:
    it is how Codex's usage is read."""
    count = 0
    for line in _out(["ps", "-eo", "etimes=,comm=,args="]).splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or int(parts[0]) <= STALE_SECS:
            continue
        if parts[1] == "claude" or (parts[1] == "codex" and "/.omar/" in parts[2]):
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


def workdir(tier):
    """The empty folder OMAR starts a tier's agents in (infra/bootstrap.sh), so
    that the agent arm's agents start in the same one. None where it is not
    set up, as on a machine that only runs the tests."""
    config = os.path.join(os.environ.get("OMAR_LOCAL", os.path.join(ROOT, "local")), f"config-{tier}.toml")
    if not os.path.exists(config):
        return None
    found = re.search(r'default_workdir\s*=\s*"([^"]+)"', open(config).read())
    return found.group(1) if found and os.path.isdir(found.group(1)) else None


USAGE = ("tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write", "cost_usd", "models", "metered")


def usage(folders=(), sessions=(), settle=12):
    """What the agents of one trial used, as their CLI recorded it: tokens in
    and out, cached tokens read and written, the cost of those at list price,
    and the models they ran on. Claude Code keeps these per folder for the last
    session run there, so they can be read for agents that each had a folder
    of their own: the agent arm's (`folders`, from the runner) and the dsl
    arm's (`sessions`, the names OMAR printed, whose agents it starts under
    ~/.omar/sessions/NAME/). `metered` is how many agents had a record, of
    how many there were. Codex keeps no such record here: its columns stay empty."""
    path = os.path.join(os.path.expanduser("~"), ".claude.json")
    roots = [os.path.join(os.path.expanduser("~"), ".omar", "sessions", name) + os.sep for name in sessions]
    found, last = {}, None
    for _ in range(settle):  # the records are written as the agents exit: read once they stop changing
        try:
            projects = json.load(open(path)).get("projects", {})
        except (OSError, ValueError):
            projects = {}
        found = {k: v for k, v in projects.items() if k in folders or k.startswith(tuple(roots) or ("\0",))}
        state = json.dumps(found, sort_keys=True)
        if found and state == last:
            break
        last = state
        time.sleep(1)
    total = {"tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0, "tokens_cache_write": 0, "cost_usd": 0.0}
    models, metered, agents = set(), 0, []
    for folder, record in sorted(found.items()):
        by_model = record.get("lastModelUsage") or {}
        if by_model:
            metered += 1
        for model, u in by_model.items():
            models.add(model)
            total["tokens_in"] += u.get("inputTokens", 0)
            total["tokens_out"] += u.get("outputTokens", 0)
            total["tokens_cache_read"] += u.get("cacheReadInputTokens", 0)
            total["tokens_cache_write"] += u.get("cacheCreationInputTokens", 0)
            total["cost_usd"] += u.get("costUSD", 0.0)
        agents.append({"folder": folder, "usage": by_model})
    if not metered:
        return {key: "" for key in USAGE}, agents
    total["cost_usd"] = round(total["cost_usd"], 6)
    count = len(folders) if folders else len(found)
    return {**total, "models": " ".join(sorted(models)), "metered": f"{metered}/{count}"}, agents


def stop_dsl(omar_dsl, printed):
    """End the session of a dsl-arm run that failed or was killed, by the name
    OMAR printed when it started it. A run that ends well ends its own; one
    that fails leaves its session's daemon running, with or without agents."""
    for name in re.findall(r"Started session (\S+?);", printed):
        _out([omar_dsl, "down", "-s", name], timeout=20)
        # `down` can hang on a session that is half gone, and its daemon can
        # ignore a polite signal: end the daemon directly, then for certain.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for line in _out(["pgrep", "-af", "omar session-daemon"]).splitlines():
                pid, *args = line.split()
                if args and args[-1].rstrip("/").endswith(f"/sessions/{name}"):
                    try:
                        os.kill(int(pid), sig)
                    except (OSError, ValueError):
                        pass
            time.sleep(1)


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
