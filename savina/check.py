"""Score one Savina trial, with the same definitions in both arms.

Each arm is first reduced to the same ordered events, then one checker scores
them. Agent arm: the runner's delivery log (who sent what to whom, in delivery
order). DSL arm: the trace its code reaction writes to `log`.
"""
import re

import spec

# Agent messages are free text inside <answer> tags; these patterns read the
# protocol's message shapes and nothing looser.
EAT = re.compile(r"^\W*eat\b", re.I)
DONE_MEAL = re.compile(r"done\s+meal\s+(\d+)\s*:?\s*(-?\d+)", re.I)
TRANSFER = re.compile(r"\btransfer\s+T(\d+)\b", re.I)
CREDIT = re.compile(r"\bcredit\s+T(\d+)\s*:?\s*\$?\s*([\d,]+)", re.I)
CREDITED = re.compile(r"\bcredited\s+T(\d+)\b", re.I)
DENIED = re.compile(r"\bdenied\s+T(\d+)\b", re.I)
BALANCE = re.compile(r"\bbalance\s*:?\s*\$?\s*(-?[\d,]+)", re.I)


def _int(s):
    return int(s.replace(",", ""))


# ---------------------------------------------------------------- Philosophers
def philosophers(events, completed):
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
    counts_ok = all([k for k, _ in meals[i]] == list(range(1, spec.MEALS + 1)) for i in meals)
    wrong = sum(1 for i in meals for k, a in meals[i]
                if a is None or k < 1 or k > spec.MEALS or a != sum(spec.meal(i, k)))
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
        body = m.get("body") or ""
        if m["from"] == arb and 1 <= m["to"] <= spec.N:
            if EAT.search(body):
                events.append(("grant", m["to"]))
            elif "denied" not in body.lower():
                deviations += 1
        elif 1 <= m["from"] <= spec.N and m["to"] == arb:
            d = DONE_MEAL.search(body)
            if d:
                events.append(("done", m["from"], int(d.group(1)), int(d.group(2))))
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
        body = m.get("body") or ""
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
