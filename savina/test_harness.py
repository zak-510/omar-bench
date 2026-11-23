"""The runner and the checker together, with no model: scripted agents play
each protocol through the real delivery loop (`omar-mass simulate`), and a
recorded trial is replayed. Every way of writing a reply that should do no
harm must pass, and every real mistake must still fail, under its own cause.

Run: python3 savina/test_harness.py   (after building the runner)"""
import copy
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import check  # noqa: E402
import mock_agents  # noqa: E402
import spec  # noqa: E402

BIN = os.environ.get("OMAR_MASS_BIN", os.path.join(os.path.dirname(HERE), "runner", "target", "release", "omar-mass"))
BENCHES = ("philosophers", "banking", "boundedbuffer")
# Deliveries each protocol takes when it is followed exactly.
EXACT = {"philosophers": 120, "banking": 95, "boundedbuffer": 160, "philosophers-90": 336}
HARMLESS = ("done-with-address", "worker-signs-done", "address-in-tags", "no-tags", "markdown")
# Mistake -> the cause it must be filed under, per benchmark where they differ.
MISTAKES = {
    "premature-done": "noncompletion",
    "never-finishes": "noncompletion",
    "no-address": "noncompletion",  # nothing says where the message was meant to go
    "unsafe": "safety",
    "bad-address": "protocol",
    "two-messages": "protocol",
    "silent": "runtime",
    "wrong-destination": {"philosophers": "protocol", "boundedbuffer": "protocol", "banking": "safety"},
    "lost-state": {"philosophers": "noncompletion", "boundedbuffer": "safety", "banking": "safety"},
}
FIXTURE = os.path.join(HERE, "fixtures", "boundedbuffer-done-with-address.json")


def simulate(bench, *args):
    out = subprocess.run([BIN, "simulate", "--spec", os.path.join(HERE, "specs", f"{bench}.json"),
                          "--max-messages", str(spec.FUSE[bench]), *args], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-400:]
    return json.loads(out.stdout)


def play(bench, fault="", rules="v2"):
    result = simulate(bench, "--rules", rules, "--agents",
                      f"{sys.executable} {os.path.join(HERE, 'mock_agents.py')} {bench} {fault}")
    return result, check.agent(bench, result)


def replay(bench, record, rules):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(record, f)
    try:
        return simulate(bench, "--rules", rules, "--replay", f.name)
    finally:
        os.remove(f.name)


def test_protocols_followed_exactly():
    for bench, hops in EXACT.items():
        result, score = play(bench)
        assert score["success"] and score["cause"] == "success", (bench, score)
        assert result["hops"] == hops and result["end"] == "done" and result["end_node"] == spec.FINISHER, bench
        assert result["errors"] == 0 and score["deviations"] == 0, bench
        assert all(not m.get("notes") and not m.get("errors") for m in result["messages"]), bench


def test_harmless_ways_of_writing_a_reply_pass():
    for bench in BENCHES:
        for fault in HARMLESS:
            result, score = play(bench, fault)
            assert score["success"], (bench, fault, score["reason"], result["reason"])
            # The run itself is unchanged: the same deliveries, nothing lost.
            assert result["hops"] == EXACT[bench] and result["errors"] == 0, (bench, fault)
            assert score["deviations"] == 0, (bench, fault)


def test_mistakes_still_fail_under_their_own_cause():
    for bench in BENCHES:
        for fault, want in MISTAKES.items():
            want = want[bench] if isinstance(want, dict) else want
            result, score = play(bench, fault)
            assert not score["success"], (bench, fault)
            assert score["cause"] == want, (bench, fault, score["cause"], score["reason"])
    # A run that says DONE is not thereby complete: the invariants decide.
    for bench in BENCHES:
        result, score = play(bench, "premature-done")
        assert result["completed"] and result["end"] == "done" and not score["success"], bench
    # A broken invariant is seen even in a run that finishes.
    result, score = play("philosophers", "unsafe")
    assert result["completed"] and score["safety_violations"] >= 1 and score["reason"] == "fork held by two"
    assert play("banking", "unsafe")[1]["overdrafts"] == 1
    assert play("boundedbuffer", "unsafe")[1]["overflows"] >= 1
    # A lost message is on the record, with the reply that lost it.
    result, score = play("philosophers", "two-messages")
    lost = [m for m in result["messages"] if m.get("errors")]
    assert len(lost) == 1 and lost[0]["errors"] == ["more than one message: only the last was sent"], lost
    result, score = play("banking", "bad-address")
    assert result["errors"] == 1 and result["end"] == "quiet" and "1 lost" in result["reason"], result["reason"]
    # A silent agent ends the run, and its unanswered request is the last thing recorded.
    result, score = play("boundedbuffer", "silent")
    assert result["end"] == "no-reply" and result["messages"][-1]["reply"] is None
    assert result["end_node"] == result["messages"][-1]["to"] and score["cause"] == "runtime"


def test_mail_waiting_at_done_is_dropped_and_its_work_is_missed():
    # BoundedBuffer followed exactly ends with one 'ready' still waiting: harmless.
    result, score = play("boundedbuffer")
    assert result["undelivered"] == 1 and score["success"]
    # Ended halfway, the mail still waiting carried work the run then lacks.
    result, score = play("boundedbuffer", "premature-done")
    assert result["undelivered"] >= 1 and score["reason"] == "items missing" and score["items_done"] == 15


def test_the_recorded_trial_that_ran_to_its_cut():
    """claude-haiku-5-5, BoundedBuffer, trial 1 (prereg-v6): every item was
    finished by delivery 160 and the manager said DONE, with a 'To:' line."""
    record = json.load(open(FIXTURE))
    assert len(record["messages"]) == 500
    reply = record["messages"][159]["reply"]
    assert reply.startswith("To: node 4\n<answer>") and reply.endswith("Decision: DONE")
    # Under the rules it ran with, the replay retraces it to the cut.
    v1 = replay("boundedbuffer", record, "v1")
    assert (v1["hops"], v1["end"], v1["completed"]) == (500, "fuse", False), v1["reason"]
    assert v1["replay"] == {"recorded": 500, "unused": 0, "ran_out": False, "reworded": 0, "diverged": None}
    assert [m["reply"] for m in v1["messages"]] == [m["reply"] for m in record["messages"]]
    assert v1["messages"][161]["to"] == 4 and v1["messages"][161]["body"].startswith("P1-1: 56")
    old = check.agent("boundedbuffer", v1)
    assert not old["success"] and old["reason"] == "not finished"
    # Under the rules agents are now told, the same replies end the run at the DONE.
    v2 = replay("boundedbuffer", record, "v2")
    assert (v2["hops"], v2["end"], v2["end_node"], v2["completed"]) == (160, "done", 6, True), v2["reason"]
    assert v2["replay"]["diverged"] is None and v2["replay"]["unused"] == 340
    assert v2["messages"][159]["notes"] == ["To line ignored on DONE"]
    new = check.agent("boundedbuffer", v2)
    assert new["success"] and new["cause"] == "success", new
    assert (new["items_done"], new["overflows"], new["safety_violations"], new["wrong_answers"]) == (30, 0, 0, 0)
    # The correction is the rule's, not the checker's: with one item's 'done'
    # taken out of the record, the same run still fails.
    cut = copy.deepcopy(record)
    gone = next(i for i, m in enumerate(cut["messages"]) if m["body"].startswith("done P2-5"))
    cut["messages"][gone]["body"] = "ready"
    assert not check.agent("boundedbuffer", {**v2, "messages": cut["messages"][:160]})["success"]


def test_a_replay_says_where_a_run_would_have_gone_differently():
    record = json.load(open(FIXTURE))
    # Had consumer 4 addressed its tenth reply to node 5, the run parts ways there.
    changed = copy.deepcopy(record)
    at = [i for i, m in enumerate(changed["messages"]) if m["to"] == 4][9]
    changed["messages"][at]["reply"] = changed["messages"][at]["reply"].replace("To: node 6", "To: node 5")
    out = replay("boundedbuffer", changed, "v2")
    assert out["replay"]["diverged"] and out["replay"]["diverged"]["delivery"] > at + 1, out["replay"]
    assert out["end"] == "no-reply"


def test_duplicate_and_missing_messages_fail():
    result, score = play("philosophers")
    msgs = result["messages"]
    done = next(i for i, m in enumerate(msgs) if m["body"].startswith("done meal"))
    twice = msgs[:done + 1] + [msgs[done]] + msgs[done + 1:]
    assert not check.agent("philosophers", {**result, "messages": twice})["success"]
    assert not check.agent("philosophers", {**result, "messages": msgs[:done] + msgs[done + 1:]})["success"]
    result, score = play("boundedbuffer")
    msgs = result["messages"]
    stored = next(i for i, m in enumerate(msgs) if m["body"].startswith("stored"))
    bad = check.agent("boundedbuffer", {**result, "messages": msgs[:stored + 1] + [msgs[stored]] + msgs[stored + 1:]})
    assert not bad["success"] and bad["safety_violations"] >= 1, bad
    result, score = play("banking")
    msgs = result["messages"]
    credit = next(i for i, m in enumerate(msgs) if m["body"].startswith("credit T"))
    assert not check.agent("banking", {**result, "messages": msgs[:credit] + msgs[credit + 1:]})["success"]


def test_an_unreadable_answer_is_a_wrong_answer_not_a_lost_event():
    result, score = play("philosophers")
    msgs = copy.deepcopy(result["messages"])
    done = next(m for m in msgs if m["body"].startswith("done meal"))
    done["body"] = done["body"].split(":")[0] + ": the answer is forty"
    again = check.agent("philosophers", {**result, "messages": msgs})
    assert again["success"] and again["wrong_answers"] == 1 and again["deviations"] == 0, again
    # The dsl arm has always read it this way.
    log = "grant 1 1;done 1 1 forty;"
    assert check.philosophers_dsl(log)[1] == ("done", 1, 1, None)


def test_messages_are_read_without_their_control_lines():
    # Logs from before the runner stripped them: an address inside the tags.
    msgs = [{"from": 0, "to": 1, "body": "start"}, {"from": 1, "to": 6, "body": "To: node 6\nhungry"},
            {"from": 6, "to": 1, "body": "To: node 1\neat"}, {"from": 1, "to": 6, "body": "To: node 6\ndone meal 1: 56"}]
    events, deviations = check.philosophers_agent(msgs)
    assert events == [("grant", 1), ("done", 1, 1, 56)] and deviations == 0, events
    assert check.philosophers(events, True, 1)["safety_violations"] == 0


def test_causes():
    failed = {"success": False, "completed": False}
    # An agent that never answered: the machinery, unless its terminal shows the provider.
    assert check.cause(failed, arm="agent", end="no-reply") == "runtime"
    assert check.cause(failed, arm="agent", end="no-reply", evidence="You've hit your usage limit") == "provider"
    assert check.cause(failed, arm="agent", end="deadline", silent_secs=7000) == "runtime"
    # The time limit with the agents still exchanging mail is the run's own slowness.
    assert check.cause(failed, arm="agent", end="deadline", silent_secs=40) == "noncompletion"
    assert check.cause(failed, arm="agent", end="deadline", silent_secs=40, lost=1) == "protocol"
    # A run that finished with a broken invariant, and one whose mail went astray.
    assert check.cause({"success": False, "completed": True, "safety_violations": 2}, arm="agent", end="done") == "safety"
    assert check.cause({"success": False, "completed": True, "safety_violations": 2}, arm="agent", end="done",
                       deviations=1) == "protocol"
    assert check.cause({"success": False, "completed": False}, arm="agent", end="fuse") == "noncompletion"
    # No result at all, and a runner that had to be killed.
    none = check.agent("banking", None, rc=1, stderr="Agents never became ready after 4 attempts")
    assert (none["success"], none["cause"], none["reason"]) == (False, "runtime", "no result")
    # Like a dsl run stopped at the hard timeout, it did not finish, and counts.
    killed = check.agent("banking", {"completed": False, "end": "killed", "messages": [], "hops": 0})
    assert killed["cause"] == "runtime" and not killed["success"]
    # The dsl arm: what the runtime says when it stops.
    silent = "Error: topology failed: \"reaction 'p1.reaction.0' invocation '11': 'p1.agent' did not answer within 7200s\""
    for stderr, want in ((silent, "runtime"), ("failed to deliver ... (os error 11)", "runtime"),
                         ("hard timeout", "runtime"), ("API Error: 529 overloaded_error", "provider")):
        got = check.dsl("boundedbuffer", {}, False, rc=1, stderr=stderr)
        assert not got["success"] and got["cause"] == want, (stderr, got["cause"])
    # A dsl run that finishes is scored on its trace like any other.
    decisions, bal = [], {i: spec.START_BALANCE for i in range(1, spec.N + 1)}
    for k, p, q, x in spec.schedule():
        ok = x <= bal[p]
        decisions.append(f"T{k} {'approve' if ok else 'deny'}")
        if ok:
            bal[p], bal[q] = bal[p] - x, bal[q] + x
    log = ";".join(decisions) + ";balances " + ",".join(str(bal[i]) for i in range(1, spec.N + 1))
    assert check.dsl("banking", {"t.log": log}, True)["cause"] == "success"
    denied = next(i for i, d in enumerate(decisions) if d.endswith("deny"))
    wrong = decisions[:denied] + [decisions[denied].replace("deny", "approve")] + decisions[denied + 1:]
    got = check.dsl("banking", {"t.log": ";".join(wrong) + ";balances 0,0,0,0,0"}, True)
    assert (got["success"], got["cause"], got["overdrafts"] >= 1) == (False, "safety", True), got


def test_the_sweep_and_the_report_with_scripted_agents():
    """sweep.py -> runner -> checker -> CSV -> report.py, with no model."""
    root = os.path.dirname(HERE)
    with tempfile.TemporaryDirectory() as tmp:
        def sweep(bench, fault, trials):
            env = dict(os.environ, RESULTS_DIR=tmp, OMAR_MASS_BIN=BIN, MOCK_FAULT=fault, VOID_FILE=os.path.join(tmp, "void"))
            out = subprocess.run([sys.executable, os.path.join(HERE, "sweep.py"), "--bench", bench, "--arm", "agent",
                                  "--tier", "mock", "--trials", str(trials)], env=env, capture_output=True, text=True)
            assert out.returncode == 0, out.stderr[-400:]
            import csv
            return list(csv.DictReader(open(os.path.join(tmp, f"savina-{bench}-mock-agent.csv"))))
        rows = sweep("banking", "", 2)
        assert [(r["success"], r["cause"], r["end"], r["deliveries"], r["attempts"]) for r in rows] == \
            [("True", "success", "done", "95", "1")] * 2, rows
        # A third trial with a fault: finished trials are skipped, the new one is scored under its cause.
        rows = sweep("banking", "unsafe", 3)
        assert len(rows) == 3 and (rows[2]["success"], rows[2]["cause"], rows[2]["reason"]) == \
            ("False", "safety", "overdraft"), rows[2]
        rows = sweep("banking", "bad-address", 4)
        assert (rows[3]["cause"], rows[3]["lost"], rows[3]["end"]) == ("protocol", "1", "quiet"), rows[3]
        rows = sweep("banking", "silent", 5)
        assert (rows[4]["cause"], rows[4]["end"]) == ("runtime", "no-reply"), rows[4]
        # None of these is the provider's or the harness's doing, so none is voided.
        assert not os.path.exists(os.path.join(tmp, "void"))
        # The full delivery log of every trial is kept beside the scores.
        logs = [json.loads(l) for l in open(os.path.join(tmp, "savina-banking-mock-agent.jsonl"))]
        assert len(logs) == 5 and len(logs[0]["messages"]) == 95 and logs[4]["messages"][-1]["reply"] is None
        report = subprocess.run([sys.executable, os.path.join(root, "run", "report.py"), tmp], capture_output=True, text=True)
        assert report.returncode == 0, report.stderr[-400:]
        line = next(l for l in report.stdout.splitlines() if l.startswith("mock") and " agent " in l)
        # plan 60, run 5, none voided, 5 counted: 2 ok, 1 safety, 0 incomplete, 1 protocol, 1 runtime.
        assert line.split()[3:12] == ["60", "5", "0", "5", "2", "1", "0", "1", "1"], line
        assert "2/5 [12%, 77%]" in line and "UNDERPOWERED" in line, line


def test_the_dsl_sweep_with_a_stand_in_runtime():
    """The dsl arm's retries, causes and voiding, with a script standing in
    for OMAR: it prints what the runtime prints for each way a run can go."""
    import csv
    import test_check
    trace = test_check.correct_buffer_trace()
    with tempfile.TemporaryDirectory() as tmp:
        os.makedirs(os.path.join(tmp, "bin"))
        stand_in = os.path.join(tmp, "bin", "omar-dsl")
        with open(stand_in, "w") as f:
            f.write(f"""#!/bin/sh
# What the next run does is the first line of {tmp}/script; each run uses one line up.
[ "$1" = down ] && {{ echo "$3" >> {tmp}/stopped; exit 0; }}
mode=$(head -1 {tmp}/script); sed -i.bak 1d {tmp}/script
echo "Started session calm-otter; omar down -s calm-otter stops it" >&2
# Claude Code's own record of what each of the run's five agents used, when a test names their model.
[ -f {tmp}/ran-on ] && {sys.executable} -c "
import json, os
home = os.environ['HOME']
use = {{'inputTokens': 10, 'outputTokens': 100, 'cacheReadInputTokens': 1000, 'cacheCreationInputTokens': 50, 'costUSD': 0.01}}
json.dump({{'projects': {{f'{{home}}/.omar/sessions/calm-otter/workspace-history/{{i}}/repository.git':
          {{'lastModelUsage': {{open('{tmp}/ran-on').read().strip(): use}}}} for i in range(5)}}}}, open(f'{{home}}/.claude.json', 'w'))"
case "$mode" in
  ok) echo 'Output m.log = "{trace}"'; echo "Run completed"; exit 0 ;;
  undelivered) echo "Error: failed to deliver the task to p1.agent: Resource temporarily unavailable (os error 11)" >&2; exit 1 ;;
  silent) echo "Error: topology failed: 'p1.agent' did not answer within 7200s" >&2; exit 1 ;;
  limit) echo "Error: agent reported: You have hit your usage limit" >&2; exit 1 ;;
esac
""")
        os.chmod(stand_in, 0o755)

        def sweep(trials, *script, tier="stub", model="stub"):
            open(os.path.join(tmp, "script"), "w").write("\n".join(script) + "\n")
            env = dict(os.environ, RESULTS_DIR=tmp, OMAR_LOCAL=tmp, VOID_FILE=os.path.join(tmp, "void"), HOME=tmp)
            out = subprocess.run([sys.executable, os.path.join(HERE, "sweep.py"), "--bench", "boundedbuffer", "--arm",
                                  "dsl", "--tier", tier, "--trials", str(trials)], env=env, capture_output=True, text=True)
            assert out.returncode == 0, out.stderr[-400:]
            return list(csv.DictReader(open(os.path.join(tmp, f"savina-boundedbuffer-{model}-dsl.csv"))))[-1]

        row = sweep(1, "ok")
        assert (row["success"], row["cause"], row["attempts"], row["items_done"]) == ("True", "success", "1", "30"), row
        # The runtime fails to hand an agent its task: retried, and the failed attempt is logged.
        row = sweep(2, "undelivered", "ok")
        assert (row["success"], row["cause"], row["attempts"]) == ("True", "success", "2"), row
        attempts = list(csv.DictReader(open(os.path.join(tmp, "savina-boundedbuffer-stub-dsl-attempts.csv"))))
        assert len(attempts) == 1 and "failed to deliver" in attempts[0]["stderr"]
        # Three failures to deliver are the trial's outcome: a runtime failure, which counts.
        row = sweep(3, "undelivered", "undelivered", "undelivered")
        assert (row["success"], row["cause"], row["attempts"]) == ("False", "runtime", "3"), row
        # An agent that never answers is not retried, and counts.
        row = sweep(4, "silent", "ok")
        assert (row["success"], row["cause"], row["attempts"]) == ("False", "runtime", "1"), row
        assert not os.path.exists(os.path.join(tmp, "void"))
        # A provider limit is not the arm's doing: the trial is named for the scheduler.
        row = sweep(5, "limit")
        assert (row["success"], row["cause"]) == ("False", "provider"), row
        void = [json.loads(l) for l in open(os.path.join(tmp, "void"))]
        assert len(void) == 1 and void[0]["trial"] == 5 and void[0]["reason"].startswith("provider:"), void
        # Every run that failed had its session ended by name; the two that finished ended their own.
        assert open(os.path.join(tmp, "stopped")).read().split() == ["calm-otter"] * 6
        # What the trial's agents used is read back from the agent CLI's own record, with the model they ran on.
        haiku = dict(tier="haiku55", model="claude-haiku-5-5")
        open(os.path.join(tmp, "ran-on"), "w").write("claude-haiku-5-5")
        row = sweep(1, "ok", **haiku)
        assert (row["success"], row["cause"], row["models"], row["metered"]) == \
            ("True", "success", "claude-haiku-5-5", "5/5"), row
        assert (row["tokens_in"], row["tokens_out"], row["tokens_cache_read"], row["tokens_cache_write"],
                row["cost_usd"]) == ("50", "500", "5000", "250", "0.05"), row
        # A trial whose agents ran on another model is not this model's trial, whatever it scored:
        # it is voided and run again. (This is how the dsl arm once ran on a larger model unnoticed.)
        os.remove(os.path.join(tmp, "void"))
        open(os.path.join(tmp, "ran-on"), "w").write("claude-sonnet-5-5")
        row = sweep(2, "ok", **haiku)
        assert (row["success"], row["cause"]) == ("False", "harness"), row
        assert row["reason"] == "agents ran on claude-sonnet-5-5, not claude-haiku-5-5", row
        void = [json.loads(l) for l in open(os.path.join(tmp, "void"))]
        assert len(void) == 1 and void[0]["reason"].startswith("harness: agents ran on claude-sonnet-5-5"), void
        # The report gives tokens and cost for the trials that count.
        report = subprocess.run([sys.executable, os.path.join(os.path.dirname(HERE), "run", "report.py"), tmp],
                                capture_output=True, text=True)
        assert report.returncode == 0, report.stderr[-400:]
        line = next(l for l in report.stdout.split("TOKENS AND COST")[1].splitlines() if l.startswith("claude-haiku-5-5"))
        assert line.split()[3:9] == ["1/1", "500", "50", "250", "5,000", "0.050"], line


def test_the_agent_sweep_when_the_runner_fails_to_start_or_has_to_be_killed():
    """The agent arm's retries and its last resort, with a script standing in
    for the runner on a live tier: it fails to start, runs, or hangs."""
    import csv
    with tempfile.TemporaryDirectory() as tmp:
        stand_in = os.path.join(tmp, "omar-mass")
        with open(stand_in, "w") as f:
            f.write(f"""#!{sys.executable}
import json, os, sys, time
if sys.argv[1] == "teardown":
    sys.exit(0)
lines = open("{tmp}/script").read().split()
open("{tmp}/script", "w").write(" ".join(lines[1:]))
spec = sys.argv[sys.argv.index("--spec") + 1]
if lines[0] == "no-start":
    sys.exit("Error: Agents never became ready after 4 attempts: mass12ab-actor-3")
if lines[0] == "hang":
    journal = "{tmp}/ab12.jsonl"
    with open(journal, "w") as j:
        j.write(json.dumps({{"from": 0, "to": 6, "body": "start: run the transfers", "reply": "To: node 3\\n<answer>transfer T1: pay $700 to account 2</answer>"}}) + "\\n")
        j.write(json.dumps({{"from": 6, "to": 3, "body": "transfer T1: pay $700 to account 2", "reply": None}}) + "\\n")
    print(f"[mass] graph run ab12 ready; results under {tmp}/runs/ab12; journal {{journal}}", file=sys.stderr, flush=True)
    time.sleep(120)
os.execv("{BIN}", ["{BIN}", "simulate", "--spec", spec, "--max-messages", "400", "--agents",
                   "{sys.executable} {os.path.join(HERE, 'mock_agents.py')} banking"])
""")
        os.chmod(stand_in, 0o755)
        # The sweep itself, with its waits shortened: a 1 s trial, stopped 1 s past its limit, killed 1 s later.
        fast = open(os.path.join(HERE, "sweep.py")).read()
        for old, new in (("GRACE = 120\nKILL_AFTER = 300", "GRACE = 1\nKILL_AFTER = 1"), ("time.sleep(30)", "time.sleep(0)"),
                         ("HERE = os.path.dirname(os.path.abspath(__file__))", f"HERE = {HERE!r}")):
            assert old in fast, old
            fast = fast.replace(old, new)
        open(os.path.join(tmp, "sweep.py"), "w").write(fast)

        def sweep(trials, *script):
            open(os.path.join(tmp, "script"), "w").write(" ".join(script))
            env = dict(os.environ, RESULTS_DIR=tmp, OMAR_MASS_BIN=stand_in, VOID_FILE=os.path.join(tmp, "void"))
            out = subprocess.run([sys.executable, os.path.join(tmp, "sweep.py"), "--bench", "banking", "--arm", "agent",
                                  "--tier", "haiku55", "--trials", str(trials), "--timeout", "1"],
                                 env=env, capture_output=True, text=True)
            assert out.returncode == 0, out.stderr[-400:]
            return list(csv.DictReader(open(os.path.join(tmp, "savina-banking-claude-haiku-5-5-agent.csv"))))[-1]

        # Agents that do not start: the run is tried again, and the failed attempt is logged.
        row = sweep(1, "no-start", "ok")
        assert (row["success"], row["cause"], row["attempts"], row["deliveries"]) == ("True", "success", "2", "95"), row
        attempts = list(csv.DictReader(open(os.path.join(tmp, "savina-banking-claude-haiku-5-5-agent-attempts.csv"))))
        assert len(attempts) == 1 and "never became ready" in attempts[0]["stderr"], attempts
        # Three times running: that is the trial's outcome, a runtime failure, which counts.
        row = sweep(2, "no-start", "no-start", "no-start")
        assert (row["success"], row["cause"], row["reason"], row["attempts"]) == ("False", "runtime", "no result", "3"), row
        # A runner that does not stop at the time limit is killed; its journal is its record.
        row = sweep(3, "hang", "ok")
        assert (row["success"], row["cause"], row["end"], row["deliveries"], row["attempts"]) == \
            ("False", "runtime", "killed", "1", "1"), row
        log = [json.loads(l) for l in open(os.path.join(tmp, "savina-banking-claude-haiku-5-5-agent.jsonl"))][-1]
        assert len(log["messages"]) == 2 and log["messages"][1]["reply"] is None, log
        # None of the three is voided: each is an outcome of the arm.
        assert not os.path.exists(os.path.join(tmp, "void"))


def test_a_trial_the_scorer_cannot_read_is_kept_and_voided_not_lost():
    import csv
    with tempfile.TemporaryDirectory() as tmp:
        broken = os.path.join(tmp, "check.py")  # a scorer that fails on every trial
        patched = open(os.path.join(HERE, "sweep.py")).read().replace(
            "import check  # noqa: E402", "import check  # noqa: E402\ncheck.agent = lambda *a: 1 / 0")
        assert "1 / 0" in patched
        open(os.path.join(tmp, "sweep.py"), "w").write(patched.replace(
            "HERE = os.path.dirname(os.path.abspath(__file__))", f"HERE = {HERE!r}"))
        env = dict(os.environ, RESULTS_DIR=tmp, OMAR_MASS_BIN=BIN, VOID_FILE=os.path.join(tmp, "void"))
        out = subprocess.run([sys.executable, os.path.join(tmp, "sweep.py"), "--bench", "banking", "--arm", "agent",
                              "--tier", "mock", "--trials", "1"], env=env, capture_output=True, text=True)
        assert out.returncode == 0, out.stderr[-400:]
        row = list(csv.DictReader(open(os.path.join(tmp, "savina-banking-mock-agent.csv"))))[0]
        assert (row["success"], row["cause"]) == ("False", "harness") and "scorer failed" in row["reason"], row
        # Its whole record is on disk, and the scheduler is told to run it again.
        assert len(json.loads(open(os.path.join(tmp, "savina-banking-mock-agent.jsonl")).readline())["messages"]) == 95
        assert json.loads(open(os.path.join(tmp, "void")).readline())["reason"].startswith("harness: the scorer failed")
        assert not os.path.exists(broken)


def test_every_fault_is_tested():
    assert set(HARMLESS) | set(MISTAKES) == set(mock_agents.FAULTS)


if __name__ == "__main__":
    if not os.path.exists(BIN):
        sys.exit(f"build the runner first: (cd runner && cargo build --release --locked); looked for {BIN}")
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"harness: {len(tests)} tests pass ({len(BENCHES) * (len(HARMLESS) + len(MISTAKES))} scripted runs, "
          "1 recorded trial replayed under both rule sets)")
