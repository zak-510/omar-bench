"""The agent-arm probe sweep's bookkeeping, with a script standing in for the
runner: what each run is recorded as, what is kept, and what is voided.

Run: python3 probes/test_sweep.py"""
import csv
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

with tempfile.TemporaryDirectory() as tmp:
    stand_in = os.path.join(tmp, "omar-mass")
    with open(stand_in, "w") as f:
        f.write(f"""#!{sys.executable}
import json, os, sys, time
if sys.argv[1] == "teardown":
    sys.exit(0)
arg = lambda name: sys.argv[sys.argv.index(name) + 1]
assert "--deadline-secs" in sys.argv and arg("--agent-dirs") == "/somewhere/empty/agents"
fail = int(arg("--fail-count"))
policy = arg("--gather-question") if "--gather-question" in sys.argv else ""
mail = [{{"from": 0, "to": w, "body": "q", "reply": "To: node 6\\n<answer>144</answer>", "action": "send 6"}} for w in range(fail + 1, 6)]
base = {{"topology": "scatter-gather", "records": [], "messages": mail, "undelivered": 0, "errors": 0}}
if "all 5 workers" in policy and fail == 3:
    # An aggregator that stops answering, with a usage limit on its screen.
    out = dict(base, hops=2, completed=False, end="no-reply", end_node=6, reason="stalled at node 6 after 2 deliveries",
               output=None, silent_secs=900, evidence="You've hit your usage limit. Resets at 9pm.")
elif "at least 3" in policy and fail == 3:
    # An aggregator that stops answering, for no reason its screen shows.
    out = dict(base, hops=2, completed=False, end="no-reply", end_node=6, reason="stalled at node 6 after 2 deliveries",
               output=None, silent_secs=900, evidence="> ")
elif "at least 3" in policy and fail == 2:
    sys.exit("Error: Agents never became ready after 4 attempts: mass12ab-node-6")
elif policy and "all 5" in policy:
    out = dict(base, hops=8, completed=False, end="quiet", reason="went quiet after 8 deliveries", output=None)
else:
    # A finished run, with each agent's folder and Claude Code's record of what it used there:
    # on the tier's model with one worker missing, on another model with three missing.
    folders = [f"/somewhere/empty/agents/mass12ab-node-{{k}}" for k in (5, 6)]
    model = "claude-haiku-5-5" if fail == 1 else "claude-sonnet-5-5"
    use = {{"inputTokens": 3, "outputTokens": 40, "cacheReadInputTokens": 700, "cacheCreationInputTokens": 20, "costUSD": 0.004}}
    json.dump({{"projects": {{f: {{"lastModelUsage": {{model: use}}}} for f in folders}}}}, open(os.environ["HOME"] + "/.claude.json", "w"))
    out = dict(base, hops=6, completed=True, end="done", end_node=6, reason="done by node 6, 6 deliveries", output="144",
               folders=folders)
print(json.dumps(out))
""")
    os.chmod(stand_in, 0o755)
    env = dict(os.environ, RESULTS_DIR=tmp, OMAR_MASS_BIN=stand_in, VOID_FILE=os.path.join(tmp, "void"), HOME=tmp)
    out = subprocess.run([sys.executable, os.path.join(HERE, "sweep.py"), "--only", "sg-missing", "--trials", "1",
                          "--model", "claude-haiku-5-5", "--backend", "claude", "--hard-timeout", "900",
                          "--retry-backoff", "0", "--agent-dirs", "/somewhere/empty/agents"],
                         env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-600:]
    rows = {r["param"]: r for r in csv.DictReader(open(os.path.join(tmp, "sweep-claude-haiku-5-5-agent.csv")))}
    assert len(rows) == 7, list(rows)
    # A run that ended by an agent's own choice is the arm's, whichever way it went.
    answered, waited = rows["policy=none fail=1"], rows["policy=all fail=1"]
    assert (answered["completed"], answered["end"], answered["trouble"], answered["metric"]) == \
        ("True", "done", "", "available=yes"), answered
    assert (waited["completed"], waited["end"], waited["trouble"], waited["metric"]) == \
        ("False", "quiet", "", "available=no"), waited
    # An agent that stopped answering is the machinery's failure: the provider's
    # if its screen says so, the runtime's if not. Neither is "the aggregator waited".
    limited, silent = rows["policy=all fail=3"], rows["policy=quorum3 fail=3"]
    assert (limited["end"], limited["trouble"]) == ("no-reply", "provider") and "usage limit" in limited["evidence"]
    assert (silent["end"], silent["trouble"], silent["silent_secs"]) == ("no-reply", "runtime", "900"), silent
    # Agents that never started: retried three times, each attempt logged, then a runtime failure.
    unborn = rows["policy=quorum3 fail=2"]
    assert (unborn["end"], unborn["trouble"], unborn["rc"]) == ("no-result", "runtime", "1"), unborn
    attempts = list(csv.DictReader(open(os.path.join(tmp, "sweep-claude-haiku-5-5-agent-attempts.csv"))))
    assert len(attempts) == 3 and all("never became ready" in a["stderr"] for a in attempts), attempts
    # What a run's agents used is read back, with the model they ran on.
    assert (answered["models"], answered["metered"], answered["tokens_out"], answered["cost_usd"]) == \
        ("claude-haiku-5-5", "2/2", "80", "0.008"), answered
    # A run whose agents ran on another model is not this model's run.
    other = rows["policy=none fail=3"]
    assert other["trouble"] == "harness" and "ran on claude-sonnet-5-5" in other["evidence"], other
    # Only those two, the provider's failure and the wrong model, are named for the scheduler to run again.
    void = [json.loads(l) for l in open(os.path.join(tmp, "void"))]
    assert sorted(v["reason"].split(":")[0] for v in void) == ["harness", "provider"], void
    assert all(v["trial"] == 1 for v in void)
    # Every delivery and reply of every run is kept.
    logs = [json.loads(l) for l in open(os.path.join(tmp, "sweep-claude-haiku-5-5-agent.jsonl"))]
    assert len(logs) == 7 and len(logs[0]["messages"]) == 4 and logs[0]["messages"][0]["reply"].startswith("To: node 6")
print("probe sweep: all tests pass")
