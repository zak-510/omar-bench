# Running on EC2

One AWS template gives everyone the same machine: Ubuntu 24.04 with Docker, plus an S3 bucket for run logs. No SSH keys; you connect from the AWS console in a browser.

## Cost

- **Default machine** (`m7i.4xlarge`, 16 CPUs): about $0.81/hour, plus about $0.80/day for the disk. Check current prices.
- **Free-plan accounts** can only launch `m7i-flex.large` (2 CPUs, 8 GB). Enough for small tasks, not for a full suite.
- **Stop it when idle.** A stopped machine still bills for its disk.

## Setup

#### Step 1: Launch

In [AWS CloudShell](https://console.aws.amazon.com/cloudshell), upload `infra/ec2/stack.yaml` (Actions → Upload file), then:

```bash
aws cloudformation deploy --stack-name omar-bench \
  --template-file stack.yaml --capabilities CAPABILITY_IAM
```

On a free-plan account, add `--parameter-overrides InstanceType=m7i-flex.large VolumeGiB=100`.

#### Step 2: Connect and install

EC2 console → the instance → Connect → Session Manager.

```bash
sudo -iu ubuntu
git clone https://github.com/zak-510/omar-bench
sudo bash ~/omar-bench/infra/ec2/bootstrap.sh    # ends with "omar-bench bootstrap done"
exit; sudo -iu ubuntu                            # pick up the docker group
cd ~/omar-bench && cp .env.example .env
```

#### Step 3: Log in

```bash
claude setup-token    # paste the token into .env as CLAUDE_CODE_OAUTH_TOKEN
codex login           # only for Codex
```

In `.env`, set `OMAR_BENCH_OPERATOR` to your GitHub handle. Never commit `.env`.

#### Step 4: Check

```bash
uv run omar-bench doctor --topologies    # tools, login, versions; compiles the OMAR programs
```

#### Step 5: Run

```bash
STUDY=mine tmux new -d -s queue infra/ec2/queue.sh
tail -f ~/queue.log
```

Runs `fix-git` (15-minute limit) once per arm. After each attempt it copies results to the bucket. At the end it writes `~/queue-report.md` and stops the machine 30 minutes later.

Other tasks, arms, models or attempt counts:

```bash
STUDY=mine TASKS="fix-git prove-plus-comm" K=5 MODEL=haiku \
  tmux new -d -s queue infra/ec2/queue.sh
STUDY=mine SUITE=terminal-bench TASKS=bun-sourcemap-leak MODEL=sol \
  tmux new -d -s queue infra/ec2/queue.sh      # a Terminal-Bench 4.0 task
```

## Good to know

- **Subscription limits**: runs draw on the same allowance as your own daily use. The queue pauses at 80% of the five-hour window and stops at 85% of the week.
- **Watching a team work**: `docker ps`, then `docker exec -it <id> tmux ls` and `tmux attach -t <name>` inside it. `Ctrl-b d` leaves.
- **Interrupted run**: `uv run omar-bench ingest --interrupted runs/<run-id>` records the unfinished attempts as errors.
- **Big tasks**: Terminal-Bench 4.0 tasks allow 8 hours each, and some ask for up to 16 CPUs.
- **OMAR needs a recent base image**: Debian 12+ or Ubuntu 22.04+, not Alpine. A task on an older image has to be left out for every arm.
- **Cleaning up**: `aws cloudformation delete-stack --stack-name omar-bench` removes everything except the bucket.
