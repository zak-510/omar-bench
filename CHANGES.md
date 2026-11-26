# Changes to the pre-registration

One entry per tag, oldest first. Entries up to `prereg-v10` are as they were
written at the time, moved here from PREREGISTRATION.md at `prereg-v11`.

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

*Validated* at commit `e147d3b`, which this tag differs from in documentation
only (this file, two READMEs and `probes/CHANGES.md`). With no model, on a benchmark machine: the runner's
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

**`prereg-v10`, 2026-11-23: a trial's own usage records, and all of them.**
The first run under `prereg-v9` was stopped after 26 minutes, at 21:23 UTC, on
what its first trials recorded. It showed two faults in how v9 read what a
trial's agents used. Neither is in how a trial is run or scored.

1. *Records of earlier runs.* Claude Code keeps a folder's record for good,
   and OMAR names its sessions from some 1,600 names, so a name comes round
   again: on a machine where 280 had been used, for about one new session in
   six. v9 took every record under a session's name to be the trial's. A
   `dsl`-arm trial whose session name the `prereg-v8` run had used earlier in
   the day was therefore read as having ten or more agents, some of them on
   `claude-sonnet-5-5`, and voided as run on the wrong model: 3 of the 11
   `dsl`-arm trials and runs v9 recorded. In each of the three, the sessions
   that started during the trial were all on `claude-haiku-5-5`, and the
   others had started four hours or more before it (the records carry their
   sessions' start times). No record of a session started during the v9 run
   names a model other than its cell's. Now a record is a trial's only if its
   session started at or after the trial did. Each record counted is kept in the trial's raw log with its
   session's id and start time, beside the number of earlier records left
   out (the `dsl`-arm probes keep no raw log: their rows hold the totals, the
   models and the count).
2. *Records lost as a run ended.* Claude Code writes a session's record as
   the session exits, into one file that every session on the machine shares,
   and sessions that exit together overwrite each other's. Of six sessions
   ended at once on a benchmark machine, one record was left, in each of two
   tries; ended one after another, each gone before the next, all six were, in
   each of two tries. The runner ended a run's agents together. Under v9, 4 of
   the 22 `agent`-arm trials and runs kept every agent's record, and neither
   of the two Savina trials did; all 11 in the `dsl` arm did, whose agents
   OMAR ends itself. A comparison of what the two arms use would have stood on
   almost no `agent`-arm trials. Now the runner ends its agents one at a time,
   waiting for each to exit before it ends the next, and so does the clearing
   of a machine between trials. Agents are ended after a trial's result is
   recorded, and how they are ended changes nothing in it.

With these, `metered` counts a trial's records against the agents it had: six
in the `agent` arm of a Savina benchmark, five in the `dsl` arm, where the
coordinator is the runtime. The report takes a trial as fully metered when it
has a record for each of them, and its line setting the two arms' use side by
side, which failed on the first table it was given, is fixed.

What the recorded cost is, said more exactly than in v9: Claude Code's own
figure, each request priced from the price list built into the CLI (2.1.326).
For `claude-haiku-5-5` that is, per million tokens, $0.10 in, $0.50 out,
$0.20 for a cached write kept an hour and $0.01 for a cached read, and five
times each for a request whose prompt is over 100,000 tokens. The higher rate is not a
detail here: an agent that carries a whole run in its context, as the
`agent` arm's coordinator does, is charged it on most of its requests. Cost
is reported as recorded, beside the tokens it was worked out from.

Nothing else changes: the model pin and its check, the action rules, prompts,
programs, checker and causes are those of `prereg-v9`.

*What becomes of the `prereg-v9` run.* Its 6 Savina trials and 27 probe runs
are published under its tag, the three voided ones with the records that show
why. None is pooled with this tag's.

*Validated* at commit `681e31c`, which this tag differs from in documentation
only. With no model, on a benchmark machine: the runner's 119 tests, one of
them new (an ended agent is waited for until nothing runs in its folder); the
scorer, harness, probe-sweep and scheduler tests, which now also check that
the records of an earlier run under the same session name are left out and
that a trial is not voided for them; and the `dsl` arm of all four programs
on OMAR's stub backend. With `claude-haiku-5-5`, as pilot trials through the
scheduler on the three machines: Banking and Philosophers in both arms, all
four successes, and the missing-workers probe in both arms, seven runs each.
Every agent of every one of them was metered, six of six in the `agent` arm
and five of five or six of six in the `dsl` arm, all on `claude-haiku-5-5`
(Banking: about 85,000 output tokens and $1.00 in the `agent` arm, about
10,000 and $0.04 in the `dsl` arm; Philosophers: about 118,000 and $2.36,
about 8,500 and $0.04). A run whose sweep and runner were killed mid-trial
left six agents: clearing the machine ended them one at a time, and all six
records were written. Read again with this tag's reader, the three v9 trials
voided for their model each show their own five or six agents, all on
`claude-haiku-5-5`, and 5, 10 and 16 earlier records left out.

*First run under this tag:* `claude-haiku-5-5` on the same three machines,
cleared: Philosophers, Banking, BoundedBuffer, scatter-gather and missing
workers, both arms, each machine taking every third pick of one shuffled
schedule, stopping at 2026-11-25 00:00 UTC.

*Second run under this tag* (added 2026-11-23, before it started):
`gpt-6.1-sol` on the four machines that had been running it under
`prereg-v8`. That run was stopped by hand at 22:12 UTC on 2026-11-23, six
hours in, so that Codex is measured under the same tag as Claude. Its 122
trials, all of them successes in both arms, are published under `prereg-v8`
and are not pooled with this tag's.

For Codex this tag differs from `prereg-v8` in two things: each `agent`-arm
agent starts in an empty folder of its own, as each `dsl`-arm agent always
did, and a run's agents are ended one at a time. Codex keeps no record of
use that the harness reads, so the token and cost columns of its trials are
empty (Analysis says what is reported for Codex).

Before the run, at the tagged commit: the no-model tests on one of the four
machines (the runner's 119 tests, the scorer, harness, probe-sweep and
scheduler tests, and the `dsl` arm on OMAR's stub backend); pilot trials of
Banking, Philosophers and BoundedBuffer in both arms through the scheduler,
all six successes; and a run whose sweep and runner were killed mid-trial,
whose six agents, each found running in its own folder, the clearing of the
machine ended one at a time.

The run: Philosophers, Banking, BoundedBuffer and Philosophers-90, both
arms, each machine taking every fourth pick of one shuffled schedule,
stopping at 2026-11-25 00:00 UTC or at 98% of the week's Codex use,
whichever comes first. That use stood at 7% when the run started, at 22:40
UTC. By what the `prereg-v8` trials used, the rest of the week does not
cover every planned trial: each cell is reported at the n it reaches, and
one short of its planned n is called underpowered.

**`prereg-v11`, 2026-11-26: a longer job, and its test named first.** The
`prereg-v10` runs stopped at their deadline with every cell short of its
planned n. On `claude-haiku-5-5` some `agent`-arm trials failed and no
`dsl`-arm trial did. On `gpt-6.1-sol` nothing failed in either arm,
Philosophers-90 included. This tag was written after those results were read,
and because of them: the 30-round jobs are too short to tell the arms apart.

1. *A longer job on Haiku.* Philosophers-90 on `claude-haiku-5-5`, both arms,
   30 trials each, on one of the three machines, stopping at 2026-11-29 00:00
   UTC. It is reported beside the `prereg-v10` runs and never pooled with
   them. Longer versions of Banking and BoundedBuffer may follow under a later
   tag, entered here before they start.
2. *The primary test, named now:* the 90-round benchmarks that run by the
   stop, together, stratified by benchmark (Analysis).
3. *No trial starts past the deadline.* Under `prereg-v10` three did, with a
   usage limit still on, and were excluded by the limit rule.
4. *The machine's model is checked.* The scheduler refuses a tier on a machine
   whose Claude Code or Codex is set to another model. There are tiers for
   `gpt-6.1-terra` and `gpt-6-luna`; neither is run at this tag.
5. *A shorter pre-registration.* Its history moved to this file. No rule
   changes but the ones in this entry.

Noted late, about earlier runs: Haiku scatter-gather ran on the 8 GiB machines
although its `dsl` arm keeps 10 agents alive, against the 6-agent rule (free
memory never fell below 4.4 GiB). The reasoning effort agents used was to be
read back from session logs and is not reported, because Codex kept almost
none.
