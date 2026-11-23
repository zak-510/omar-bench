<div align="center">

# omar-bench

**Does coordination owned by a runtime make agent teams fail less than coordination written into their prompts?**

**Pre-registered multi-agent benchmarks on [OMAR](https://omar.rs): same model, same agents, same machines.**

<p align="center">
  <a href="PREREGISTRATION.md">Pre-registration</a>&nbsp; • &nbsp;
  <a href="#benchmarks">Benchmarks</a>&nbsp; • &nbsp;
  <a href="#quick-start">Quick Start</a>&nbsp; • &nbsp;
  <a href="https://omar.rs">omar.rs</a>
</p>

</div>

## Design

Every benchmark runs in two arms. Only the coordinator differs.

| Arm | Who coordinates | How |
|-----|-----------------|-----|
| `agent` | The agents | A protocol in their prompts; agents address each other by message |
| `dsl` | OMAR | An OMAR program; agents only do the work they are handed |

What runs, how many times and how it is scored is fixed in the
[pre-registration](PREREGISTRATION.md) before any counted trial, and every
later change is a new tag with its reason.

Every trial is filed under one cause, and the causes are reported apart: a
broken invariant, unfinished work, a lost message, a failure of the machinery
around the agents. A trial the model provider spoiled, one the scorer itself
failed on, or one whose agents ran on the wrong model is listed with its
evidence and run again. Each trial also records the tokens its agents used,
their cost at list price and the model they ran on. The headings are in
[`savina/`](savina/#scoring), and how an agent's reply is read is in
[`runner/`](runner/#action-rules).

## Benchmarks

- **Savina Philosophers**: Five agents share five forks through an arbitrator. Tests mutual exclusion and starvation.
- **Savina Banking**: Five accounts and a teller run 30 transfers. Tests consistent shared state.
- **Savina BoundedBuffer**: Three producers and two consumers share a three-item buffer. Tests backpressure.
- **Coordination probes**: Chain, ring, scatter-gather and missing workers, from the original study of July–August 2026.

## Supported Models

| Tier | Model | Agent CLI |
|------|-------|-----------|
| `sol` | `gpt-6.1-sol` | Codex CLI 0.162.0 |
| `haiku55` | `claude-haiku-5-5` | Claude Code 2.1.326 |

## Installation

### Prerequisites

- An AWS account and the AWS CLI (runs use EC2 in us-east-2)
- A Codex login, and for Claude a token from `claude setup-token`
- Python 3 on your own machine

Everything else (OMAR 0.4.4, Node.js, Rust and both agent CLIs) is installed
on the benchmark machine at the versions pinned in [`versions.env`](versions.env);
OMAR and Node.js are checked against their checksums.

## Quick Start

#### Step 1: Launch a machine

```bash
infra/launch.sh <aws-profile>
infra/sync.sh push
```

#### Step 2: Install the toolchain and sign in

```bash
infra/sync.sh ssh
cd omar-bench && sh infra/bootstrap.sh
. ~/omar-rerun.env && codex login --device-auth
```

For Claude, copy the token from `claude setup-token` and run
`infra/claude-token.sh` from your own machine, then `sh infra/claude-onboard.sh`
on the benchmark machine.

#### Step 3: Run

```bash
python3 run/schedule.py --tier sol --pilot 1     # one pilot trial of every cell
python3 run/schedule.py --tier sol               # the counted run
```

A counted run starts only from a clean, tagged commit with every tool at its
pinned version, and writes under `results/<tag>/`. `infra/sync.sh pull` brings
`results/` back to your machine. `--shard 0/2` and `--shard 1/2` split a run
across two machines; `--dry-run` prints what a run would take.

#### Step 4: Read the results

```bash
python3 run/report.py results/<tag>     # reliability by cause, then tokens and cost per trial
```

## Known Issues

- **OMAR 0.4.4 and Codex**: OMAR gives up on a Codex agent's app server that takes more than 3 seconds to answer, so the `dsl` arm can fail to hand tasks to several Codex agents starting at once. Savina trials recover on the pre-registered retry; the probes run on Codex once the fix is released. See [Running](PREREGISTRATION.md#running).
- **Trials before `prereg-v8`**: An audit of the harness found faults in how the agent arm's replies were read and how machines were left between trials. They are fixed at `prereg-v8`. Earlier trials are published under their own tags with the audit's row beside each (`run/audit.py`), and are never pooled with later ones. See [Changes](PREREGISTRATION.md#changes).

## Repository Layout

| Path | Contents |
|------|----------|
| [`PREREGISTRATION.md`](PREREGISTRATION.md) | Claim, arms, outcomes, trial counts, stopping rule and every change |
| [`savina/`](savina/) | Savina benchmarks: specs, both arms and the checker |
| [`probes/`](probes/) | The original study's coordination probes |
| [`runner/`](runner/) | The agent-arm runner (Rust) |
| `run/` | Trial plan, scheduler, results table and the audit of earlier runs |
| `infra/` | Machine launch, setup and sync |
| `results/` | Counted results, by model and machine |

## Tests

None of these uses a model.

```bash
(cd runner && cargo test --release --locked && cargo build --release --locked)
python3 savina/test_check.py      # the scorer
python3 savina/test_harness.py    # runner and scorer together: scripted agents, and a recorded trial replayed
python3 probes/test_sweep.py      # what the probe sweep records, keeps and voids
python3 run/test_schedule.py      # which trials run, count, and are run again
```
