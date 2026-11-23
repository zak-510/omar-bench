#!/usr/bin/env python3
"""Agent-arm sweep of the coordination probes (chain, ring, scatter-gather,
missing workers) through the runner's `graph` command. Writes one CSV row per run.

Paths default to the repo layout; override with env vars if needed:
  OMAR_MASS_BIN  the runner binary (default: runner/target/release/omar-mass)
  OMAR_MASS_OUT  output dir        (default: $RESULTS_DIR, else results/)
Add --daemon to detach and run in the background.

Each row says how its run ended (`end`) and, in `trouble`, whether the
machinery around the agents stopped it (run/machine.py); every delivery and
reply is kept in the matching .jsonl. A run that the provider or the
benchmark's own machinery spoiled is named in $VOID_FILE, for the scheduler
to list its trial and run it again.
"""
import csv, json, re, signal, subprocess, sys, time, os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run"))
import machine  # noqa: E402

def _raw_flag(name, default):
    """--name V from argv, else env, else default."""
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return os.environ.get(name.lstrip("-").upper().replace("-", "_"), default)

def flag(name, default):
    return int(_raw_flag(name, default))

# Repeat trials, not RNG seeds (CLI backend has none). --trials overrides all.
_TRIALS_ALL = _raw_flag("--trials", None)

def trials(name, default):
    if _TRIALS_ALL is not None:
        return int(_TRIALS_ALL)
    return flag(f"--trials-{name}", default)

TRIALS = {
    "chain": trials("chain", 10),
    "ring": trials("ring", 10),
    "ring-big": trials("ring-big", 5),
    "sg": trials("sg", 20),
    "sg-missing": trials("sg-missing", 10),
}
# --only sg,sg-missing or --skip chain,ring-big
def _csv_flag(name):
    return [s.strip() for s in (_raw_flag(name, "") or "").split(",") if s.strip()]

_ONLY, _SKIP = _csv_flag("--only"), _csv_flag("--skip")
for _k in TRIALS:
    if (_ONLY and _k not in _ONLY) or _k in _SKIP:
        TRIALS[_k] = 0

MAX_TRIALS = max(TRIALS.values())

def sizes(name, default):
    raw = _raw_flag(f"--{name}-ns", None)
    return [int(x) for x in raw.split(",")] if raw else default

CHAIN_NS = sizes("chain", [20, 50, 100])
RING_NS = sizes("ring", [10, 25, 40])

RETRIES = flag("--retries", 3)
RETRY_BACKOFF_SECS = flag("--retry-backoff", 30)
HARD_TIMEOUT = flag("--hard-timeout", 2400)
# Both arms stop a run this long after its time limit. The runner stops itself
# then and keeps its record; killing it is the last resort.
GRACE, KILL_AFTER = 120, 300
MODEL = _raw_flag("--model", "claude-haiku-4-5-20251001")
BACKEND = _raw_flag("--backend", "claude")
SLUG = "".join(c if c.isalnum() or c in "-_" else "-" for c in MODEL)

# One CSV per (model, arm).
ARM = _raw_flag("--arm", "agent")
assert ARM in ("agent", "runtime"), f"--arm must be agent or runtime, got {ARM}"

# Chain/ring resident-agent cap; 0 = spawn all upfront. Infra-only.
SPAWN_WINDOW = flag("--spawn-window", 0)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BIN = os.environ.get("OMAR_MASS_BIN", os.path.join(ROOT, "runner", "target", "release", "omar-mass"))
DIR = os.environ.get("OMAR_MASS_OUT", os.environ.get("RESULTS_DIR", os.path.join(ROOT, "results")))
os.makedirs(DIR, exist_ok=True)
OUT = os.path.join(DIR, f"sweep-{SLUG}-{ARM}.csv")
RAW = OUT[:-4] + ".jsonl"
DONE = os.path.join(DIR, f"DONE-{SLUG}-{ARM}")
LOG = os.path.join(DIR, f"sweep-{SLUG}-{ARM}.log")

def daemonize():
    """Detach so the run survives a parent exit. Opt-in via --daemon."""
    if os.fork() > 0: os._exit(0)
    os.setsid()
    if os.fork() > 0: os._exit(0)
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 1); os.dup2(fd, 2)

if "--daemon" in sys.argv:
    daemonize()

FACTS = ["order_id=4471", "ship_date=09-30", "city=Lisbon", "sku=XZ12", "qty=37"]
CHAIN_Q = "PAYLOAD (preserve every field exactly, carry forward): " + ", ".join(FACTS) + ". Pass onward."
RING_Q = "Continuous token-passing heartbeat with no natural end. Keep circulating; pass onward each hop."
# Checkable: gather can verify 12x12 itself. Blind: honest value reaches the
# gather only through workers. Curve gap = knowledge substitution.
SG_Q = "What is 12 times 12? Give ONLY the number."
GOOD, CORRUPT = "144", "999"

BLIND_GOOD, BLIND_CORRUPT = "7731", "4102"
BLIND_Q = (f"The vault code for terminal 7 is {BLIND_GOOD}. "
           "Report that code and nothing else. Give ONLY the 4-digit code.")
BLIND_GATHER_Q = "What is the vault code for terminal 7? Give ONLY the 4-digit code."

# n=9 gives a nine-point dose curve; n=5 kept for continuity with early runs.
SG_WIDE_N = 9
SG_WIDE_LEVELS = list(range(SG_WIDE_N))  # 0..8, one honest worker always left

FIELDS = ["model", "arm", "family", "n", "param", "trial", "hops", "completed",
          "reason", "metric", "rc", "stderr", "output", "started_at", "secs",
          "end", "lost", "silent_secs", "evidence", "trouble"]

def cell_key(family, n, param, trial):
    return (str(family), str(n), str(param), str(trial))

def load_done(path):
    """Cells already on disk, so a killed sweep resumes. Rows with no rc are
    infra failures; dropped on load so re-runs retry them."""
    if not os.path.exists(path):
        return set(), 0
    with open(path, newline="") as f:
        seen = [r for r in csv.DictReader(f) if r.get("family")]  # skip a torn last row
    keep = [r for r in seen if (r.get("rc") or "") != ""]
    if len(keep) != len(seen):
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", restval="")
            w.writeheader(); w.writerows(keep)
    return ({cell_key(r["family"], r["n"], r["param"], r["trial"]) for r in keep},
            len(seen) - len(keep))

if "--fresh" in sys.argv and os.path.exists(OUT):
    os.rename(OUT, f"{OUT}.{time.strftime('%Y%m%d-%H%M%S')}.bak")

DONE_CELLS, _stale = load_done(OUT)
if DONE_CELLS or _stale:
    print(f"resuming: {len(DONE_CELLS)} cells already done"
          + (f", {_stale} unfinished row(s) dropped" if _stale else ""), flush=True)

def done(family, n, param, trial):
    return cell_key(family, n, param, trial) in DONE_CELLS

def kill_tree(p):
    """Kill the process group. Drop skips on SIGKILL, so killing the binary
    alone orphans mcp-server and its agents. TERM first, then KILL."""
    try:
        pgid = os.getpgid(p.pid)
    except ProcessLookupError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            p.wait(timeout=15)
            return
        except subprocess.TimeoutExpired:
            pass

def run_once(args, per_wave_timeout, hard_timeout):
    """One invocation. Always returns rc + stderr so a failure stays diagnosable."""
    t0 = time.time()
    d = _run_once(args, per_wave_timeout, hard_timeout)
    d["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0))
    d["secs"] = round(time.time() - t0)
    return d

def _run_once(args, per_wave_timeout, hard_timeout):
    cmd = [BIN, "graph"] + args + ["--timeout-secs", str(per_wave_timeout),
                                   "--deadline-secs", str(hard_timeout + GRACE),
                                   "--model", MODEL, "--backend", BACKEND, "--coordination", ARM]
    kind = args[args.index("--kind") + 1]
    if SPAWN_WINDOW and kind in ("chain", "ring"):
        cmd += ["--spawn-window", str(SPAWN_WINDOW)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=True)
    try:
        out, err = p.communicate(timeout=hard_timeout + GRACE + KILL_AFTER)
    except subprocess.TimeoutExpired:
        # The runner stops itself at its deadline; reaching this is our failure.
        kill_tree(p)
        out, err = p.communicate()
        return {"error": f"killed: the runner did not stop at {hard_timeout + GRACE}s", "rc": "timeout",
                "end": "killed", "stderr": (err or "").strip().replace("\n", " ")[-300:]}
    finally:
        machine.clear()  # a runner that failed half-started leaves agents
    err = (err or "").strip().replace("\n", " ")[-300:]
    try:
        d = json.loads(out)
    except json.JSONDecodeError:
        # Empty stdout means the binary bailed; the reason is on stderr.
        return {"error": f"no JSON on stdout (rc={p.returncode})",
                "rc": p.returncode, "stderr": err}
    d["rc"], d["stderr"] = p.returncode, err
    return d

ATTEMPTS = OUT[:-4] + "-attempts.csv"

# A reply may take as long as the trial, as an agent's reaction may in the dsl
# arm; the re-nudge machinery still fires at 180s.
def run(args, per_wave_timeout=HARD_TIMEOUT, hard_timeout=HARD_TIMEOUT, attempts=RETRIES):
    """Retry boot failures only. A stall or timeout is an outcome of the arm and
    is never retried; every failed boot attempt is kept in the -attempts.csv log."""
    for attempt in range(1, attempts + 1):
        d = run_once(args, per_wave_timeout, hard_timeout)
        if "error" not in d or d["rc"] == "timeout":
            return d
        new = not os.path.exists(ATTEMPTS)
        with open(ATTEMPTS, "a", newline="") as f:
            w = csv.writer(f)
            if new: w.writerow(["time", "args", "attempt", "error", "stderr"])
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), " ".join(args), attempt,
                        d.get("error", ""), d.get("stderr", "")[-300:]])
        if attempt < attempts:
            why = d.get("error") or d.get("reason")
            print(f"  retry {attempt}/{attempts - 1}: {why} | {d['stderr'][:120]}",
                  flush=True)
            time.sleep(RETRY_BACKOFF_SECS)
    return d

def log(family, n, param, trial, d, metric):
    end = d.get("end") or ("no-result" if "error" in d else "")
    evidence = d.get("evidence") or ""
    trouble = "harness" if end == "killed" else machine.trouble(
        end=end, silent_secs=d.get("silent_secs"), evidence=evidence,
        rc=0 if d.get("rc") in (0, "timeout") else d.get("rc"), stderr=d.get("stderr", ""))
    row = {"model": MODEL, "arm": ARM, "family": family, "n": n, "param": param, "trial": trial,
           "hops": d.get("hops"), "completed": d.get("completed"),
           "reason": d.get("reason", d.get("error", "")), "metric": metric,
           "rc": d.get("rc"), "stderr": d.get("stderr", ""),
           "output": (d.get("output") or "")[:60],
           "started_at": d.get("started_at", ""), "secs": d.get("secs", ""),
           "end": end, "lost": d.get("errors", ""), "silent_secs": d.get("silent_secs", ""),
           "evidence": evidence[-300:].replace("\n", " | "), "trouble": trouble}
    with open(RAW, "a") as f:
        f.write(json.dumps({"family": family, "n": n, "param": param, "trial": trial,
                            **{k: d.get(k) for k in ("messages", "output", "end", "end_node", "evidence")}}) + "\n")
    if trouble in ("provider", "harness") and os.environ.get("VOID_FILE"):
        with open(os.environ["VOID_FILE"], "a") as f:
            f.write(json.dumps({"unit": family, "arm": ARM, "trial": trial,
                                "reason": f"{trouble}: {evidence[-200:] or row['reason']}"}) + "\n")
    # Append + fsync per row.
    header = not os.path.exists(OUT) or os.path.getsize(OUT) == 0
    with open(OUT, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", restval="")
        if header: w.writeheader()
        w.writerow(row)
        f.flush(); os.fsync(f.fileno())
    if row["rc"] is not None:
        DONE_CELLS.add(cell_key(family, n, param, trial))
    print(f"[{time.strftime('%H:%M:%S')}] {family} n={n} {param} t{trial}: "
          f"hops={row['hops']} completed={row['completed']} metric={metric}", flush=True)

if os.path.exists(DONE): os.remove(DONE)

def sg_corrupt(family, n, levels, trial, question, gather_q=None, corrupt=None,
               good=GOOD, bad=CORRUPT):
    """One corruption sweep. gather_q=None leaves the aggregator able to self-check."""
    for c in levels:
        if done(family, n, f"corrupt={c}", trial): continue
        args = ["--kind", "scatter-gather", "--n", str(n),
                "--corrupt-count", str(c), "--question", question]
        if gather_q: args += ["--gather-question", gather_q]
        if corrupt: args += ["--corrupt-answer", corrupt]
        d = run(args)
        out = d.get("output") or ""
        verdict = "good" if good in out else ("corrupt" if bad in out else "other/none")
        log(family, n, f"corrupt={c}", trial, d, f"verdict={verdict}")

# Trial is the outermost loop so an interruption leaves whole trials.
for trial in range(1, MAX_TRIALS + 1):
    print(f"=== trial {trial}/{MAX_TRIALS} ===", flush=True)

    if trial <= TRIALS["chain"]:
        for n in CHAIN_NS:
            if done("chain", n, "-", trial): continue
            d = run(["--kind", "chain", "--n", str(n), "--question", CHAIN_Q])
            out = d.get("output") or ""
            survived = sum(1 for fct in FACTS if fct in out)
            trail = sum(1 for i in range(1, n + 1) if re.search(rf"node {i}\b", out))
            log("chain", n, "-", trial, d,
                f"facts={survived}/{len(FACTS)} trail={trail}/{n} len={len(out)}")

    ring_sizes = RING_NS if trial <= TRIALS["ring"] else []
    if trial <= TRIALS["ring-big"]:
        ring_sizes = ring_sizes + [100]
    for n in ring_sizes:
        if done("ring", n, "-", trial): continue
        # Same 2n horizon both arms.
        budget = ["--max-messages", str(2 * n)] if ARM == "agent" else ["--max-hops", str(2 * n)]
        d = run(["--kind", "ring", "--n", str(n)] + budget + ["--question", RING_Q])
        stop = d.get("hops")
        laps = round(stop / n, 2) if stop else None
        log("ring", n, "-", trial, d, f"stop_hop={stop} laps={laps}")

    if trial <= TRIALS["sg"]:
        sg_corrupt("sg-corrupt", 5, [0, 1, 2, 3, 4], trial, SG_Q)
        # Controlled pair: same n and levels, differ only in self-checkability.
        sg_corrupt("sg-corrupt", SG_WIDE_N, SG_WIDE_LEVELS, trial, SG_Q)
        sg_corrupt("sg-corrupt-blind", SG_WIDE_N, SG_WIDE_LEVELS, trial, BLIND_Q,
                   gather_q=BLIND_GATHER_Q, corrupt=BLIND_CORRUPT,
                   good=BLIND_GOOD, bad=BLIND_CORRUPT)

    if trial <= TRIALS["sg-missing"]:
        if ARM == "agent":
            # Barrier policy is declared in the prompt, never enforced;
            # conformance is the measurement.
            POLICIES = [
                ("none", None, [1, 3]),
                ("all", "Answer only once you have reports from all 5 workers."
                        " If any report is missing, do not answer; keep waiting.", [1, 3]),
                ("quorum3", "Answer as soon as you have reports from at least 3"
                            " of the 5 workers. With fewer than 3 reports, do not"
                            " answer; keep waiting.", [1, 2, 3]),
            ]
            for pol, prose, fails in POLICIES:
                for fc in fails:
                    param = f"policy={pol} fail={fc}"
                    if done("sg-missing", 5, param, trial): continue
                    args = ["--kind", "scatter-gather", "--n", "5",
                            "--fail-count", str(fc), "--question", SG_Q]
                    if prose: args += ["--gather-question", f"{SG_Q} {prose}"]
                    d = run(args)
                    avail = "yes" if d.get("completed") else "no"
                    log("sg-missing", 5, param, trial, d, f"available={avail}")
        else:
            # Runtime arm: barrier policy is a flag.
            for fc in [1, 3]:
                for relaxed in [False, True]:
                    param = f"fail={fc} relaxed={relaxed}"
                    if done("sg-missing", 5, param, trial): continue
                    args = ["--kind", "scatter-gather", "--n", "5",
                            "--fail-count", str(fc), "--question", SG_Q]
                    if relaxed: args.append("--relaxed")
                    d = run(args)
                    avail = "yes" if d.get("completed") else "no"
                    log("sg-missing", 5, param, trial, d, f"available={avail}")

total = len(DONE_CELLS)
open(DONE, "w").write(f"{total} runs\n")
print(f"\nDONE. {total} runs -> {OUT}", flush=True)
