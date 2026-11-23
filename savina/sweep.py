#!/usr/bin/env python3
"""Run Savina trials in one arm and score each with check.py.

Usage:
  python3 savina/sweep.py --bench philosophers|philosophers-90|banking|boundedbuffer --arm agent|dsl --tier sol|haiku55|stub|mock
                          [--trials N] [--timeout SECS]

Two tiers use no model, to try the harness itself: `stub` runs the dsl arm on
OMAR's stub backend, and `mock` runs the agent arm against scripted agents
(mock_agents.py, with $MOCK_FAULT naming a fault to play).

One CSV row per trial in $RESULTS_DIR/savina-<bench>-<model>-<arm>.csv, the
full delivery log or trace in the matching .jsonl, and every failed boot
attempt in -attempts.csv. Completed trials are skipped on a rerun.

Each row carries a `cause` (check.py): what the trial was, kept apart from
what merely went wrong around it. A trial that the provider spoiled, or that
the scorer failed on, is named in $VOID_FILE, for the scheduler to list and
run again.

Each row also carries what the trial's agents used, as their CLI recorded it
(run/machine.py, `usage`): tokens, their cost at list price, and the models
they ran on. A trial any of whose agents ran on another model than the tier's
is voided the same way: it is not a trial of this study.

Run by hand, a sweep takes the machine's lock like a scheduler, since it
clears agent sessions before each trial: it refuses to start where a
scheduler is already running.
"""
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "run"))
import check  # noqa: E402
import machine  # noqa: E402
import spec  # noqa: E402


def flag(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


BENCH = flag("--bench", "philosophers")
ARM = flag("--arm", "dsl")
TIER = flag("--tier", "sol")
TRIALS = int(flag("--trials", "3"))
TIMEOUT = int(flag("--timeout", "3600"))
assert BENCH in spec.FUSE and ARM in ("agent", "dsl"), (BENCH, ARM)
TIERS = {"sol": ("gpt-6.1-sol", "Codex"), "haiku55": ("claude-haiku-5-5", "Claude"),
         "opus55": ("claude-opus-5-5", "Claude"), "stub": ("stub", "Stub"), "mock": ("mock", "Mock")}
MODEL, BACKEND = TIERS[TIER]
FUSE = spec.FUSE[BENCH]  # agent arm only; see spec.py
RETRIES = 3
# Both arms stop a trial this long after its time limit. The agent arm's
# runner stops itself then and keeps its record; killing it is the last resort.
GRACE = 120
KILL_AFTER = 300

RESULTS = os.environ.get("RESULTS_DIR", os.path.join(ROOT, "results"))
os.makedirs(RESULTS, exist_ok=True)
SLUG = re.sub(r"[^A-Za-z0-9_-]", "-", MODEL)
OUT = os.path.join(RESULTS, f"savina-{BENCH}-{SLUG}-{ARM}.csv")
RAW = OUT[:-4] + ".jsonl"
ATTEMPTS = OUT[:-4] + "-attempts.csv"
FIELDS = ["model", "arm", "bench", "trial", "completed", "success", "reason", "safety_violations",
          "meal_counts", "wrong_answers", "overdrafts", "wrongful_denials", "wrong_transfers",
          "decided", "balances_ok", "deviations", "deliveries", "detail", "rc", "stderr",
          "started_at", "secs", "overflows", "items_done",
          "cause", "end", "lost", "undelivered", "silent_secs", "evidence", "attempts", *machine.USAGE]


def done_trials():
    if not os.path.exists(OUT):
        return set()
    with open(OUT, newline="") as f:
        return {int(r["trial"]) for r in csv.DictReader(f)}


def run(cmd, limit, env=None):
    """Run to the end or to `limit` seconds, then kill. Returns what it printed either way."""
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True, env=env)
    killed = False
    try:
        out, err = p.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        killed = True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
                p.wait(timeout=15)
                break
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        try:
            out, err = p.communicate(timeout=15)
        except (subprocess.TimeoutExpired, ValueError):
            out, err = "", ""
    return p.returncode, out or "", err or "", t0, killed


def log_attempt(attempt, rc, err):
    new = not os.path.exists(ATTEMPTS)
    with open(ATTEMPTS, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "bench", "attempt", "rc", "stderr"])
        w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), BENCH, attempt, rc, err[-300:]])


def journal(err):
    """The deliveries a killed runner had recorded, from the journal it named when it started."""
    m = re.search(r"journal (\S+)", err)
    if not m or not os.path.exists(m.group(1)):
        return None
    messages = []
    for line in open(m.group(1)):
        try:
            messages.append(json.loads(line))
        except ValueError:
            break  # a torn last line
    return messages


def agent_trial():
    """Retry only a boot failure (the runner gave no result and was not killed);
    every failed attempt is logged."""
    spec_file = os.path.join(HERE, "specs", f"{BENCH}.json")
    runner = os.environ.get("OMAR_MASS_BIN", os.path.join(ROOT, "runner", "target", "release", "omar-mass"))
    cmd = [runner, "graph", "--kind", "custom", "--spec", spec_file, "--coordination", "agent",
           "--max-messages", str(FUSE), "--backend", BACKEND.lower(), "--model", MODEL,
           # A reply may take as long as the trial, as a dsl-arm agent's reaction may.
           "--timeout-secs", str(TIMEOUT), "--deadline-secs", str(TIMEOUT + GRACE)]
    if machine.workdir(TIER):
        # Each agent in a folder of its own under the workdir the dsl arm's
        # tier uses, as OMAR starts the dsl arm's agents each in its own.
        cmd += ["--agent-dirs", os.path.join(machine.workdir(TIER), "agents")]
    if BACKEND == "Mock":
        script = f"{sys.executable} {os.path.join(HERE, 'mock_agents.py')} {BENCH} {os.environ.get('MOCK_FAULT', '')}"
        cmd = [runner, "simulate", "--spec", spec_file, "--max-messages", str(FUSE), "--agents", script]
    for attempt in range(1, RETRIES + 1):
        rc, out, err, t0, killed = run(cmd, TIMEOUT + GRACE + KILL_AFTER)
        machine.clear()  # a runner that was killed, or failed half-started, leaves agents
        try:
            result = json.loads(out[out.index("{"):])
        except ValueError:
            result = None
        if killed and result is None:
            log = journal(err)
            result = {"completed": False, "end": "killed", "hops": sum(1 for m in log or [] if m.get("reply")),
                      "reason": "killed: the runner did not stop at the time limit", "messages": log or []}
        if result is not None:
            if not result.get("folders"):  # a killed runner printed no result: its agents' folders, by its run id
                run_id = re.search(r"graph run (\w+) ready", err)
                base = os.path.join(machine.workdir(TIER) or "", "agents")
                result["folders"] = sorted(
                    os.path.join(base, d) for d in (os.listdir(base) if run_id and os.path.isdir(base) else [])
                    if d.startswith(f"mass{run_id.group(1)}-"))
            return result, rc, err, t0, attempt
        log_attempt(attempt, rc, err)
        time.sleep(30)
    return None, rc, err, t0, RETRIES


def dsl_trial():
    assert BACKEND != "Mock", "the mock tier plays the agent arm; use --tier stub for the dsl arm"
    suffix = {"Codex": ".codex.omar", "Claude": ".omar", "Stub": ".stub.omar"}[BACKEND]
    name = {"philosophers": "Philosophers", "philosophers-90": "Philosophers90", "banking": "Banking",
            "boundedbuffer": "BoundedBuffer"}[BENCH]
    program = os.path.join(HERE, "programs", name + suffix)
    local = os.environ.get("OMAR_LOCAL", os.path.join(ROOT, "local"))  # installed by infra/bootstrap.sh
    cmd = [os.path.join(local, "bin", "omar-dsl"), "-c", os.path.join(local, f"config-{TIER}.toml"), "run",
           program, "--replace", "--fg", "--timeout-seconds", str(TIMEOUT)]
    env = dict(os.environ, OMARC_BIN=os.path.join(local, "bin", "omarc"))
    for attempt in range(1, RETRIES + 1):
        rc, out, err, t0, killed = run(cmd, TIMEOUT + GRACE, env)
        if killed or rc != 0:
            machine.stop_dsl(cmd[0], out + err)
        if killed:
            err = (err + "\nhard timeout").strip()
        outputs = dict(re.findall(r'Output (\S+) = "(.*)"', out))
        # Retry only a run whose agent never received its task, like a failed boot.
        if rc == 0 or "failed to deliver" not in err or outputs:
            break
        log_attempt(attempt, rc, err)
    return outputs, "completed" in out, rc, err, t0, attempt, re.findall(r"Started session (\S+?);", out + err)[-1:]


def void(trial, cause, why):
    """Name a trial the arm is not answerable for, so the scheduler lists it and runs it again."""
    path = os.environ.get("VOID_FILE")
    if path:
        with open(path, "a") as f:
            f.write(json.dumps({"unit": BENCH, "arm": ARM, "trial": trial, "reason": f"{cause}: {why}"}) + "\n")


def main():
    held = None
    if not os.environ.get("OMAR_BENCH_SCHEDULER") and BACKEND != "Mock":
        held = machine.lock()
        if not held:
            sys.exit("a scheduler is running on this machine; trials run one at a time")
    done = done_trials()
    for trial in range(1, TRIALS + 1):
        if trial in done:
            continue
        machine.clear()
        try:
            if ARM == "agent":
                result, rc, err, t0, attempts = agent_trial()
                raw = {k: result[k] for k in ("messages", "output", "end", "end_node", "evidence")
                       if result and k in result}
                folders, sessions = (result or {}).get("folders") or [], []
            else:
                outputs, completed, rc, err, t0, attempts, sessions = dsl_trial()
                raw, folders = {"outputs": outputs}, []
        finally:
            machine.clear()
        # Only Claude Code keeps the record this reads.
        used, raw["usage"] = (machine.usage(folders, sessions) if BACKEND == "Claude" and (folders or sessions)
                              else ({}, []))
        # The record is kept before it is scored, so that a trial the scorer
        # cannot read is still there to be read.
        with open(RAW, "a") as f:
            f.write(json.dumps({"trial": trial, **raw}) + "\n")
        try:
            score = (check.agent(BENCH, result, rc, err) if ARM == "agent"
                     else check.dsl(BENCH, outputs, completed, rc, err))
        except Exception as e:  # noqa: BLE001: whatever it is, the trial must not vanish
            score = {"success": False, "cause": "harness", "reason": f"the scorer failed: {e!r}"[:200]}
        if used.get("models") and used["models"] != MODEL:
            # Whatever it scored, it was not this model's trial.
            score = {**score, "success": False, "cause": "harness",
                     "reason": f"agents ran on {used['models']}, not {MODEL}"}
        row = {"model": MODEL, "arm": ARM, "bench": BENCH, "trial": trial, "rc": rc,
               "stderr": err[-300:], "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
               "secs": round(time.time() - t0), "attempts": attempts, **score, **used}
        new = not os.path.exists(OUT)
        with open(OUT, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", restval="")
            if new:
                w.writeheader()
            w.writerow(row)
        if score["cause"] in ("provider", "harness"):
            void(trial, score["cause"], score.get("evidence") or (score["reason"] if score["cause"] == "harness"
                                                                   else err[-200:].replace("\n", " | ")))
        print(f"  {BENCH} {ARM} t{trial}: {score['cause']} {score.get('reason', '')} "
              f"({row['secs']}s)", flush=True)


if __name__ == "__main__":
    main()
