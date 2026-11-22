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
| `check.py` | Scores a trial in either arm from the same ordered events |
| `test_check.py` | Checks the scorer, and that each protocol followed exactly finishes under its cut |
| `sweep.py` | Runs trials in one arm and records each one |

## Usage

```bash
python3 savina/build.py && python3 savina/test_check.py
python3 savina/sweep.py --bench philosophers --arm dsl --tier sol --trials 1
```
