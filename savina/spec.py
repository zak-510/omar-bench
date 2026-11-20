"""Savina Philosophers and Banking, adapted for agents: the one source for sizes,
schedules and prompts. build.py renders both arms from it, so the two arms
cannot drift apart: an agent's job text is shared, and only the coordination
instructions differ (the agent arm carries them in its prompts, the DSL arm in
its wiring and code reactions).

Sizes follow the pre-registration: 5 agents and 30 rounds per benchmark. A
round is one meal (Philosophers) or one transfer (Banking)."""
import random

N = 5
ROUNDS = 30

# ---------------------------------------------------------------- Philosophers
# Savina's arbitrator variant: a philosopher eats only while the arbitrator has
# given it both of its forks. Philosopher i uses fork i (left) and fork i%N+1
# (right), so neighbours share a fork.
MEALS = ROUNDS // N  # 6 meals each


def forks(i):
    return i, i % N + 1


def meal(i, k):
    """Meal k of philosopher i: a sum with a single checkable answer."""
    a = 10 + (i * 7 + k * 13) % 90
    b = 10 + (i * 11 + k * 5) % 90
    return a, b


def meal_text(i, k):
    a, b = meal(i, k)
    return f"Meal {k}: what is {a} + {b}? Answer with the number only."


PHILOSOPHER_JOB = """You are philosopher {i} of {n} at a round table. You use fork {left} on \
your left and fork {right} on your right; philosophers {lnb} and {rnb} share them with you. \
You eat {meals} meals, one at a time, and you may eat only while you hold both of your forks."""

ARBITRATOR_NODE = N + 1


def philosopher_agent_prompt(i):
    left, right = forks(i)
    lnb, rnb = (i - 2) % N + 1, i % N + 1
    meals = "\n".join(meal_text(i, k) for k in range(1, MEALS + 1))
    return (PHILOSOPHER_JOB.format(i=i, n=N, left=left, right=right, lnb=lnb, rnb=rnb, meals=MEALS)
            + f"""

Your meals, in order:
{meals}

Protocol. Forks are handed out by the arbitrator, node {ARBITRATOR_NODE}. To ask for your forks, \
send the message 'hungry' to node {ARBITRATOR_NODE}. If node {ARBITRATOR_NODE} sends you 'eat', you \
hold both forks: do your next uneaten meal, then send 'done meal K: ANSWER' to node \
{ARBITRATOR_NODE} (K is the meal number), which gives both forks back. If node {ARBITRATOR_NODE} \
sends you 'denied', send 'hungry' to node {ARBITRATOR_NODE} again. Once you have sent 'done' for \
meal {MEALS}, you are finished: send nothing more and end every later turn with 'Decision: WAIT'. \
Only the arbitrator finishes the run.

New message from {{sender}}:

{{body}}""")


def arbitrator_agent_prompt():
    table = "\n".join(f"Philosopher {i} uses forks {forks(i)[0]} and {forks(i)[1]}."
                      for i in range(1, N + 1))
    return f"""You are the arbitrator for {N} philosophers (nodes 1 to {N}) at a round table with \
{N} forks. Each philosopher must eat {MEALS} meals.
{table}
All forks start free.

Protocol. When philosopher i sends 'hungry': if both of its forks are free, they are now held by \
philosopher i; send 'eat' to node i. Otherwise send 'denied' to node i. When philosopher i sends \
'done meal K: ANSWER', both of its forks are free again and its meal K is eaten; send nothing \
(Decision: WAIT). Never let a fork be held by two philosophers at once. When all {N} philosophers \
have eaten {MEALS} meals each ({ROUNDS} in all), finish the run: put every philosopher's meal \
answers, in order, between <answer> and </answer>, and end with 'Decision: DONE'.

New message from {{sender}}:

{{body}}"""


def philosophers_seeds():
    return [(i, "start: ask for your forks") for i in range(1, N + 1)]


# --------------------------------------------------------------------- Banking
# Savina's Bank Transaction, run one transfer at a time so that each decision
# has one correct answer: the paying account approves a transfer only if the
# amount does not exceed its balance at that moment.
START_BALANCE = 1000
SCHEDULE_SEED = 20261119  # fixed in the pre-registration; never tuned


def schedule():
    """[(k, payer, payee, amount)] for k = 1..ROUNDS."""
    rng = random.Random(SCHEDULE_SEED)
    out = []
    for k in range(1, ROUNDS + 1):
        payer = rng.randint(1, N)
        payee = rng.choice([j for j in range(1, N + 1) if j != payer])
        amount = 50 * rng.randint(1, 18)  # $50 to $900
        out.append((k, payer, payee, amount))
    return out


def expected_banking():
    """What a correct run does: decisions per transfer and final balances."""
    bal = {i: START_BALANCE for i in range(1, N + 1)}
    decisions = {}
    for k, payer, payee, amount in schedule():
        ok = amount <= bal[payer]
        decisions[k] = ok
        if ok:
            bal[payer] -= amount
            bal[payee] += amount
    return decisions, bal


ACCOUNT_JOB = """You are the budget agent for account {i}. A transfer from your account may go \
ahead only if its amount is not more than your current balance."""

TELLER_NODE = N + 1


def account_agent_prompt(i):
    return (ACCOUNT_JOB.format(i=i) + f""" Account {i} starts with ${START_BALANCE}. Nobody \
else tracks its balance: keep it yourself, across every message you receive.

Protocol. Messages you can receive:
- From the teller, node {TELLER_NODE}: 'transfer Tk: pay $X to account j'. If X is not more than \
your current balance, subtract X from your balance and send 'credit Tk: $X from account {i}' to \
node j. Otherwise leave your balance as it is and send 'denied Tk' to node {TELLER_NODE}.
- From another account: 'credit Tk: $X from account m'. Add X to your balance and send \
'credited Tk' to node {TELLER_NODE}.
- From the teller: 'report balance'. Send 'balance: $B' to node {TELLER_NODE}, where B is your \
current balance.
Only the teller finishes the run.

New message from {{sender}}:

{{body}}""")


def teller_agent_prompt():
    rows = "\n".join(f"T{k}: account {p} pays ${x} to account {q}" for k, p, q, x in schedule())
    return f"""You are the teller for {N} accounts (nodes 1 to {N}). Run these {ROUNDS} transfers \
in order, one at a time:
{rows}

Protocol. For each transfer, send 'transfer Tk: pay $X to account j' to the paying account. The \
transfer is finished when you receive either 'denied Tk' from the paying account or \
'credited Tk' from the receiving account; only then send the next transfer. After the last \
transfer is finished, ask each account for its balance by sending 'report balance', starting with \
node 1, and ask the next account after each 'balance' reply. When you have all {N} balances, \
finish the run: put each transfer's outcome (approved or denied) and the {N} balances between \
<answer> and </answer>, and end with 'Decision: DONE'.

New message from {{sender}}:

{{body}}"""


def banking_seeds():
    return [(TELLER_NODE, "start: run the transfers")]
