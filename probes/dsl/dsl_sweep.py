#!/usr/bin/env python3
"""DSL-arm sweep of chain and scatter-gather: the generated .omar programs
through the OMAR runtime.

Mirrors ../sweep.py (same tasks, doses, CSV schema) with coordination owned by
the runtime. Corruption stays harness-injected: the last c workers get a
same-shape corrupt question. Rings and missing workers are in dsl41_sweep.py.

Usage:
  python3 dsl_sweep.py --tier haiku [--trials 20] [--families chain,sg,sg-blind]
"""
import csv, json, os, re, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(ROOT, "run"))
import machine  # noqa: E402
LOCAL = os.path.join(ROOT, "local")  # OMAR and the tier configs, installed by infra/bootstrap.sh

def flag(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default

TIER = flag("--tier", "haiku")
# tier -> (model id reported in the CSV, OMAR backend). The model itself is pinned
# outside the program: Claude through the workdir's .claude/settings.json, Codex
# through CODEX_HOME/config.toml (see infra/).
TIERS = {"haiku": ("claude-haiku-4-5-20251001", "Claude"), "opus": ("claude-opus-5", "Claude"),
         "haiku55": ("claude-haiku-5-5", "Claude"), "sol": ("gpt-6.1-sol", "Codex"),
         "stub": ("stub", "Stub")}
MODEL, BACKEND = TIERS[TIER]
CFG = os.path.join(LOCAL, f"config-{TIER}.toml")
TRIALS = int(flag("--trials", "20"))
FAMILIES = flag("--families", "chain,sg,sg-blind").split(",")
CHAIN_NS = [int(x) for x in flag("--chain-ns", "20").split(",")]
CHAIN_PROGRAMS = {20: "Chain20.omar", 50: "Chain50.omar", 100: "Chain100.claude.omar"}
# n=100 runs as two 50-node halves; hop 50->51 is harness-ferried byte-exact.
CHAIN_SEGMENTS = {100: [("Chain100a.claude.omar", "r1.msg", "r50.fwd"),
                        ("Chain100b.claude.omar", "r51.msg", "r100.fwd")]}
TIMEOUT = int(flag("--timeout", "300"))
STUB = "--stub" in sys.argv  # harness dry-run: swap every program for its .stub.omar twin

FACTS = ["order_id=4471", "ship_date=09-30", "city=Lisbon", "sku=XZ12", "qty=37"]
CHAIN_Q = "PAYLOAD (preserve every field exactly, carry forward): " + ", ".join(FACTS) + ". Pass onward. TRAIL:"
SG_Q = "What is 12 times 12? Give ONLY the number."
CORRUPT_Q = "Report exactly this number: 999. Give ONLY the number."
GOOD, CORRUPT = "144", "999"
BLIND_GOOD, BLIND_CORRUPT = "7731", "4102"
BLIND_Q = (f"The vault code for terminal 7 is {BLIND_GOOD}. "
           "Report that code and nothing else. Give ONLY the 4-digit code.")
BLIND_CORRUPT_Q = (f"The vault code for terminal 7 is {BLIND_CORRUPT}. "
                   "Report that code and nothing else. Give ONLY the 4-digit code.")

SLUG = "".join(c if c.isalnum() or c in "-_" else "-" for c in MODEL)
OUT = os.path.join(os.environ.get("RESULTS_DIR", os.path.join(ROOT, "results")),
                   f"sweep-{SLUG}-dsl{'-stub' if STUB else ''}.csv")
FIELDS = ["model", "arm", "family", "n", "param", "trial", "hops", "completed",
          "reason", "metric", "rc", "stderr", "output", "started_at", "secs", "trouble", *machine.USAGE]

def load_done():
    if not os.path.exists(OUT):
        return set()
    with open(OUT, newline="") as f:
        return {(r["family"], r["n"], r["param"], r["trial"])
                for r in csv.DictReader(f) if r.get("rc") != ""}

DONE = load_done()
RUN_T = {}  # start and seconds of the trial being logged

def log(family, n, param, trial, row):
    # Whether the runtime or the provider stopped the run, rather than its agents
    # (run/machine.py). A run the provider spoiled is named for the scheduler,
    # which lists its trial and runs it again.
    trouble = machine.trouble(rc=row.get("rc"), stderr=row.get("stderr", ""))
    why = "provider: " + row.get("stderr", "")[-200:]
    # What the run's agents used, and on which model (only Claude Code keeps the record).
    sessions, since = RUN_T.pop("sessions", []), RUN_T.pop("t0", 0)
    used, _ = machine.usage(sessions=sessions, since=since) if BACKEND == "Claude" and sessions else ({}, {})
    if used.get("models") and used["models"] != MODEL:
        trouble, why = "harness", f"harness: agents ran on {used['models']}, not {MODEL}"
    row = {**row, **used}
    if trouble in ("provider", "harness") and os.environ.get("VOID_FILE"):
        with open(os.environ["VOID_FILE"], "a") as f:
            f.write(json.dumps({"unit": family, "arm": "dsl", "trial": trial, "reason": why}) + "\n")
    new = not os.path.exists(OUT)
    with open(OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", restval="")
        if new: w.writeheader()
        w.writerow({"model": MODEL, "arm": "dsl", "family": family, "n": n,
                    "param": param, "trial": trial, **row, **RUN_T, "trouble": trouble})
    RUN_T.clear()

BOOT_FAILURE = "failed to deliver"  # the runtime could not hand an agent its task
ATTEMPTS = OUT[:-4] + "-attempts.csv"

def run_omar(program, inputs, attempts=3):
    """Retry only a run whose agent never received its task, at most 3 times,
    logging every failed attempt; any other outcome is the arm's."""
    for attempt in range(1, attempts + 1):
        d = _run_omar(program, inputs)
        if d["rc"] == 0 or BOOT_FAILURE not in d["stderr"] or d["outputs"]:
            return d
        new = not os.path.exists(ATTEMPTS)
        with open(ATTEMPTS, "a", newline="") as f:
            w = csv.writer(f)
            if new: w.writerow(["time", "program", "attempt", "stderr"])
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), program, attempt, d["stderr"][-300:]])
        RUN_T.clear()
    return d

def _run_omar(program, inputs):
    if STUB:
        program = program.replace(".claude.omar", ".omar").replace(".omar", ".stub.omar")
    elif BACKEND == "Codex":
        program = re.sub(r"(\.claude)?\.omar$", ".codex.omar", program)
    cmd = [os.path.join(LOCAL, "bin", "omar-dsl"), "-c", CFG, "run",
           os.path.join(HERE, program), "--replace", "--fg",
           "--timeout-seconds", str(TIMEOUT)]
    for k, v in inputs.items():
        cmd += ["--input", f"{k}={v}"]
    env = dict(os.environ, OMARC_BIN=os.path.join(LOCAL, "bin", "omarc"))
    t0 = time.time()
    RUN_T.setdefault("t0", t0)
    RUN_T.setdefault("started_at", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=TIMEOUT + 120, env=env)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        text = lambda x: x.decode(errors="replace") if isinstance(x, bytes) else (x or "")
        rc, out, err = -9, text(e.stdout), "hard timeout"
        machine.stop_dsl(cmd[0], out + text(e.stderr))
    if rc not in (0, -9):  # a run that ends well ends its own session
        machine.stop_dsl(cmd[0], out + err)
    outputs = dict(re.findall(r'Output (\S+) = "(.*)"', out))
    RUN_T.setdefault("sessions", []).extend(re.findall(r"Started session (\S+?);", out + (err or "")))
    RUN_T["secs"] = RUN_T.get("secs", 0) + round(time.time() - t0)
    return {"rc": rc, "outputs": outputs, "stdout": out,
            "stderr": (err or "")[-300:],
            "completed": "completed" in out, "secs": round(time.time() - t0)}

def kill_probe_sessions():
    ls = subprocess.run(["tmux", "ls"], capture_output=True, text=True).stdout
    for line in ls.splitlines():
        name = line.split(":")[0]
        if name.startswith("dslprobe-"):
            subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)

def run_chain_segments(segments, first_input):
    total = {"rc": 0, "outputs": {}, "stdout": "", "stderr": "",
             "completed": True, "secs": 0}
    value = first_input
    for i, (program, in_port, out_port) in enumerate(segments):
        d = run_omar(program, {in_port: value})
        total["outputs"].update(d["outputs"])
        total["secs"] += d["secs"]
        total["stderr"] = (total["stderr"] + d["stderr"])[-300:]
        total["rc"] = d["rc"]
        kill_probe_sessions()
        if d["rc"] != 0 or not d["completed"]:
            total["completed"] = False
            return total
        if i + 1 < len(segments):
            m = re.search(rf'Output {re.escape(out_port)} = (".*")', d["stdout"])
            if not m:
                total["completed"] = False
                total["stderr"] = (total["stderr"] + f"; no {out_port} to ferry")[-300:]
                return total
            value = json.loads(m.group(1))
    return total

def sg_family(family, program, n, levels, honest_q, corrupt_q, good, bad, trial):
    for c in levels:
        param = f"corrupt={c}"
        if (family, str(n), param, str(trial)) in DONE: continue
        # Arm A corrupts the LAST c slots (runner.rs: slot >= n - c + 1).
        inputs = {f"w{i}.q": (corrupt_q if i > n - c else honest_q)
                  for i in range(1, n + 1)}
        d = run_omar(program, inputs)
        ans = d["outputs"].get("g.answer", "")
        verdict = "good" if good in ans else ("corrupt" if bad in ans else "other/none")
        arrived = sum(1 for k in d["outputs"] if k.endswith(".report"))
        log(family, n, param, trial,
            {"hops": arrived, "completed": "yes" if d["completed"] else "no",
             "reason": f"{arrived}/{n} arrived", "metric": f"verdict={verdict}",
             "rc": d["rc"], "stderr": d["stderr"], "output": ans})
        print(f"  {family} n={n} {param} t{trial}: {verdict} ({d['secs']}s)", flush=True)

for trial in range(1, TRIALS + 1):
    print(f"=== trial {trial}/{TRIALS} tier={TIER} ===", flush=True)
    if "chain" in FAMILIES:
        for cn in CHAIN_NS:
            if ("chain", str(cn), "-", str(trial)) in DONE: continue
            if cn in CHAIN_SEGMENTS:
                d = run_chain_segments(CHAIN_SEGMENTS[cn], CHAIN_Q)
            else:
                d = run_omar(CHAIN_PROGRAMS[cn], {"r1.msg": CHAIN_Q})
            out = d["outputs"].get(f"r{cn}.fwd", "")
            facts = sum(1 for f_ in FACTS if f_ in out)
            trail = sum(1 for i in range(1, cn + 1) if re.search(rf"node {i}\b", out))
            log("chain", cn, "-", trial,
                {"hops": len([k for k in d["outputs"] if k.endswith(".fwd")]),
                 "completed": "yes" if d["completed"] else "no", "reason": "",
                 "metric": f"facts={facts}/{len(FACTS)} trail={trail}/{cn} len={len(out)}",
                 "rc": d["rc"], "stderr": d["stderr"], "output": out[:500]})
            print(f"  chain n={cn} t{trial}: facts={facts}/5 trail={trail}/{cn} ({d['secs']}s)", flush=True)
    if "sg" in FAMILIES:
        sg_family("sg-corrupt", "SGcheck5.omar", 5, [0, 1, 2, 3, 4], SG_Q, CORRUPT_Q, GOOD, CORRUPT, trial)
        sg_family("sg-corrupt", "SGcheck9.omar", 9, list(range(9)), SG_Q, CORRUPT_Q, GOOD, CORRUPT, trial)
    if "sg-blind" in FAMILIES:
        sg_family("sg-corrupt-blind", "SGblind9.omar", 9, list(range(9)),
                  BLIND_Q, BLIND_CORRUPT_Q, BLIND_GOOD, BLIND_CORRUPT, trial)

print(f"DONE -> {OUT}", flush=True)
