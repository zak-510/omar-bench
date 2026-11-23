# Coordination Probes

**The coordination probes from the original study of July–August 2026.**

The same code as the original study; every change is listed in
[CHANGES.md](CHANGES.md). The pre-registration calls this the summer study.

## Probes

| Probe | Sizes | What it tests |
|-------|-------|---------------|
| Chain | 20, 50, 100 agents | Relaying a message without losing fields |
| Ring | 10, 25, 40 agents | Passing a token for two laps, or stopping legally |
| Scatter-gather | 5 or 9 workers | Gathering the right answer when some workers are corrupted |
| Missing workers | 5 workers | Following a stated policy when workers fail |

## Files

| Path | Purpose |
|------|---------|
| `sweep.py` | Agent-arm sweep, through the [runner](../runner/) and its [action rules](../runner/README.md#action-rules) |
| `test_sweep.py` | Checks what that sweep records, keeps and voids, with a stand-in for the runner |
| `dsl/dsl_sweep.py` | DSL-arm sweep of chain and scatter-gather |
| `dsl/dsl41_sweep.py` | DSL-arm sweep of rings and missing workers |
| `dsl/gen.py`, `dsl/sg_gen.py` | Generators for every `.omar` program |
| `dsl/build_programs.sh` | Regenerates all 57 programs |
| `original-results/` | The original study's results, unchanged |

## Usage

```bash
sh probes/dsl/build_programs.sh
python3 run/schedule.py --tier sol --units chain-20 --dry-run
```
