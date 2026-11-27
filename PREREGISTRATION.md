# Pre-registration: coordination reliability

Frozen at tag `prereg-v1` on 2026-11-21 and amended by tag since. The current
tag is `prereg-v11`. Every change and its reason is in [CHANGES.md](CHANGES.md).
No counted run starts before its tag. Pilot runs are kept in `results/pilot/`
and never count.

This file was shortened at `prereg-v11`. Where it and the text frozen at
`prereg-v10` differ on a rule that CHANGES.md does not name, the frozen text
holds (`git show prereg-v10:PREREGISTRATION.md`).

## Claim

Coordination owned by a runtime fails less often than the same coordination
written into agents' prompts, with the same model, the same job text and the
same budgets. This is not a comparison with other frameworks.

## Arms

| Arm | Who coordinates | How it runs |
|---|---|---|
| `agent` | The agents, following a protocol in their prompts | `runner/`: OMAR `spawn_agent` plus a file mailbox that delivers addressed messages and records them |
| `dsl` | An OMAR program | `.omar` programs from `savina/build.py` and `probes/dsl/build_programs.sh` |

Both arms run on OMAR 0.4.4. Probe prompts are the summer's apart from their
coordination clauses (`probes/CHANGES.md`).

The treatment, and nothing else by design:

- **Routing and ending.** In the `agent` arm the agents decide whom to message
  and when the run is over. In the `dsl` arm the program does.
- **State.** In the `agent` arm the coordinator and the workers keep it in
  their own memory of the run. In the `dsl` arm the program keeps it and hands
  each agent what it needs with each task. So in the `dsl` arm routing and
  state hold by construction; its agents can still answer wrongly or not at all.

The same in both arms: the model and its CLI, the machine, an empty folder for
each agent to start in, the job text (`savina/spec.py`), the time limit, the
retry rule, the trial count and the scoring.

Other differences, each with its direction:

- The `agent` arm delivers one message at a time and waits for the reply. This
  favours its reliability and costs it time. Time is reported, but the
  like-for-like measure of cost is agent turns.
- OMAR reminds a `spawn_agent` agent of its task after every turn, and the
  agent answers. This costs the `agent` arm time and tokens and is not counted
  as a turn.
- A silent `agent`-arm agent is reminded after 180 s and then every 60 s. The
  `dsl` runtime does not remind. This favours the `agent` arm.
- Agents share a file system, so an `agent`-arm agent could read mail not
  addressed to it. No prompt suggests it, and it could only help that arm.
- Only the `agent` arm has a delivery cut. It can only count against that arm.

## Machines and models

EC2 in us-east-2, Ubuntu 24.04 (`ami-06013a79bb46505e8`), 8 GiB of swap: an
`m7i-flex.2xlarge` (32 GiB), or an `m7i-flex.large` (8 GiB) for cells with at
most 6 agents. Both arms of a cell run on the same instance type. Versions are
pinned in `versions.env`, installed by `infra/bootstrap.sh` and checked by the
scheduler. Each run's manifest records its instance, image, versions and commit.

| Model | CLI | Counted runs |
|---|---|---|
| `gpt-6.1-sol` | Codex CLI | to `prereg-v10` |
| `claude-haiku-5-5` | Claude Code | `prereg-v3` to `prereg-v11` |

Reasoning effort is each CLI's default. The model is set for the machine,
outside the programs (`infra/claude-model.sh`, `infra/codex-model.sh`), and
the `agent` arm also names it when it starts an agent. The scheduler
refuses a tier on a machine set to another model. On Claude, every trial also
reads back the model each of its agents ran on and is voided if it was
another.

## Benchmarks

**Savina concurrency benchmarks, adapted for agents** ([`savina/`](savina/)):
5 worker agents and 30 rounds each. The coordinator is an agent following
Savina's protocol in the `agent` arm and code in the `dsl` arm. Both arms'
job text comes from `savina/spec.py`, and one checker scores both from the
same events.

- **Philosophers** (arbitrator variant): 5 philosophers eat 6 meals each, a
  meal only while holding both forks. Each meal is a sum with one right answer.
  **Philosophers-90** is the same at 18 meals each.
- **Banking** (one transfer at a time): 5 accounts start at $1,000 and a
  teller runs 30 transfers drawn with seed 20261119, of which 6 should be
  refused. The paying account decides each transfer.
- **BoundedBuffer**: 3 producers make 10 items each, 2 consumers answer them,
  and the manager's buffer holds at most 3.

In the `agent` arm every prompt ends with the action rules in
[`runner/README.md`](runner/README.md#action-rules), one message is delivered
at a time, and only the coordinator may finish a run. A run is cut after four
times the fewest deliveries a correct run needs: 400 (500 for BoundedBuffer,
1,100 for Philosophers-90). Agents are never told.

**Summer probes** ([`probes/`](probes/)), with the sizes, tasks and prompts of
the July-August 2026 study: chain (20, 50, 100), scatter-gather with a
checkable answer (5 and 9 workers) and with a blind answer (9, the control),
ring (10, 25, 40) and missing workers (5 workers, 7 settings).

## Outcomes

The same definition in both arms.

| Probe | Success | Failure |
|---|---|---|
| Chain | Final message carries all 5 facts and all n trail entries | Anything else, including an empty body or a run that does not finish |
| Scatter-gather, checkable | Answer is 144 | Answer is the planted 999 (flip) or anything else (empty) |
| Scatter-gather, blind | Reported per corruption level against the majority | Control only: no failure label |
| Ring | Token passed to the horizon, or the run ended by a legal stop | A node goes silent without passing or stopping |
| Missing workers, policy given | Gather follows the stated policy | Answers when the policy says wait, or waits when it says answer |
| Missing workers, no policy | Reported as the share that answer, per arm | No failure label: this cell measures consistency |
| Savina Philosophers | Run finishes, no fork held by two philosophers at once, nobody eats without both forks, everyone eats meals 1 to 6 exactly once | Any of those broken, or no finish (deadlock, silence, the 400-delivery cut) |
| Savina Banking | Run finishes, all 30 transfers decided once and in order, no overdraft, no covered transfer refused, right amounts and payees, final balances right | Any of those broken, or no finish |
| Savina Philosophers-90 | As Philosophers, with 18 meals each (90 in all) | As Philosophers, or no finish (the 1,100-delivery cut) |
| Savina BoundedBuffer | Run finishes, the buffer never holds more than 3 items, no item stored twice or handed out without being stored, every item answered exactly once by the consumer it was given to | Any of those broken, or no finish (deadlock, silence, the 500-delivery cut) |

Wrong answers to meals and items are content errors, not coordination. They
are reported per arm and are not part of success. An agent-arm ring that keeps
passing until its message cap counts as a success, as it does in the `dsl`
arm (the summer write-up counted it as a failure). Legal early stops are
reported separately in both arms.

A run that says it is done is not thereby a success: the events decide. Every
trial is filed under one cause by code (`savina/check.py`), and the causes
are reported apart.

| Cause | What it is | Counts as an outcome |
|---|---|---|
| success | The run finished and every rule above held | Yes |
| safety | A rule was broken: a fork held by two, an overdraft, a covered transfer refused, a wrong ledger, a buffer overflow, an item handed out twice | Yes |
| noncompletion | The work was not finished: deadlock, silence between agents, the delivery cut, or a run ended before its work was done | Yes |
| protocol | `agent` arm: the run failed and a message was lost or unreadable (an address that names no node, two messages in one reply, a message the protocol has no place for) | Yes |
| runtime | The machinery of the arm failed: agents that did not start, an agent that never answered a delivered request, the runtime failing to deliver or exiting with an error | Yes |
| provider | The model provider was limiting use, down or signed out | No: listed, run again |
| harness | The benchmark's own code failed to score the trial | No: listed with its full record, run again |

A failed `agent`-arm trial in which any message was lost or unreadable is
filed under protocol even if a rule was also broken.

## Trials

Per arm and cell, the smallest n with 80% power (one-sided Fisher exact test,
alpha = 0.05) to detect the gap the summer study measured in that cell, never
below 30. New Savina cells assume the smallest summer gap, 11.4%. One trial of
a probe runs every level of its cell.

| Cell | Summer gap (agent vs dsl) | Needed per arm | Trials | Runs per arm |
|---|---|---|---|---|
| Scatter-gather, checkable | 32/280 vs 0/280 = 11.4% | 60 | 5 | 70 (5 per level) |
| Scatter-gather, blind | control | 30 | (same 5) | 45 (5 per level) |
| Chain 20, Chain 50 | none | 30 | 30 each | 30 each |
| Chain 100 | 2/10 vs 0/10 = 20% | 40 | 40 | 40 |
| Ring 10, 25, 40 | 6/30 vs 0/30 = 20% (corrected) | 40 | 14 per size | 42 |
| Ring that cannot stop (`dsl` only) | illustration, not a comparison | 30 | 10 per size | 30 |
| Missing workers, policy given | 2/50 vs 0/50 = 4% | 30 | 15 | 75 (15 per setting); still too few to detect 4%, reported as such |
| Missing workers, no policy | consistency | 30 | (same 15) | 30 (15 per setting) |
| Savina Philosophers | planned at 11.4% | 60 | 60 | 60 |
| Savina Banking | planned at 11.4% | 60 | 60 | 60 |
| Savina BoundedBuffer | planned at 11.4% | 60 | 60 | 60 |
| Savina Philosophers-90 | the rule's floor | 30 | 30 | 30 |

## Running

- **Order.** `run/schedule.py` lists every (arm, cell, trial), shuffles the
  list once with seed 20261119 and runs it one trial at a time, so load and
  model updates land on both arms alike. The order may be split across
  identical machines, each taking every Nth pick, and the machine is recorded
  with every trial.
- **Tags.** A counted run starts only from a clean commit carrying a tag, and
  writes under `results/<tag>/`. Trials under different tags are never pooled.
- **Time limits**, the same in both arms: 900 s for scatter-gather, missing
  workers, chain 20 and ring 10; 1,800 s for chain 50 and ring 25; 2,400 s for
  chain 100; 3,600 s for ring 40; 7,200 s for a Savina trial; 14,400 s for
  Philosophers-90. A trial still running two minutes past its limit is
  stopped. An agent may take as long as its trial to answer.
- **Retries**, the same in both arms: only when an agent never receives its
  task, at most 3, each logged in `*-attempts.csv`. A stall or a timeout is an
  outcome of its arm.
- **Exclusions.** A trial is listed in `exclusions.csv`, is not an outcome of
  either arm and is run again in these cases only, each decided by code and
  none by reading the trial's result:
  - *Provider.* On Claude the scheduler checks for a usage limit before every
    trial and every 5 minutes during it, waits one out, and lists a trial that
    overlapped one. On Codex it asks before and after every trial whether
    Codex is refusing work. In either arm a trial is also listed when a silent
    agent's terminal or the runtime's error shows a usage limit, an overload
    or a sign-in failure (`PROVIDER` in `run/machine.py`).
  - *Harness.* The scorer failed on the trial's record, or an agent ran on
    another model than its cell's.

  An agent that never answers, agents that do not start, a runtime that fails
  and a run stopped from outside are none of these. They count.
- **One trial at a time.** One scheduler per machine. Before every trial it
  ends any agent an earlier trial left running and records what it found.
- **Counted trials.** The first planned n per arm and cell, in start order.
  A trial past that count is reported and not counted.
- **Stopping.** When every trial is done, at 98% of the week's Codex use, or
  at a stated deadline, past which no trial starts. The order is shuffled, so
  a stopped run is a balanced sample. A cell short of its planned n is called
  underpowered.

## Analysis

`run/report.py` reports every cell by cause. Reliability is successes over
counted trials, runtime failures included, with a Wilson 95% interval per arm
and a one-sided Fisher exact test per cell. The same test leaving runtime
failures out is given beside it. No result is described as "100%".

From `prereg-v11` the primary test is named before the run: the 90-round
benchmarks that run by the stop (Philosophers-90, and Banking and
BoundedBuffer at 90 rounds if they are added), together, one-sided and exact,
stratified by benchmark, alpha = 0.05 (`stratified` in `run/report.py`). With
one benchmark it is that benchmark's Fisher test. The second run under
`prereg-v11` has its own, tested the same way: its three 30-round benchmarks
together. A failed trial is a counted trial under
any cause but success. Tests of single benchmarks are secondary. For earlier
tags no test over benchmarks was named in advance; where the report gives
one, it was found afterwards.

Time, and the week's Codex use before and after every trial, come from the
scheduler's ledger. On Claude every trial records what its agents used as
Claude Code recorded it: tokens in and out, cached tokens read and written,
and cost at the CLI's list prices. A record is a trial's only if its session
started during the trial, and means are over the counted trials whose every
agent had a record. Codex keeps a session log for almost none of the agents
the runtime starts, so its tokens are not reported.

Raw logs, CSVs and each run's command are published with the results.

## Runs

| Tag | Model | Cells, both arms | Machines | Stop |
|---|---|---|---|---|
| `prereg-v10` | `claude-haiku-5-5` | Philosophers, Banking, BoundedBuffer, scatter-gather, missing workers | 3 x 8 GiB | 2026-11-25 00:00 UTC |
| `prereg-v10` | `gpt-6.1-sol` | Philosophers, Banking, BoundedBuffer, Philosophers-90 | 4 x 8 GiB | the same, or 98% of the week's Codex use |
| `prereg-v11` | `claude-haiku-5-5` | Philosophers-90 | 1 x 8 GiB | 2026-11-29 00:00 UTC |
| `prereg-v11` | `claude-haiku-5-5` | Philosophers, Banking, BoundedBuffer | 2 x 8 GiB | 2026-11-27 21:00 UTC |

Runs under earlier tags were stopped or replaced, for the reasons in
CHANGES.md. Every tag's trials are kept and are published under
`results/<tag>/` with the write-up.

Planned at earlier tags and not run: chain and ring, the probes on Codex, a
`claude-opus-5-5` phase, and four more Savina benchmarks (CigaretteSmoker,
SleepingBarber, Dictionary, SortedList).
