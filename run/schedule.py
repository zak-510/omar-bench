#!/usr/bin/env python3
"""Run the pre-registered trials in one fixed random order, one at a time.

Every unit in run/plan.py appears once per trial it needs, and the list is
shuffled with the pre-registered seed, so arms and benchmarks interleave and a
run stopped early leaves a balanced sample. Each pick runs the next trial of its
unit; the sweeps skip finished trials, so rerunning continues where it stopped.
The ledger records weekly Codex use before and after every trial.

  python3 run/schedule.py --tier sol [--max-agents N] [--units a,b] [--quota 85]
                          [--until 2026-11-25T18:00] [--shard 0/2] [--pilot 3] [--dry-run]

--max-agents keeps units this machine has memory for; both arms of a unit
always run on the same instance type. --shard I/N splits the order across N
identical machines: machine I takes picks I, I+N, I+2N..., so each machine's
share stays interleaved, and writes to its own results folder. Counted runs (no --pilot) refuse to start
unless REVISION names a clean, tagged commit.
"""
import csv
import json
import os
import random
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "run"))
import codex_quota  # noqa: E402
import plan  # noqa: E402

SEED = 20261119
TIERS = {"sol": ("gpt-6.1-sol", "codex"), "haiku55": ("claude-haiku-5-5", "claude")}


def flag(name, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def revision():
    path = os.path.join(ROOT, "REVISION")
    return open(path).read().strip() if os.path.exists(path) else ""


def manifest(results, tier):
    def out(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    token = out(["curl", "-s", "-X", "PUT", "http://169.254.169.254/latest/api/token",
                 "-H", "X-aws-ec2-metadata-token-ttl-seconds: 60"])
    meta = {k: out(["curl", "-s", "-H", f"X-aws-ec2-metadata-token: {token}",
                    f"http://169.254.169.254/latest/meta-data/{k}"])
            for k in ("instance-id", "instance-type", "ami-id")}
    m = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tier": tier, "argv": sys.argv[1:],
         "revision": revision(), "seed": SEED, **meta,
         "versions": open(os.path.join(ROOT, "versions.env")).read(),
         "omar": out(["omar", "--version"]), "codex": out(["codex", "--version"]),
         "claude": out(["claude", "--version"]), "rustc": out(["rustc", "--version"]),
         "kernel": out(["uname", "-r"])}
    with open(os.path.join(results, f"manifest-{m['started']}.json"), "w") as f:
        json.dump(m, f, indent=1)


def main():
    tier = flag("--tier", "sol")
    model, backend = TIERS[tier]
    pilot = int(flag("--pilot", "0"))
    shard, shards = (int(x) for x in flag("--shard", "0/1").split("/"))
    results = os.path.join(ROOT, "results", "pilot" if pilot else "", tier,
                           f"shard-{shard}-of-{shards}" if shards > 1 else "")
    rev = revision()
    if not pilot and "--dry-run" not in sys.argv and (not rev or "dirty" in rev or "tag:" not in rev):
        sys.exit(f"counted runs need a clean, tagged commit; REVISION says {rev!r}")

    units = plan.units()
    keep = set(flag("--units", "").split(",")) - {""}
    max_agents = int(flag("--max-agents", "1000"))
    fits = {u.name for u in units if all(v.agents <= max_agents for v in units if v.name == u.name)}
    units = [u for u in units if u.name in fits and (not keep or u.name in keep)]

    order = [u for u in plan.units() for _ in range(pilot or u.trials)]
    random.Random(SEED).shuffle(order)
    order = [u for u in order if u in units][shard::shards]
    ledger_path = os.path.join(results, "schedule.csv")
    finished = {}
    if os.path.exists(ledger_path):
        for row in csv.DictReader(open(ledger_path)):
            if row["rc"] == "0":
                finished[(row["unit"], row["arm"])] = finished.get((row["unit"], row["arm"]), 0) + 1
    if "--dry-run" in sys.argv:
        for arm, (trials, steps, calls) in plan.totals(units).items():
            print(f"{arm:6} {trials:4} trials, {steps:6} sequential steps, {calls:6} agent calls")
        print(f"{len(order)} picks; already finished: {sum(finished.values())}")
        return

    os.makedirs(results, exist_ok=True)
    manifest(results, tier)
    env = dict(os.environ, RESULTS_DIR=results)
    mem = subprocess.Popen(["sh", os.path.join(ROOT, "infra", "memlog.sh"), os.path.join(results, "mem.csv")])
    quota = float(flag("--quota", "85"))
    # Also stop mid-trial if the limit is reached; the interrupted trial is rerun next time.
    guard = (subprocess.Popen(["python3", os.path.join(ROOT, "run", "codex_quota.py"), "--guard", str(quota)])
             if backend == "codex" else None)
    until = flag("--until")
    try:
        for u in order:
            key = (u.name, u.arm)
            k = finished.get(key, 0) + 1
            if k > (pilot or u.trials):
                continue
            if until and time.strftime("%Y-%m-%dT%H:%M", time.gmtime()) >= until:
                print(f"deadline {until} reached; stopping", flush=True)
                break
            used = codex_quota.reading() if backend == "codex" else None
            if used and used[0] >= quota:
                print(f"weekly Codex use {used[0]}% >= {quota}%; stopping", flush=True)
                break
            argv = ["python3", *u.argv, "--tier", tier, "--trials", str(k), "--timeout", str(u.timeout)]
            if u.argv[0] == plan.SUMMER_AGENT:
                argv = ["python3", u.argv[0], "--spawn-window", "6", "--model", model, "--backend", backend,
                        *u.argv[1:], "--trials", str(k), "--hard-timeout", str(u.timeout)]
            started = time.time()
            print(f"=== {time.strftime('%H:%M:%S')} {u.name} {u.arm} trial {k}", flush=True)
            rc = subprocess.run(argv, cwd=ROOT, env=env).returncode
            secs = round(time.time() - started)
            after = codex_quota.reading() if backend == "codex" else None
            new = not os.path.exists(ledger_path)
            with open(ledger_path, "a", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["unit", "arm", "trial", "started_at", "secs", "rc", "used_before", "used_after"])
                w.writerow([u.name, u.arm, k, time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started)), secs, rc,
                            used[0] if used else "", after[0] if after else ""])
            if rc == 0:
                finished[key] = k
    finally:
        mem.terminate()
        if guard:
            guard.terminate()


if __name__ == "__main__":
    main()
