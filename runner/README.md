# Runner

The agent-arm runner, `omar-mass` (Rust), copied from the original study (see
[../probes/CHANGES.md](../probes/CHANGES.md) for what changed). Its `graph`
command starts OMAR agents with `spawn_agent` and delivers the addressed mail
they send each other, one delivery at a time, recording every delivery and
reply. It routes nothing itself: in the agent arm, the agents decide who hears
what. The probes use its chain, ring and scatter-gather topologies; Savina uses
`--kind custom --spec`, whose agents' prompts come from `savina/specs/`.

The crate also holds the original study's MASS and HARDMath commands (`run`,
`bench`, `grade`), and `data/` the problem set its unit tests read. This study
does not use them; they stay so the runner is the original study's code.

```sh
cargo build --release --locked && cargo test --release --locked
```
