# omar-bench

Benchmarks for [OMAR](https://github.com/omar-os/omar). Does deterministic coordination make an agent team succeed more often, for less cost, than one agent alone?

**No result yet.** Two small trial runs show every setup runs and gets graded. See [Status](#status).

## Features

- **Official tasks and graders**: tasks run on [Harbor](https://docs.harborframework.com) and are graded by their own tests.
- **Only coordination changes**: every setup gets the same task, model and agent CLI.
- **Fixed versions**: every version, dataset, model and price is set in `bench.toml`.
- **Every attempt kept**: one row per attempt in `results/`, failures and timeouts included.
- **Cost on a subscription**: tokens are counted from the agents' own logs and priced at API list prices.

## Arms

The three setups being compared (`--arm`).

| Arm | What runs |
|-----|-----------|
| `solo` | Claude Code or Codex alone |
| `omar-solo` | One agent through OMAR |
| `omar-plan-build-check` | An OMAR team: planner, builder, checker, one revision |

## Suites

| Suite | Tasks | Time limit | State |
|-------|-------|------------|-------|
| `terminal-bench` | Terminal-Bench 4.0, 63 of 66 (no GPU) | 8 h | tried on one task |
| `terminal-bench-2` | Terminal-Bench 2.0, 89 | 15 min for most | tried on one task |
| `apex-agents`, `swe-atlas-rf` | Graded by a model, which needs an API key | 3 h | set up, not run |

Models: `opus`, `haiku`, `glm` on Claude Code; `sol`, `astra` on Codex.

## Prerequisites

- Linux x86_64 with Docker, [uv](https://docs.astral.sh/uv/), tmux and git
- A Claude subscription token (`claude setup-token`), or a ChatGPT login for Codex (`codex login`)

A fresh EC2 machine with all of it: [infra/ec2/README.md](infra/ec2/README.md).

## Quick Start

```bash
cp .env.example .env          # add your token and GitHub handle
uv run omar-bench doctor      # check tools, login, versions

# one 15-minute task, one attempt
uv run omar-bench run terminal-bench-2 --arm omar-plan-build-check --model haiku \
  --study demo -k 1 --tasks '*fix-git'

uv run omar-bench report      # pass rate, cost per attempt, cost per success
```

## Status

Haiku 5.5, one attempt per arm. Times are how long the agents worked.

| Task | `solo` | `omar-solo` | `omar-plan-build-check` |
|------|--------|-------------|-------------------------|
| 4.0 `photonic-waveguide-routing` | fail, 3 h 59 min | fail, 5 h 17 min | timed out at 8 h |
| 2.0 `fix-git` | pass, 2 min 19 s | pass, 1 min 20 s | pass, 11 min 31 s |

Two tasks tried once is not a comparison of the arms.

## Not yet

- **A result**: needs many tasks at five attempts each.
- **Other frameworks**: Google ADK, LangGraph, Omnigent, and the older OMAR where agents coordinate themselves.
- **Codex**: written, never run.
- **A revision round**: no run has had the checker reject work.

## Layout

```
bench.toml      versions, models, prices
omar_bench/     the benchmark code, including the adapter that runs OMAR inside Harbor
topologies/     the OMAR programs being tested
infra/ec2/      machine setup and the unattended run queue
results/        one row per attempt
tools/          recount results from raw run logs
tests/          unit tests
```

## Tests

```bash
PYTHONPATH=. python3 -m unittest discover -s tests                        # core
PYTHONPATH=. uv run --with harbor python -m unittest discover -s tests    # plus the OMAR adapter
```
