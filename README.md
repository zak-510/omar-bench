# omar-bench

Does a runtime that owns coordination make agent teams fail less than agents
coordinating through their prompts? Both arms run the same model, the same
agent job text and the same machines; only who coordinates differs: an agent
following a protocol in its prompt, or an OMAR program. What runs, how many
times and how it is scored is fixed in [PREREGISTRATION.md](PREREGISTRATION.md)
before anything counted runs, and every later change is a new tag with its
reason.

Status: counted runs under way. Savina Philosophers and Banking on
`gpt-6.1-sol` (`prereg-v1`, Philosophers from `prereg-v2`); BoundedBuffer and
`claude-haiku-5-5` next (`prereg-v3`). The summer probes follow once OMAR's
Codex delivery fix is released.

## Layout

```
PREREGISTRATION.md   the claim, arms, outcomes, trial counts, stopping rule and every change
versions.env         every pinned version, checksum and the machine image
infra/               launch an EC2 machine, install the toolchain, sync, sample memory,
                     install a Claude Code token
run/                 plan.py (the trial table), schedule.py (runs it), codex_quota.py
savina/              Savina Philosophers, Banking and BoundedBuffer: both arms, their checker
summer/              the summer probes: agent-arm runner (Rust) and DSL programs
results/             counted results by model and machine; pilots stay in results/pilot/
```

## Run

```sh
infra/launch.sh <aws-profile> [alert-email]           # one machine in us-east-2
infra/sync.sh push && infra/sync.sh ssh
# on the machine
cd omar-bench && sh infra/bootstrap.sh
. ~/omar-rerun.env && codex login --device-auth
python3 run/schedule.py --tier sol --pilot 1          # pilot: 1 trial of every cell
python3 run/schedule.py --tier sol                    # counted: needs a clean, tagged commit
```

- Claude: make a token with `claude setup-token`, copy it, and run
  `infra/claude-token.sh` from your own machine; then `--tier haiku55`.
- Two machines: launch the second with `NAME` and `ENV_FILE` (see
  `infra/launch.sh`), sync it with `ENV_FILE=… infra/sync.sh push`, and run
  `--shard 0/2` on one and `--shard 1/2` on the other.
- `--max-agents 6` keeps the cells an 8 GiB machine can hold, `--units` picks
  cells, `--until` sets the deadline, and `--dry-run` prints the trials and
  agent calls a run would take.
- A counted run refuses to start from an untagged or modified commit, or when
  an installed OMAR, Codex CLI or Claude Code differs from `versions.env`. Its
  ledger records weekly Codex use before and after every trial.
- `infra/sync.sh pull` brings `results/` back. Each scheduler start writes a
  manifest with the commit, tag, machine, image and tool versions.

## Tests

```sh
python3 savina/build.py && python3 savina/test_check.py   # rebuild both arms; checker and protocol tests
(cd summer && cargo test --release --locked)              # the agent-arm runner
```
