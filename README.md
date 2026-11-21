# omar-bench

Does a runtime that owns coordination make agent teams fail less than agents
coordinating through their prompts? Two studies, both arms on the same machine
and model: a reproduction of the summer 2026 coordination probes, and agent
adaptations of Savina's concurrency benchmarks. What runs, how many times and
how it is scored is fixed in [PREREGISTRATION.md](PREREGISTRATION.md) before
anything counted runs.

Status: pilot. No counted results yet.

## Layout

```
PREREGISTRATION.md   the claim, arms, outcomes, trial counts and stopping rule
versions.env         every pinned version and checksum
infra/               launch the EC2 machine, install the toolchain, sync, sample memory
run/                 plan.py (the trial table), schedule.py (runs it), codex_quota.py
summer/              the summer probes: agent-arm runner (Rust) and DSL programs
savina/              Savina Philosophers and Banking, both arms, and their checker
results/             counted results; pilots go to results/pilot/ and are not committed
```

## Run

```sh
infra/launch.sh <aws-profile> [alert-email]     # one machine in us-east-2
infra/sync.sh push && infra/sync.sh ssh
# on the machine
cd omar-bench && sh infra/bootstrap.sh
. ~/omar-rerun.env && codex login --device-auth
python3 run/schedule.py --tier sol --pilot 3      # pilot: 3 trials of every cell
python3 run/schedule.py --tier sol                # counted: needs the tagged commit
```

`--max-agents 6` restricts a run to the cells an 8 GiB machine can hold, and
`--shard 0/2` / `--shard 1/2` split one run across two identical machines
(launch the second with `NAME`, `AMI` and `ENV_FILE`; see `infra/launch.sh`).
`--dry-run` prints the trials and agent calls a run would take. From your own
machine, `infra/sync.sh pull` brings `results/` back.

## Tests

```sh
python3 savina/test_check.py                     # the Savina checker
(cd summer && cargo test --release --locked)     # the agent-arm runner
```
