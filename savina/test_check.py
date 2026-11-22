"""The checker passes a correct run and catches each failure it names.
Run: python3 savina/test_check.py"""
from collections import deque

import check
import spec


def correct_philosophers_trace(count=spec.MEALS):
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
            if eaten[i] < count and holder[l] == 0 and holder[r] == 0:
                holder[l] = holder[r] = i + 1
                trace.append(f"grant {i + 1} {eaten[i] + 1}")
                pending.append(i)
        if all(e == count for e in eaten):
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


def rule_following_philosophers(count=spec.MEALS):
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
                if all(e == count for e in eaten.values()):
                    return msgs
                if eaten[i] < count:  # a done with meals left also asks again
                    answer_hungry(i)
            else:
                answer_hungry(i)
        elif body == "eat":
            k = eaten[to] + 1
            queue.append((to, arb, f"done meal {k}: {sum(spec.meal(to, k))}"))
        else:  # start or denied
            queue.append((to, arb, "hungry"))
    return msgs


def test_agent_protocol_fits_the_fuse(bench="philosophers"):
    count = spec.PHILOSOPHER_MEALS[bench]
    msgs = rule_following_philosophers(count)
    events, dev = check.philosophers_agent(msgs)
    ok = check.philosophers(events, True, count)
    assert dev == 0 and ok["success"] and ok["safety_violations"] == 0, (dev, ok)
    assert len(msgs) < spec.FUSE[bench], len(msgs)
    return len(msgs)


def test_longer_philosophers():
    count = spec.PHILOSOPHER_MEALS["philosophers-90"]
    log = correct_philosophers_trace(count)
    ok = check.philosophers(check.philosophers_dsl(log), True, count)
    assert ok["success"] and ok["wrong_answers"] == 0, ok
    # The 30-round scorer must not pass an 18-meal run, nor the reverse.
    assert not check.philosophers(check.philosophers_dsl(log), True)["success"]
    assert not check.philosophers(check.philosophers_dsl(correct_philosophers_trace()), True, count)["success"]


def correct_buffer_trace():
    """What the DSL manager writes when every agent answers at once and right."""
    P, C, B, PER = spec.PRODUCERS, spec.CONSUMERS, spec.BUFFER, spec.PER_PRODUCER
    buf, nxt, asked, hold, trace, finished = deque(), [1] * P, [0] * P, [None] * C, [], 0
    made, answered = [], []
    while True:
        for p in made:
            buf.append((p + 1, asked[p]))
            trace.append(f"put {p + 1} {asked[p]}")
            asked[p] = 0
        for c in answered:
            p, k = hold[c]
            trace.append(f"done {c + 1} {p} {k} {sum(spec.item(p, k))}")
            hold[c] = None
            finished += 1
        if finished == spec.ROUNDS:
            return ";".join(trace) + ";"
        answered = []
        for c in range(C):
            if hold[c] is None and buf:
                hold[c] = buf.popleft()
                trace.append(f"take {c + 1} {hold[c][0]} {hold[c][1]}")
                answered.append(c)
        room = B - len(buf) - sum(1 for k in asked if k)
        made = []
        for p in sorted(range(P), key=lambda p: (nxt[p], p)):
            if room > 0 and not asked[p] and nxt[p] <= PER:
                asked[p], nxt[p], room = nxt[p], nxt[p] + 1, room - 1
                made.append(p)


def rule_following_buffer():
    """The agent-arm protocol played exactly, one delivery at a time in order sent."""
    man, P, msgs = spec.MANAGER_NODE, spec.PRODUCERS, []
    buf, nxt, finished = deque(), {p: 1 for p in range(1, P + 1)}, 0
    queue = deque([(0, a, b) for a, b in spec.boundedbuffer_seeds()])

    def give(c):
        if buf:
            p, k = buf.popleft()
            queue.append((man, c, f"take P{p}-{k}: {spec.item_text(p, k).split(': ', 1)[1]}"))
        else:
            queue.append((man, c, "empty"))

    while queue:
        frm, to, body = queue.popleft()
        msgs.append({"from": frm, "to": to, "body": body})
        if to == man:
            if body.startswith("put"):
                k = int(body.split("-", 1)[1].split(":")[0])
                if len(buf) < spec.BUFFER:
                    buf.append((frm, k))
                    queue.append((man, frm, f"stored P{frm}-{k}"))
                else:
                    queue.append((man, frm, f"full P{frm}-{k}"))
            else:
                if body.startswith("done"):
                    finished += 1
                    if finished == spec.ROUNDS:
                        return msgs
                give(frm)
        elif to <= P:
            if body.startswith("stored"):
                nxt[to] += 1
            if nxt[to] <= spec.PER_PRODUCER:
                queue.append((to, man, f"put {spec.item_text(to, nxt[to])}"))
        elif body.startswith("take"):
            p, k = (int(x) for x in body.split()[1].rstrip(":")[1:].split("-"))
            queue.append((to, man, f"done P{p}-{k}: {sum(spec.item(p, k))}"))
        else:
            queue.append((to, man, "ready"))
    return msgs


def test_boundedbuffer():
    log = correct_buffer_trace()
    ok = check.boundedbuffer(check.boundedbuffer_dsl(log), True)
    assert ok["success"] and ok["overflows"] == 0 and ok["wrong_answers"] == 0, ok
    assert not check.boundedbuffer(check.boundedbuffer_dsl(log), False)["success"]
    # One item more than the buffer holds.
    over = [("put", 1, k) for k in range(1, spec.BUFFER + 2)]
    assert check.boundedbuffer(over, True)["overflows"] == 1
    # An item handed out that the buffer never held, and one finished twice.
    bad = check.boundedbuffer([("take", 1, 2, 9)] + check.boundedbuffer_dsl(log), True)
    assert not bad["success"] and bad["safety_violations"] >= 1, bad
    events = check.boundedbuffer_dsl(log)
    twice = events + [e for e in events if e[0] == "done"][:1]
    assert not check.boundedbuffer(twice, True)["success"]
    # An item never finished.
    short = [e for e in events if not (e[0] == "done" and e[2:4] == (3, 10))]
    assert check.boundedbuffer(short, True)["reason"] == "items missing"
    # Agent arm: the protocol followed exactly fits the fuse and passes.
    msgs = rule_following_buffer()
    events, dev = check.boundedbuffer_agent(msgs)
    ok = check.boundedbuffer(events, True)
    assert dev == 0 and ok["success"], (dev, ok)
    assert len(msgs) < spec.FUSE["boundedbuffer"], len(msgs)
    return len(msgs)


if __name__ == "__main__":
    test_philosophers()
    print(f"agent-arm Philosophers protocol, followed exactly: {test_agent_protocol_fits_the_fuse()} "
          f"deliveries (fuse {spec.FUSE['philosophers']})")
    test_longer_philosophers()
    print(f"agent-arm Philosophers-90 protocol, followed exactly: "
          f"{test_agent_protocol_fits_the_fuse('philosophers-90')} deliveries (fuse {spec.FUSE['philosophers-90']})")
    print(f"agent-arm BoundedBuffer protocol, followed exactly: {test_boundedbuffer()} "
          f"deliveries (fuse {spec.FUSE['boundedbuffer']})")
    test_banking()
    print("check.py: all tests pass")
