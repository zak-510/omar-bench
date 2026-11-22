# Agent-controlled vs DSL (Haiku 4.5)

Same tasks, corruption doses, trial counts, and pinned model in both
configurations. Prompts are mirrored verbatim except the coordination clauses
(routing instructions and ADDRESS_RULES); removing those clauses is the
treatment.

| Probe cell | Agent-controlled | DSL |
|---|---|---|
| Chain n=20 | 10/10 | 20/20 |
| Chain n=50 | 10/10 | 10/10 |
| Chain n=100 | 8/10 (2 husk deliveries) | 10/10 |
| Scatter-gather checkable n=5, all doses | 94/100 (1 flip, 5 empties) | 100/100 |
| Scatter-gather checkable n=9, all doses | 154/180 (11 flips, 15 empties) | 180/180 |
| Scatter-gather blind, below majority | 98/100 good | 100/100 good |
| Scatter-gather blind, at or above majority | 78/80 corrupt | 80/80 corrupt (expected majority behavior) |

## Failure modes

- **Husk**: correctly addressed envelope with the body missing; downstream
  nodes relay the empty message to the end. Seen twice in the agent-controlled
  chain at n=100.
- **Flip**: the checkable aggregator reports the planted 999 instead of the 144
  it could compute itself.
- **Empty**: no usable verdict; a refusal, a blank, or neither value.

All three are failures of protocol carried in the prompt, and all three reached
zero under the DSL: 0 husks in 1,000 hops, 0 flips or empties in 280
aggregations.

## Interpretation

- Strongly supported: runtime-owned coordination removes Haiku's coordination
  noise. 32 bad outcomes in 280 checkable trials under agent control, 0 in 280
  under the DSL. The blind majority threshold, which no coordinator can fix, is
  unchanged in both configurations, so the improvement localizes to what the
  runtime owns.
- Tier-dependent: Opus ran 0 flips in 280 trials under agent control; the
  runtime gives Haiku the reliability Opus gets from capability.
- Ring termination and missing-worker barriers need syntax v0.4.0 lacks;
  tested on 0.4.1 below.

Note: DSL n=100 ran as two 50-node halves for memory reasons; the seam (hop 50
to 51) was carried byte-exact and is invisible to agents.

## Rings and missing workers (runtime 0.4.1)

Runtime 0.4.1 (within deadlines, optional and either-or contracts) added the
missing syntax. Separate arm (`arm=dsl41`, own CSVs in `results/`, both
tiers), same doses and mirrored prompts; never pooled with the 0.4.0 table
above.

The agent-controlled ring is one family: agents own termination under a 2n
message cap. The DSL splits it in two: ring-judged gives each hop a legal stop
(contract `(fwd | done)`), ring-bounded removes the stop entirely and the
wiring ends the run after exactly 2 laps.

| Probe cell | Agent-controlled | DSL 0.4.1 |
|---|---|---|
| Ring n=10/25/40 | 16/30 never halt, 8/30 stop near one lap, 6/30 quit mid-lap | judged: 0 protocol failures in 30 (28 run the full horizon, 2 choose done); bounded: 30/30 exactly 2.0 laps |
| Missing workers, declared policy (all, quorum3) | 48/50 conformant | 50/50 conformant |
| Missing workers, no policy | unpredictable: 7/20 answer, 13/20 decline | 20/20 answer with what arrived |

The agent-controlled failure modes (silent mid-lap death, never halting,
policy coin flip) all reached zero. Mid-lap silence is structurally impossible
under the DSL: the contract requires a value every hop.

Tier note, from the dsl41 CSVs: give Opus the legal stop and it takes it 30/30
(Haiku 2/30). Remove the stop and Opus still ends the heartbeat 30/30, inside
the message body: halt banners downstream nodes co-sign, an emptied payload,
or a stripped directive. Ports were always set and every run delivered exactly
2.0 laps, so coordination stayed deterministic; the takeover is content only,
and trail counts alone miss it (read the output column). Haiku circulated
faithfully 30/30. The runtime deletes the coordination failures at both tiers;
what it cannot reach is what a capable model writes inside the message.