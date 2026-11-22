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
  alone, and that says requests are numbered across the whole run, so a gap in
  the numbers is not missing memory. Chain, ring and scatter-gather are
  unchanged.
- **Ring scoring:** see the correction in `PREREGISTRATION.md`.
- **Layout:** the runner moved to `runner/` and the probes to `probes/`, with
  the installed OMAR tools in `local/`; no code changed in the move.
