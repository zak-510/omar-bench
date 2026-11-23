//! The agent arm's delivery loop: it hands each addressed message to its
//! recipient, one at a time, reads the reply and records both. It never picks
//! a recipient and never decides when a run is over. The loop is separate from
//! the transport, so the same code runs against live agents (`Runner`),
//! recorded replies and scripted agents (`simulate`).

use crate::graph::Topology;
use crate::prompts;
use crate::protocol::{self, Act};
use crate::runner::{CustomSpec, GraphOpts};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;

/// Which reading of a reply applies. V2 is what agents are told
/// (`prompts::action_rules`) and what every new run uses. V1 is the original
/// study's, kept so a recorded run can be replayed as it ran.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, clap::ValueEnum)]
pub enum Rules {
    V1,
    #[default]
    V2,
}

/// One delivered message and the reply it got, in delivery order.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Delivery {
    pub from: usize,
    pub to: usize,
    pub body: String,
    pub reply: Option<String>,
    /// What the reply did: "send K", "finish", "wait" or "lost".
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub action: String,
    /// Harmless variations in how the reply was written.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub notes: Vec<String>,
    /// Mistakes in the reply that lost a message.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub errors: Vec<String>,
}

/// One request on its way to an agent.
#[derive(Debug, Clone)]
pub struct Request {
    pub from: usize,
    pub to: usize,
    /// The number the recipient sees on this request.
    pub round: usize,
    pub body: String,
    /// The full prompt: the recipient's job, this message and the action rules.
    pub payload: String,
}

/// Why an agent's reply never came.
#[derive(Debug, Clone, Default)]
pub struct Silence {
    /// The run's time limit passed while waiting.
    pub deadline: bool,
    /// How long the unanswered request had been waiting.
    pub waited_secs: u64,
    /// What the silent agent's terminal showed, when it could be read.
    pub evidence: Option<String>,
}

/// Whatever carries requests to agents and brings replies back.
pub trait Post {
    /// Called before a delivery to `to`; `next` is its likely successor.
    fn prepare(&mut self, _to: usize, _next: Option<usize>) -> Result<()> {
        Ok(())
    }
    /// Deliver one request; None when no reply came in time.
    fn deliver(&mut self, request: &Request) -> Result<Option<String>>;
    /// Deliver several requests at once (the scatter-gather task hand-out).
    fn wave(&mut self, requests: &[Request]) -> Result<Vec<Option<String>>> {
        requests.iter().map(|r| self.deliver(r)).collect()
    }
    /// Asked once, after a delivery to `to` got no reply.
    fn silence(&mut self, _to: usize) -> Silence {
        Silence::default()
    }
    /// Called with each delivery as soon as its reply is read.
    fn record(&mut self, _delivery: &Delivery) {}
}

/// How a run ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum End {
    /// An agent allowed to finish the run did.
    Done,
    /// No message was left to deliver and nobody had finished the run.
    Quiet,
    /// The delivery cap was reached.
    Fuse,
    /// An agent did not answer a delivered request before its reply timeout.
    NoReply,
    /// The run's time limit passed while an agent had not answered.
    Deadline,
}

/// What the loop reports; the runner wraps it into a `GraphResult`.
#[derive(Debug, Clone)]
pub struct Outcome {
    pub hops: usize,
    pub end: End,
    /// The agent that finished the run, or the one that went silent.
    pub end_node: Option<usize>,
    pub reason: String,
    pub output: Option<String>,
    pub messages: Vec<Delivery>,
    /// Messages still waiting when the run ended.
    pub undelivered: usize,
    /// Replies with a mistake that lost a message.
    pub errors: usize,
    /// For a silent agent: how long its request waited, and what its
    /// terminal showed.
    pub silent_secs: Option<u64>,
    pub evidence: Option<String>,
}

/// Whether agent `slot` is allowed to end the run.
pub fn may_finish(topology: Topology, custom: Option<&CustomSpec>, slot: usize) -> bool {
    match topology {
        Topology::Chain { n } => slot == n,
        Topology::Ring { .. } => true,
        Topology::ScatterGather { n, .. } => slot == n + 1,
        Topology::Custom { .. } => {
            custom.is_none_or(|s| s.finishers.is_empty() || s.finishers.contains(&slot))
        }
    }
}

/// The agents allowed to end the run, as an agent that is not one is told.
fn finishers(topology: Topology, custom: Option<&CustomSpec>, count: usize) -> String {
    let who: Vec<String> = (1..=count)
        .filter(|&slot| may_finish(topology, custom, slot))
        .map(|slot| slot.to_string())
        .collect();
    match who.len() {
        1 => format!("node {}", who[0]),
        _ => format!("nodes {}", who.join(", ")),
    }
}

/// Transit fault: mail from a corrupt scatter-gather worker is garbled in
/// flight. Same injection point in both arms, so faults stay arm-independent.
pub fn transit_body(topology: Topology, from: usize, opts: &GraphOpts, body: String) -> String {
    if let Topology::ScatterGather {
        n, corrupt_count, ..
    } = topology
    {
        if corrupt_count > 0 && from > n - corrupt_count && from <= n {
            return opts.corrupt().to_string();
        }
    }
    body
}

/// Likely-next recipient for spawn prefetch, from the topology's nominal
/// direction. A heuristic only: a wrong guess wastes one spawn, never mail.
pub fn next_slot_hint(topology: Topology, to: usize) -> Option<usize> {
    match topology {
        Topology::Chain { n } => (to < n).then_some(to + 1),
        Topology::Ring { n, .. } => Some((to % n) + 1),
        Topology::ScatterGather { .. } | Topology::Custom { .. } => None,
    }
}

/// The run's mutable state: the mail waiting, and how the run ended if it has.
struct State {
    /// (from, to, body) in the order sent; the loop never reorders it.
    queue: VecDeque<(usize, usize, String)>,
    done: Option<Option<String>>,
    errors: usize,
}

/// What stays fixed for a whole run.
struct Setup<'a> {
    rules: Rules,
    topology: Topology,
    opts: &'a GraphOpts,
    /// How many agents there are.
    count: usize,
    /// What the scatter-gather aggregator is asked.
    gather_q: String,
}

impl Setup<'_> {
    fn may_finish(&self, slot: usize) -> bool {
        may_finish(self.topology, self.opts.custom.as_ref(), slot)
    }

    /// Apply one reply from `from`. Returns what it did, for the record.
    fn apply(&self, content: &str, from: usize, state: &mut State) -> (String, Vec<String>, Vec<String>) {
        let (topology, opts) = (self.topology, self.opts);
        match self.rules {
            Rules::V1 => {
                let before = (state.queue.len(), state.done.is_some());
                enqueue_action(content, from, topology, opts, &mut state.queue, &mut state.done);
                let action = if state.done.is_some() && !before.1 {
                    "finish".to_string()
                } else if state.queue.len() > before.0 {
                    format!("send {}", state.queue.back().map_or(0, |m| m.1))
                } else {
                    "wait".to_string()
                };
                (action, Vec::new(), Vec::new())
            }
            Rules::V2 => {
                let reading = protocol::read_action(content, self.may_finish(from), self.count);
                let action = match reading.act {
                    // Corruption also hits a final result, else a corrupt worker
                    // ending the run hands over its answer with the fault never firing.
                    Act::Finish { result } => {
                        state.done = Some(result.map(|r| transit_body(topology, from, opts, r)));
                        "finish".to_string()
                    }
                    Act::Send { to, body } => {
                        state
                            .queue
                            .push_back((from, to, transit_body(topology, from, opts, body)));
                        format!("send {to}")
                    }
                    Act::Wait if reading.errors.is_empty() => "wait".to_string(),
                    Act::Wait => "lost".to_string(),
                };
                if !reading.errors.is_empty() {
                    state.errors += 1;
                }
                let text = |v: Vec<&'static str>| v.into_iter().map(str::to_string).collect();
                (action, text(reading.notes), text(reading.errors))
            }
        }
    }

    /// The full prompt for one delivery: the recipient's job, the message and
    /// the action rules that apply to it.
    fn payload(&self, from: usize, to: usize, body: &str) -> Result<String> {
        let how = match self.rules {
            Rules::V1 => prompts::ADDRESS_RULES_V1.to_string(),
            Rules::V2 => prompts::action_rules(
                self.may_finish(to),
                &finishers(self.topology, self.opts.custom.as_ref(), self.count),
            ),
        };
        Ok(match self.topology {
            Topology::Chain { n } => prompts::postal_chain(to, n, body, &how),
            Topology::Ring { n, .. } => prompts::postal_ring(to, n, body, &how),
            Topology::ScatterGather { n, .. } => {
                if to == n + 1 {
                    prompts::postal_aggregator(n, &self.gather_q, from, body, &how)
                } else {
                    prompts::postal_worker(to, n, body, &how)
                }
            }
            Topology::Custom { .. } => {
                let spec = self.opts.custom.as_ref().context("custom topology without a spec")?;
                prompts::postal_custom(&spec.prompts[to - 1], from, body, &how)
            }
        })
    }
}

/// The original study's handling of a reply: record a DONE, or enqueue its
/// send. A send addressed back to its own author with a DONE sign-off ends the
/// run: tails echo the incoming `To:` line, and delivering it would self-loop
/// the same reply until the fuse.
fn enqueue_action(
    content: &str,
    from: usize,
    topology: Topology,
    opts: &GraphOpts,
    queue: &mut VecDeque<(usize, usize, String)>,
    done: &mut Option<Option<String>>,
) {
    match protocol::parse_action(content) {
        protocol::Action::Done { body } => {
            *done = Some(body.map(|b| transit_body(topology, from, opts, b)))
        }
        protocol::Action::Wait => {}
        protocol::Action::Send {
            to,
            body,
            done: signed_off,
        } => {
            if signed_off && to == from {
                *done = Some(Some(transit_body(topology, from, opts, body)));
            } else {
                queue.push_back((from, to, transit_body(topology, from, opts, body)));
            }
        }
    }
}

/// Drive one agent-coordinated run to its end.
pub fn run(
    rules: Rules,
    topology: Topology,
    opts: &GraphOpts,
    question: &str,
    post: &mut dyn Post,
) -> Result<Outcome> {
    let count = topology.session_specs().len();
    let fuse = if opts.max_messages == 0 {
        3 * count + 10
    } else {
        opts.max_messages
    };
    let custom = matches!(topology, Topology::Custom { .. });
    let setup = Setup {
        rules,
        topology,
        opts,
        count,
        gather_q: opts.gather(question).to_string(),
    };
    let mut state = State {
        queue: VecDeque::new(),
        done: None,
        errors: 0,
    };
    let mut delivered = 0usize;
    let mut dead_letters = 0usize;
    let mut last_responder = 0usize;
    let mut silent: Option<(usize, Silence)> = None;
    let mut messages: Vec<Delivery> = Vec::new();
    let note = |post: &mut dyn Post, messages: &mut Vec<Delivery>, d: Delivery| {
        post.record(&d);
        messages.push(d);
    };

    // Seed: hand out the task (the experimenter's job, not coordination).
    // Chain and ring seed node 1; scatter-gather seeds all live workers in
    // one wave, since the task hand-out is not part of the protocol.
    match topology {
        Topology::Chain { .. } | Topology::Ring { .. } => {
            state.queue.push_back((0, 1, question.to_string()));
        }
        Topology::Custom { .. } => {
            let spec = opts.custom.as_ref().context("custom topology without a spec")?;
            for (to, body) in &spec.seeds {
                state.queue.push_back((0, *to, body.clone()));
            }
        }
        Topology::ScatterGather { n, fail_count, .. } => {
            let live: Vec<usize> = (1..=n).filter(|slot| *slot > fail_count).collect();
            let mut wave = Vec::with_capacity(live.len());
            for &slot in &live {
                wave.push(Request {
                    from: 0,
                    to: slot,
                    round: 0,
                    body: question.to_string(),
                    payload: setup.payload(0, slot, question)?,
                });
            }
            let replies = post.wave(&wave)?;
            for (reply, request) in replies.into_iter().zip(&wave) {
                let mut d = Delivery {
                    from: 0,
                    to: request.to,
                    body: request.body.clone(),
                    reply: reply.clone(),
                    action: String::new(),
                    notes: Vec::new(),
                    errors: Vec::new(),
                };
                if let Some(content) = reply {
                    delivered += 1;
                    last_responder = request.to;
                    (d.action, d.notes, d.errors) = setup.apply(&content, request.to, &mut state);
                }
                note(post, &mut messages, d);
            }
        }
    }

    let mut fuse_hit = false;
    let mut per_slot = vec![0usize; count + 1];
    while state.done.is_none() {
        let Some((from, to, body)) = state.queue.pop_front() else {
            break;
        };
        if delivered >= fuse {
            fuse_hit = true;
            state.queue.push_front((from, to, body));
            break;
        }
        // V2 never queues mail for a node that does not exist; V1 did, and
        // dropped it here.
        if to < 1 || to > count {
            dead_letters += 1;
            continue;
        }
        // Any addressable slot is deliverable: a non-resident recipient is
        // spawned on demand, so agent-chosen routing (including mistakes)
        // stays observable under a spawn window.
        post.prepare(to, next_slot_hint(topology, to))?;
        // A custom agent sees its own requests numbered 1, 2, 3...: a
        // run-wide count leaves gaps an agent can read as lost memory.
        let round = if custom {
            per_slot[to] += 1;
            per_slot[to]
        } else {
            delivered
        };
        let request = Request {
            from,
            to,
            round,
            payload: setup.payload(from, to, &body)?,
            body,
        };
        let reply = post.deliver(&request)?;
        let mut d = Delivery {
            from,
            to,
            body: request.body,
            reply: reply.clone(),
            action: String::new(),
            notes: Vec::new(),
            errors: Vec::new(),
        };
        match reply {
            Some(content) => {
                delivered += 1;
                last_responder = to;
                (d.action, d.notes, d.errors) = setup.apply(&content, to, &mut state);
                note(post, &mut messages, d);
            }
            None => {
                note(post, &mut messages, d);
                silent = Some((to, post.silence(to)));
                break;
            }
        }
    }

    let lost = dead_letters + state.errors;
    let (end, end_node, reason) = if state.done.is_some() {
        (
            End::Done,
            Some(last_responder),
            format!("done by node {last_responder}, {delivered} deliveries"),
        )
    } else if let Some((slot, silence)) = &silent {
        if silence.deadline {
            (
                End::Deadline,
                Some(*slot),
                format!("time limit: node {slot} had not answered after {delivered} deliveries"),
            )
        } else {
            (
                End::NoReply,
                Some(*slot),
                format!("stalled at node {slot} after {delivered} deliveries"),
            )
        }
    } else if fuse_hit {
        (End::Fuse, None, format!("fuse: {fuse} message cap hit"))
    } else if lost > 0 {
        let word = if rules == Rules::V1 { "undeliverable" } else { "lost" };
        (
            End::Quiet,
            None,
            format!("went quiet after {delivered} deliveries, {lost} {word}"),
        )
    } else {
        (
            End::Quiet,
            None,
            format!("went quiet after {delivered} deliveries"),
        )
    };
    Ok(Outcome {
        hops: delivered,
        end,
        end_node,
        reason,
        output: state.done.flatten(),
        messages,
        undelivered: state.queue.len(),
        errors: lost,
        silent_secs: silent.as_ref().map(|(_, s)| s.waited_secs),
        evidence: silent.and_then(|(_, s)| s.evidence),
    })
}

/// Replies for `simulate`: a recorded run played back, or scripted agents.
pub mod sim {
    use super::*;
    use std::io::{BufRead, BufReader, Write};
    use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

    /// Plays a recorded run's replies back in order. The loop is deterministic
    /// given the replies, so under the rules the run used it retraces the run
    /// exactly; under other rules it stops where the two part ways.
    pub struct Replay {
        log: Vec<Delivery>,
        next: usize,
        /// The first delivery that did not match the record, if any:
        /// (delivery number, what the record has, what this run asked for).
        pub diverged: Option<(usize, String, String)>,
        /// Deliveries whose message matched the record only once both had
        /// their `To:` and `Decision:` lines set aside.
        pub reworded: usize,
        /// Whether the record ran out while the run still wanted replies.
        pub exhausted: bool,
    }

    impl Replay {
        pub fn new(log: Vec<Delivery>) -> Replay {
            Replay {
                log,
                next: 0,
                diverged: None,
                reworded: 0,
                exhausted: false,
            }
        }

        /// Recorded deliveries this run never asked for.
        pub fn unused(&self) -> usize {
            self.log.len() - self.next
        }
    }

    impl Post for Replay {
        fn deliver(&mut self, request: &Request) -> Result<Option<String>> {
            if self.diverged.is_some() {
                return Ok(None);
            }
            let Some(rec) = self.log.get(self.next) else {
                self.exhausted = true;
                return Ok(None);
            };
            // The original rules delivered a message's control lines with it;
            // the same message without them is the same message.
            let plain = protocol::without_control_lines;
            let same_route = (request.from, request.to) == (rec.from, rec.to);
            if !same_route || plain(&request.body) != plain(&rec.body) {
                self.diverged = Some((
                    self.next + 1,
                    format!("{}->{} {:?}", rec.from, rec.to, rec.body),
                    format!("{}->{} {:?}", request.from, request.to, request.body),
                ));
                return Ok(None);
            }
            if request.body != rec.body {
                self.reworded += 1;
            }
            self.next += 1;
            Ok(rec.reply.clone())
        }
    }

    /// Scripted agents in another process: for each delivery it is sent one
    /// JSON line `{"n", "from", "to", "round", "body"}` and answers with one
    /// JSON line `{"reply": "..."}` (or `{"reply": null}` to stay silent).
    pub struct Script {
        child: Child,
        stdin: ChildStdin,
        stdout: BufReader<ChildStdout>,
        n: usize,
    }

    impl Script {
        pub fn start(command: &str) -> Result<Script> {
            let mut child = Command::new("sh")
                .arg("-c")
                .arg(command)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .spawn()
                .with_context(|| format!("starting the scripted agents: {command}"))?;
            let stdin = child.stdin.take().context("scripted agents have no stdin")?;
            let stdout = child.stdout.take().context("scripted agents have no stdout")?;
            Ok(Script {
                child,
                stdin,
                stdout: BufReader::new(stdout),
                n: 0,
            })
        }
    }

    impl Post for Script {
        fn deliver(&mut self, request: &Request) -> Result<Option<String>> {
            self.n += 1;
            let line = serde_json::json!({
                "n": self.n, "from": request.from, "to": request.to,
                "round": request.round, "body": request.body,
            });
            writeln!(self.stdin, "{line}").context("writing to the scripted agents")?;
            self.stdin.flush().context("writing to the scripted agents")?;
            let mut answer = String::new();
            let read = self
                .stdout
                .read_line(&mut answer)
                .context("reading from the scripted agents")?;
            if read == 0 {
                anyhow::bail!("the scripted agents exited at delivery {}", self.n);
            }
            let value: serde_json::Value = serde_json::from_str(answer.trim())
                .with_context(|| format!("scripted agents sent a bad line: {answer}"))?;
            Ok(value.get("reply").and_then(|r| r.as_str()).map(str::to_string))
        }
    }

    impl Drop for Script {
        fn drop(&mut self) {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Agents as a closure over (from, to, body), for tests.
    struct Agents<F: FnMut(usize, usize, &str) -> Option<String>>(F);

    impl<F: FnMut(usize, usize, &str) -> Option<String>> Post for Agents<F> {
        fn deliver(&mut self, r: &Request) -> Result<Option<String>> {
            Ok((self.0)(r.from, r.to, &r.body))
        }
    }

    /// Two workers and a coordinator (node 3): each worker reports once, and
    /// the coordinator finishes when it has heard from both.
    fn spec() -> GraphOpts {
        GraphOpts {
            max_messages: 20,
            custom: Some(CustomSpec {
                name: "toy".into(),
                prompts: vec!["w {sender} {body}".into(); 3],
                seeds: vec![(1, "go".into()), (2, "go".into())],
                finishers: vec![3],
            }),
            ..Default::default()
        }
    }

    fn toy(rules: Rules, mut coordinator: impl FnMut(usize) -> String) -> Outcome {
        let mut heard = 0;
        let mut agents = Agents(|_from, to, _body: &str| {
            Some(if to == 3 {
                heard += 1;
                coordinator(heard)
            } else {
                format!("To: node 3\n<answer>report {to}</answer>\nDecision: WAIT")
            })
        });
        run(rules, Topology::Custom { n: 3 }, &spec(), "", &mut agents).unwrap()
    }

    #[test]
    fn the_original_rules_end_a_self_addressed_done_instead_of_looping() {
        let opts = GraphOpts::default();
        let content = "To: node 20\n<answer>trail complete</answer>\nDecision: DONE";
        // Tail echoes its own address with DONE: run ends, nothing delivered.
        let mut queue = VecDeque::new();
        let mut done = None;
        enqueue_action(content, 20, Topology::Chain { n: 20 }, &opts, &mut queue, &mut done);
        assert!(queue.is_empty());
        assert_eq!(done, Some(Some("trail complete".into())));
        // Same reply from node 19 was a real forward; the sign-off rode along.
        let mut queue = VecDeque::new();
        let mut done = None;
        enqueue_action(content, 19, Topology::Chain { n: 20 }, &opts, &mut queue, &mut done);
        assert_eq!(queue.len(), 1);
        assert!(done.is_none());
    }

    #[test]
    fn transit_corruption_hits_only_corrupt_workers() {
        let sg = Topology::ScatterGather {
            n: 5,
            fail_count: 0,
            corrupt_count: 2,
            relaxed: false,
        };
        let opts = GraphOpts::default();
        // Slots 4 and 5 are corrupt; honest workers and the aggregator are not.
        assert_eq!(transit_body(sg, 4, &opts, "144".into()), "999");
        assert_eq!(transit_body(sg, 5, &opts, "144".into()), "999");
        assert_eq!(transit_body(sg, 3, &opts, "144".into()), "144");
        assert_eq!(transit_body(sg, 6, &opts, "final".into()), "final");
        // Chain mail is never touched.
        let chain = Topology::Chain { n: 5 };
        assert_eq!(transit_body(chain, 4, &opts, "x".into()), "x");
    }

    #[test]
    fn next_slot_hint_follows_topology_direction() {
        // Chain: successor, and no hint past the tail.
        assert_eq!(next_slot_hint(Topology::Chain { n: 5 }, 3), Some(4));
        assert_eq!(next_slot_hint(Topology::Chain { n: 5 }, 5), None);
        // Ring: wraps the last slot back to 1.
        let ring = Topology::Ring { n: 4, max_hops: 8 };
        assert_eq!(next_slot_hint(ring, 2), Some(3));
        assert_eq!(next_slot_hint(ring, 4), Some(1));
        // Scatter-gather and spec-defined runs have no direction to prefetch.
        assert_eq!(next_slot_hint(Topology::Custom { n: 6 }, 2), None);
    }

    #[test]
    fn a_run_ends_when_the_finisher_says_done() {
        let out = toy(Rules::V2, |heard| match heard {
            1 => "Decision: WAIT".into(),
            _ => "<answer>both in</answer>\nDecision: DONE".into(),
        });
        assert_eq!(out.end, End::Done);
        assert_eq!(out.end_node, Some(3));
        assert_eq!(out.hops, 4);
        assert_eq!(out.output.as_deref(), Some("both in"));
        assert_eq!(out.reason, "done by node 3, 4 deliveries");
        assert_eq!(out.messages.len(), 4);
        assert_eq!(out.messages[0].action, "send 3");
        assert_eq!(out.messages[3].action, "finish");
    }

    #[test]
    fn done_with_an_address_ends_the_run_under_v2_and_ran_on_under_v1() {
        // The coordinator finishes but keeps addressing the node it answers.
        let finish = |heard| match heard {
            1 => "Decision: WAIT".to_string(),
            2 => "To: node 2\n<answer>both in</answer>\nDecision: DONE".to_string(),
            _ => "Decision: WAIT".to_string(),
        };
        let v2 = toy(Rules::V2, finish);
        assert_eq!(v2.end, End::Done);
        assert_eq!(v2.hops, 4);
        assert_eq!(v2.messages[3].notes, vec!["To line ignored on DONE"]);
        // The original rules delivered the final report to node 2 and never
        // learned the run was over.
        let v1 = toy(Rules::V1, finish);
        assert_eq!(v1.end, End::Quiet);
        assert_eq!(v1.hops, 6);
        assert_eq!(v1.messages[4].body, "both in");
    }

    #[test]
    fn a_worker_cannot_end_the_run() {
        let mut agents = Agents(|_from, to, _body: &str| {
            Some(if to == 3 {
                "Decision: WAIT".into()
            } else {
                format!("To: node 3\n<answer>report {to}</answer>\nDecision: DONE")
            })
        });
        let out = run(Rules::V2, Topology::Custom { n: 3 }, &spec(), "", &mut agents).unwrap();
        // Both reports arrive; the coordinator never finishes, so the run goes quiet.
        assert_eq!(out.end, End::Quiet);
        assert_eq!(out.hops, 4);
        assert!(out.messages[0].notes[0].starts_with("DONE ignored"));
        // Under the original rules the first worker's sign-off rode along too.
        let mut agents = Agents(|_from, to, _body: &str| {
            Some(if to == 3 {
                "Decision: WAIT".into()
            } else {
                "<answer>report</answer>\nDecision: DONE".to_string()
            })
        });
        let v1 = run(Rules::V1, Topology::Custom { n: 3 }, &spec(), "", &mut agents).unwrap();
        assert_eq!((v1.end, v1.hops), (End::Done, 1));
    }

    #[test]
    fn mail_waiting_at_done_is_never_delivered() {
        // The coordinator finishes on the first report; the second is dropped.
        let out = toy(Rules::V2, |_| "<answer>early</answer>\nDecision: DONE".into());
        assert_eq!(out.end, End::Done);
        assert_eq!(out.hops, 3);
        assert_eq!(out.undelivered, 1);
        assert!(out.messages.iter().all(|m| m.body != "report 2"));
    }

    #[test]
    fn lost_mail_is_counted_and_the_run_goes_quiet() {
        let mut agents = Agents(|_from, to, _body: &str| {
            Some(match to {
                1 => "To: node 7\n<answer>report 1</answer>".into(),
                2 => "To: the coordinator\n<answer>report 2</answer>".into(),
                _ => "Decision: WAIT".into(),
            })
        });
        let out = run(Rules::V2, Topology::Custom { n: 3 }, &spec(), "", &mut agents).unwrap();
        assert_eq!(out.end, End::Quiet);
        assert_eq!(out.errors, 2);
        assert_eq!(out.reason, "went quiet after 2 deliveries, 2 lost");
        assert_eq!(out.messages[0].action, "lost");
        assert_eq!(out.messages[1].errors.len(), 1);
    }

    #[test]
    fn the_fuse_cuts_a_run_that_never_ends() {
        // Two nodes that answer each other forever.
        let opts = GraphOpts {
            max_messages: 9,
            custom: Some(CustomSpec {
                name: "ping".into(),
                prompts: vec!["p {sender} {body}".into(); 2],
                seeds: vec![(1, "go".into())],
                finishers: vec![2],
            }),
            ..Default::default()
        };
        let mut agents = Agents(|_from, to, _body: &str| {
            Some(format!("To: node {}\n<answer>ping</answer>", 3 - to))
        });
        let out = run(Rules::V2, Topology::Custom { n: 2 }, &opts, "", &mut agents).unwrap();
        assert_eq!(out.end, End::Fuse);
        assert_eq!(out.hops, 9);
        assert_eq!(out.undelivered, 1);
        assert_eq!(out.reason, "fuse: 9 message cap hit");
    }

    #[test]
    fn a_silent_agent_ends_the_run_and_names_it() {
        struct Mute;
        impl Post for Mute {
            fn deliver(&mut self, r: &Request) -> Result<Option<String>> {
                Ok((r.to == 1).then(|| "To: node 3\n<answer>report 1</answer>".to_string()))
            }
            fn silence(&mut self, _to: usize) -> Silence {
                Silence {
                    deadline: true,
                    waited_secs: 7000,
                    evidence: Some("usage limit reached".into()),
                }
            }
        }
        let out = run(Rules::V2, Topology::Custom { n: 3 }, &spec(), "", &mut Mute).unwrap();
        assert_eq!(out.end, End::Deadline);
        assert_eq!(out.end_node, Some(2));
        assert_eq!(out.hops, 1);
        assert_eq!(out.evidence.as_deref(), Some("usage limit reached"));
        assert_eq!(out.silent_secs, Some(7000));
        // The unanswered request is on the record, with no reply.
        assert_eq!(out.messages.len(), 2);
        assert!(out.messages[1].reply.is_none());
    }

    #[test]
    fn requests_are_numbered_per_agent_from_one() {
        let mut rounds: Vec<(usize, usize)> = Vec::new();
        struct Count<'a>(&'a mut Vec<(usize, usize)>, usize);
        impl Post for Count<'_> {
            fn deliver(&mut self, r: &Request) -> Result<Option<String>> {
                self.0.push((r.to, r.round));
                Ok(Some(if r.to == 3 {
                    self.1 += 1;
                    if self.1 == 2 {
                        "Decision: DONE".to_string()
                    } else {
                        "Decision: WAIT".to_string()
                    }
                } else {
                    "To: node 3\n<answer>r</answer>".to_string()
                }))
            }
        }
        run(
            Rules::V2,
            Topology::Custom { n: 3 },
            &spec(),
            "",
            &mut Count(&mut rounds, 0),
        )
        .unwrap();
        assert_eq!(rounds, vec![(1, 1), (2, 1), (3, 1), (3, 2)]);
    }

    #[test]
    fn every_agent_is_told_who_may_finish() {
        let opts = spec();
        let topology = Topology::Custom { n: 3 };
        let setup = Setup {
            rules: Rules::V2,
            topology,
            opts: &opts,
            count: 3,
            gather_q: String::new(),
        };
        let worker = setup.payload(0, 1, "go").unwrap();
        assert!(worker.contains("You cannot finish the run: only node 3 can"));
        assert!(!worker.contains("3. Finish the whole run"));
        let coordinator = setup.payload(1, 3, "report").unwrap();
        assert!(coordinator.contains("3. Finish the whole run"));
        assert!(!coordinator.contains("You cannot finish"));
        // A chain's tail finishes; a ring's every node may; the aggregator gathers.
        assert!(may_finish(Topology::Chain { n: 5 }, None, 5));
        assert!(!may_finish(Topology::Chain { n: 5 }, None, 4));
        assert!(may_finish(Topology::Ring { n: 4, max_hops: 8 }, None, 2));
        let sg = Topology::ScatterGather {
            n: 5,
            fail_count: 0,
            corrupt_count: 0,
            relaxed: false,
        };
        assert!(may_finish(sg, None, 6) && !may_finish(sg, None, 5));
    }

    #[test]
    fn scatter_gather_workers_report_and_only_the_aggregator_finishes() {
        let sg = Topology::ScatterGather {
            n: 3,
            fail_count: 0,
            corrupt_count: 1,
            relaxed: false,
        };
        let mut seen = 0;
        let mut agents = Agents(|_from, to, body: &str| {
            Some(if to == 4 {
                seen += 1;
                if seen < 3 {
                    "Decision: WAIT".to_string()
                } else {
                    format!("<answer>last heard {body}</answer>\nDecision: DONE")
                }
            } else {
                // A worker signing off with DONE used to end the whole run.
                "To: node 4\n<answer>144</answer>\nDecision: DONE".to_string()
            })
        });
        let out = run(Rules::V2, sg, &GraphOpts::default(), "12x12?", &mut agents).unwrap();
        assert_eq!(out.end, End::Done);
        assert_eq!(out.end_node, Some(4));
        assert_eq!(out.hops, 6);
        // Worker 3 is the corrupt one: its report is garbled in transit.
        assert_eq!(out.output.as_deref(), Some("last heard 999"));
    }

    #[test]
    fn a_chain_is_relayed_to_its_tail_which_alone_may_finish() {
        let chain = Topology::Chain { n: 4 };
        let opts = GraphOpts::default();
        let relay = |to: usize, body: &str| format!("{body} > node {to}");
        // Node 2 signs off with DONE as it forwards; only the tail's DONE counts.
        let mut agents = Agents(|_from, to, body: &str| {
            Some(match to {
                4 => format!("To: node 4\n<answer>{}</answer>\nDecision: DONE", relay(4, body)),
                2 => format!("To: node 3\n<answer>{}</answer>\nDecision: DONE", relay(2, body)),
                k => format!("To: node {}\n<answer>{}</answer>", k + 1, relay(k, body)),
            })
        });
        let out = run(Rules::V2, chain, &opts, "seed", &mut agents).unwrap();
        assert_eq!((out.end, out.end_node, out.hops), (End::Done, Some(4), 4));
        assert_eq!(out.output.as_deref(), Some("seed > node 1 > node 2 > node 3 > node 4"));
        assert!(out.messages[1].notes[0].starts_with("DONE ignored"));
        // A middle node that says DONE and sends nothing ends nothing: the chain breaks there.
        let mut agents = Agents(|_from, to, _body: &str| {
            Some(match to {
                2 => "<answer>finished</answer>\nDecision: DONE".to_string(),
                k => format!("To: node {}\n<answer>x</answer>", k + 1),
            })
        });
        let out = run(Rules::V2, chain, &opts, "seed", &mut agents).unwrap();
        assert_eq!((out.end, out.hops, out.output), (End::Quiet, 2, None));
        // The tail finishing with a 'To:' line that names no node: under the
        // original rules the result was mailed nowhere and the run never ended.
        let tail = |_from, to, _body: &str| {
            Some(match to {
                4 => "To: node 5\n<answer>all four</answer>\nDecision: DONE".to_string(),
                k => format!("To: node {}\n<answer>x</answer>", k + 1),
            })
        };
        let v2 = run(Rules::V2, chain, &opts, "seed", &mut Agents(tail)).unwrap();
        assert_eq!((v2.end, v2.output.as_deref()), (End::Done, Some("all four")));
        let v1 = run(Rules::V1, chain, &opts, "seed", &mut Agents(tail)).unwrap();
        assert_eq!(v1.end, End::Quiet);
        assert_eq!(v1.reason, "went quiet after 4 deliveries, 1 undeliverable");
    }

    #[test]
    fn any_node_of_a_ring_may_stop_it_and_the_cap_bounds_one_that_never_stops() {
        let ring = Topology::Ring { n: 3, max_hops: 6 };
        let opts = GraphOpts {
            max_messages: 6,
            ..Default::default()
        };
        // Passed on forever: cut at two laps.
        let mut agents = Agents(|_from, to, _body: &str| {
            Some(format!("To: node {}\n<answer>token</answer>\nDecision: WAIT", to % 3 + 1))
        });
        let out = run(Rules::V2, ring, &opts, "token", &mut agents).unwrap();
        assert_eq!((out.end, out.hops), (End::Fuse, 6));
        // Node 2 stops it on the second lap.
        let mut seen = 0;
        let mut agents = Agents(|_from, to, _body: &str| {
            seen += 1;
            Some(if to == 2 && seen > 3 {
                "<answer>stopped</answer>\nDecision: DONE".to_string()
            } else {
                format!("To: node {}\n<answer>token</answer>", to % 3 + 1)
            })
        });
        let out = run(Rules::V2, ring, &opts, "token", &mut agents).unwrap();
        assert_eq!((out.end, out.end_node, out.hops), (End::Done, Some(2), 5));
    }

    #[test]
    fn a_replay_retraces_a_recorded_run_and_reports_where_rules_part_ways() {
        let finish = |heard| match heard {
            1 => "Decision: WAIT".to_string(),
            2 => "To: node 2\n<answer>both in</answer>\nDecision: DONE".to_string(),
            _ => "Decision: WAIT".to_string(),
        };
        let recorded = toy(Rules::V1, finish);
        // Under the rules it ran with, the record is retraced to the end.
        let mut replay = sim::Replay::new(recorded.messages.clone());
        let again = run(Rules::V1, Topology::Custom { n: 3 }, &spec(), "", &mut replay).unwrap();
        assert_eq!(again.reason, recorded.reason);
        assert_eq!((replay.unused(), replay.exhausted), (0, false));
        assert!(replay.diverged.is_none());
        // Under V2 the same replies end the run at the DONE, two deliveries early.
        let mut replay = sim::Replay::new(recorded.messages.clone());
        let v2 = run(Rules::V2, Topology::Custom { n: 3 }, &spec(), "", &mut replay).unwrap();
        assert_eq!((v2.end, v2.hops), (End::Done, 4));
        assert_eq!(replay.unused(), 2);
        assert!(replay.diverged.is_none());
    }
}
