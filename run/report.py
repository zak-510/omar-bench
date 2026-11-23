#!/usr/bin/env python3
"""The results table the pre-registration promises, from the files the sweeps
and the scheduler write.

  python3 run/report.py RESULTS_DIR [RESULTS_DIR...]     e.g. results/prereg-v8

Every trial is under one heading (savina/check.py, `cause`), and the headings
are never merged:

  ok              the run finished and every invariant held
  safety          an invariant was broken
  incomplete      the work was not finished
  protocol        agent arm: a message was lost or unreadable, and the run failed
  runtime         the machinery of the arm failed (agents that did not start
                  or never answered, the runtime failing)
  not counted     the provider was limiting use, the scorer itself failed, or
                  an agent ran on another model than the tier's: listed in
                  exclusions.csv with the reason, and run again

Reliability is ok over counted, with a Wilson 95% interval and no percentage
of its own (nothing here is to be read as "100%"). Counted is every trial not
listed as not counted, up to the planned number in start order. Runtime
failures count: a run that did not finish did not finish, whichever part of
the stack stopped it.
"""
import collections
import csv
import glob
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plan  # noqa: E402

HEADINGS = (("ok", "success"), ("safety", "safety"), ("incomplete", "noncompletion"),
            ("protocol", "protocol"), ("runtime", "runtime"))


def wilson(k, n, z=1.96):
    if n == 0:
        return 0.0, 0.0
    p = k / n
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, c - h), min(1.0, c + h)


def fisher(fa, na, fd, nd):
    """One-sided: the chance of at least fa of the failures landing in the agent arm."""
    F, N = fa + fd, na + nd
    if not na or not nd:
        return float("nan")
    return sum(math.comb(F, x) * math.comb(N - F, na - x) for x in range(fa, min(F, na) + 1)) / math.comb(N, na)


def excluded(folder):
    """Trials listed as not counted in one results folder: (unit, arm, trial) -> reason."""
    path = os.path.join(folder, "exclusions.csv")
    if not os.path.exists(path):
        return {}
    return {(r["unit"], r["arm"], r["trial"]): r["reason"] for r in csv.DictReader(open(path))}


def savina(roots):
    planned = {(u.name, u.arm): u.trials for u in plan.units()}
    cells = collections.defaultdict(list)
    void = collections.defaultdict(list)
    old = 0
    for root in roots:
        for path in sorted(glob.glob(os.path.join(root, "**", "savina-*.csv"), recursive=True)):
            if path.endswith("-attempts.csv"):
                continue
            gone = excluded(os.path.dirname(path))
            for r in csv.DictReader(open(path)):
                key = (r["model"], r["bench"], r["arm"])
                if not r.get("cause"):
                    old += 1  # written before prereg-v8: run/audit.py reads those
                elif (r["bench"], r["arm"], r["trial"]) in gone or r["cause"] in ("provider", "harness"):
                    void[key].append(gone.get((r["bench"], r["arm"], r["trial"]), r["cause"]))
                else:
                    cells[key].append(r)
    if old:
        print(f"({old} rows have no cause: they were written before prereg-v8; see run/audit.py)\n")
    if not cells and not void:
        return
    print("SAVINA")
    print(f"{'model':18} {'benchmark':16} {'arm':6} {'plan':>4} {'run':>4} {'not':>4} {'cnt':>4} "
          + " ".join(f"{h:>10}" for h, _ in HEADINGS) + f"  {'ok/counted (95% CI)':22} {'mean s':>6}")
    for model in sorted({k[0] for k in list(cells) + list(void)}):
        for bench in sorted({k[1] for k in list(cells) + list(void) if k[0] == model}):
            fails = {}
            for arm in ("agent", "dsl"):
                key = (model, bench, arm)
                n_plan = planned.get((bench, arm), 0)
                rows = sorted(cells.get(key, []), key=lambda r: r["started_at"])
                if not rows and not void.get(key):
                    fails[arm] = (0, 0, 0)
                    continue
                counted = rows[:n_plan] if n_plan else rows
                by = collections.Counter(r["cause"] for r in counted)
                ok, n = by["success"], len(counted)
                lo, hi = wilson(ok, n)
                secs = [float(r["secs"]) for r in counted if r.get("secs")]
                fails[arm] = (n - ok, n, by["runtime"])
                print(f"{model:18} {bench:16} {arm:6} {n_plan:>4} {len(rows) + len(void.get(key, [])):>4} "
                      f"{len(void.get(key, [])):>4} {n:>4} "
                      + " ".join(f"{by[c]:>10}" for _, c in HEADINGS)
                      + (f"  {ok}/{n} [{lo:.0%}, {hi:.0%}]".ljust(24) if n else "  -".ljust(24))
                      + (f" {sum(secs) / len(secs):>6.0f}" if secs else "")
                      + (f"  (+{len(rows) - n} past plan, not counted)" if len(rows) > n else "")
                      + ("  UNDERPOWERED" if n < n_plan else ""))
            (fa, na, ra), (fd, nd, rd) = fails["agent"], fails["dsl"]
            if na and nd:
                print(f"{'':18} {bench:16} failures, agent vs dsl: {fa}/{na} vs {fd}/{nd}, one-sided Fisher "
                      f"p = {fisher(fa, na, fd, nd):.3f}; leaving runtime failures out: {fa - ra}/{na - ra} vs "
                      f"{fd - rd}/{nd - rd}, p = {fisher(fa - ra, na - ra, fd - rd, nd - rd):.3f}")
    reasons = collections.Counter((k[0], k[1], k[2], r.split(":")[0]) for k, rs in void.items() for r in rs)
    if reasons:
        print("\nnot counted, and run again:")
        for (model, bench, arm, kind), n in sorted(reasons.items()):
            print(f"  {model} {bench} {arm}: {n} ({kind})")
    tokens(cells, planned)


def tokens(cells, planned):
    """What a trial's agents used, as their CLI recorded it, over the counted
    trials whose every agent had a record. Cached input is most of what an
    agent reads, so it is shown apart; cost is at list price."""
    rows = []
    for (model, bench, arm), trials in sorted(cells.items()):
        n_plan = planned.get((bench, arm), 0)
        counted = sorted(trials, key=lambda r: r["started_at"])[:n_plan or None]
        metered = [r for r in counted if r.get("metered") and r["metered"].split("/")[0] == r["metered"].split("/")[1]]
        if not metered:
            continue
        mean = lambda key: sum(float(r[key] or 0) for r in metered) / len(metered)
        rows.append((model, bench, arm, len(metered), len(counted), mean("tokens_out"), mean("tokens_in"),
                     mean("tokens_cache_write"), mean("tokens_cache_read"), mean("cost_usd"),
                     " ".join(sorted({r["models"] for r in metered}))))
    if not rows:
        return
    print("\nTOKENS AND COST per trial (mean over the counted trials whose every agent was metered; cost at list price)")
    print(f"{'model':18} {'benchmark':16} {'arm':6} {'metered':>9} {'output':>9} {'input':>8} {'cache write':>12} "
          f"{'cache read':>12} {'cost $':>8}  ran on")
    for model, bench, arm, n, of, out, inp, cw, cr, cost, models in rows:
        print(f"{model:18} {bench:16} {arm:6} {f'{n}/{of}':>9} {out:>9,.0f} {inp:>8,.0f} {cw:>12,.0f} {cr:>12,.0f} "
              f"{cost:>8.3f}  {models}")
    by = {(m, b, a): r for m, b, a, *r in rows}
    for (model, bench, arm), r in sorted(by.items()):
        other = by.get((model, bench, "dsl"))
        if arm == "agent" and other and other[7]:
            print(f"{'':18} {bench:16} agent arm over dsl arm: {r[2] / other[2]:.1f}x the output tokens, "
                  f"{r[7] / other[7]:.1f}x the cost")


EXPECT = {("all", f): "no" for f in "123"}
EXPECT.update({("quorum3", "1"): "yes", ("quorum3", "2"): "yes", ("quorum3", "3"): "no"})


def probes(roots):
    runs = collections.defaultdict(list)
    for root in roots:
        for path in sorted(glob.glob(os.path.join(root, "**", "sweep-*.csv"), recursive=True)):
            if path.endswith("-attempts.csv"):
                continue
            gone = {(arm, trial) for (unit, arm, trial) in excluded(os.path.dirname(path))
                    if unit in ("scatter-gather", "missing-workers") or unit.startswith(("chain", "ring"))}
            arm = "agent" if path.endswith("-agent.csv") else "dsl"
            for r in csv.DictReader(open(path)):
                if (arm, r["trial"]) in gone:
                    continue
                runs[(r["model"], r["family"], r["n"], arm)].append(r)
    if not runs:
        return
    print("\nPROBES (runs; a run the machinery stopped is under 'stopped', never under the arm's outcomes)")
    for (model, family, n, arm), rows in sorted(runs.items()):
        stopped = [r for r in rows if r.get("trouble")]
        rows = [r for r in rows if not r.get("trouble")]
        metric = lambda r: r.get("metric") or ""
        if family == "sg-corrupt":
            good = sum("verdict=good" in metric(r) for r in rows)
            flips = sum("verdict=corrupt" in metric(r) for r in rows)
            text = f"{good}/{len(rows)} answered 144, {flips} took the planted 999, {len(rows) - good - flips} other or none"
        elif family == "sg-corrupt-blind":
            text = f"{len(rows)} runs (control): " + str(dict(collections.Counter(
                metric(r).replace("verdict=", "") for r in rows)))
        elif family == "sg-missing":
            graded = [r for r in rows if "policy=none" not in r["param"]]
            ok = sum(("yes" if "available=yes" in metric(r) else "no")
                     == EXPECT[tuple(p.split("=")[1] for p in r["param"].split()[:2])] for r in graded)
            free = [r for r in rows if "policy=none" in r["param"]]
            text = (f"{ok}/{len(graded)} followed the stated policy; with none stated, "
                    f"{sum('available=yes' in metric(r) for r in free)}/{len(free)} answered")
        elif family == "chain":
            ok = sum(f"facts=5/5 trail={n}/{n}" in metric(r) for r in rows)
            text = f"{ok}/{len(rows)} delivered all 5 facts and all {n} trail entries"
        else:
            text = f"{len(rows)} runs: " + str(dict(collections.Counter(
                (r.get("end") or r.get("reason") or "")[:18] for r in rows)))
        print(f"  {model:18} {family:17} n={n:<4} {arm:6} {text}"
              + (f" | stopped: {dict(collections.Counter(r['trouble'] for r in stopped))}" if stopped else ""))


if __name__ == "__main__":
    roots = sys.argv[1:] or [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")]
    savina(roots)
    probes(roots)
