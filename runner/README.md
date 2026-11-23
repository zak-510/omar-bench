# Runner

**`omar-mass`: the agent-arm runner. It delivers the mail agents address to each other, and routes nothing itself.**

The runner starts OMAR agents with `spawn_agent`, delivers each addressed
message one at a time, and records every delivery and reply. In the `agent`
arm, the agents decide who hears what and when the run is over. The code is
the original study's; what changed is in [CHANGES.md](../probes/CHANGES.md).

## Commands

| Command | Used for |
|---------|----------|
| `graph --kind chain\|ring\|scatter-gather` | The coordination probes |
| `graph --kind custom --spec FILE` | Savina, with each agent's prompt from `savina/specs/` |
| `simulate --spec FILE --agents CMD` | The same delivery loop against scripted agents, with no model |
| `simulate --spec FILE --replay LOG` | A recorded run played back, under `--rules v1` or `v2` |
| `run`, `bench`, `grade` | The original study's other experiments; kept, not used here |

## Action Rules

Every agent-coordinated prompt ends with the same rules (`action_rules` in
`src/prompts.rs`), and the delivery loop reads a reply by exactly those rules
(`read_action` in `src/protocol.rs`). A test checks each clause of the text
against the reader.

| A reply that | Does this |
|--------------|-----------|
| Has a line `To: node K` | **Send.** Delivers the text between `<answer>` and `</answer>` to node K, after the messages already waiting |
| Has no `To:` line | **Wait.** Sends nothing; the agent is called again when a message reaches it |
| Ends with `Decision: DONE`, from an agent that may finish | **Finish.** Ends the run at once, whatever else the reply holds. Messages still waiting are dropped |

- **Who may finish:** the last node of a chain, any node of a ring, the
  aggregator of a scatter-gather, and the agents a spec lists as `finishers`
  (the coordinator, in Savina). Every agent is told who may. `Decision: DONE`
  from any other agent ends nothing: its message, if it has one, is still sent.
- **One message per reply:** if a reply addresses two nodes, or one node with
  two messages, only the last is sent, and the reply is counted as having lost
  a message.
- **Control lines:** `To:` and `Decision:` lines are never part of a message.
  The last of each is the one that counts, and a decision is its first word.
  `Decision: WAIT` after a message changes nothing.
- **Addresses:** `node K`, or a bare number. An address that names no node,
  two nodes or a node that does not exist loses the message. Nothing is guessed.
- **Formatting:** emphasis, list or quote marks around a control line, an
  address inside the tags, and a message with no tags at all are read as what
  they plainly say. Each is noted on the delivery's record.

A run ends in one of five ways, recorded as `end`: `done`, `quiet` (no message
left and nobody finished), `fuse` (the delivery cap), `no-reply` (an agent did
not answer in time) or `deadline` (the run's time limit passed while waiting).
At its deadline the runner stops itself, keeps its record and ends its agents.

## Usage

```bash
cargo build --release --locked
cargo test --release --locked
```
