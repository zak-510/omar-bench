# Savina

**Agent adaptations of three benchmarks from Savina's concurrency category.**

Each benchmark has worker agents and one coordinator, and runs 30 rounds. In
the `agent` arm the coordinator is an agent following Savina's protocol in its
prompt; in the `dsl` arm it is code in an OMAR program. The full design is in
the [pre-registration](../PREREGISTRATION.md).

## Benchmarks

| Benchmark | Workers | Coordinator | Rounds |
|-----------|---------|-------------|--------|
| Philosophers | 5 philosophers | Arbitrator | 30 meals |
| Banking | 5 accounts | Teller | 30 transfers |
| BoundedBuffer | 3 producers, 2 consumers | Buffer manager (3 items) | 30 items |

## Files

| File | Purpose |
|------|---------|
| `spec.py` | Sizes, schedules, questions, delivery cuts and every prompt, for both arms |
| `build.py` | Renders `specs/` (agent arm) and `programs/` (DSL arm) from `spec.py` |
| `check.py` | Scores a trial in either arm from the same ordered events, and files it under one cause |
| `sweep.py` | Runs trials in one arm and records each one |
| `mock_agents.py` | Scripted agents that play each protocol exactly, with one fault on request |
| `test_check.py` | Checks the scorer, and that each protocol followed exactly finishes under its cut |
| `test_harness.py` | Runs the runner and scorer together against scripted agents and a recorded trial |
| `fixtures/` | The recorded trial that the tests replay |

## Scoring

A trial succeeds when its run finishes and every invariant held. A run that
says it is done is not thereby complete: the events decide. Every trial is
filed under one cause, and the causes are reported apart.

| Cause | Meaning | Counts |
|-------|---------|--------|
| `success` | Finished, every invariant held | Yes |
| `safety` | An invariant was broken | Yes |
| `noncompletion` | The work was not finished | Yes |
| `protocol` | Agent arm: a message was lost or unreadable, and the run failed | Yes |
| `runtime` | Agents did not start or never answered, or the runtime failed | Yes |
| `provider` | The model provider was limited, down or signed out | No: listed, run again |
| `harness` | The benchmark's own code or machine failed | No: listed, run again |

## Usage

```bash
python3 savina/build.py && python3 savina/test_check.py
(cd runner && cargo build --release --locked) && python3 savina/test_harness.py
python3 savina/sweep.py --bench banking --arm agent --tier mock --trials 1   # scripted agents, no model
python3 savina/sweep.py --bench philosophers --arm dsl --tier sol --trials 1
```
