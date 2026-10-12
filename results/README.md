# Results

One row per attempt. Only `exclusions.toml` is edited by hand.

| Path | What it is |
|------|------------|
| `attempts/<run-id>.jsonl` | One line per attempt. Failed, errored and timed-out attempts are kept. |
| `runs/<run-id>.json` | One record per run: how many attempts were planned and how many finished. |
| `exclusions.toml` | Attempts left out of the totals, each with a written reason. |

Raw run logs (transcripts, test output) are too large for git. They stay in the S3 bucket of whoever ran them; each row's `trial_dir` says where.

## Fields

| Field | Meaning |
|-------|---------|
| `attempt_id`, `run_id`, `trial` | Which attempt, in which run |
| `study` | The name its results are reported under. Studies are never mixed |
| `suite`, `arm`, `model`, `model_id` | What ran |
| `task`, `task_checksum` | Which task, and a checksum of its files |
| `outcome`, `reward` | `pass`, `fail` or `error`, from the task's own tests |
| `error_type` | What went wrong, if anything. A timeout, for example |
| `tokens`, `usage` | Tokens used, in total and per model, from the agents' own logs |
| `served_models` | The model that actually answered |
| `cost_usd`, `cost_source` | Those tokens at API list prices, and which price list. Not a bill: a subscription charges nothing per run |
| `unpriced_models` | Models with no price set. Cost is empty when there are any |
| `wall_secs`, `started_at` | How long the attempt took, and when it started |
| `operator`, `auth` | Who ran it, and whether on a subscription or an API key |
| `cell_sha256`, `config_sha256` | Hashes of the settings, so only attempts run the same way are combined |
| `workspace_files` | OMAR arms only: files the team left outside the task's own directory |
| `harbor_trial_id`, `trial_dir` | Harbor's id for the attempt, and where its raw logs are |

## Reports

```bash
uv run omar-bench report                 # tables built from these rows
python3 tools/recount.py RUNS_DIR        # recount every row from the raw logs
```

- Each study is reported separately.
- Attempts run under different settings or prices are not combined. The report stops and names the runs.
- A row marked † includes a run that did not finish.
