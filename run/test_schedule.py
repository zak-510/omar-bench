"""The scheduler's accounting, with the sweeps and the machine faked: which
trials run, which count, which are listed as not counted and run again.

Run: python3 run/test_schedule.py"""
import collections
import contextlib
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule as S  # noqa: E402

tmp = tempfile.mkdtemp()
S.ROOT = tmp
os.makedirs(os.path.join(tmp, "infra"))
open(os.path.join(tmp, "infra", "memlog.sh"), "w").write("")
S.manifest = lambda results, tier: None
S.version_drift = lambda: []


class Quiet:  # stands in for the memory log and the Codex guard
    def __init__(self, *a, **k):
        pass

    def terminate(self):
        pass


S.subprocess.Popen = Quiet
real_run = subprocess.run
ran = []          # (script, arm, trial) of every sweep started
sweep = {}        # (arm, trial) -> what that sweep does: {"rc": 1} or {"void": "provider: ..."}
limits = []       # scripted claude_limited() answers; default False
codex = []        # scripted codex_quota.state() answers; default: 10% used, working
machine = {"clear": [], "stale": []}  # scripted (found, left) and stale counts per trial; default clean


def fake_run(argv, *a, **k):
    if argv[0] != "python3":
        return real_run(argv, *a, **k)
    arm = argv[argv.index("--arm") + 1]
    trial = int(argv[argv.index("--trials") + 1])
    ran.append((argv[1], arm, trial))
    does = sweep.get((arm, trial), {})
    if "void" in does:
        with open(k["env"]["VOID_FILE"], "a") as f:
            f.write(json.dumps({"unit": "banking", "arm": arm, "trial": trial, "reason": does["void"]}) + "\n")
    return subprocess.CompletedProcess(argv, does.get("rc", 0))


S.subprocess.run = fake_run
S.claude_limited = lambda: limits.pop(0) if limits else False
S.codex_quota.state = lambda: codex.pop(0) if codex else {"used": 10, "resets_at": 0, "limited": False}
S.machine.clear = lambda: machine["clear"].pop(0) if machine["clear"] else (0, 0)
S.machine.snapshot = lambda: {"mem_available_mib": 6000, "swap_used_mib": 0, "load1": "0.1",
                              "stale_agents": machine["stale"].pop(0) if machine["stale"] else 0}
real_lock = S.machine.lock
S.machine.lock = lambda: open(os.devnull)
S.time.sleep = lambda s: None
S.LimitWatch.run = lambda self: None   # no background polling in the test


def go(tier="haiku55", pilot="4", units="banking,philosophers", fresh=True):
    if fresh:
        shutil.rmtree(os.path.join(tmp, "results"), ignore_errors=True)
    del ran[:]
    sys.argv = ["schedule.py", "--tier", tier, "--units", units] + (["--pilot", pilot] if pilot else [])
    code = None
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            S.main()
    except SystemExit as e:
        code = e.code
    folder = os.path.join(tmp, "results", "pilot" if pilot else "prereg-v8", tier)
    read = lambda name: (list(csv.DictReader(open(os.path.join(folder, name))))
                         if os.path.exists(os.path.join(folder, name)) else [])
    return read("schedule.csv"), read("exclusions.csv"), code


def cells(rows):
    return collections.Counter((r["unit"], r["arm"]) for r in rows)


# A fresh run gives every cell its trials, and a restart adds nothing.
rows, excl, code = go()
assert cells(rows) == {(u, a): 4 for u in ("banking", "philosophers") for a in ("agent", "dsl")} and not excl
assert go(fresh=False)[0] == rows
assert rows[0].keys() >= {"cleared", "stale_agents", "mem_available_mib", "load1"}
print("fresh run: 4 trials per cell; a restart adds none")

# A usage limit seen during a trial: that trial is not counted and is run again.
limits[:] = [False, True]   # clear before the first trial, limited after it
rows, excl, code = go()
first = (rows[0]["unit"], rows[0]["arm"])
assert len(excl) == 1 and excl[0]["reason"].startswith("provider: Claude usage limit")
assert (excl[0]["unit"], excl[0]["arm"], excl[0]["trial"]) == (*first, "1")
assert cells(rows)[first] == 5 and sum(cells(rows).values()) == 17
assert [r["trial"] for r in rows if (r["unit"], r["arm"]) == first] == ["1", "2", "3", "4", "5"]
print("a provider limit during a trial: listed, and the cell runs one more")

# A trial the sweep names as not the arm's doing is treated the same, with its reason.
sweep.update({("agent", 2): {"void": "provider: API Error: 529 overloaded"},
              ("dsl", 3): {"void": "harness: the scorer failed: ValueError('x')"}})
rows, excl, code = go(units="banking")
assert sorted((e["arm"], e["trial"], e["reason"].split(":")[0]) for e in excl) == \
    [("agent", "2", "provider"), ("dsl", "3", "harness")], excl
assert cells(rows) == {("banking", "agent"): 5, ("banking", "dsl"): 5}
sweep.clear()
print("a trial the sweep voids: listed with its reason, and run again")

# Codex refusing work when a trial ends: not counted. Refusing before one: wait.
working = {"used": 10, "resets_at": 0, "limited": False}
refusing = {"used": 100, "resets_at": 0, "limited": True}
codex[:] = [working, refusing]            # before and after the first trial
rows, excl, code = go(tier="sol", units="banking")
assert len(excl) == 1 and "Codex was refusing work" in excl[0]["reason"] and sum(cells(rows).values()) == 9
codex[:] = [dict(refusing, used=50), working]    # refusing at the start, then working again
rows, excl, code = go(tier="sol", units="banking")
assert not excl and sum(cells(rows).values()) == 8
# At the stopping rule's 98% nothing more starts.
codex[:] = [working, working, dict(working, used=98)]
rows, excl, code = go(tier="sol", units="banking")
assert len(rows) == 1
del codex[:]
print("codex: a refusal at a trial's end voids it; one before a trial is waited out; 98% stops the run")

# Agents left over from an earlier trial: cleared, and recorded. If they will
# not stop, the trial is not started (and keeps its number); three in a row stop the run.
machine["clear"][:] = [(6, 0), (6, 2)]     # six found and cleared before trial 1; two survive before trial 2
rows, excl, code = go(units="banking")
assert rows[0]["cleared"] == "6" and rows[0]["rc"] == "0" and rows[1]["rc"] == "unclean" and not excl, rows[:2]
assert cells([r for r in rows if r["rc"] == "0"]) == {("banking", "agent"): 4, ("banking", "dsl"): 4}
assert sorted(int(r["trial"]) for r in rows if r["rc"] == "0" and r["arm"] == rows[1]["arm"]) == [1, 2, 3, 4]
machine["stale"][:] = [3, 3, 3, 3]
rows, excl, code = go(units="banking")
assert "keeps agents alive" in str(code) and [r["rc"] for r in rows] == ["unclean"] * 3 and not ran
del machine["stale"][:]
print("an unclean machine: the trial waits its turn again; three in a row stop the run")

# A sweep that crashes scores nothing: the trial is tried again, and three crashes stop the run.
sweep.update({(arm, 1): {"rc": 1} for arm in ("agent", "dsl")})
rows, excl, code = go(units="banking")
assert "failed 3 times in a row" in str(code) and len(rows) == 3 and not excl
sweep.clear()
print("a crashing sweep: retried, then the run stops")

# Counted runs need a clean tagged commit, and write under that tag.
open(os.path.join(tmp, "REVISION"), "w").write("abc123 dirty ")
assert "clean, tagged commit" in str(go(pilot="")[2])
open(os.path.join(tmp, "REVISION"), "w").write("abc123 tag:prereg-v8 ")
shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "versions.env"), tmp)
rows, excl, code = go(pilot="", units="philosophers-90")
assert code is None and cells(rows) == {("philosophers-90", "agent"): 30, ("philosophers-90", "dsl"): 30}
assert os.path.isdir(os.path.join(tmp, "results", "prereg-v8", "haiku55"))
# A folder whose ledger another version wrote is refused, not appended to.
ledger = os.path.join(tmp, "results", "prereg-v8", "haiku55", "schedule.csv")
open(ledger, "w").write("unit,arm,trial,started_at,secs,rc,used_before,used_after\n")
assert "another version" in str(go(pilot="", units="philosophers-90", fresh=False)[2])
print("counted runs: refused unless clean and tagged; results under results/<tag>/; an older ledger is refused")

# One scheduler per machine.
S.machine.lock = lambda: None
assert "another scheduler" in str(go()[2]) and not ran
print("a second scheduler on the machine: refused")

# What counts as an agent left over from an earlier trial, and what does not.
ps = """   9000 claude          claude --dangerously-skip-permissions --model claude-haiku-5-5
     40 claude          claude --dangerously-skip-permissions --model claude-haiku-5-5
     90 claude          claude -p Reply with OK. --output-format json
   7000 codex           /opt/codex app-server --listen unix:///home/ubuntu/.omar/codex-runtime/263d/app.sock -c x=y
   7000 codex           /opt/codex --model gpt-6.1-sol --remote unix:///home/ubuntu/.omar/codex-runtime/263d/app.sock
   7000 codex           /opt/codex app-server
     12 codex           /opt/codex app-server --listen unix:///home/ubuntu/.omar/codex-runtime/77aa/app.sock
  99999 python3         python3 /home/ubuntu/redeem.py 97 1
"""
real_out, S.machine._out = S.machine._out, lambda cmd, timeout=60: ps
assert S.machine.stale_agents() == 3   # the old Claude agent and the old Codex agent's two processes
S.machine._out = real_out
for text, provider in (("You've hit your 5-hour limit · resets 5:10am (UTC)", True), ("Usage limit reached", True),
                       ("API Error: 529 overloaded_error", True), ("full P1-3: the buffer limit is reached", False),
                       ("the buffer is overloaded with items", False), ("", False)):
    assert bool(S.machine.PROVIDER.search(text)) == provider, text
assert S.machine.trouble(end="no-reply", evidence="⚠ Usage limit reached · limit resets 5:10am") == "provider"
assert S.machine.trouble(end="no-reply", evidence="❯ ") == "runtime"
assert S.machine.trouble(end="deadline", silent_secs=30) == "" and S.machine.trouble(end="done") == ""
print("leftover agents and provider phrases: told apart")

# The real lock: held by one, refused to the next, free again once released.
os.environ["HOME"] = tmp
a = real_lock("test-lock")
assert a and real_lock("test-lock") is None
a.close()
assert real_lock("test-lock")
print("all scheduler tests pass")
