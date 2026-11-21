"""The checker passes a correct run and catches each failure it names.
Run: python3 savina/test_check.py"""
import os
import re
from collections import deque

import check
import spec

FUSE = int(re.search(r"^FUSE = (\d+)", open(os.path.join(os.path.dirname(__file__), "sweep.py")).read(),
                     re.M).group(1))


def correct_philosophers_trace():
    """What the DSL arbitrator writes for a run where every answer is right."""
    holder, eaten, trace = [0] * spec.N, [0] * spec.N, []
    pending = []
    while True:
        for i in pending:
            p = i + 1
            for f in (i, (i + 1) % spec.N):
                holder[f] = 0 if holder[f] == p else holder[f]
            eaten[i] += 1
            trace.append(f"done {p} {eaten[i]} {sum(spec.meal(p, eaten[i]))}")
        pending = []
        for i in sorted(range(spec.N), key=lambda i: (eaten[i], i)):
            l, r = i, (i + 1) % spec.N
            if eaten[i] < spec.MEALS and holder[l] == 0 and holder[r] == 0:
                holder[l] = holder[r] = i + 1
                trace.append(f"grant {i + 1} {eaten[i] + 1}")
                pending.append(i)
        if all(e == spec.MEALS for e in eaten):
            return ";".join(trace) + ";"


def test_philosophers():
    log = correct_philosophers_trace()
    ok = check.philosophers(check.philosophers_dsl(log), True)
    assert ok["success"] and ok["safety_violations"] == 0 and ok["wrong_answers"] == 0, ok
    # Grant a neighbour while philosopher 1 still holds fork 2.
    bad = check.philosophers([("grant", 1), ("grant", 2)] + check.philosophers_dsl(log), True)
    assert not bad["success"] and bad["safety_violations"] >= 1, bad
    # A meal returned by a philosopher that was never given its forks.
    bad = check.philosophers([("done", 3, 1, 0)] + check.philosophers_dsl(log), True)
    assert not bad["success"] and bad["safety_violations"] >= 1, bad
    assert not check.philosophers(check.philosophers_dsl(log), False)["success"]
    # Agent arm: the same run as a delivery log.
    msgs = [{"from": 0, "to": i, "body": "start"} for i in range(1, 6)]
    for ev in check.philosophers_dsl(log):
        if ev[0] == "grant":
            msgs += [{"from": ev[1], "to": 6, "body": "hungry"}, {"from": 6, "to": ev[1], "body": "eat"}]
        else:
            msgs.append({"from": ev[1], "to": 6, "body": f"done meal {ev[2]}: {ev[3]}"})
    events, dev = check.philosophers_agent(msgs)
    assert dev == 0 and check.philosophers(events, True)["success"], dev


def correct_banking():
    decisions, bal = [], {i: spec.START_BALANCE for i in range(1, spec.N + 1)}
    for k, p, q, x in spec.schedule():
        if x <= bal[p]:
            decisions.append((k, "approve", x, q))
            bal[p] -= x
            bal[q] += x
        else:
            decisions.append((k, "deny", None, None))
    return decisions, bal


def test_banking():
    decisions, bal = correct_banking()
    assert check.banking(decisions, bal, True)["success"]
    exp_decisions, exp_bal = spec.expected_banking()
    assert exp_bal == bal and sum(v for v in bal.values()) == spec.N * spec.START_BALANCE
    # Approve the first transfer the schedule says to deny: an overdraft.
    k_deny = next(k for k, ok in exp_decisions.items() if not ok)
    flipped = [(k, "approve", None, None) if k == k_deny else d for k, *_ in decisions for d in [(k, *_)]]
    r = check.banking(flipped, bal, True)
    assert not r["success"] and r["overdrafts"] >= 1, r
    # Deny a covered transfer.
    k_ok = next(k for k, ok in exp_decisions.items() if ok)
    flipped = [(k, "deny", None, None) if k == k_ok else d for k, *_ in decisions for d in [(k, *_)]]
    r = check.banking(flipped, bal, True)
    assert not r["success"] and r["wrongful_denials"] >= 1, r
    # A repeated transfer, and wrong final balances.
    assert not check.banking(decisions + decisions[-1:], bal, True)["success"]
    assert not check.banking(decisions, {**bal, 1: bal[1] + 1}, True)["success"]
    # Agent arm: the same run as a delivery log, then the DSL trace.
    sched = {k: (p, q, x) for k, p, q, x in spec.schedule()}
    msgs = [{"from": 0, "to": 6, "body": "start"}]
    for k, verdict, *_ in decisions:
        p, q, x = sched[k]
        msgs.append({"from": 6, "to": p, "body": f"transfer T{k}: pay ${x} to account {q}"})
        if verdict == "approve":
            msgs += [{"from": p, "to": q, "body": f"credit T{k}: ${x} from account {p}"},
                     {"from": q, "to": 6, "body": f"credited T{k}"}]
        else:
            msgs.append({"from": p, "to": 6, "body": f"denied T{k}"})
    for i in range(1, 6):
        msgs += [{"from": 6, "to": i, "body": "report balance"},
                 {"from": i, "to": 6, "body": f"balance: ${bal[i]:,}"}]
    d, rep, dev = check.banking_agent(msgs)
    assert dev == 0 and check.banking(d, rep, True)["success"], (dev, rep)
    log = "".join(f"T{k} {v};" for k, v, *_ in decisions) + "balances " + ",".join(str(bal[i]) for i in range(1, 6))
    d, rep = check.banking_dsl(log)
    assert check.banking(d, rep, True)["success"]


def rule_following_philosophers():
    """The agent-arm protocol in savina/spec.py played by agents that follow it
    exactly, one delivery at a time in the order sent, as the runner delivers."""
    arb, msgs, held, eaten = spec.ARBITRATOR_NODE, [], {}, {i: 0 for i in range(1, spec.N + 1)}
    queue = deque((0, i, "start: ask for your forks") for i in range(1, spec.N + 1))

    def answer_hungry(i):
        if all(f not in held for f in spec.forks(i)):
            held.update({f: i for f in spec.forks(i)})
            queue.append((arb, i, "eat"))
        else:
            queue.append((arb, i, "denied"))

    while queue:
        frm, to, body = queue.popleft()
        msgs.append({"from": frm, "to": to, "body": body})
        if to == arb:
            i = frm
            if body.startswith("done"):
                for f in spec.forks(i):
                    del held[f]
                eaten[i] += 1
                if all(e == spec.MEALS for e in eaten.values()):
                    return msgs
                if eaten[i] < spec.MEALS:  # a done with meals left also asks again
                    answer_hungry(i)
            else:
                answer_hungry(i)
        elif body == "eat":
            k = eaten[to] + 1
            queue.append((to, arb, f"done meal {k}: {sum(spec.meal(to, k))}"))
        else:  # start or denied
            queue.append((to, arb, "hungry"))
    return msgs


def test_agent_protocol_fits_the_fuse():
    msgs = rule_following_philosophers()
    events, dev = check.philosophers_agent(msgs)
    ok = check.philosophers(events, True)
    assert dev == 0 and ok["success"] and ok["safety_violations"] == 0, (dev, ok)
    assert len(msgs) < FUSE, len(msgs)
    return len(msgs)


if __name__ == "__main__":
    test_philosophers()
    print(f"agent-arm Philosophers protocol, followed exactly: {test_agent_protocol_fits_the_fuse()} "
          f"deliveries (fuse {FUSE})")
    test_banking()
    print("check.py: all tests pass")
