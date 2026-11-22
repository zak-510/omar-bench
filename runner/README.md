# Runner

**`omar-mass`: the agent-arm runner. It delivers the mail agents address to each other, and routes nothing itself.**

The runner starts OMAR agents with `spawn_agent`, delivers each addressed
message one at a time, and records every delivery and reply. In the `agent`
arm, the agents decide who hears what. The code is the original study's; what
changed is in [CHANGES.md](../probes/CHANGES.md).

## Commands

| Command | Used for |
|---------|----------|
| `graph --kind chain\|ring\|scatter-gather` | The coordination probes |
| `graph --kind custom --spec FILE` | Savina, with each agent's prompt from `savina/specs/` |
| `run`, `bench`, `grade` | The original study's other experiments; kept, not used here |

## Usage

```bash
cargo build --release --locked
cargo test --release --locked
```
