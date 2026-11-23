# Pre-registration: coordination reliability, summer reproduction and Savina

**Status: frozen** at tag `prereg-v1`, 2026-11-21, and amended at `prereg-v2`
to `prereg-v9` (see Changes, at the end). No counted run starts before a tag, and
any later change gets a new tag with the reason stated.
Pilot runs (kept in `results/pilot/`) never count; the pilots before this tag were
used only to measure time, memory and usage, and are published with the results.

## Claim under test

Coordination owned by a runtime fails less often than the same coordination
written into agents' prompts, with the same model, the same agent prompts
apart from the coordination instructions, and the same budgets. This is not a
comparison with other frameworks: LangGraph and similar tools also route in
code and would likely do well here. Framework comparisons are planned for a
later post.

## Arms

| Arm | What owns coordination | How it runs |
|---|---|---|
| `agent` | The agents, following the protocol written in their prompts | `runner/`: OMAR `spawn_agent` plus a file mailbox that delivers addressed messages and records them |
| `dsl` | The OMAR program | `.omar` programs from `probes/dsl/build_programs.sh` and `savina/build.py` |

Both arms run on OMAR 0.4.4. Agent prompts are the summer's, identical across
arms except for the coordination clauses (routing instructions and action
rules); the diff is published with the results.

### What differs between the arms

The treatment, and nothing else by design:

- **Who decides what is sent to whom, and when the run is over.** In the
  `agent` arm, agents do, by the messages they address and by one of them
  finishing the run. In the `dsl` arm, the program does.
- **Who keeps the state coordination needs.** In the `agent` arm it lives in
  the agents' memory of the run: the coordinator's (forks held, transfers
  done, the buffer) and each worker's own (its next meal or item, its
  balance). In the `dsl` arm it is the program's, and an agent is handed what
  it needs with each task. So in the `dsl` arm the rules on routing and state
  hold by construction: its agents can still answer wrongly, or not at all.

The same in both arms: the model and its CLI, the machine, an empty folder
of its own for each agent to start in, the job text (one source,
`savina/spec.py`), the time limit, the retry rule, the trial count, and the
scoring (one checker, the same events, the same causes).

Differences that are not the treatment, each stated with its direction:

- The `agent` arm delivers one message at a time and waits for the reply, so
  its agents never meet two messages at once or out of order. This favours
  the `agent` arm's reliability and costs it wall time: time is reported, but
  the like-for-like measure of cost is the agent turns each arm's protocol
  takes (deliveries in the `agent` arm, tasks in the `dsl` arm).
- OMAR 0.4.4 reminds an agent started with `spawn_agent` of its unfinished
  task each time the agent ends a turn, and the agent answers the reminder.
  Every delivery therefore costs the `agent` arm a second, short model turn
  and some context that the `dsl` arm's agents never spend. This counts
  against the `agent` arm's time and cost, and is not counted as a turn.
- A silent `agent`-arm agent is reminded after 180 s and every 60 s; the
  `dsl` runtime does not remind. This favours the `agent` arm.
- Agents share the machine's file system, the runner's mail folder included,
  so an `agent`-arm agent could read mail not addressed to it. No prompt
  suggests it. It could only help the `agent` arm.
- Only the `agent` arm has a delivery cut (four times the fewest deliveries a
  correct run needs). It can only count against the `agent` arm.

## Machine and versions

EC2 in us-east-2, Ubuntu 24.04 (`ami-06013a79bb46505e8`, the image the public
SSM parameter named on 2026-11-20): an
`m7i-flex.2xlarge` (8 vCPU, 32 GiB, 100 GB gp3), and for cells with at most 6
agents an `m7i-flex.large` (2 vCPU, 8 GiB, 40 GB gp3) may stand in (see
Running). Both have 8 GiB of swap. Every version in `versions.env`, installed by
`infra/bootstrap.sh` with checksums. The run manifest records the AMI ID,
instance ID, every tool version and the repo commit.

Why the memory: a Codex agent started by the runtime (`dsl` arm) measured about
250 MB resident on OMAR 0.4.4; one started with `spawn_agent` (`agent` arm)
measured about 0.8 GB once running and close to 1 GB while starting. The DSL ring
of 40 keeps 40 agents alive at once (the DSL chain of 100 runs as two halves of
50), and an 8 GiB machine ran out of memory at about 30. The `agent` arm keeps
at most 6 alive; six starting together peaked at 7.6 GiB in a Banking pilot and
ran an 8 GiB machine without swap out of memory in a Philosophers pilot, which
is why both machines have swap. (Until `prereg-v8` a Savina `agent`-arm run in
fact kept 12 alive, and those two measurements were of 12: see Changes.)

## Models

| Phase | Model | Cells |
|---|---|---|
| 1 | `gpt-6.1-sol` through Codex CLI, default reasoning effort | All cells, both arms |
| 2 | `claude-haiku-5-5` through Claude Code, default effort | All cells, both arms |
| 2 | `claude-opus-5-5`, agent arm only | Checkable scatter-gather, ring, Philosophers; 30 each |

The model is pinned outside the programs: `CODEX_HOME/config.toml` for Codex,
and for Claude the user settings of Claude Code on the machine
(`infra/claude-model.sh`), which every session reads wherever it starts; the
`agent` arm also names the model when it starts an agent. On Claude every
trial reads back the model each of its agents ran on, and a trial with an
agent on any other model is voided and run again (until `prereg-v9` the pin
was a setting in one folder, which the `dsl` arm's agents never read: see
Changes). Effort used is read back from the session logs and reported.

## Benchmarks

1. **Summer probes** (their sizes, tasks and prompts unchanged; from
   `prereg-v8` their `agent`-arm prompts end with the action rules in place of
   the summer's addressing rules, see Changes): chain (n = 20, 50, 100), scatter-gather with a
   checkable answer (n = 5 and 9, every corruption level), scatter-gather with a
   blind answer (n = 9, the control), ring (n = 10, 25, 40, horizon 2 laps),
   missing workers (n = 5, 7 policy and failure settings).
2. **Savina concurrency benchmarks, adapted for agents:** Savina's own
   concurrency category minus LogisticMap (excluded by Lingua Franca's Savina
   port paper as violating actor semantics), leaving 7. **Philosophers** and
   **Banking** were chosen before any results, for the launch case studies;
   **BoundedBuffer** was added at `prereg-v3`, before any of its trials (see
   Changes). CigaretteSmoker, SleepingBarber, Dictionary and SortedList follow
   in a later tag, and all seven are reported.
   - Size: 5 agents, 30 rounds, for every Savina benchmark (DPBench's default).
   - The coordinator role (Savina's arbitrator, teller or buffer manager) is an agent following
     Savina's correct protocol in the `agent` arm and a code reaction in the
     `dsl` arm. That is the treatment.
   - Everything an agent is asked to do comes from `savina/spec.py`, which
     renders both arms (`savina/build.py`), so the job text is identical and
     only the coordination instructions differ. `savina/check.py` scores both
     arms from the same ordered events; `savina/test_check.py` shows it passes
     a correct run and catches each failure below.
   - **Philosophers** (Savina's arbitrator variant): philosopher i uses forks i
     and i+1 and eats 6 meals; each meal is a sum with one right answer, eaten
     only while holding both forks. Agent arm: philosophers ask the arbitrator
     agent for forks and give them back after each meal; the arbitrator agent
     tracks which forks are held. DSL arm: the arbitrator is a code reaction
     that gives forks to every philosopher with meals left whose forks are
     free, least-fed first.
   - **Banking** (Savina's bank transaction, one transfer at a time): 5 accounts
     start at $1,000; 30 transfers from a schedule drawn with seed 20261119
     ($50 to $900; 6 of the 30 should be refused). The paying account's agent
     decides each transfer: go ahead only if the amount is not more than its
     balance. Agent arm: each account agent keeps its own balance and messages
     the payee and teller; the teller agent runs the schedule. DSL arm: the
     teller is a code reaction that keeps the ledger and gives the paying
     account its current balance with each request.
   - **BoundedBuffer** (Savina's producer-consumer with a bounded buffer): 3
     producers make 10 items each, every item a sum with one right answer, and
     2 consumers answer them. The manager's buffer holds at most 3 items, so
     the producers, who outnumber the consumers, fill it. Agent arm: a producer
     offers its next item to the manager agent, which stores it or says the
     buffer is full; a consumer asks for an item, and the manager hands it the
     oldest stored one or says the buffer is empty. DSL arm: the manager is a
     code reaction that stores what producers make, hands the oldest item to
     every idle consumer, and asks producers for more only while the buffer has
     room for what is already on its way.
   - Agent arm: the summer runner's postal mode with each agent's prompt from
     `savina/specs/`, one delivery at a time, cut after 400 deliveries (500 for
     BoundedBuffer): four times the fewest a correct run needs, and agents are
     never told. Every agent answers only whoever wrote to it, so each turn
     sends one message. Followed exactly, the protocols finish well under the
     cut: 120 deliveries for Philosophers and 160 for BoundedBuffer
     (`savina/test_check.py`).
   - Only the coordinator may finish a run. Every agent is told so, and told
     exactly how its reply is read: the action rules in `runner/README.md`,
     which every `agent`-arm prompt ends with (the probes' too, where the last
     node of a chain, any node of a ring and the aggregator may finish).

## Outcomes (same definition in both arms)

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

Wrong answers to meals and items are content errors, not coordination:
reported per arm, not part of success.

A run that says it is done is not thereby a success: the events decide. Every
trial is filed under one cause, by code (`savina/check.py`, `cause`), and the
causes are reported apart and never merged:

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
filed under protocol even if a rule was also broken, so that a safety
violation is never reported from a record the checker could not fully read;
the count of broken rules is still given.

**Correction to the summer analysis.** The summer write-up counted an
agent-arm ring that kept passing until the 2n message cap as a failure, and a
DSL ring that did the same as a success. Here both count as success. Legal
early stops are reported separately in both arms.

## Trials

One rule: per arm and cell, the smallest n with 80% power (one-sided Fisher
exact test, alpha = 0.05) to detect the gap the summer measured in that cell,
never below 30. Thirty also catches, with 95% probability, any failure that
occurs in at least 10% of runs. New Savina cells assume the smallest summer
gap, 11.4%.

A trial of a sweep runs every level of its cell: one scatter-gather trial runs
all 23 corruption levels (5 with 5 workers, 9 with 9, 9 blind), one
missing-workers trial all 7 settings. Trials are rounded up to whole sweeps, so
those cells get at least the n the rule asks for. `run/plan.py` holds the same
table for the scheduler.

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
| Savina Philosophers-90 (`gpt-6.1-sol` only) | the rule's floor | 30 | 30 | 30 |

That is 542 runs in the `agent` arm and 572 in `dsl` per model (`python3
run/schedule.py --dry-run` prints the agent calls this takes).

## Running

- `run/schedule.py` runs everything. Every (arm, cell, trial) is listed and
  shuffled once with seed 20261119, then run one at a time in that order, so
  provider load and model updates during the run land on both arms alike. The
  order may be split across identical machines (same instance type and AMI),
  each taking every Nth pick and still running one at a time; the machine is
  recorded with every trial and reported as a covariate.
- First run (`prereg-v1`, Philosophers from `prereg-v2`): Savina Philosophers
  and Banking on `gpt-6.1-sol`, both arms, on two `m7i-flex.large` machines,
  stopping at 2026-11-25 00:00 UTC. Second run (`prereg-v3`), with the same
  stop and five `m7i-flex.large` machines of the same image: BoundedBuffer on
  `gpt-6.1-sol` on the first two, and on `claude-haiku-5-5` on the other three,
  Philosophers, Banking, BoundedBuffer, scatter-gather and missing workers, both
  arms. Each run is its own shuffled schedule, so either one stopped by the
  deadline is still a balanced sample. The summer cells on `gpt-6.1-sol` follow
  under a later tag, on the
  `m7i-flex.2xlarge`, once OMAR's Codex delivery is fixed: in pilots on 2 vCPUs,
  OMAR 0.4.4 gave up on a Codex app server that took over 3 s to answer (shown
  as `os error 11`), so the `dsl` arm failed to hand five agents their tasks at
  once in 4 of 7 missing-workers settings and in every 9-worker scatter-gather
  run. Counting those would score a runtime bug as a coordination failure;
  dropping them would hide it. Both are reported, with the fixed version's
  rerun.
- Machine: a cell runs only where both its arms fit in memory, and both arms of
  a cell always run on the same instance type. Cells with at most 6 agents
  (Savina, missing workers) may run on an `m7i-flex.large` (8 GiB); the rest
  need the `m7i-flex.2xlarge`. Every run writes a manifest with its instance
  type, AMI, versions and commit.
- Counted runs start only from a clean commit carrying this pre-registration's
  tag; the scheduler refuses anything else. Pilots write to `results/pilot/`.
- Timeouts, the same in both arms: 900 s for scatter-gather, missing workers,
  chain 20 and ring 10; 1,800 s for chain 50 and ring 25; 2,400 s for chain
  100; 3,600 s for ring 40; 7,200 s for a Savina trial (14,400 s for Philosophers-90,
  three times as long). These are the summer's
  longest (the set it used for Opus), so a slower model is not cut short. A
  trial still running two minutes past its limit is stopped, in both arms. The
  `agent` arm's runner stops itself then, keeps its record and ends its
  agents; the `dsl` arm's sweep stops the runtime.
- An agent may take as long as its trial to answer, in both arms: the `dsl`
  arm's runtime waits for a reaction until the run's timeout, and the `agent`
  arm's runner waits as long for a reply (reminding a silent agent after
  180 s, then every 60 s).
- Retries, the same rule in both arms: only when an agent never receives its
  task (its process fails to start, or the runtime fails to deliver the task to
  it), at most 3, every attempt logged in `*-attempts.csv` and counted per arm
  in the results. A stall or timeout is an outcome of its arm.
- Exclusions: a trial is listed in `exclusions.csv` with its reason, is not an
  outcome of either arm, and is run again, in these cases only, each decided
  by code and none by reading the trial's result:
  - Provider. On Claude, the scheduler checks for a usage limit before every
    trial and every 5 minutes during it (`claude-limit.csv`), waits one out
    before starting, and lists a trial that overlapped one. On Codex, it asks
    Codex before and after every trial whether it is refusing work, and does
    the same. In either arm, a trial is also listed when the terminal of an
    agent that stopped answering, or the runtime's error, shows a usage limit,
    an overload or a sign-in failure (the phrases are `PROVIDER` in
    `run/machine.py`).
  - Harness. The scorer itself failed on the trial's record (the record is
    kept, so the trial can still be scored once the scorer is fixed), or an
    agent of the trial ran on another model than its cell's.
  An agent that never answers, agents that do not start, a runtime that fails
  and a run that had to be stopped from outside are none of these: they are
  runtime failures of their arm, and count.
- The machine: one scheduler per machine, and one trial at a time. Before
  every trial the scheduler ends any agent an earlier trial left running and
  records what it found, with the machine's free memory and load, in the
  ledger. A trial due on a machine whose leftover agents will not stop is not
  started.
- Results of each tag are written under `results/<tag>/`, so trials run under
  one frozen version never share a folder with another's, and are never
  pooled with them in the analysis.
- Counted trials: the first 60 per arm and cell, in start order. A trial past
  that count is reported and not counted.
- Stopping: the run stops when every trial is done, when weekly Codex use
  reaches 98% (so no trial runs into the limit), or at a stated deadline.
  Because the order is shuffled and interleaved, a stopped run is a balanced
  sample: the results give the runs achieved against the runs planned for
  every cell, and call a cell short of its planned n underpowered.

## Analysis

Every cell is reported, by cause (`run/report.py`): reliability is successes
over counted trials, runtime failures included, with a Wilson 95% interval per
arm and a one-sided Fisher exact test where a gap is expected; the same test
leaving runtime failures out is given beside it, so that neither arm's
failures of machinery are hidden or mistaken for failures of coordination. No
result is described as "100%". Wall time and weekly Codex use before and after every trial come from the
scheduler's ledger. On Claude, every trial records what its agents used as
Claude Code itself recorded it for each of them: tokens in and out, cached
tokens read and written, and the cost of those at list price. Both arms are
metered the same way, and means are over the counted trials whose every
agent had a record, with that coverage stated. On Codex, tokens are reported
for the agents whose CLI kept a session log (it does not keep one for every
agent the runtime starts), with that coverage stated. Raw logs, CSVs and the commands that
produced them are published with the results.

## Changes

**`prereg-v2`, 2026-11-21: the Philosophers protocol in the `agent` arm.**
Found while reading the transcripts of the first counted Philosophers trials:
all 6 finished `agent`-arm trials had hit the 400-delivery cut, and all 6 `dsl`-arm
trials had succeeded. The philosopher prompt left out a step of Savina's
protocol: after handing its forks back, a philosopher asks for them again. The
agents made that step up. Some put 'done' and 'hungry' in one message, which
worked; others wrote two messages in one reply, and the runner, which delivers
one message per reply and said so nowhere, delivered only the second. The
arbitrator then never learned those forks were free, and it correctly denied
their neighbours until the cut. That is a fault in our harness, not a
coordination failure of the agents.

In v2 both prompts say that each turn sends one message, and that a
philosopher's 'done meal K' also asks for its forks again when it has meals
left; the arbitrator answers it as it would 'hungry'. Followed exactly, this
protocol finishes in 120 deliveries, under the 400 cut
(`test_agent_protocol_fits_the_fuse` in `savina/test_check.py`). Every change
is to the `agent` arm's prompts and can only help it: the `dsl` programs and
everything in Banking are byte-for-byte unchanged.

Philosophers is rerun in both arms under v2, on the same machines and schedule.
The v1 Philosophers trials (6 `agent`, 6 `dsl`, and one `agent` trial stopped
mid-run for this change) are published separately as v1 and not counted.
Banking was not affected and its v1 trials count.

**`prereg-v3`, 2026-11-22: BoundedBuffer, the second model, and two fixes.**
BoundedBuffer is the third Savina benchmark because it is the
producer-consumer pattern behind agent work queues, its rules can be checked
from the events alone, and it fits 6 agents. It was chosen after the interim
Philosophers (v1) and Banking results had been seen, for those reasons only,
and before any BoundedBuffer trial ran. Its fuse is 500 deliveries by the same
rule as the others (four times the fewest, 125).

Phase 2 starts on the cells that fit an `m7i-flex.large`: `claude-haiku-5-5`
runs Philosophers, Banking, BoundedBuffer, scatter-gather and missing workers
in both arms, with Claude Code signed in by a token from `claude setup-token`
and the model pinned in the workdir's settings (`infra/bootstrap.sh`). OMAR's
Codex delivery bug does not reach Claude agents: the pilots on 0.4.4 showed no
failed delivery on Claude, 9-worker scatter-gather included. Nothing in
Philosophers or Banking changes: their specs and programs are byte-for-byte
the same as at `prereg-v2`.

Two fixes, both found in the first run. The scheduler, restarted for
`prereg-v2`, ran every pick of its share again on top of the Banking trials
already done, so Banking ran 64 `agent` and 63 `dsl` trials before it was
stopped; by the rule above, the first 60 per arm count, and the extras are
reported. The scheduler now caps each unit at its share of the machine's
picks. And on Claude, a usage limit stopped agents from answering in a pilot
(eight scatter-gather runs timed out while it lasted), so the scheduler now
detects one and the trial it touched is excluded and run again, as the
exclusion rule says.

The repository layout also changed at this tag, with no code changed: the
original study's runner is now `runner/`, its probes `probes/` (results in
`probes/original-results/`), and the OMAR tools each machine installs `local/`.

The repository's history was also rebuilt at this tag, before it was first
published, to drop unrelated earlier work; every commit kept its files. Counted
runs before this tag recorded the original commit IDs in their manifests:

| Tag | Commit recorded by its runs | File tree, the same in both |
|---|---|---|
| `prereg-v1` | `3789a5cdf9e795195ecd03b30a83e0ea80b8b533` | `33342cf6879f5a041b090b590f16b2d8db56017e` |
| `prereg-v2` | `bc90f91fb52d2319f304aca68d4ad98d326b5a69` | `03c21c44e0255487eae4fb0ebfb200628a885743` |

`git rev-parse prereg-v1^{tree}` prints a tag's file tree.

**`prereg-v4`, 2026-11-22: the Codex guard.** The run now stops at 98% of
weekly Codex use instead of 85%. The 85% kept room for other use of the
account, which the study does not need. A resource rule: no cell, trial count
or outcome changes.

**`prereg-v5`, 2026-11-22: the Savina charter on Claude.** Found while reading
the transcripts of the first counted Claude Philosophers trials: all three
`agent`-arm failures were the arbitrator refusing to act, each time citing "no
record of rounds 1-5". The runner numbers requests across the whole run, and
its charter for Savina agents says the earlier requests are their memory, so
Claude read the rounds that went to the philosophers as memory it had lost.
Codex never did this: no reply in any of its 142 `agent`-arm transcripts cites
missing rounds. The charter now says requests are numbered across the whole
run and a gap is not missing memory. A change to the `agent` arm only, which
can only help it. Claude's Philosophers, Banking and BoundedBuffer rerun in both
arms under this tag; their trials so far are published as v3 and not counted.
The Codex runs and Claude's summer cells, whose charter is the original
study's, are unaffected and continue.

**`prereg-v6`, 2026-11-22: request numbers in the `agent` arm.** The v5
sentence was not enough: in 2 of 22 Claude Philosophers `agent`-arm trials the
arbitrator still refused every request ("This is round 7. The only requests I
have received are rounds 5, 6, 7"), and 2 more stalled after replies citing
missing rounds. The cause is the number itself: the runner gave each request
the run-wide delivery count. A Savina agent's requests are now numbered from 1
in the order they reach it, so there is no gap to read, and the v5 sentence is
replaced by one saying so. Again an `agent`-arm change that can only help it.
Claude's Philosophers, Banking and BoundedBuffer `agent` arms rerun under this
tag; their v5 trials are published and not counted. Claude's `dsl`-arm trials
do not go through the runner and keep counting, as do Claude's summer cells and
every Codex run (no Codex reply in 142 transcripts cites missing rounds).

**`prereg-v7`, 2026-11-22: Philosophers at three times the length.** A first
point on a scaling curve, on `gpt-6.1-sol`: Philosophers with the same 5
philosophers, prompts and protocol, but 18 meals each (90 in all) instead of 6.
It asks whether a gap opens as the coordinator has to keep track of more,
which the 30-round cells cannot show. Prediction, written before any of its
trials: the `agent` arm's failure rate is higher at 90 meals than at 30, and
the `dsl` arm stays at or near 0%. Whatever it shows is reported. 30 trials per
arm (the rule's floor), on two new `m7i-flex.large` machines of the same image,
in its own results folder (`results/sol/philosophers-90/`), stopping at
2026-11-25 00:00 UTC. The fuse follows the same rule (four times the fewest,
4 x 275 = 1,100); followed exactly, the protocol finishes in 336 deliveries
(`savina/test_check.py`). The 30-round Philosophers specs and programs are
byte-for-byte unchanged. The runs already going keep the code they started
with, since adding a cell to the plan reorders the shuffled schedule of any
run started after it.


**`prereg-v8`, 2026-11-23: the harness, audited.** A counted Claude
BoundedBuffer trial was scored a failure at the 500-delivery cut although
every item had been finished, with no rule broken, by delivery 160. The manager
had said `Decision: DONE` in a reply that also had a `To:` line. Its prompt
told it to "always reply to the node whose message you are answering" and to
finish with `Decision: DONE`; nothing said which won, and the runner read the
reply as a message and delivered the final report to a consumer. That is a
fault in our harness, not a coordination failure. We then audited everything
between a prompt and a score, and this tag fixes what the audit confirmed.

1. *How a reply is read.* The summer's addressing rules did not say what a
   reply with both a `To:` line and `Decision: DONE` does, who may finish a
   run, or what becomes of mail still waiting; the runner sent such a reply on
   as a message, let any agent's `DONE` end the run, and took any decision
   line containing "done" for one. Every `agent`-arm prompt now ends with
   action rules that say all of it (`runner/README.md`), the runner reads
   replies by exactly those rules, and a test holds the two together. The
   Savina prompts lose the one-message sentence the rules now carry, and the
   manager's "always reply" becomes "unless you are finishing the run". No
   `dsl` program changes: all are byte-for-byte the same.
2. *Twelve agents, not six.* For every Savina `agent`-arm run the runner
   started a second, idle agent beside each real one. They took no part in
   any run, but they doubled the arm's memory and added about two minutes of
   start-up to each trial. Fixed.
3. *Runs stopped from outside.* A run still going at its time limit was killed
   by the sweep: its delivery log was lost, so its cause could not be told,
   and its agents were left running, on that machine, under every later trial
   of either arm. The runner now stops itself at the limit, keeps its record
   and ends its agents; it journals every delivery as it happens; and the
   scheduler clears and records leftovers before every trial.
4. *Where agents start.* `agent`-arm agents started in a checkout of this
   repository, `dsl`-arm agents in an empty folder. Both now start in the
   same empty folder.
5. *Causes.* A trial was a success or a failure, so a machine out of memory,
   an agent that never answered and a broken rule were all "not finished".
   Every trial is now filed under one of seven causes (Outcomes), in both
   arms, by code.
6. *The checker* now reads a message without its control lines, and counts a
   'done' whose answer it cannot read as a 'done' with a wrong answer, as the
   `dsl` arm's trace always has. Neither changes the score of any trial so far.
7. *The scheduler* now also asks Codex whether it is refusing work, runs one
   at a time per machine by a lock, and writes each tag's results in a folder
   of its own.

*What this did to the trials already run* (`run/audit.py`, over all 703
Savina trials pulled at 2026-11-23 05:21 UTC; its table is published with the
results). Each `agent`-arm trial's own delivery log was played back through
the runner under the rules it ran with and under these. Seven killed trials
had lost their logs; theirs were rebuilt from the mail the runner and the
agents had left on the machines. Under its own rules every one of the 330
logs retraced its trial exactly, which is what makes the replay evidence.
Under these rules 328 are read identically from first reply to last; none
takes a different course; and two end early, at a `DONE` the old rules read
as a send: the trial above, and a Claude BoundedBuffer trial of `prereg-v5`
that was already listed as not counted for a usage limit. Both had finished
all 30 items with no rule broken. Of the seven killed trials, six were
already listed as not counted for a usage limit; the seventh, a Codex
BoundedBuffer trial, is an agent that never answered its second request. The
checker's changes alter no score.

The two machine faults cannot be undone from a record. All 330 `agent`-arm
trials ran with the six idle agents. 215 trials, of either arm, started on a
machine still running agents from an earlier killed run (53 on one Codex
machine; 49, 64 and 49 on the three Claude machines, the first of which ran
out of memory and failed `dsl`-arm trials in under 90 seconds). Neither
fault touches what agents wrote to each other; both bear on how long trials
took and on which failed for want of an answer.

*What becomes of them.* Trials run before this tag are published as scored,
under their tags, with the audit's row beside each, and are never pooled with
trials run from this tag on. The trial above is reported both as recorded and
as replayed. Because of the two machine faults, the durations of `agent`-arm
trials before this tag, and runtime failures of either arm before it, are
reported with that caveat and are not compared with later ones.

*Validated* at commit `e8be16d`, which this tag differs from in this file
only; some of the live trials below ran at the two commits before it
(`3ff9e3d`, `61a7424`), which differ from it in the results table, in how
Codex's usage is read and in which processes count as leftover agents. With
no model, on a benchmark machine: the runner's 118 tests; the
scorer, harness, probe-sweep and scheduler tests (scripted agents playing
each protocol exactly, five harmless ways of writing a reply that must pass
and nine mistakes that must each fail under its own cause, the trial above
replayed under both rule sets, and what is retried, counted and run again);
and the `dsl` arm of all four programs on OMAR's stub backend. With
`claude-haiku-5-5`, as pilot trials kept in `results/pilot/`: Philosophers,
Banking, BoundedBuffer and missing workers in both arms through the
scheduler, every Savina trial that ran to its end a success and every stated
missing-workers policy followed, the BoundedBuffer manager finishing with no
`To:` line; a run stopped by its own time limit,
which kept its whole record and left no agent behind; a runner killed from
outside, whose six leftover agents the clearing removed; and, unplanned, two
trials that a usage limit interrupted, which stopped at their time limit with
the limit on the silent agent's screen in their record, were listed as not
counted, and were run again once the limit lifted. With `gpt-6.1-sol`: a
Banking pilot in both arms, both successes.

*First run under this tag:* `claude-haiku-5-5` on the three `m7i-flex.large`
machines of the second run, cleared of every earlier agent: Philosophers,
Banking, BoundedBuffer, scatter-gather and missing workers, both arms, each
machine taking every third pick of one shuffled schedule, stopping at
2026-11-25 00:00 UTC.

**`prereg-v9`, 2026-11-23: the same model in both arms on Claude, and what
each trial used.** Found while looking for token counts: Claude Code keeps,
for each folder, the model its last session there ran on, and every record of
a `dsl`-arm agent named `claude-sonnet-5-5`. The model had been pinned by a
setting in one working folder. The `agent` arm did not depend on it, because
the runner names the model when it starts an agent; but OMAR starts each
`dsl`-arm agent in a folder of its own, where that setting is not read, so
those agents ran on Claude Code's default model. Since `prereg-v3`, then,
every Claude trial of the `dsl` arm ran on a larger model than the `agent`
arm's: no comparison of the two arms on Claude made before this tag is a
comparison at one model, and none is reported as one. The `agent`-arm trials
are what they say, trials of `claude-haiku-5-5`; the `dsl`-arm trials are
published as trials of `claude-sonnet-5-5`. Codex is not affected: its model
is set once for the machine, and both arms' sessions name it.

What changes:

1. *The model.* It is pinned in Claude Code's user settings on the machine,
   which every session reads wherever it starts (`infra/claude-model.sh`).
2. *Read back, every trial.* Each trial's row records the model each of its
   agents ran on, from Claude Code's own record, and a trial with an agent on
   any other model than its cell's is voided and run again. The pin is no
   longer taken on trust.
3. *Tokens and cost.* The same record holds what a session used. So that it
   exists for every agent, each `agent`-arm agent now starts in an empty
   folder of its own, as OMAR already starts each `dsl`-arm agent. Each
   trial's row then carries its agents' tokens (in, out, cached read and
   written) and their cost at list price, in both arms (Analysis).
4. *One trial at a time, by hand as well.* A sweep run by hand now takes the
   machine's lock as a scheduler does. On 2026-11-23 at 20:28 UTC a test of
   change 1, run by hand on a machine wrongly believed idle, cleared the
   agents of a Codex `agent`-arm Banking trial of the `prereg-v8` rerun in
   progress there (shard 1 of 4, trial 5). That trial's runner was stopped
   and the sweep ran the trial again; the stopped attempt is in its attempts
   file, with a note beside it, and is not an outcome of either arm.

Nothing else changes: the action rules, prompts, programs, checker and causes
are those of `prereg-v8`.

*What becomes of the `prereg-v8` Claude run.* It was stopped at 20:37 UTC on
2026-11-23 with about half its trials done, and is published under its tag:
its `agent` arm as `claude-haiku-5-5` trials under the audited harness, its
`dsl` arm as `claude-sonnet-5-5` trials. Neither is pooled with this tag's.
The Codex rerun under `prereg-v8` is unaffected and continues.

*Validated* at commit `e147d3b`, which this tag differs from in this file and
the two READMEs only. With no model, on a benchmark machine: the runner's
118 tests; the scorer, harness, probe-sweep and scheduler tests, which now
also check that a trial's tokens, cost and model are read back for each
agent and that a trial run on another model is voided; and the `dsl` arm of
all four programs on OMAR's stub backend. With `claude-haiku-5-5`, as pilot
trials through the scheduler on a machine carrying the pin: BoundedBuffer in
the `dsl` arm and Banking in both arms, all successes, with every agent of
both arms recorded on `claude-haiku-5-5` and every agent metered (the
Banking pair: about 89,000 output tokens and $1.00 at list price in the
`agent` arm, about 10,000 and $0.04 in the `dsl` arm).

*First run under this tag:* `claude-haiku-5-5` on the same three machines,
cleared: Philosophers, Banking, BoundedBuffer, scatter-gather and missing
workers, both arms, each machine taking every third pick of one shuffled
schedule, stopping at 2026-11-25 00:00 UTC.
