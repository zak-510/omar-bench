"""Score one Savina trial, with the same definitions in both arms.

Each arm is first reduced to the same ordered events, then one checker scores
them. Agent arm: the runner's delivery log (who sent what to whom, in delivery
order). DSL arm: the trace its code reaction writes to `log`.

`cause` then files every trial under one heading, so that a failure of
coordination is never mixed with a failure of the machinery around it.
"""
import os
import re
import sys

import spec

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "run"))
import machine  # noqa: E402

# Agent messages are free text inside <answer> tags; these patterns read the
# protocol's message shapes and nothing looser. An event is the message's
# shape alone: a 'done' whose answer cannot be read is still a 'done', with a
# wrong answer, as it is in the dsl arm.
EAT = re.compile(r"^\W*eat\b", re.I)
DONE_MEAL = re.compile(r"done\s+meal\s+(\d+)(?:\s*:?\s*(-?\d+))?", re.I)
TRANSFER = re.compile(r"\btransfer\s+T(\d+)\b", re.I)
CREDIT = re.compile(r"\bcredit\s+T(\d+)(?:\s*:?\s*\$?\s*([\d,]+))?", re.I)
CREDITED = re.compile(r"\bcredited\s+T(\d+)\b", re.I)
DENIED = re.compile(r"\bdenied\s+T(\d+)\b", re.I)
BALANCE = re.compile(r"\bbalance\s*:?\s*\$?\s*(-?[\d,]+)", re.I)
STORED = re.compile(r"^\W*stored\s+P(\d+)\s*-\s*(\d+)", re.I)
TAKE = re.compile(r"^\W*take\s+P(\d+)\s*-\s*(\d+)", re.I)
DONE_ITEM = re.compile(r"\bdone\s+P(\d+)\s*-\s*(\d+)(?:\s*:?\s*(-?\d+))?", re.I)
# Lines the delivery service reads rather than delivers. The runner strips
# them from a message; logs from before it did still carry them.
CONTROL = re.compile(r"^[\s*#>\-_`]*(to|decision)[\s*_`]*:", re.I)


def _int(s):
    return int(s.replace(",", "")) if s else None


def _body(m):
    """A delivered message without control lines, as the recipient is meant to read it."""
    lines = (m.get("body") or "").splitlines()
    return "\n".join(l for l in lines if not CONTROL.match(l)).strip()


# ---------------------------------------------------------------- Philosophers
def philosophers(events, completed, count=spec.MEALS):
    """events: ('grant', i) when philosopher i is given both forks, and
    ('done', i, k, answer) when it hands them back after meal k."""
    owner = {f: None for f in range(1, spec.N + 1)}
    meals = {i: [] for i in range(1, spec.N + 1)}
    violations = 0
    for ev in events:
        i = ev[1]
        mine = spec.forks(i)
        if ev[0] == "grant":
            if any(owner[f] not in (None, i) for f in mine):
                violations += 1  # a fork given to two philosophers at once
            for f in mine:
                owner[f] = i
        else:
            _, i, k, answer = ev
            if any(owner[f] != i for f in mine):
                violations += 1  # ate without holding both forks
            for f in mine:
                if owner[f] == i:
                    owner[f] = None
            meals[i].append((k, answer))
    counts_ok = all([k for k, _ in meals[i]] == list(range(1, count + 1)) for i in meals)
    wrong = sum(1 for i in meals for k, a in meals[i]
                if a is None or k < 1 or k > count or a != sum(spec.meal(i, k)))
    ok = completed and violations == 0 and counts_ok
    reason = ("" if ok else "not finished" if not completed
              else "fork held by two" if violations else "wrong meal count")
    return {"success": ok, "reason": reason, "safety_violations": violations,
            "meal_counts": ",".join(str(len(meals[i])) for i in meals),
            "wrong_answers": wrong}


def philosophers_agent(messages):
    """Delivery log -> events. A grant is the arbitrator's 'eat' delivered to a
    philosopher; a return is its 'done meal K' delivered to the arbitrator."""
    events, deviations = [], 0
    arb = spec.ARBITRATOR_NODE
    for m in messages:
        body = _body(m)
        if m["from"] == arb and 1 <= m["to"] <= spec.N:
            if EAT.search(body):
                events.append(("grant", m["to"]))
            elif "denied" not in body.lower():
                deviations += 1
        elif 1 <= m["from"] <= spec.N and m["to"] == arb:
            d = DONE_MEAL.search(body)
            if d:
                events.append(("done", m["from"], int(d.group(1)), _int(d.group(2))))
            elif "hungry" not in body.lower():
                deviations += 1
        elif m["from"] != 0:
            deviations += 1  # a message the protocol has no place for
    return events, deviations


def philosophers_dsl(log):
    events = []
    for item in (log or "").split(";"):
        parts = item.split()
        if len(parts) >= 3 and parts[0] == "grant":
            events.append(("grant", int(parts[1])))
        elif len(parts) >= 3 and parts[0] == "done":
            ans = re.search(r"-?\d+", " ".join(parts[3:]))
            events.append(("done", int(parts[1]), int(parts[2]), int(ans.group()) if ans else None))
    return events


# --------------------------------------------------------------------- Banking
def banking(decisions, reported, completed):
    """decisions: [(k, 'approve'|'deny', amount_or_None, payee_or_None)] in order.
    reported: final balances {account: amount} as the run reported them."""
    bal = {i: spec.START_BALANCE for i in range(1, spec.N + 1)}
    sched = {k: (p, q, x) for k, p, q, x in spec.schedule()}
    seen = [k for k, *_ in decisions]
    in_order = seen == list(range(1, spec.ROUNDS + 1))
    overdrafts = wrongful = wrong_transfer = 0
    for k, verdict, amount, payee in decisions:
        if k not in sched:
            wrong_transfer += 1
            continue
        p, q, x = sched[k]
        if verdict == "approve":
            if (amount is not None and amount != x) or (payee is not None and payee != q):
                wrong_transfer += 1
            if x > bal[p]:
                overdrafts += 1
            bal[p] -= x
            bal[q] += x
        elif x <= bal[p]:
            wrongful += 1
    balances_ok = reported == bal
    ok = (completed and in_order and overdrafts == 0 and wrongful == 0
          and wrong_transfer == 0 and balances_ok)
    reason = ("" if ok else "not finished" if not completed
              else "transfers missing, repeated or out of order" if not in_order
              else "overdraft" if overdrafts else "refused a covered transfer" if wrongful
              else "wrong amount or payee" if wrong_transfer else "wrong final balances")
    return {"success": ok, "reason": reason, "overdrafts": overdrafts,
            "wrongful_denials": wrongful, "wrong_transfers": wrong_transfer,
            "decided": len(decisions), "balances_ok": balances_ok}


def banking_agent(messages):
    """Delivery log -> decisions and reported balances. A transfer is approved
    by the payer's 'credit Tk' to the payee and denied by its 'denied Tk' to
    the teller."""
    teller = spec.TELLER_NODE
    decisions, reported, deviations = [], {}, 0
    asked = None
    for m in messages:
        body = _body(m)
        f, t = m["from"], m["to"]
        if f == teller and 1 <= t <= spec.N:
            if "report balance" in body.lower():
                asked = t
            elif not TRANSFER.search(body):
                deviations += 1
        elif 1 <= f <= spec.N and 1 <= t <= spec.N:
            c = CREDIT.search(body)
            if c:
                decisions.append((int(c.group(1)), "approve", _int(c.group(2)), t))
            else:
                deviations += 1
        elif 1 <= f <= spec.N and t == teller:
            d, b = DENIED.search(body), BALANCE.search(body)
            if d:
                decisions.append((int(d.group(1)), "deny", None, None))
            elif b and f == asked:
                reported[f] = _int(b.group(1))
            elif not CREDITED.search(body):
                deviations += 1
        elif f != 0:
            deviations += 1
    return decisions, reported, deviations


def banking_dsl(log):
    decisions, reported = [], {}
    for item in (log or "").split(";"):
        item = item.strip()
        m = re.match(r"T(\d+) (approve|deny)$", item)
        if m:
            decisions.append((int(m.group(1)), m.group(2), None, None))
        elif item.startswith("balances "):
            vals = item.split(" ", 1)[1].split(",")
            reported = {i + 1: int(v) for i, v in enumerate(vals)}
    return decisions, reported


# --------------------------------------------------------------- BoundedBuffer
def boundedbuffer(events, completed):
    """events: ('put', p, k) when item Pp-k enters the buffer, ('take', c, p, k)
    when consumer c is given it, and ('done', c, p, k, answer) when c finishes it."""
    every = {(p, k) for p in range(1, spec.PRODUCERS + 1) for k in range(1, spec.PER_PRODUCER + 1)}
    buf, held, stored, done = set(), {}, set(), {}
    overflows = violations = 0
    for ev in events:
        if ev[0] == "put":
            it = ev[1:]
            if it in stored:
                violations += 1  # stored twice
            stored.add(it)
            buf.add(it)
            if len(buf) > spec.BUFFER:
                overflows += 1
        elif ev[0] == "take":
            c, it = ev[1], ev[2:]
            if it not in buf:
                violations += 1  # handed out an item the buffer did not hold
            buf.discard(it)
            held[c] = it
        else:
            _, c, p, k, answer = ev
            it = (p, k)
            if held.get(c) != it or it in done:
                violations += 1  # finished an item it was not given, or twice
            held.pop(c, None)
            done[it] = answer
    all_done = set(done) == every
    wrong = sum(1 for (p, k), a in done.items() if (p, k) not in every or a != sum(spec.item(p, k)))
    ok = completed and overflows == 0 and violations == 0 and all_done
    reason = ("" if ok else "not finished" if not completed else "buffer overflow" if overflows
              else "item repeated or handed out wrongly" if violations else "items missing")
    return {"success": ok, "reason": reason, "overflows": overflows, "safety_violations": violations,
            "items_done": len(done), "wrong_answers": wrong}


def boundedbuffer_agent(messages):
    """Delivery log -> events. An item enters the buffer when the manager's
    'stored ITEM' reaches its producer, is handed out by 'take ITEM' reaching a
    consumer, and is finished by that consumer's 'done ITEM: ANSWER'."""
    man, events, deviations = spec.MANAGER_NODE, [], 0
    prods = range(1, spec.PRODUCERS + 1)
    cons = range(spec.PRODUCERS + 1, spec.PRODUCERS + spec.CONSUMERS + 1)
    for m in messages:
        body = _body(m)
        f, t, low = m["from"], m["to"], body.lower()
        if f == man and t in prods:
            s = STORED.search(body)
            if s:
                events.append(("put", int(s.group(1)), int(s.group(2))))
            elif "full" not in low:
                deviations += 1
        elif f == man and t in cons:
            g = TAKE.search(body)
            if g:
                events.append(("take", t - spec.PRODUCERS, int(g.group(1)), int(g.group(2))))
            elif "empty" not in low:
                deviations += 1
        elif f in cons and t == man:
            d = DONE_ITEM.search(body)
            if d:
                events.append(("done", f - spec.PRODUCERS, int(d.group(1)), int(d.group(2)), _int(d.group(3))))
            elif "ready" not in low:
                deviations += 1
        elif f in prods and t == man:
            if "put" not in low:
                deviations += 1
        elif f != 0:
            deviations += 1
    return events, deviations


def boundedbuffer_dsl(log):
    events = []
    for item in (log or "").split(";"):
        parts = item.split()
        if len(parts) == 3 and parts[0] == "put":
            events.append(("put", int(parts[1]), int(parts[2])))
        elif len(parts) == 4 and parts[0] == "take":
            events.append(("take", int(parts[1]), int(parts[2]), int(parts[3])))
        elif len(parts) >= 4 and parts[0] == "done":
            ans = re.search(r"-?\d+", " ".join(parts[4:]))
            events.append(("done", int(parts[1]), int(parts[2]), int(parts[3]), int(ans.group()) if ans else None))
    return events



# ------------------------------------------------------------ what went wrong
# One heading per trial. Success and the four kinds of failure after it are
# outcomes of the arm and count. The last two are not the arm's doing: such a
# trial is listed with its evidence and run again.
CAUSES = ("success", "safety", "noncompletion", "protocol", "runtime", "harness", "provider")
COUNTED = CAUSES[:5]


def cause(score, *, arm, end="", lost=0, deviations=0, silent_secs=None, evidence="", rc=0, stderr=""):
    """File a scored trial under one of CAUSES.

    success        the run finished and every invariant held
    safety         an invariant was broken (a fork held by two, an overdraft,
                   a buffer overflow, an item handed out twice, a wrong ledger)
    noncompletion  the work was not finished: deadlock, silence between
                   agents, the delivery cut, or a run ended before its work
    protocol       agent arm only: the run failed and a message was lost or
                   unreadable (an address that names no node, two messages in
                   one reply, a message the protocol has no place for)
    runtime        the machinery of the arm failed: agents that did not start,
                   an agent that never answered a delivered request, the
                   runtime failing to deliver or exiting with an error
    provider       the model provider was limited, down or signed out
    harness        the benchmark's own code failed to score the trial (set
                   by the sweep, never here)
    """
    if score.get("success"):
        return "success"
    stopped = machine.trouble(end=end, silent_secs=silent_secs, evidence=evidence, rc=rc, stderr=stderr,
                              stopped=arm == "dsl" and not score.get("completed"))
    if stopped:
        return stopped
    if arm == "agent" and (lost or deviations):
        return "protocol"
    if score.get("safety_violations") or score.get("overflows") or score.get("overdrafts") \
            or score.get("wrongful_denials") or score.get("wrong_transfers") \
            or score.get("reason") == "wrong final balances":
        return "safety"
    return "noncompletion"


# ------------------------------------------------------- one trial, one score
def agent(bench, result, rc=0, stderr=""):
    """Score an agent-arm trial from the runner's result (None when it gave none)."""
    if result is None:
        score = {"success": False, "reason": "no result", "completed": False, "end": "no-result"}
        score["cause"] = cause(score, arm="agent", end="no-result", rc=rc, stderr=stderr)
        return score
    messages = result.get("messages", [])
    completed = bool(result.get("completed"))
    if bench in spec.PHILOSOPHER_MEALS:
        events, dev = philosophers_agent(messages)
        score = philosophers(events, completed, spec.PHILOSOPHER_MEALS[bench])
    elif bench == "boundedbuffer":
        events, dev = boundedbuffer_agent(messages)
        score = boundedbuffer(events, completed)
    else:
        decisions, reported, dev = banking_agent(messages)
        score = banking(decisions, reported, completed)
    end, evidence, lost = result.get("end") or "", result.get("evidence") or "", result.get("errors", 0)
    score.update(deviations=dev, deliveries=result.get("hops"), detail=result.get("reason", ""),
                 completed=completed, end=end, lost=lost, undelivered=result.get("undelivered", ""),
                 silent_secs=result.get("silent_secs", ""), evidence=evidence[-300:].replace("\n", " | "))
    # A runner that had to be killed past its time limit is filed as the dsl
    # arm's runtime is when it has to be: a run that did not finish, counted.
    score["cause"] = cause(score, arm="agent", end="deadline" if end == "killed" else end, lost=lost,
                           deviations=dev, silent_secs=result.get("silent_secs"), evidence=evidence,
                           stderr=stderr)
    return score


def dsl(bench, outputs, completed, rc=0, stderr=""):
    """Score a dsl-arm trial from the outputs its program printed."""
    if bench in spec.PHILOSOPHER_MEALS:
        log = outputs.get("a.log")
        score = philosophers(philosophers_dsl(log), completed and log is not None,
                             spec.PHILOSOPHER_MEALS[bench])
    elif bench == "boundedbuffer":
        log = outputs.get("m.log")
        score = boundedbuffer(boundedbuffer_dsl(log), completed and log is not None)
    else:
        log = outputs.get("t.log")
        decisions, reported = banking_dsl(log)
        score = banking(decisions, reported, completed and log is not None)
    score.update(completed=completed and log is not None, deviations=0)
    score["cause"] = cause(score, arm="dsl", rc=rc, stderr=stderr)
    return score
