#!/usr/bin/env python3
"""Scripted agents for the harness tests: each Savina protocol in spec.py
played exactly, by code, with one fault switched on when a test asks for it.

The runner drives them in place of live agents:

  omar-mass simulate --spec savina/specs/<bench>.json --max-messages N \\
      --agents "python3 savina/mock_agents.py <bench> [fault]"

One JSON line arrives per delivery ({"n", "from", "to", "round", "body"}) and
one goes back ({"reply": ...}), so a run is the same every time. A fault is
either a way of writing a reply that must do no harm, or a mistake that must
still fail; test_harness.py says which, and what each must be scored as.
"""
import json
import sys

import spec

FAULTS = {
    # Ways of writing a reply that must do no harm.
    "done-with-address": "the coordinator finishes but still addresses the node it is answering",
    "worker-signs-done": "workers end their sends with 'Decision: DONE'",
    "address-in-tags": "the 'To:' line is written inside the answer tags",
    "no-tags": "messages are written without answer tags",
    "markdown": "control lines are written in bold, with a bare decision word in prose",
    # Mistakes that must still fail.
    "premature-done": "the coordinator finishes the run halfway",
    "unsafe": "the coordinator breaks its invariant once (a fork to two, a fourth item, an overdraft)",
    "wrong-destination": "one worker sends one message to the wrong node",
    "bad-address": "one worker addresses 'the coordinator' instead of a node",
    "no-address": "one worker leaves the 'To:' line off one message",
    "two-messages": "one worker puts two messages in one reply",
    "lost-state": "one worker forgets what it did earlier",
    "never-finishes": "the coordinator never says DONE",
    "silent": "one agent stops answering",
}
COORD = spec.FINISHER


def send(to, body):
    return ("send", to, body)


WAIT = ("wait",)


def done(result):
    return ("done", result)


class Philosophers:
    def __init__(self, count):
        self.count = count
        self.held = {}  # fork -> philosopher, as the arbitrator sees it
        self.eaten = {i: 0 for i in range(1, spec.N + 1)}  # as the arbitrator sees it
        self.mine = {i: 0 for i in range(1, spec.N + 1)}  # each philosopher's own count
        self.answers = {i: [] for i in range(1, spec.N + 1)}

    def hungry(self, i, fault):
        free = all(f not in self.held for f in spec.forks(i))
        if fault == "unsafe" and not free and not getattr(self, "broke", False):
            self.broke = free = True  # hand over a fork its neighbour still holds
        if free:
            self.held.update({f: i for f in spec.forks(i)})
            return send(i, "eat")
        return send(i, "denied")

    def act(self, frm, to, body, fault):
        if to == COORD:
            if body.startswith("done"):
                for f in spec.forks(frm):
                    if self.held.get(f) == frm:
                        del self.held[f]
                self.eaten[frm] += 1
                self.answers[frm].append(body.split(":", 1)[1].strip())
                finished = sum(self.eaten.values())
                if fault == "premature-done" and finished == spec.N * self.count // 2:
                    return done("stopping early")
                if finished == spec.N * self.count:
                    if fault == "never-finishes":
                        return WAIT
                    return done("\n".join(f"philosopher {i}: {', '.join(a)}" for i, a in self.answers.items()))
                return self.hungry(frm, fault) if self.eaten[frm] < self.count else WAIT
            return self.hungry(frm, fault)
        if body == "eat":
            if fault == "lost-state" and to == 2 and self.mine[2] == 2:
                self.mine[2] = 0  # philosopher 2 loses count and starts its meals again
            self.mine[to] += 1
            k = self.mine[to]
            return send(COORD, f"done meal {k}: {sum(spec.meal(to, k))}")
        return send(COORD, "hungry") if self.mine[to] < self.count else WAIT


class Banking:
    def __init__(self):
        self.sched = spec.schedule()
        self.at = 0
        self.outcome = {}
        self.bal = {i: spec.START_BALANCE for i in range(1, spec.N + 1)}  # each account's own
        self.reports = {}

    def next_transfer(self):
        if self.at < len(self.sched):
            k, payer, payee, amount = self.sched[self.at]
            return send(payer, f"transfer T{k}: pay ${amount} to account {payee}")
        return send(1, "report balance")

    def act(self, frm, to, body, fault):
        if to == COORD:
            if body.startswith("start"):
                return self.next_transfer()
            if body.startswith("balance"):
                self.reports[frm] = body.split("$", 1)[1]
                if frm < spec.N:
                    return send(frm + 1, "report balance")
                if fault == "never-finishes":
                    return WAIT
                return done("\n".join(f"T{k}: {v}" for k, v in self.outcome.items())
                            + "\n" + ", ".join(f"account {i}: ${b}" for i, b in self.reports.items()))
            k = self.sched[self.at][0]
            self.outcome[k] = "denied" if body.startswith("denied") else "approved"
            self.at += 1
            if fault == "premature-done" and self.at == len(self.sched) // 2:
                return done("stopping early")
            return self.next_transfer()
        if body.startswith("transfer"):
            k = int(body.split()[1][1:].rstrip(":"))
            amount = int(body.split("$", 1)[1].split()[0])
            payee = int(body.rsplit(" ", 1)[1])
            covered = amount <= self.bal[to]
            if fault == "unsafe" and not covered and not getattr(self, "broke", False):
                self.broke = covered = True  # pay out more than the account holds
            if covered:
                if not (fault == "lost-state" and to == self.sched[0][1] and k == self.sched[0][0]):
                    self.bal[to] -= amount  # with the fault, the first payer forgets its debit
                return send(payee, f"credit T{k}: ${amount} from account {to}")
            return send(COORD, f"denied T{k}")
        if body.startswith("credit"):
            self.bal[to] += int(body.split("$", 1)[1].split()[0])
            return send(COORD, f"credited {body.split()[1].rstrip(':')}")
        return send(COORD, f"balance: ${self.bal[to]}")


class BoundedBuffer:
    def __init__(self):
        self.buf = []
        self.next = {p: 1 for p in range(1, spec.PRODUCERS + 1)}
        self.finished = {}
        self.given = {}  # consumer -> items it has answered, for the lost-state fault

    def give(self, c):
        if self.buf:
            p, k = self.buf.pop(0)
            return send(c, f"take P{p}-{k}: {spec.item_text(p, k).split(': ', 1)[1]}")
        return send(c, "empty")

    def act(self, frm, to, body, fault):
        if to == COORD:
            if body.startswith("put"):
                k = int(body.split("-", 1)[1].split(":")[0])
                room = len(self.buf) < spec.BUFFER
                if fault == "unsafe" and not room and not getattr(self, "broke", False):
                    self.broke = room = True  # store a fourth item
                if room:
                    self.buf.append((frm, k))
                    return send(frm, f"stored P{frm}-{k}")
                return send(frm, f"full P{frm}-{k}")
            if body.startswith("done"):
                name, answer = body.split()[1].rstrip(":"), body.rsplit(" ", 1)[1]
                self.finished[name] = answer
                if fault == "premature-done" and len(self.finished) == spec.ROUNDS // 2:
                    return done("stopping early")
                if len(self.finished) == spec.ROUNDS:
                    if fault == "never-finishes":
                        return self.give(frm)
                    return done("\n".join(f"{n}: {a}" for n, a in sorted(self.finished.items())))
            return self.give(frm)
        if to <= spec.PRODUCERS:
            if body.startswith("stored"):
                self.next[to] += 1
            if self.next[to] <= spec.PER_PRODUCER:
                return send(COORD, f"put {spec.item_text(to, self.next[to])}")
            return WAIT
        if body.startswith("take"):
            p, k = (int(x) for x in body.split()[1].rstrip(":")[1:].split("-"))
            mine = self.given.setdefault(to, [])
            mine.append((p, k))
            if fault == "lost-state" and len(mine) == 3:
                p, k = mine[0]  # answers an item it already finished, not the one it was given
            return send(COORD, f"done P{p}-{k}: {sum(spec.item(p, k))}")
        return send(COORD, "ready")


def render(action, frm, fault, state):
    """Write an action the way the fault says to. `frm` is the agent replying."""
    worker = frm != COORD
    if action[0] == "wait":
        return "Decision: WAIT (not done yet)" if fault == "markdown" else "Decision: WAIT"
    if action[0] == "done":
        if fault == "done-with-address":
            return f"To: node {state['asked_by']}\n<answer>\n{action[1]}\n</answer>\nDecision: DONE"
        if fault == "markdown":
            return f"All work is complete.\n\n<answer>\n{action[1]}\n</answer>\n\n**Decision: DONE**"
        return f"<answer>\n{action[1]}\n</answer>\nDecision: DONE"
    _, to, body = action
    once = worker and not state.get("used") and state["n"] > 12
    if once and fault in ("wrong-destination", "bad-address", "no-address", "two-messages"):
        state["used"] = True
        if fault == "wrong-destination":
            to = to % spec.N + 1 if to != COORD else frm % spec.N + 1
        elif fault == "bad-address":
            return f"To: the coordinator\n<answer>{body}</answer>\nDecision: WAIT"
        elif fault == "no-address":
            return f"<answer>{body}</answer>\nDecision: WAIT"
        else:
            return (f"To: node {to}\n<answer>{body}</answer>\nTo: node {to}\n<answer>and one more thing</answer>"
                    "\nDecision: WAIT")
    if fault == "address-in-tags":
        return f"<answer>\nTo: node {to}\n{body}\n</answer>\nDecision: WAIT"
    if fault == "no-tags":
        return f"To: node {to}\n{body}\nDecision: WAIT"
    if fault == "markdown":
        return f"Replying now.\n\n**To:** node {to}\n\n<answer>\n{body}\n</answer>\n\n**Decision: WAIT**"
    if fault == "worker-signs-done" and worker:
        return f"To: node {to}\n<answer>{body}</answer>\nDecision: DONE"
    return f"To: node {to}\n<answer>{body}</answer>\nDecision: WAIT"


def main():
    bench = sys.argv[1]
    fault = sys.argv[2] if len(sys.argv) > 2 else ""
    assert not fault or fault in FAULTS, fault
    game = (Philosophers(spec.PHILOSOPHER_MEALS[bench]) if bench in spec.PHILOSOPHER_MEALS
            else Banking() if bench == "banking" else BoundedBuffer())
    state = {}
    for line in sys.stdin:
        d = json.loads(line)
        state["n"], state["asked_by"] = d["n"], d["from"]
        if fault == "silent" and d["n"] == 20:
            reply = None
        else:
            reply = render(game.act(d["from"], d["to"], d["body"], fault), d["to"], fault, state)
        sys.stdout.write(json.dumps({"reply": reply}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
