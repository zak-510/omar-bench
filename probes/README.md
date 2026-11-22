# Coordination probes

The probes from the original study of July–August 2026 (called the summer
study in [../PREREGISTRATION.md](../PREREGISTRATION.md)): chain, ring,
scatter-gather and missing workers, each in both arms. They run the same code
as the original study; what changed and why is in [CHANGES.md](CHANGES.md).

| Path | What it is |
|---|---|
| `sweep.py` | Agent-arm sweep: agents coordinate by addressed mail through the runner (`../runner/`) |
| `dsl/dsl_sweep.py` | DSL-arm sweep of chain and scatter-gather |
| `dsl/dsl41_sweep.py` | DSL-arm sweep of rings and missing workers, which need OMAR 0.4.1's features |
| `dsl/gen.py`, `dsl/sg_gen.py` | The generators that write every `.omar` program |
| `dsl/build_programs.sh` | Regenerates all 57 programs for the Claude, Codex and Stub backends |
| `dsl/*.omar` | The generated programs |
| `original-results/` | The original study's CSVs and its write-up, unchanged |

```sh
sh probes/dsl/build_programs.sh                  # regenerate the programs; no diff means none was edited by hand
python3 run/schedule.py --tier sol --units chain-20 --dry-run
```
