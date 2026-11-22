# Pre-registration: coordination reliability, summer reproduction and Savina

**Status: frozen** at tag `prereg-v1`, 2026-11-21, and amended at `prereg-v2`
the same day (see Changes, at the end). No counted run starts before a tag, and
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
arms except for the coordination clauses (routing instructions and address
rules); the diff is published with the results.

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
is why both machines have swap.

## Models

| Phase | Model | Cells |
|---|---|---|
| 1 | `gpt-6.1-sol` through Codex CLI, default reasoning effort | All cells, both arms |
| 2 | `claude-haiku-5-5` through Claude Code, default effort | All cells, both arms |
| 2 | `claude-opus-5-5`, agent arm only | Checkable scatter-gather, ring, Philosophers; 30 each |

The model is pinned outside the programs: `CODEX_HOME/config.toml` for Codex,
the workdir's `.claude/settings.json` for Claude. Effort used is read back from
the session logs and reported.

## Benchmarks

1. **Summer probes, unchanged:** chain (n = 20, 50, 100), scatter-gather with a
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
| Savina BoundedBuffer | Run finishes, the buffer never holds more than 3 items, no item stored twice or handed out without being stored, every item answered exactly once by the consumer it was given to | Any of those broken, or no finish (deadlock, silence, the 500-delivery cut) |

Wrong answers to meals and items are content errors, not coordination:
reported per arm, not part of success.

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
  stopping at 2026-11-25 00:00 UTC. Second run (`prereg-v3`), on the same two
  machines and with the same stop: BoundedBuffer on `gpt-6.1-sol`, then
  Philosophers, Banking and BoundedBuffer on `claude-haiku-5-5`, both arms.
  Each run is its own shuffled schedule, so either one stopped by the deadline
  is still a balanced sample. Missing workers
  and the other summer cells follow under a later tag, on the
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
  100; 3,600 s for ring 40; 7,200 s for a Savina trial. These are the summer's
  longest (the set it used for Opus), so a slower model is not cut short.
- An agent may take as long as its trial to answer, in both arms: the `dsl`
  arm's runtime waits for a reaction until the run's timeout, and the `agent`
  arm's runner waits as long for a reply (reminding a silent agent after
  180 s, then every 60 s).
- Retries, the same rule in both arms: only when an agent never receives its
  task (its process fails to start, or the runtime fails to deliver the task to
  it), at most 3, every attempt logged in `*-attempts.csv` and counted per arm
  in the results. A stall or timeout is an outcome of its arm.
- Exclusions: only a trial whose logs show a provider outage or usage limit.
  The analysis lists each one with the log line, and the trial is run again.
- Stopping: the run stops when every trial is done, when weekly Codex use
  reaches 85% (the guard keeps the account usable), or at a stated deadline.
  Because the order is shuffled and interleaved, a stopped run is a balanced
  sample: the results give the runs achieved against the runs planned for
  every cell, and call a cell short of its planned n underpowered.

## Analysis

Every cell is reported: failure rate with a Wilson 95% interval per arm, and a
one-sided Fisher exact test where a gap is expected. No result is described as
"100%". Wall time and weekly Codex use before and after every trial come from the
scheduler's ledger. Tokens are reported for the agents whose CLI kept a session
log (Codex does not keep one for every agent the runtime starts), with that
coverage stated; cost is tokens times list price on the tag date. Raw logs, CSVs and the commands that
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

**`prereg-v3`, 2026-11-21: BoundedBuffer, and Savina on a second model.**
BoundedBuffer is the third Savina benchmark because it is the
producer-consumer pattern behind agent work queues, its rules can be checked
from the events alone, and it fits 6 agents. It was chosen after the interim
Philosophers (v1) and Banking results had been seen, for those reasons only,
and before any BoundedBuffer trial ran. Its fuse is 500 deliveries by the same
rule as the others (four times the fewest, 125). Phase 2 starts with Savina:
`claude-haiku-5-5` runs Philosophers, Banking and BoundedBuffer in both arms,
60 trials each, with Claude Code signed in by a token from `claude setup-token`
and the model pinned in the workdir's settings (`infra/bootstrap.sh`). The
summer cells on `claude-haiku-5-5` follow with the OMAR fix. Nothing in
Philosophers or Banking changes: their specs and programs are byte-for-byte
the same as at `prereg-v2`. The repository layout also changed at this tag,
with no code changed: the original study's runner is now `runner/`, its probes
`probes/` (results in `probes/original-results/`), and the OMAR tools each
machine installs `local/`.

