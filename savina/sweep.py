#!/usr/bin/env python3
"""Run Savina trials in one arm and score each with check.py.

Usage:
  python3 savina/sweep.py --bench philosophers|banking --arm agent|dsl --tier sol|stub
                          [--trials N] [--timeout SECS]

One CSV row per trial in $RESULTS_DIR/savina-<bench>-<model>-<arm>.csv, the
full delivery log or trace in the matching .jsonl, and every failed agent-arm
boot attempt in -attempts.csv. Completed trials are skipped on a rerun.
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
import check  # noqa: E402


def flag(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


BENCH = flag("--bench", "philosophers")
ARM = flag("--arm", "dsl")
TIER = flag("--tier", "sol")
TRIALS = int(flag("--trials", "3"))
TIMEOUT = int(flag("--timeout", "3600"))
assert BENCH in ("philosophers", "banking") and ARM in ("agent", "dsl"), (BENCH, ARM)
TIERS = {"sol": ("gpt-6.1-sol", "Codex"), "haiku55": ("claude-haiku-5-5", "Claude"),
         "opus55": ("claude-opus-5-5", "Claude"), "stub": ("stub", "Stub")}
MODEL, BACKEND = TIERS[TIER]
# Agent arm: total deliveries allowed before the run is cut, a safety fuse the
# agents are never told about: four times the fewest deliveries a correct run needs.
FUSE = 400
RETRIES = 3

RESULTS = os.environ.get("RESULTS_DIR", os.path.join(ROOT, "results"))
os.makedirs(RESULTS, exist_ok=True)
SLUG = re.sub(r"[^A-Za-z0-9_-]", "-", MODEL)
OUT = os.path.join(RESULTS, f"savina-{BENCH}-{SLUG}-{ARM}.csv")
RAW = OUT[:-4] + ".jsonl"
ATTEMPTS = OUT[:-4] + "-attempts.csv"
FIELDS = ["model", "arm", "bench", "trial", "completed", "success", "reason", "safety_violations",
          "meal_counts", "wrong_answers", "overdrafts", "wrongful_denials", "wrong_transfers",
          "decided", "balances_ok", "deviations", "deliveries", "detail", "rc", "stderr",
          "started_at", "secs"]


def done_trials():
    if not os.path.exists(OUT):
        return set()
    with open(OUT, newline="") as f:
        return {int(r["trial"]) for r in csv.DictReader(f)}


def run(cmd, env=None):
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         start_new_session=True, env=env)
    try:
        out, err = p.communicate(timeout=TIMEOUT + 120)
    except subprocess.TimeoutExpired:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(p.pid, sig)
                p.wait(timeout=15)
                break
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        out, err = "", "hard timeout"
    return p.returncode, out or "", err or "", t0


def agent_trial():
    """Retry only a boot failure (no result and no timeout); every failure is logged."""
    spec_file = os.path.join(HERE, "specs", f"{BENCH}.json")
    runner = os.environ.get("OMAR_MASS_BIN", os.path.join(ROOT, "summer", "target", "release", "omar-mass"))
    cmd = [runner, "graph", "--kind", "custom", "--spec", spec_file, "--coordination", "agent",
           "--max-messages", str(FUSE), "--backend", BACKEND.lower(), "--model", MODEL,
           # A reply may take as long as the trial, as a dsl-arm agent's reaction may.
           "--timeout-secs", str(TIMEOUT)]
    for attempt in range(1, RETRIES + 1):
        rc, out, err, t0 = run(cmd)
        try:
            result = json.loads(out[out.index("{"):])
        except ValueError:
            result = None
        if result is not None or err == "hard timeout":  # a timeout is an outcome
            return result, rc, err, t0
        new = not os.path.exists(ATTEMPTS)
        with open(ATTEMPTS, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "bench", "attempt", "rc", "stderr"])
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), BENCH, attempt, rc, err[-300:]])
        time.sleep(30)
    return None, rc, err, t0


def score_agent(result, err=""):
    if result is None:
        return {"success": False, "reason": "timed out" if err == "hard timeout" else "no result"}, {}
    messages = result.get("messages", [])
    completed = bool(result.get("completed"))
    if BENCH == "philosophers":
        events, dev = check.philosophers_agent(messages)
        score = check.philosophers(events, completed)
    else:
        decisions, reported, dev = check.banking_agent(messages)
        score = check.banking(decisions, reported, completed)
    score.update(deviations=dev, deliveries=result.get("hops"), detail=result.get("reason", ""),
                 completed=completed)
    return score, {"messages": messages, "output": result.get("output")}


def dsl_trial():
    suffix = {"Codex": ".codex.omar", "Claude": ".omar", "Stub": ".stub.omar"}[BACKEND]
    program = os.path.join(HERE, "programs", ("Philosophers" if BENCH == "philosophers" else "Banking") + suffix)
    dsl = os.path.join(ROOT, "summer", "probes", "dsl")
    cmd = [os.path.join(dsl, "bin", "omar-dsl"), "-c", os.path.join(dsl, f"config-{TIER}.toml"), "run",
           program, "--replace", "--fg", "--timeout-seconds", str(TIMEOUT)]
    env = dict(os.environ, OMARC_BIN=os.path.join(dsl, "bin", "omarc"))
    for attempt in range(1, RETRIES + 1):
        rc, out, err, t0 = run(cmd, env)
        outputs = dict(re.findall(r'Output (\S+) = "(.*)"', out))
        # Retry only a run whose agent never received its task, like a failed boot.
        if rc == 0 or "failed to deliver" not in err or outputs:
            break
        new = not os.path.exists(ATTEMPTS)
        with open(ATTEMPTS, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["time", "bench", "attempt", "rc", "stderr"])
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), BENCH, attempt, rc, err[-300:]])
    return outputs, "completed" in out, rc, err, t0


def score_dsl(outputs, completed):
    if BENCH == "philosophers":
        log = outputs.get("a.log")
        score = check.philosophers(check.philosophers_dsl(log), completed and log is not None)
    else:
        log = outputs.get("t.log")
        decisions, reported = check.banking_dsl(log)
        score = check.banking(decisions, reported, completed and log is not None)
    score.update(completed=completed, deviations=0)
    return score, {"outputs": outputs}


def main():
    done = done_trials()
    for trial in range(1, TRIALS + 1):
        if trial in done:
            continue
        if ARM == "agent":
            result, rc, err, t0 = agent_trial()
            score, raw = score_agent(result, err)
        else:
            outputs, completed, rc, err, t0 = dsl_trial()
            score, raw = score_dsl(outputs, completed)
        row = {"model": MODEL, "arm": ARM, "bench": BENCH, "trial": trial, "rc": rc,
               "stderr": err[-300:], "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
               "secs": round(time.time() - t0), **score}
        new = not os.path.exists(OUT)
        with open(OUT, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", restval="")
            if new:
                w.writeheader()
            w.writerow(row)
        with open(RAW, "a") as f:
            f.write(json.dumps({"trial": trial, **raw}) + "\n")
        print(f"  {BENCH} {ARM} t{trial}: success={score['success']} {score.get('reason', '')} "
              f"({row['secs']}s)", flush=True)


if __name__ == "__main__":
    main()
