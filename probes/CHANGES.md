# Changes from the original study

The runner (`../runner/`), the probes and their DSL programs are copied from
the original study of July–August 2026, with its results in
`original-results/` for reference. Only what follows was changed.

- **OMAR 0.4.4 syntax:** reactions are declared with `task`, not `prompt`.
  Applied to the generators; `probes/dsl/build_programs.sh` regenerates every
  program for the Claude, Codex and Stub backends. The original study's
  programs are byte-for-byte the generators' output, so nothing is hand-edited.
- **OMAR 0.4.4 runs in the background by default:** both DSL sweeps pass `--fg`
  so a run prints its outputs and cleans up its session.
- **One runtime for both arms:** the agent arm and the DSL arm both run on
  OMAR 0.4.4 (the original study used 0.4.0 and 0.4.1).
- **Codex:** the sweeps take a `sol` tier; the agent-arm sweep passes
  `--backend` through to the runner, which already supported it.
- **Retries, the same rule in both arms:** the agent arm used to retry stalls
  as well as boot failures and keep only the last attempt. Now either arm
  retries only a run whose agent never received its task (a failed boot, or
  the runtime failing to deliver the task) and logs every failed attempt to
  `*-attempts.csv`; a stall is an outcome.
- **Reply limit:** the agent arm waited 300 s for an agent's reply, while the
  DSL arm's runtime waits for a reaction until the run's timeout. The agent arm
  now waits as long; the reminders at 180 s and every 60 s are unchanged.
- **Timeouts are outcomes:** a run that hit the hard timeout used to be retried
  like a failed boot. It is now recorded as an outcome (`rc` = `timeout`).
- **Results:** written to the repo's `results/` (or `$RESULTS_DIR`), with each
  trial's start time and duration.
- **Added, for Savina:** `graph --kind custom --spec FILE`, a postal topology
  whose agents' prompts come from a file. The runner still only delivers
  addressed mail, now also logging every delivery and reply. Its agents get a
  charter that keeps earlier messages as memory instead of treating each one
  alone; each agent's requests are numbered from 1 in the order they reach it.
  Chain, ring and scatter-gather are unchanged.
- **Action rules (`prereg-v8`):** the original addressing rules did not say
  what a reply with both a `To:` line and `Decision: DONE` does, nor who may
  finish a run: the runner sent such a reply on as a message, and let any
  agent's `DONE` end the run. Every agent-arm prompt now ends with rules that
  say both, and the runner reads replies by exactly those rules
  ([runner README](../runner/README.md)). The original reading is kept as
  `simulate --rules v1`, to replay recorded runs as they ran.
- **Time limit and records (`prereg-v8`):** the runner stops itself at the
  run's time limit, keeps its record and ends its agents; a runner killed from
  outside used to lose its delivery log and leave its agents running. It
  appends every delivery to a journal as it happens, logs deliveries for the
  probes as well as Savina, waits for a whole reply file before reading it,
  and no longer starts a second, idle agent per slot in a `--kind custom` run.
- **Where agents start (`prereg-v8`):** the agent arm's agents started in the
  folder the runner was run from, a checkout of this repository, while the DSL
  arm's start in an empty workdir. `--workdir` now starts both in the same one.
- **Ring scoring:** see the correction in `PREREGISTRATION.md`.
- **Layout:** the runner moved to `runner/` and the probes to `probes/`, with
  the installed OMAR tools in `local/`; no code changed in the move.
