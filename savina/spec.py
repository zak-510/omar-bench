"""Savina Philosophers, Banking and BoundedBuffer, adapted for agents: the one source for sizes,
schedules and prompts. build.py renders both arms from it, so the two arms
cannot drift apart: an agent's job text is shared, and only the coordination
instructions differ (the agent arm carries them in its prompts, the DSL arm in
its wiring and code reactions).

Sizes follow the pre-registration: 5 agents and 30 rounds per benchmark. A
round is one meal (Philosophers), one transfer (Banking) or one item
(BoundedBuffer)."""
import random

N = 5
ROUNDS = 30
# Agent arm: in every benchmark the coordinator is node N + 1, and it is the
# only agent that may finish the run. The runner enforces this and tells every
# agent (runner/src/prompts.rs, `action_rules`), along with how a reply is read:
# one message per reply, and what 'Decision: WAIT' and 'Decision: DONE' do.
FINISHER = N + 1
# Agent arm: deliveries allowed before the runner cuts a trial, which the agents
# are never told: four times the fewest a correct run needs (seeds included).
FUSE = {"philosophers": 400, "banking": 400, "boundedbuffer": 500, "philosophers-90": 1100}

# ---------------------------------------------------------------- Philosophers
# Savina's arbitrator variant: a philosopher eats only while the arbitrator has
# given it both of its forks. Philosopher i uses fork i (left) and fork i%N+1
# (right), so neighbours share a fork.
MEALS = ROUNDS // N  # 6 meals each
# Philosophers at more than one length: meals per philosopher, by bench name.
PHILOSOPHER_MEALS = {"philosophers": MEALS, "philosophers-90": 18}


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


def philosopher_agent_prompt(i, count=MEALS):
    left, right = forks(i)
    lnb, rnb = (i - 2) % N + 1, i % N + 1
    meals = "\n".join(meal_text(i, k) for k in range(1, count + 1))
    return (PHILOSOPHER_JOB.format(i=i, n=N, left=left, right=right, lnb=lnb, rnb=rnb, meals=count)
            + f"""

Your meals, in order:
{meals}

Protocol. Forks are handed out by the arbitrator, node {ARBITRATOR_NODE}. To ask for your forks, \
send the message 'hungry' to node {ARBITRATOR_NODE}. If node {ARBITRATOR_NODE} sends \
you 'eat', you hold both forks: do your next uneaten meal, then send 'done meal K: ANSWER' to node \
{ARBITRATOR_NODE} (K is the meal number). That message gives both forks back and, if you have meals \
left, also asks for your forks again, so do not send 'hungry' with it. If node {ARBITRATOR_NODE} \
sends you 'denied', send 'hungry' to node {ARBITRATOR_NODE} again. Once you have sent 'done' for \
meal {count}, you are finished: send nothing more and end every later turn with 'Decision: WAIT'. \
Only the arbitrator finishes the run.

New message from {{sender}}:

{{body}}""")


def arbitrator_agent_prompt(count=MEALS):
    table = "\n".join(f"Philosopher {i} uses forks {forks(i)[0]} and {forks(i)[1]}."
                      for i in range(1, N + 1))
    return f"""You are the arbitrator for {N} philosophers (nodes 1 to {N}) at a round table with \
{N} forks. Each philosopher must eat {count} meals.
{table}
All forks start free.

Protocol. When philosopher i sends 'hungry': if both of its forks are free, they are now held by \
philosopher i; send 'eat' to node i. Otherwise send 'denied' to node i. When philosopher \
i sends 'done meal K: ANSWER', both of its forks are free again and its meal K is eaten. If it has \
meals left, that message also asks for its forks again: answer it as you would 'hungry' from \
philosopher i. If it has eaten all {count}, send nothing (Decision: WAIT). Never let a fork be held \
by two philosophers at once. When all {N} philosophers have eaten {count} meals each ({N * count} in \
all), finish the run: put every philosopher's meal answers, in order, between <answer> and \
</answer>, and end with 'Decision: DONE'.

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


# --------------------------------------------------------------- BoundedBuffer
# Savina's Producer-Consumer with Bounded Buffer: producers hand items to a
# manager whose buffer holds at most BUFFER of them, and consumers take them
# out. Three producers outpace two consumers, so the buffer fills and empties.
PRODUCERS, CONSUMERS, BUFFER = 3, 2, 3
PER_PRODUCER = ROUNDS // PRODUCERS  # 10 items each
MANAGER_NODE = N + 1


def item(p, k):
    """Item k of producer p: a sum with a single checkable answer."""
    a = 10 + (p * 13 + k * 7) % 90
    b = 10 + (p * 5 + k * 11) % 90
    return a, b


def item_text(p, k):
    a, b = item(p, k)
    return f"P{p}-{k}: what is {a} + {b}? Answer with the number only."


PRODUCER_JOB = """You are producer {p} of {np}. You make {per} items, in order; each item is a \
question that a consumer will answer."""

CONSUMER_JOB = """You are consumer {c} of {nc}. You work items one at a time: each item is a \
question, and you answer it."""


def producer_agent_prompt(p):
    items = "\n".join(item_text(p, k) for k in range(1, PER_PRODUCER + 1))
    return (PRODUCER_JOB.format(p=p, np=PRODUCERS, per=PER_PRODUCER) + f"""

Your items, in order:
{items}

Protocol. Your items go to the manager, node {MANAGER_NODE}, who keeps them in a buffer that holds \
at most {BUFFER} items. To offer an item, send 'put ITEM: QUESTION' to node {MANAGER_NODE}, with the \
item's name and question exactly as listed above. If node {MANAGER_NODE} \
replies 'stored ITEM', that item is in the buffer: offer your next item. If node {MANAGER_NODE} \
replies 'full ITEM', the buffer had no room: offer the same item again. Once node {MANAGER_NODE} has \
stored item P{p}-{PER_PRODUCER}, you are finished: send nothing more and end every later turn with \
'Decision: WAIT'. Only the manager finishes the run.

New message from {{sender}}:

{{body}}""")


def consumer_agent_prompt(c):
    return (CONSUMER_JOB.format(c=c, nc=CONSUMERS) + f"""

Protocol. Items come from the manager, node {MANAGER_NODE}. To ask for an item, send 'ready' to \
node {MANAGER_NODE}. If node {MANAGER_NODE} sends you 'take ITEM: QUESTION', answer the \
question and send 'done ITEM: ANSWER' to node {MANAGER_NODE}, with the number only as ANSWER. That \
message also asks for your next item, so do not send 'ready' with it. If node {MANAGER_NODE} \
replies 'empty', there was no item for you: send 'ready' to node {MANAGER_NODE} again. Only the \
manager finishes the run.

New message from {{sender}}:

{{body}}""")


def manager_agent_prompt():
    first, last = PRODUCERS + 1, PRODUCERS + CONSUMERS
    return f"""You are the manager of a buffer between {PRODUCERS} producers (nodes 1 to {PRODUCERS}) \
and {CONSUMERS} consumers (nodes {first} and {last}). The buffer holds at most {BUFFER} items and \
starts empty. There are {ROUNDS} items in all, {PER_PRODUCER} from each producer, named P1-1 to \
P{PRODUCERS}-{PER_PRODUCER}.

Protocol. Unless you are finishing the run, reply to the node whose message you are answering.
- 'put ITEM: QUESTION' from a producer: if the buffer holds fewer than {BUFFER} items, add the item \
and its question to the buffer and send 'stored ITEM' to that producer. Otherwise send 'full ITEM' \
to that producer and leave the buffer as it is.
- 'ready' from a consumer: if the buffer holds an item, take the one that has been there longest \
out of the buffer and send 'take ITEM: QUESTION' to that consumer. Otherwise send 'empty' to that \
consumer.
- 'done ITEM: ANSWER' from a consumer: that item is finished. If all {ROUNDS} items are now \
finished, finish the run: put every item's answer, by item name, between <answer> and </answer>, \
and end with 'Decision: DONE'. Otherwise answer it as you would 'ready' from that consumer.
Never hold more than {BUFFER} items, never hand out an item twice, and never lose one.

New message from {{sender}}:

{{body}}"""


def boundedbuffer_seeds():
    return ([(p, "start: offer your first item") for p in range(1, PRODUCERS + 1)]
            + [(PRODUCERS + c, "start: ask for an item") for c in range(1, CONSUMERS + 1)])

