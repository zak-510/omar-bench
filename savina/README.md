# Savina for agents

Agent adaptations of two benchmarks from Savina's concurrency category,
Philosophers and Banking. The design and outcomes are in
[../PREREGISTRATION.md](../PREREGISTRATION.md).

| File | What it is |
|---|---|
| `spec.py` | Sizes, the banking schedule, meal questions and every prompt, for both arms |
| `build.py` | Renders `specs/*.json` (agent arm) and `programs/*.omar` (DSL arm) from `spec.py` |
| `check.py` | Scores a trial in either arm from the same ordered events |
| `test_check.py` | A correct run passes; each failure the checker names is caught |
| `sweep.py` | Runs trials in one arm and writes one CSV row and one raw log per trial |

```sh
python3 savina/build.py && python3 savina/test_check.py
python3 savina/sweep.py --bench philosophers --arm dsl --tier sol --trials 3
```
