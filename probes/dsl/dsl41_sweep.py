#!/usr/bin/env python3
"""DSL-arm sweep of the probes that need OMAR 0.4.1's features: judged and
bounded rings, and scatter-gather with missing workers.

Mirrors ../sweep.py doses/prompts (ring n in 10/25/40, RING_Q verbatim, 2n
horizon as wiring; missing workers n=5, the policy settings, the first f
workers dead via within(1ms)). Rows are labelled arm=dsl41 in their own CSV,
as in the original study.

Usage: python3 dsl41_sweep.py --tier haiku [--trials 10] [--families ...]
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
# outside the program: Claude through Claude Code's user settings on the machine
# (infra/claude-model.sh), Codex through CODEX_HOME/config.toml.
TIERS = {"haiku": ("claude-haiku-4-5-20251001", "Claude"), "opus": ("claude-opus-5", "Claude"),
         "haiku55": ("claude-haiku-5-5", "Claude"), "sol": ("gpt-6.1-sol", "Codex"),
         "stub": ("stub", "Stub")}
MODEL, BACKEND = TIERS[TIER]
CFG = os.path.join(LOCAL, f"config-{TIER}.toml")
TRIALS = int(flag("--trials", "10"))
FAMILIES = flag("--families", "sg-missing,ring-judged,ring-bounded").split(",")
RING_NS = [int(x) for x in flag("--ring-ns", "10,25,40").split(",")]
LAPS = 2
TIMEOUT = int(flag("--timeout", "300"))
STUB = "--stub" in sys.argv

RING_Q = "Continuous token-passing heartbeat with no natural end. Keep circulating; pass onward each hop."
SG_Q = "What is 12 times 12? Give ONLY the number."
GOOD = "144"
MISS_DOSES = [("none", [1, 3]), ("all", [1, 3]), ("quorum3", [1, 2, 3])]

SLUG = "".join(c if c.isalnum() or c in "-_" else "-" for c in MODEL)
OUT = os.path.join(os.environ.get("RESULTS_DIR", os.path.join(ROOT, "results")),
                   f"sweep-{SLUG}-dsl41{'-stub' if STUB else ''}.csv")
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
            f.write(json.dumps({"unit": family, "arm": "dsl41", "trial": trial, "reason": why}) + "\n")
    new = not os.path.exists(OUT)
    with open(OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", restval="")
        if new: w.writeheader()
        w.writerow({"model": MODEL, "arm": "dsl41", "family": family, "n": n,
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

def trail_counts(msg, n, laps):
    # Each node should appear once per completed lap.
    hits = sum(min(len(re.findall(rf"node {i}\b", msg)), laps) for i in range(1, n + 1))
    return hits

def ring_family(judged, n, trial):
    family = "ring-judged" if judged else "ring-bounded"
    if (family, str(n), "-", str(trial)) in DONE: return
    program = f"Ring{'J' if judged else 'B'}{n}.omar"
    d = run_omar(program, {"r1.m1": RING_Q})
    fwd = sum(1 for k in d["outputs"] if re.fullmatch(r"r\d+\.fwd\d", k))
    done_ports = [k for k in d["outputs"] if re.fullmatch(r"r\d+\.done\d", k)]
    if done_ports:
        m = re.fullmatch(r"r(\d+)\.done(\d)", done_ports[0])
        stop = (int(m.group(2)) - 1) * n + int(m.group(1))
        final = d["outputs"][done_ports[0]]
    else:
        stop = None
        final = d["outputs"].get(f"r{n}.fwd{LAPS}", "")
    laps = round(stop / n, 2) if stop else round(fwd / n, 2)
    trail = trail_counts(final, n, LAPS)
    log(family, n, "-", trial,
        {"hops": fwd + len(done_ports),
         "completed": "yes" if d["completed"] else "no", "reason": "",
         "metric": f"stop_hop={stop} laps={laps} halted={'yes' if done_ports else 'no'} trail={trail}/{LAPS * n}",
         "rc": d["rc"], "stderr": d["stderr"], "output": final[:500]})
    print(f"  {family} n={n} t{trial}: stop_hop={stop} laps={laps} ({d['secs']}s)", flush=True)

def missing_family(trial):
    n = 5
    for pol, fails in MISS_DOSES:
        for fc in fails:
            param = f"policy={pol} fail={fc}"
            if ("sg-missing", str(n), param, str(trial)) in DONE: continue
            program = f"SGmiss5-{pol}-f{fc}.omar"
            inputs = {f"w{i}.q": SG_Q for i in range(1, n + 1)}
            d = run_omar(program, inputs)
            arrived = sum(1 for k in d["outputs"] if k.endswith(".report"))
            ans = d["outputs"].get("g.answer")
            avail = "yes" if ans is not None else "no"
            verdict = "" if ans is None else (" verdict=" + ("good" if GOOD in ans else "other"))
            log("sg-missing", n, param, trial,
                {"hops": arrived, "completed": "yes" if d["completed"] else "no",
                 "reason": f"{arrived}/{n} arrived",
                 "metric": f"available={avail}{verdict}",
                 "rc": d["rc"], "stderr": d["stderr"], "output": (ans or "")[:200]})
            print(f"  sg-missing {param} t{trial}: available={avail}{verdict} ({d['secs']}s)", flush=True)

for trial in range(1, TRIALS + 1):
    print(f"=== trial {trial}/{TRIALS} tier={TIER} (dsl41) ===", flush=True)
    if "sg-missing" in FAMILIES:
        missing_family(trial)
    for n in RING_NS:
        if "ring-judged" in FAMILIES:
            ring_family(True, n, trial)
        if "ring-bounded" in FAMILIES:
            ring_family(False, n, trial)

print(f"DONE -> {OUT}", flush=True)
