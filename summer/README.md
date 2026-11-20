# summer: the July–August 2026 coordination probes

The runner and probe programs from the summer study, copied so the
reproduction runs the same code. What changed and why is in
[CHANGES.md](CHANGES.md); the summer's own results are kept unchanged in
`results-2026-summer/`.

| Path | What it is |
|---|---|
| `src/` | The agent-arm runner (`omar-mass`, Rust). This study uses its `graph` command: OMAR agents coordinate by addressed mail, which the runner only delivers and records |
| `probes/sweep.py` | Agent-arm sweep: chain, ring, scatter-gather, missing workers |
| `probes/dsl/` | DSL-arm sweeps, and the generators that write every `.omar` program (`build_programs.sh`) |
| `results-2026-summer/` | The summer's CSVs and its write-up, as they were |

The crate also holds the summer's MASS and HARDMath commands (`run`,
`bench`, `grade`), and `data/` their problem set, which its unit tests read.
This study does not use them; they stay so the runner builds unchanged.

Build and test: `cargo build --release --locked && cargo test --release --locked`.
