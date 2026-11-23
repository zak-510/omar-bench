//! MASS agent protocol: roles, message envelope, and answer parsing.
//! One shared envelope and parser, so any block's output can feed another's.

use serde::{Deserialize, Serialize};
use std::fmt;

/// Agent roles from the paper.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Role {
    Predictor,
    Aggregator,
    Reflector,
    Refiner,
    Debator,
    Summarizer,
    Executor,
    Grader,
    /// Generic worker for graph topologies (chain / ring / scatter-gather).
    Node,
    /// An agent of a spec-defined postal topology (the Savina benchmarks): it
    /// keeps its own state across the messages it receives.
    Actor,
}

impl Role {
    /// Short token used in agent/session names and file names.
    pub fn token(&self) -> &'static str {
        match self {
            Role::Predictor => "pred",
            Role::Aggregator => "agg",
            Role::Reflector => "refl",
            Role::Refiner => "refine",
            Role::Debator => "deb",
            Role::Summarizer => "sum",
            Role::Executor => "exec",
            Role::Grader => "grade",
            Role::Node => "node",
            Role::Actor => "actor",
        }
    }
}

impl fmt::Display for Role {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.token())
    }
}

/// Request envelope written to `<run_dir>/inbox/<id>.json`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Envelope {
    pub id: String,
    pub sender: String,
    pub receiver: String,
    pub timestamp_ns: u64,
    pub run_id: String,
    pub task_id: String,
    pub role: Role,
    pub round: usize,
    /// Fully rendered, self-contained role prompt.
    pub payload: String,
    /// Absolute path the agent must write its reply to.
    pub reply_path: String,
}

/// Reply parsed from the agent's reply file: only `content` is consumed.
/// Lenient: bare text is accepted as content so a quoting slip isn't lost.
#[derive(Debug, Clone, Deserialize)]
pub struct Reply {
    pub content: String,
}

impl Reply {
    pub fn parse(raw: &str) -> Reply {
        if let Ok(reply) = serde_json::from_str::<Reply>(raw) {
            if !reply.content.trim().is_empty() {
                return reply;
            }
        }
        // Some weak backends wrap the reply object in a JSON array
        // (`[{...}]`); unwrap to the first non-empty element.
        if let Ok(arr) = serde_json::from_str::<Vec<Reply>>(raw) {
            if let Some(reply) = arr.into_iter().find(|r| !r.content.trim().is_empty()) {
                return reply;
            }
        }
        Reply {
            content: raw.to_string(),
        }
    }
}

/// Extract the final answer from a role output. Prefers the last
/// `<answer>...</answer>` pair; falls back to the last `Answer:` line.
pub fn parse_answer(text: &str) -> Option<String> {
    let lower = text.to_lowercase();
    let mut last: Option<(usize, usize)> = None;
    let mut from = 0;
    while let Some(open_rel) = lower[from..].find("<answer>") {
        let start = from + open_rel + "<answer>".len();
        match lower[start..].find("</answer>") {
            Some(close_rel) => {
                last = Some((start, start + close_rel));
                from = start + close_rel + "</answer>".len();
            }
            None => break,
        }
    }
    // Indices come from the lowercased copy; get() avoids a panic if a
    // length-changing lowercase ever shifts them off a char boundary.
    if let Some(answer) = last.and_then(|(s, e)| text.get(s..e)).map(str::trim) {
        if !answer.is_empty() {
            return Some(answer.to_string());
        }
    }

    // Some models emit \answer{...} instead of the requested <answer> tags.
    if let Some(a) = extract_last_braced(text, "\\answer{") {
        return Some(a);
    }

    // Fallback: last "Answer:" line (DSPy output field).
    let mut fallback = None;
    for line in text.lines() {
        let trimmed = line.trim().trim_start_matches(['*', '#', ' ']);
        if let Some(rest) = strip_prefix_ci(trimmed, "answer:") {
            let rest = rest.trim();
            if !rest.is_empty() {
                fallback = Some(rest.to_string());
            }
        }
    }
    if fallback.is_some() {
        return fallback;
    }

    // Last resort: a `\boxed{...}` expression. Weaker models often skip the
    // requested tags/field but still box the final answer in LaTeX.
    extract_last_boxed(text)
}

/// If the LLM aggregator answered with a bare candidate reference ("Agent 4",
/// "Candidate 2:") instead of the answer itself, map it back to that 1-indexed
/// candidate's answer. Returns None when real answer text follows the number.
pub fn resolve_agent_reference(answer: &str, candidates: &[String]) -> Option<String> {
    let rest = strip_prefix_ci(answer.trim(), "agent")
        .or_else(|| strip_prefix_ci(answer.trim(), "candidate"))?
        .trim_start();
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        return None;
    }
    // Bare reference only: nothing alphanumeric may follow the number.
    if rest[digits.len()..].chars().any(|c| c.is_alphanumeric()) {
        return None;
    }
    let idx: usize = digits.parse().ok()?;
    candidates.get(idx.checked_sub(1)?).cloned()
}

/// Parse per-label grades ("A: 0.5, B: 0.0") from a batched judge reply, aligned
/// to 0-based label index (A=0). None where a label is missing or unparseable.
pub fn parse_label_scores(text: &str, n: usize) -> Vec<Option<f64>> {
    let body = parse_answer(text).unwrap_or_else(|| text.to_string());
    let chars: Vec<char> = body.chars().collect();
    let mut out = vec![None; n];
    for i in 0..chars.len() {
        let idx = match chars[i] {
            c if c.is_ascii_uppercase() => (c as u8 - b'A') as usize,
            _ => continue,
        };
        if idx >= n || (i > 0 && chars[i - 1].is_ascii_alphanumeric()) {
            continue;
        }
        let mut j = i + 1;
        while j < chars.len() && chars[j] == ' ' {
            j += 1;
        }
        if j >= chars.len() || (chars[j] != ':' && chars[j] != '=') {
            continue;
        }
        let rest: String = chars[j + 1..].iter().collect();
        if let Some(v) = parse_ratio_or_float(&rest) {
            out[idx] = Some(v.clamp(0.0, 1.0));
        }
    }
    out
}

/// Parse the judge's 0-1 score, clamped to [0,1]. Strict score contract:
/// prefer the requested `<answer>`/`\boxed` payload, then a labeled
/// "grade/score", then a bare leading number. Handles fractions (`1/2` -> 0.5)
/// and returns None only on a genuine parse failure (so a true 0.0 is kept).
pub fn parse_score(text: &str) -> Option<f64> {
    let value = parse_answer(text)
        .as_deref()
        .and_then(parse_ratio_or_float)
        .or_else(|| labeled_score(text))
        .or_else(|| parse_ratio_or_float(text))?;
    Some(value.clamp(0.0, 1.0))
}

/// First numeric token of `s` as a float, supporting an `a/b` fraction so a
/// grade like `1/2` reads as 0.5 rather than truncating to 1.
fn parse_ratio_or_float(s: &str) -> Option<f64> {
    let start = s.find(|c: char| c.is_ascii_digit() || c == '-')?;
    let num_str: String = s[start..]
        .chars()
        .take_while(|c| c.is_ascii_digit() || *c == '.' || *c == '-')
        .collect();
    let num: f64 = num_str.parse().ok()?;
    let rest = s[start + num_str.len()..].trim_start();
    if let Some(after_slash) = rest.strip_prefix('/') {
        let den_str: String = after_slash
            .trim_start()
            .chars()
            .take_while(|c| c.is_ascii_digit() || *c == '.')
            .collect();
        if let Ok(den) = den_str.parse::<f64>() {
            if den != 0.0 {
                return Some(num / den);
            }
        }
    }
    Some(num)
}

/// The number following the last "grade"/"score" keyword, for graders that emit
/// a bare/labeled grade (e.g. "Grade: 0.5", "The score is 0.5") without tags.
fn labeled_score(text: &str) -> Option<f64> {
    let lower = text.to_lowercase();
    let mut best = None;
    for kw in ["grade", "score"] {
        let mut from = 0;
        while let Some(rel) = lower[from..].find(kw) {
            let after = from + rel + kw.len();
            // get() (vs slicing) can't panic if a multibyte head shifts offsets.
            if let Some(v) = text.get(after..).and_then(parse_ratio_or_float) {
                best = Some(v); // last keyword wins (the final verdict)
            }
            from = after;
        }
    }
    best
}

/// Content of the last brace-balanced `\boxed{...}`, if any.
fn extract_last_boxed(text: &str) -> Option<String> {
    extract_last_braced(text, "\\boxed{")
}

/// Content of the last brace-balanced `<tag>...}` where `tag` ends in `{`
/// (e.g. `"\\boxed{"`). Handles nested braces; UTF-8 safe (matches ASCII only).
fn extract_last_braced(text: &str, tag: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut result = None;
    let mut search_from = 0;
    while let Some(rel) = text[search_from..].find(tag) {
        let open = search_from + rel + tag.len();
        let mut depth = 1usize;
        let mut i = open;
        while i < bytes.len() {
            match bytes[i] {
                b'{' => depth += 1,
                b'}' => {
                    depth -= 1;
                    if depth == 0 {
                        break;
                    }
                }
                _ => {}
            }
            i += 1;
        }
        if depth != 0 {
            break; // unbalanced; give up
        }
        let inner = text[open..i].trim();
        if !inner.is_empty() {
            result = Some(inner.to_string());
        }
        search_from = i + 1;
    }
    result
}

/// Reflector output: a correctness verdict plus free-form feedback.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reflection {
    pub correct: bool,
    pub feedback: String,
}

/// Parse a reflector output: `correct` is true when the last `Correctness:`
/// line says "true"; feedback is the last `Feedback:` section, else whole text.
pub fn parse_reflection(text: &str) -> Reflection {
    let mut correct = false;
    let mut feedback_start: Option<usize> = None;
    let mut offset = 0;
    for line in text.split_inclusive('\n') {
        let trimmed = line.trim().trim_start_matches(['*', '#', ' ']);
        if let Some(rest) = strip_prefix_ci(trimmed, "correctness:") {
            correct = rest.to_lowercase().contains("true");
        }
        if strip_prefix_ci(trimmed, "feedback:").is_some() {
            feedback_start = Some(offset);
        }
        offset += line.len();
    }
    let feedback = match feedback_start {
        Some(start) => {
            let section = &text[start..];
            // Cut a trailing Correctness line out of the feedback.
            match find_ci(section, "correctness:") {
                Some(pos) => section[..pos].trim().to_string(),
                None => section.trim().to_string(),
            }
        }
        None => text.trim().to_string(),
    };
    Reflection { correct, feedback }
}

/// Graph node output: the message to forward plus a stop vote (ring termination).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeOutput {
    pub message: Option<String>,
    pub stop: bool,
}

/// Parse a graph node reply: `message` is the `<answer>` payload; `stop` is true
/// when the last `Decision:` line says "stop".
pub fn parse_node(text: &str) -> NodeOutput {
    let mut stop = false;
    for line in text.lines() {
        let trimmed = line.trim().trim_start_matches(['*', '#', ' ']);
        if let Some(rest) = strip_prefix_ci(trimmed, "decision:") {
            stop = rest.to_lowercase().contains("stop");
        }
    }
    NodeOutput {
        message: parse_answer(text),
        stop,
    }
}

/// What an agent's reply asks the postal runtime to do next, as the original
/// study read it. Kept only to replay recorded runs under the rules they ran
/// with (`Rules::V1`); new runs use `read_action`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Action {
    /// Deliver `body` to node `to`. `done` records a DONE/STOP sign-off in the
    /// same reply; the delivery layer uses it to end the run instead of
    /// delivering when the send is addressed back to its own author.
    Send { to: usize, body: String, done: bool },
    /// End the run with `body` as the final output.
    Done { body: Option<String> },
    /// No send this turn; keep delivering queued mail.
    Wait,
}

/// Parse a reply into an Action. Last `To:`/`Decision:` lines win. A send, when
/// present, is executed; DONE or STOP ends the run only when nothing was sent
/// (a sign-off after a send means the agent's own part is finished, and agents
/// add one routinely). The sign-off still rides along in `Send::done` so the
/// delivery layer, which knows the author, can end the run on a self-addressed
/// send (tails echo the incoming `To:` line and would otherwise self-loop).
/// No address and no decision counts as Wait.
pub fn parse_action(text: &str) -> Action {
    let mut to: Option<usize> = None;
    let mut done = false;
    for line in text.lines() {
        let trimmed = line.trim().trim_start_matches(['*', '#', ' ']);
        if let Some(rest) = strip_prefix_ci(trimmed, "to:") {
            to = first_uint(rest);
        }
        if let Some(rest) = strip_prefix_ci(trimmed, "decision:") {
            let rest = rest.to_lowercase();
            done = rest.contains("done") || rest.contains("stop");
        }
    }
    if let Some(to) = to {
        Action::Send {
            to,
            // Same fallback the runtime arm uses: no tags means the whole reply.
            body: parse_answer(text).unwrap_or_else(|| text.to_string()),
            done,
        }
    } else if done {
        Action::Done {
            body: parse_answer(text),
        }
    } else {
        Action::Wait
    }
}

/// One of the three things a reply can do under the action rules that agents
/// are given (`prompts::action_rules`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Act {
    /// Deliver `body` to node `to`.
    Send { to: usize, body: String },
    /// End the run with `result` as its final output.
    Finish { result: Option<String> },
    /// Send nothing this turn.
    Wait,
}

/// How a reply was read: what it does, the harmless variations it used
/// (`notes`), and the mistakes that lost a message (`errors`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Reading {
    pub act: Act,
    pub notes: Vec<&'static str>,
    pub errors: Vec<&'static str>,
}

/// A line the delivery service reads rather than delivers.
enum Control<'a> {
    To(&'a str),
    Decision(&'a str),
}

/// `To:` and `Decision:` lines, with or without list, quote or emphasis marks
/// around the keyword ("**To:** node 3", "- To: node 3", "> Decision: WAIT").
fn control(line: &str) -> Option<Control<'_>> {
    let marks = ['*', '#', '>', '-', '_', '`', ' ', '\t'];
    let t = line.trim().trim_start_matches(marks);
    for (word, to) in [("to", true), ("decision", false)] {
        let Some(rest) = strip_prefix_ci(t, word) else {
            continue;
        };
        let rest = rest.trim_start_matches(['*', '_', '`', ' ']);
        if let Some(rest) = rest.strip_prefix(':') {
            let rest = rest.trim_matches(marks);
            return Some(if to {
                Control::To(rest)
            } else {
                Control::Decision(rest)
            });
        }
    }
    None
}

/// The node an address names: "node 3" anywhere in it, or a bare number.
/// None when it names no node or more than one, which is never guessed at.
fn address(rest: &str) -> Option<usize> {
    let lower = rest.to_lowercase();
    let mut named: Vec<usize> = Vec::new();
    let mut from = 0;
    while let Some(at) = lower[from..].find("node") {
        let after = &lower[from + at + 4..];
        from += at + 4;
        let digits: String = after
            .trim_start_matches([' ', '#'])
            .chars()
            .take_while(char::is_ascii_digit)
            .collect();
        // "nodes 1 and 2" is not an address: the word must be exactly "node".
        if after.starts_with(|c: char| c.is_alphabetic()) {
            continue;
        }
        if let Ok(k) = digits.parse() {
            if !named.contains(&k) {
                named.push(k);
            }
        }
    }
    match named.len() {
        1 => Some(named[0]),
        0 => {
            let bare = rest.trim_matches(|c: char| !c.is_ascii_alphanumeric());
            (!bare.is_empty() && bare.chars().all(|c| c.is_ascii_digit()))
                .then(|| bare.parse().ok())
                .flatten()
        }
        _ => None,
    }
}

/// The text with every answer tag on a line of its own, so that a control
/// line sharing a line with a tag ("</answer>Decision: DONE") is still a line.
fn tags_on_own_lines(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len() + 16);
    let mut i = 0;
    let mut copied = 0;
    while i < bytes.len() {
        let tag = ["</answer>", "<answer>"]
            .into_iter()
            .find(|t| bytes[i..].len() >= t.len() && bytes[i..i + t.len()].eq_ignore_ascii_case(t.as_bytes()));
        match tag {
            // The tags are ASCII, so both cuts fall on character boundaries.
            Some(t) => {
                out.push_str(&text[copied..i]);
                out.push('\n');
                out.push_str(&text[i..i + t.len()]);
                out.push('\n');
                i += t.len();
                copied = i;
            }
            None => i += 1,
        }
    }
    out.push_str(&text[copied..]);
    out
}

/// The text with its control lines and any stray answer tag removed: they are
/// instructions to the delivery service, never part of a message.
pub fn without_control_lines(text: &str) -> String {
    tags_on_own_lines(text)
        .lines()
        .filter(|line| {
            let tag = line.trim();
            control(line).is_none()
                && !tag.eq_ignore_ascii_case("<answer>")
                && !tag.eq_ignore_ascii_case("</answer>")
        })
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_string()
}

/// Contents of every non-empty `<answer>...</answer> block, in order.
fn answer_blocks(text: &str) -> Vec<&str> {
    let lower = text.to_lowercase();
    let mut blocks = Vec::new();
    let mut from = 0;
    while let Some(open) = lower[from..].find("<answer>") {
        let start = from + open + "<answer>".len();
        let Some(close) = lower[start..].find("</answer>") else {
            break;
        };
        if let Some(block) = text.get(start..start + close) {
            if !block.trim().is_empty() {
                blocks.push(block);
            }
        }
        from = start + close + "</answer>".len();
    }
    blocks
}

/// Read a reply under the action rules. `may_finish` is whether its author is
/// allowed to end the run; `nodes` is how many agents there are.
///
/// - `Decision: DONE` from an agent that may finish ends the run, whatever
///   else the reply holds. From any other agent it is ignored.
/// - Otherwise a `To:` line sends one message: the last `<answer>` block, or
///   without tags the reply itself, in both cases minus its control lines.
///   The last `To:` line is the address. A reply that addresses two nodes, or
///   one node twice with two messages, loses all but the last.
/// - Otherwise nothing is sent.
pub fn read_action(text: &str, may_finish: bool, nodes: usize) -> Reading {
    let mut notes: Vec<&'static str> = Vec::new();
    let mut errors: Vec<&'static str> = Vec::new();
    let mut addresses: Vec<Option<usize>> = Vec::new();
    let mut decision: Option<String> = None;
    for line in tags_on_own_lines(text).lines() {
        match control(line) {
            Some(Control::To(rest)) => addresses.push(address(rest)),
            Some(Control::Decision(rest)) => {
                decision = Some(
                    rest.chars()
                        .take_while(|c| c.is_alphabetic())
                        .collect::<String>()
                        .to_uppercase(),
                )
            }
            None => {}
        }
    }
    let wants_done = match decision.as_deref() {
        Some("DONE") => true,
        Some("STOP") => {
            notes.push("STOP read as DONE");
            true
        }
        Some("WAIT") | None => false,
        Some(_) => {
            notes.push("unknown decision ignored");
            false
        }
    };
    if wants_done && may_finish {
        if !addresses.is_empty() {
            notes.push("To line ignored on DONE");
        }
        return Reading {
            act: Act::Finish {
                result: parse_answer(text),
            },
            notes,
            errors,
        };
    }
    if wants_done {
        notes.push("DONE ignored: this agent cannot finish the run");
    }
    let Some(last) = addresses.last() else {
        return Reading {
            act: Act::Wait,
            notes,
            errors,
        };
    };
    let blocks = answer_blocks(text);
    let mut distinct: Vec<Option<usize>> = Vec::new();
    for a in &addresses {
        if !distinct.contains(a) {
            distinct.push(*a);
        }
    }
    if distinct.len() > 1 || (addresses.len() > 1 && blocks.len() > 1) {
        errors.push("more than one message: only the last was sent");
    } else if addresses.len() > 1 {
        notes.push("address repeated");
    } else if blocks.len() > 1 {
        notes.push("several answer blocks: the last is the message");
    }
    let body = match blocks.last() {
        Some(block) => without_control_lines(block),
        None => {
            notes.push("message not in answer tags");
            without_control_lines(text)
        }
    };
    let act = match last {
        Some(to) if (1..=nodes).contains(to) => {
            if body.is_empty() {
                notes.push("empty message");
            }
            Act::Send { to: *to, body }
        }
        Some(_) => {
            errors.push("message lost: no such node");
            Act::Wait
        }
        None => {
            errors.push("message lost: address names no single node");
            Act::Wait
        }
    };
    Reading { act, notes, errors }
}

/// First unsigned integer in a string ("node 3" -> 3).
fn first_uint(s: &str) -> Option<usize> {
    let digits: String = s
        .chars()
        .skip_while(|c| !c.is_ascii_digit())
        .take_while(|c| c.is_ascii_digit())
        .collect();
    digits.parse().ok()
}

fn strip_prefix_ci<'a>(s: &'a str, prefix: &str) -> Option<&'a str> {
    // get(..len) is None at a non-char-boundary, so multibyte heads can't panic.
    let head = s.get(..prefix.len())?;
    head.eq_ignore_ascii_case(prefix)
        .then(|| &s[prefix.len()..])
}

fn find_ci(haystack: &str, needle: &str) -> Option<usize> {
    haystack.to_lowercase().find(&needle.to_lowercase())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_action_reads_send_done_wait() {
        let send = parse_action("fwd\nTo: node 3\n<answer>payload</answer>");
        assert_eq!(
            send,
            Action::Send {
                to: 3,
                body: "payload".into(),
                done: false
            }
        );
        // A send is executed even with a DONE sign-off tacked on (the sign-off
        // is surfaced in `done`); DONE alone ends the run, STOP is a synonym.
        let sent = parse_action("To: node 2\n<answer>144</answer>\nDecision: DONE");
        assert_eq!(
            sent,
            Action::Send {
                to: 2,
                body: "144".into(),
                done: true
            }
        );
        assert!(matches!(
            parse_action("<answer>144</answer>\nDecision: DONE"),
            Action::Done { body: Some(_) }
        ));
        assert!(matches!(
            parse_action("Decision: STOP"),
            Action::Done { body: None }
        ));
        // Explicit wait and unparseable garbage both mean no send.
        assert_eq!(parse_action("Decision: WAIT"), Action::Wait);
        assert_eq!(parse_action("thinking out loud, no address"), Action::Wait);
        // Bare "To: 7" works; last To line wins.
        assert!(matches!(
            parse_action("To: 2\nTo: 7\n<answer>x</answer>"),
            Action::Send { to: 7, .. }
        ));
    }

    fn send(to: usize, body: &str) -> Act {
        Act::Send {
            to,
            body: body.into(),
        }
    }

    #[test]
    fn read_action_send_wait_finish() {
        // The three actions, written as the rules show them.
        let r = read_action("To: node 3\n<answer>eat</answer>\nDecision: WAIT", false, 6);
        assert_eq!(r.act, send(3, "eat"));
        assert!(r.notes.is_empty() && r.errors.is_empty(), "{r:?}");
        let r = read_action("Decision: WAIT", false, 6);
        assert_eq!(r.act, Act::Wait);
        assert!(r.notes.is_empty() && r.errors.is_empty());
        let r = read_action("<answer>all done</answer>\nDecision: DONE", true, 6);
        assert_eq!(
            r.act,
            Act::Finish {
                result: Some("all done".into())
            }
        );
        assert!(r.notes.is_empty() && r.errors.is_empty());
        // A send needs no decision line, and silence is a wait.
        assert_eq!(
            read_action("To: node 1\n<answer>eat</answer>", false, 6).act,
            send(1, "eat")
        );
        assert_eq!(read_action("thinking aloud", false, 6).act, Act::Wait);
    }

    #[test]
    fn done_from_the_finisher_ends_the_run_even_with_an_address() {
        // The reply that ran a finished BoundedBuffer trial to its cut: the
        // manager reported every answer, said DONE, and kept its habit of
        // addressing the node it was answering.
        let reply = "To: node 4\n<answer>\nP1-1: 56\nP3-10: 74\n</answer>\nDecision: DONE";
        let r = read_action(reply, true, 6);
        assert_eq!(
            r.act,
            Act::Finish {
                result: Some("P1-1: 56\nP3-10: 74".into())
            }
        );
        assert_eq!(r.notes, vec!["To line ignored on DONE"]);
        assert!(r.errors.is_empty());
        // The original rules sent it to node 4 instead.
        assert!(matches!(
            parse_action(reply),
            Action::Send {
                to: 4,
                done: true,
                ..
            }
        ));
    }

    #[test]
    fn done_from_anyone_else_ends_nothing() {
        // A worker's sign-off rides on a real message: the message is sent.
        let r = read_action(
            "To: node 6\n<answer>done meal 6: 72</answer>\nDecision: DONE",
            false,
            6,
        );
        assert_eq!(r.act, send(6, "done meal 6: 72"));
        assert_eq!(
            r.notes,
            vec!["DONE ignored: this agent cannot finish the run"]
        );
        // With nothing addressed, nothing is sent and the run goes on.
        let r = read_action("<answer>finished</answer>\nDecision: DONE", false, 6);
        assert_eq!(r.act, Act::Wait);
        assert_eq!(r.notes.len(), 1);
    }

    #[test]
    fn decision_is_its_first_word_not_a_substring() {
        // "not done yet" used to end the run.
        let r = read_action("Decision: WAIT (not done yet)", true, 6);
        assert_eq!(r.act, Act::Wait);
        assert!(matches!(
            parse_action("Decision: WAIT (not done yet)"),
            Action::Done { .. }
        ));
        // The last decision line wins; an unknown word decides nothing.
        let r = read_action("Decision: DONE\nDecision: WAIT", true, 6);
        assert_eq!(r.act, Act::Wait);
        let r = read_action("To: node 2\n<answer>x</answer>\nDecision: SEND", true, 6);
        assert_eq!(r.act, send(2, "x"));
        assert_eq!(r.notes, vec!["unknown decision ignored"]);
        assert!(matches!(
            read_action("Decision: stop", true, 6).act,
            Act::Finish { .. }
        ));
    }

    #[test]
    fn formatting_around_control_lines_is_harmless() {
        for reply in [
            "**To:** node 3\n<answer>eat</answer>",
            "**To**: node 3\n<answer>eat</answer>",
            "- To: node 3\n<answer>eat</answer>",
            "> to: Node 3\n<ANSWER>eat</ANSWER>",
            "To: 3\n<answer>eat</answer>",
            "To: node 3 (philosopher 3)\n<answer>eat</answer>\n**Decision: WAIT**",
            "To: the philosopher at node 3\n<answer>\neat\n</answer>",
        ] {
            let r = read_action(reply, false, 6);
            assert_eq!(r.act, send(3, "eat"), "{reply}");
            assert!(r.errors.is_empty(), "{reply}");
        }
    }

    #[test]
    fn control_lines_are_never_part_of_a_message() {
        // An address repeated inside the tags, as Claude often writes it.
        let r = read_action(
            "To: node 6\ncredited T1\n\n<answer>\nTo: node 6\ncredited T1\n</answer>\nDecision: WAIT",
            false,
            6,
        );
        assert_eq!(r.act, send(6, "credited T1"));
        assert_eq!(r.notes, vec!["address repeated"]);
        let r = read_action("<answer>\nTo: node 3\ndenied\n</answer>\nDecision: WAIT", true, 6);
        assert_eq!(r.act, send(3, "denied"));
        // The same with the tags on the control lines' own lines.
        let r = read_action("<answer>To: node 3\ndenied</answer>Decision: WAIT", true, 6);
        assert_eq!(r.act, send(3, "denied"));
        let r = read_action("<answer>all fed</answer>Decision: DONE", true, 6);
        assert!(matches!(r.act, Act::Finish { .. }));
        // An unclosed tag does not leak into the message, nor break on non-ASCII text.
        let r = read_action("To: node 3\n<answer>İ eat ε", true, 6);
        assert_eq!(r.act, send(3, "İ eat ε"));
        // No tags: the reply is the message, minus its control lines.
        let r = read_action("Node 1 reported.\nTo: node 2\nreport balance\nDecision: WAIT", true, 6);
        assert_eq!(r.act, send(2, "Node 1 reported.\nreport balance"));
        assert_eq!(r.notes, vec!["message not in answer tags"]);
    }

    #[test]
    fn a_second_message_in_one_reply_is_lost_and_counted() {
        // Two messages to one node: the first is lost.
        let r = read_action(
            "To: node 6\n<answer>done meal 1: 56</answer>\nTo: node 6\n<answer>hungry</answer>",
            false,
            6,
        );
        assert_eq!(r.act, send(6, "hungry"));
        assert_eq!(r.errors, vec!["more than one message: only the last was sent"]);
        // Two nodes addressed: only the last hears anything.
        let r = read_action("To: node 1\nTo: node 2\n<answer>eat</answer>", true, 6);
        assert_eq!(r.act, send(2, "eat"));
        assert_eq!(r.errors.len(), 1);
    }

    #[test]
    fn a_bad_address_loses_the_message_and_is_never_guessed() {
        for reply in [
            "To: node 9\n<answer>hungry</answer>",
            "To: node 0\n<answer>hungry</answer>",
            "To: the arbitrator\n<answer>hungry</answer>",
            "To: nodes 1 and 2\n<answer>eat</answer>",
            "To: node 1, node 2\n<answer>eat</answer>",
            "To: consumer 1\n<answer>take P1-1</answer>",
        ] {
            let r = read_action(reply, false, 6);
            assert_eq!(r.act, Act::Wait, "{reply}");
            assert_eq!(r.errors.len(), 1, "{reply}");
        }
        // A message with no address at all is a wait: nothing says where it goes.
        let r = read_action("<answer>denied T6</answer>\nDecision: WAIT", false, 6);
        assert_eq!(r.act, Act::Wait);
        assert!(r.errors.is_empty());
    }

    #[test]
    fn parse_action_body_falls_back_to_full_text() {
        let a = parse_action("To: node 2\nraw message with no tags");
        match a {
            Action::Send { to, body, .. } => {
                assert_eq!(to, 2);
                assert!(body.contains("raw message"));
            }
            other => panic!("expected Send, got {other:?}"),
        }
    }

    #[test]
    fn parse_answer_takes_last_tag_pair() {
        let text = "draft <answer>3</answer> revised <answer>64</answer> done";
        assert_eq!(parse_answer(text).as_deref(), Some("64"));
    }

    #[test]
    fn parse_label_scores_reads_per_label_grades() {
        let r = parse_label_scores("<answer>A: 0.5, B: 0.0, C: 1</answer>", 3);
        assert_eq!(r, vec![Some(0.5), Some(0.0), Some(1.0)]);
        // Tolerates "Answer X:" prefixes and newlines; missing label stays None.
        let r = parse_label_scores("Answer A = 0.25\nAnswer C = 0.75", 3);
        assert_eq!(r, vec![Some(0.25), None, Some(0.75)]);
        // Fractions and clamping.
        assert_eq!(parse_label_scores("A: 1/2", 1), vec![Some(0.5)]);
    }

    #[test]
    fn resolve_agent_reference_recovers_bare_picks_only() {
        let cands = vec![
            "x = 1".to_string(),
            "x = 2".to_string(),
            "x = 3".to_string(),
        ];
        // Bare references (the bug) map back to the chosen candidate.
        assert_eq!(
            resolve_agent_reference("Agent 3", &cands).as_deref(),
            Some("x = 3")
        );
        assert_eq!(
            resolve_agent_reference("agent 1", &cands).as_deref(),
            Some("x = 1")
        );
        assert_eq!(
            resolve_agent_reference("Candidate 2:", &cands).as_deref(),
            Some("x = 2")
        );
        // Real answer text after the number is left untouched.
        assert_eq!(resolve_agent_reference("Agent 2: x = 2", &cands), None);
        assert_eq!(resolve_agent_reference("x = 2", &cands), None);
        // Out-of-range or non-reference -> no recovery.
        assert_eq!(resolve_agent_reference("Agent 9", &cands), None);
    }

    #[test]
    fn parse_answer_handles_multiline_and_case() {
        let text = "Reasoning...\n<ANSWER>\n\\frac{1}{2}\n</ANSWER>";
        assert_eq!(parse_answer(text).as_deref(), Some("\\frac{1}{2}"));
    }

    #[test]
    fn parse_answer_falls_back_to_answer_line() {
        let text = "Reasoning: We compute.\nAnswer: 42\nMore prose";
        assert_eq!(parse_answer(text).as_deref(), Some("42"));
    }

    #[test]
    fn parse_answer_prefers_tags_over_field() {
        let text = "Answer: 1\n<answer>2</answer>";
        assert_eq!(parse_answer(text).as_deref(), Some("2"));
    }

    #[test]
    fn parse_answer_none_when_empty() {
        assert_eq!(parse_answer("no answer here"), None);
        assert_eq!(parse_answer("<answer> </answer>"), None);
    }

    #[test]
    fn parse_answer_falls_back_to_boxed() {
        // No tags, no "Answer:" line, so the boxed value is the last resort.
        let text = "We minimize and find \\boxed{17} at the end.";
        assert_eq!(parse_answer(text).as_deref(), Some("17"));
        // Nested braces handled; the last box wins.
        let nested = "first \\boxed{1} then \\boxed{\\frac{1}{2}}";
        assert_eq!(parse_answer(nested).as_deref(), Some("\\frac{1}{2}"));
    }

    #[test]
    fn parse_answer_recognizes_latex_answer_command() {
        // Weak models sometimes emit \answer{...} instead of <answer></answer>.
        let text = "Thus the smallest real number is $\\answer{5.5}$.";
        assert_eq!(parse_answer(text).as_deref(), Some("5.5"));
        // Even embedded in an array-wrapped reply blob.
        let blob = r#"[{"id":"x","content":"... is $\answer{5.5}$."}]"#;
        assert_eq!(parse_answer(blob).as_deref(), Some("5.5"));
    }

    #[test]
    fn reply_unwraps_single_element_array() {
        let raw = r#"[{"id":"x","sender":"a","content":"hello <answer>9</answer>"}]"#;
        let reply = Reply::parse(raw);
        assert_eq!(reply.content, "hello <answer>9</answer>");
        assert_eq!(parse_answer(&reply.content).as_deref(), Some("9"));
    }

    #[test]
    fn parse_answer_prefers_tags_and_field_over_boxed() {
        assert_eq!(
            parse_answer("Answer: 7\nwork: \\boxed{9}").as_deref(),
            Some("7")
        );
        assert_eq!(
            parse_answer("<answer>7</answer>\n\\boxed{9}").as_deref(),
            Some("7")
        );
    }

    #[test]
    fn parsers_handle_multibyte_chars_without_panic() {
        // A line whose Nth byte is inside a multibyte char must not panic the
        // prefix/score parsers (regression: HARDMath answers are full of epsilon).
        assert!(parse_reflection("Reason ε = 6^{-5/4}\nCorrectness: False")
            .feedback
            .contains('ε'));
        assert_eq!(
            parse_answer("Answer ε\n<answer>ε=0.14</answer>").as_deref(),
            Some("ε=0.14")
        );
        assert_eq!(
            parse_score("the grade is ε... <answer>0.0</answer>"),
            Some(0.0)
        );
        assert_eq!(parse_answer("Answerε is here"), None);
    }

    #[test]
    fn parse_score_reads_float_and_clamps() {
        assert_eq!(parse_score("Grade: \\boxed{1.0}"), Some(1.0));
        assert_eq!(parse_score("<answer>0.5</answer>"), Some(0.5));
        assert_eq!(parse_score("score is \\boxed{0}"), Some(0.0));
        assert_eq!(parse_score("\\boxed{1.5}"), Some(1.0));
        assert_eq!(parse_score("no score here"), None);
        // Bare/labeled grades with no wrapper must still parse (regression C2).
        assert_eq!(parse_score("Grade: 0.5"), Some(0.5));
        assert_eq!(parse_score("The score is 0.5"), Some(0.5));
        assert_eq!(parse_score("0.0"), Some(0.0)); // genuine 0, not a failure
                                                   // Fractions must not truncate to the numerator.
        assert_eq!(parse_score("<answer>1/2</answer>"), Some(0.5));
        assert_eq!(parse_score("Grade: 1/4"), Some(0.25));
    }

    #[test]
    fn parse_node_reads_message_and_stop_vote() {
        let pass = parse_node("trail: A>B <answer>A>B>C</answer>\nDecision: PASS");
        assert_eq!(pass.message.as_deref(), Some("A>B>C"));
        assert!(!pass.stop);
        let stop = parse_node("<answer>done</answer>\nDecision: STOP");
        assert!(stop.stop);
        // No decision line defaults to passing (bounded by the hop budget).
        assert!(!parse_node("<answer>x</answer>").stop);
    }

    #[test]
    fn parse_reflection_true_verdict() {
        let text = "Reasoning: looks right.\nFeedback: clean derivation.\nCorrectness: True";
        let r = parse_reflection(text);
        assert!(r.correct);
        assert_eq!(r.feedback, "Feedback: clean derivation.");
    }

    #[test]
    fn parse_reflection_false_by_default() {
        let r = parse_reflection("Feedback: sign error in step 2.\nCorrectness: False");
        assert!(!r.correct);
        assert!(r.feedback.contains("sign error"));
    }

    #[test]
    fn parse_reflection_without_fields_uses_whole_text() {
        let r = parse_reflection("the answer is wrong because ...");
        assert!(!r.correct);
        assert_eq!(r.feedback, "the answer is wrong because ...");
    }

    #[test]
    fn reply_parses_json_and_bare_text() {
        // Extra id/sender keys are ignored; only content is consumed.
        let json = r#"{"id":"x","sender":"pred-1","content":"<answer>5</answer>"}"#;
        let reply = Reply::parse(json);
        assert_eq!(reply.content, "<answer>5</answer>");

        let bare = Reply::parse("plain text <answer>7</answer>");
        assert_eq!(bare.content, "plain text <answer>7</answer>");
    }

    #[test]
    fn envelope_round_trips() {
        let env = Envelope {
            id: "e1".into(),
            sender: "runner".into(),
            receiver: "mass-ab12-pred-1".into(),
            timestamp_ns: 42,
            run_id: "ab12".into(),
            task_id: "math-001".into(),
            role: Role::Predictor,
            round: 0,
            payload: "Question: 1+1?".into(),
            reply_path: "/tmp/x.json".into(),
        };
        let json = serde_json::to_string(&env).unwrap();
        let back: Envelope = serde_json::from_str(&json).unwrap();
        assert_eq!(back.role, Role::Predictor);
        assert_eq!(back.receiver, env.receiver);
    }
}
