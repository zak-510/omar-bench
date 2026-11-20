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

/// What an agent's reply asks the postal runtime to do next (agent-coordinated mode).
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
