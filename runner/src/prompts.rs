//! Role prompt templates from the MASS paper (arXiv:2502.02533, App. D) plus
//! the HARDMath LLM-judge grading prompt (arXiv:2410.09988).

const ANSWER_TAG_INSTRUCTION: &str =
    "Show your final answer bracketed between <answer> and </answer> at the end.";

/// Deterministic per-slot strategy hint: keyed by 1-based chain index so
/// parallel chains diversify without temperature. Slot 1 is neutral, leaving
/// single-chain methods (cot, self-refine, width=1) byte-for-byte unchanged.
///
/// Every slot up to the widest topology (aggregate 9) needs a distinct hint. Two
/// slots sharing one hint means two byte-identical prompts, and with no
/// temperature to separate them the vote counts the same trace twice. Slots 2-5
/// are frozen: changing them would invalidate existing sc5 runs.
fn strategy_hint(slot: usize) -> &'static str {
    match slot {
        2 => " Prefer a numerical approach: estimate the answer, then verify it.",
        3 => " Watch the edge cases and cross-check with an alternate method.",
        4 => " Derive symbolically and simplify carefully before evaluating.",
        5 => " Reason about the asymptotic/limiting behavior to bound the answer.",
        6 => " Work through a small concrete case first, then generalize from it.",
        7 => " Restate the given constraints, then solve strictly within them.",
        8 => " Work backwards from the form the answer must take.",
        9 => " Solve it directly, then re-derive it a second way and compare.",
        _ => "",
    }
}

/// Zero-shot CoT predictor. A summary from the Summarize block, when present,
/// is passed as extra context. `slot` selects a deterministic strategy hint.
pub fn predictor(question: &str, summary: Option<&str>, slot: usize) -> String {
    let context = match summary {
        Some(s) => format!("Context (summarized): {}\n", s),
        None => String::new(),
    };
    format!(
        "Let's think step by step.{hint} {tag}\n\n---\n\nFollow the following format.\n\nQuestion: ${{question}}\nReasoning: Let's think step by step in order to ${{produce the answer}}. We ...\nAnswer: ${{answer}}\n\n---\n\n{context}Question: {question}\nReasoning: Let's think step by step in order to",
        hint = strategy_hint(slot),
        tag = ANSWER_TAG_INSTRUCTION,
    )
}

/// App. D Reflector: critiques a prediction, emits Feedback + Correctness.
pub fn reflector(question: &str, text: &str) -> String {
    format!(
        "Please review the answer above and criticize on where might be wrong. If you are absolutely sure it is correct, output 'True' in 'correctness'.\n\n---\n\nFollow the following format.\n\nQuestion: ${{question}}\nText: ${{text}}\nReasoning: Let's think step by step in order to ${{produce the correctness}}. We ...\nFeedback: ${{feedback}}\nCorrectness: True/False indicating if answer is correct given the question.\n\n---\n\nQuestion: {question}\nText: {text}\nReasoning: Let's think step by step in order to",
    )
}

/// App. D Refiner: revises a prediction given the reflection.
pub fn refiner(
    question: &str,
    previous_answer: &str,
    reflection: &str,
    correctness: bool,
) -> String {
    format!(
        "Given previous attempts and feedback, carefully consider where you could go wrong in your latest attempt. Using insights from previous attempts, try to solve the task better. Show your final answer bracketed between <answer> and </answer> at the end.\n\n---\n\nFollow the following format.\n\nQuestion: ${{question}}\nPrevious answer: ${{previous_answer}}\nReflection: ${{reflection}}\nCorrectness: ${{correctness}}\nThinking: ${{thinking}}\nAnswer: ${{answer}}\n\n---\n\nQuestion: {question}\nPrevious answer: {previous_answer}\nReflection: {reflection}\nCorrectness: {correctness}\nThinking:",
    )
}

/// App. D Debator: sees all other agents' solutions, produces an updated answer.
/// Sessions are stateless, so the agent's own previous solution rides along in
/// the prompt too, explicitly marked (`own` is its index in `solutions`); the
/// paper's agents carry theirs in conversation memory instead.
/// `slot` selects the same deterministic strategy hint as the predictor.
pub fn debator(question: &str, solutions: &[String], own: usize, slot: usize) -> String {
    let mut listed = String::new();
    for (i, solution) in solutions.iter().enumerate() {
        let label = if i == own {
            "[Your previous solution]".to_string()
        } else {
            format!("[Agent {}]", i + 1)
        };
        listed.push_str(&format!("{} {}\n\n", label, solution));
    }
    format!(
        "These are the solutions to the question from the other agents, plus your own previous solution (marked). Examine the solutions from other agents in your rationale, finish by giving an updated answer.{hint} Keep all comparison of the other agents in your reasoning; the <answer> must be the self-contained final result only, never a reference to another agent by number or label. Show your final answer bracketed between <answer> and </answer> at the end.\n\n---\n\nFollow the following format.\n\nQuestion: ${{question}}\nSolutions: the solutions to the question from other agents\nReasoning: Let's think step by step in order to ${{Examine the solutions from other agents}}. We ...\nAnswer: The updated self-contained answer for the question, with no references to other agents. Do not repeat Answer:\n\n---\n\nQuestion: {question}\nSolutions:\n{listed}Reasoning: Let's think step by step in order to",
        hint = strategy_hint(slot),
    )
}

/// App. D long-context Summarizer.
pub fn summarizer(question: &str, context: &str) -> String {
    format!(
        "Based on the question, retrieve relevant information from context that is ONLY helpful in answering the question. Include all key information. Do not repeat context.\n\n---\n\nFollow the following format.\n\nQuestion: ${{question}}\nContext: ${{context}}\nSummary: Only generate the summary. Start with Summary:\n\n---\n\nQuestion: {question}\nContext: {context}\nSummary:",
    )
}

/// LLM aggregator for SC@5 and Multi-Agent Debate. Candidates are labeled so the
/// judge can compare them, but it must restate the chosen answer in full, since naming
/// a candidate (e.g. "Agent 4") would lose the answer and score 0.
pub fn aggregator(question: &str, answers: &[String]) -> String {
    let mut listed = String::new();
    for (i, answer) in answers.iter().enumerate() {
        listed.push_str(&format!("[Candidate {}] {}\n\n", i + 1, answer));
    }
    format!(
        "Below are candidate final answers to the question. Decide which is most consistent and correct, then output that answer in full as your own. Reproduce the complete answer expression itself; never refer to a candidate by number or label (do not write 'Candidate 2' or 'Agent 4') and do not add a 'consensus' prefix. Show the final answer bracketed between <answer> and </answer> at the end.\n\nQuestion: {question}\nCandidates:\n{listed}",
    )
}

/// Executor request: run the candidate code against the public tests and
/// report the outcome verbatim (agents have real shell access).
pub fn executor(code: &str, tests: &str) -> String {
    format!(
        "You are a code executor with real shell access. Take the candidate solution below, write it to a file together with the test cases, and actually run it (e.g. with python3). Do not reason about whether the code looks correct, execute it. Report exactly what happened in this format:\n\nExecution result: PASS or FAIL\nOutput: the full stdout/stderr including any traceback, verbatim\n\nCandidate solution:\n{code}\n\nTest cases:\n{tests}",
    )
}

/// Chain node: process the incoming message and pass the whole thing along.
pub fn chain_node(idx: usize, n: usize, message: &str) -> String {
    format!(
        "You are node {idx} of {n} in a processing chain. You received this message:\n\n{message}\n\nAppend a short note that node {idx} processed it, then pass the whole updated message on. Put the full updated message bracketed between <answer> and </answer>.",
    )
}

/// Ring node: update the message and vote to keep passing or stop the ring.
pub fn ring_node(idx: usize, n: usize, hop: usize, message: &str) -> String {
    format!(
        "You are node {idx} of {n} in a message-passing ring (hop {hop}). You received:\n\n{message}\n\nAppend 'node {idx}' to the trail, then decide whether the ring should keep passing the message. Put the updated message bracketed between <answer> and </answer>. End with a final line exactly 'Decision: PASS' to keep passing or 'Decision: STOP' to end the ring.",
    )
}

/// How agent-coordinated nodes address mail. Appended to every postal prompt.
const ADDRESS_RULES: &str = "How to act: nobody routes or schedules for you; you decide. To send the message onward, include a line exactly 'To: node K' and put the full message between <answer> and </answer>. To finish the whole run and report the final result, put it in <answer> tags and end with a line exactly 'Decision: DONE'. To wait for more messages, end with 'Decision: WAIT'.";

/// Postal chain node: same relay protocol, but the node itself must route.
/// A spec-defined postal prompt: `{sender}` and `{body}` are filled per
/// delivery, and the same address rules as every other postal prompt follow.
pub fn postal_custom(template: &str, from: usize, body: &str) -> String {
    let sender = if from == 0 {
        "the experimenter".to_string()
    } else {
        format!("node {from}")
    };
    format!(
        "{}\n\n{ADDRESS_RULES}",
        template.replace("{sender}", &sender).replace("{body}", body)
    )
}

pub fn postal_chain(idx: usize, n: usize, message: &str) -> String {
    format!(
        "You are node {idx} of {n} in a relay chain. Chain protocol: append 'node {idx}' to the trail, keep every existing payload field and trail entry intact, and send the full updated message to the next node, node {next}. The last node, node {n}, finishes the run and reports the full final message instead of sending. You received:\n\n{message}\n\n{ADDRESS_RULES}",
        next = idx + 1,
    )
}

/// Postal ring node: same ring protocol, but the node itself must route.
pub fn postal_ring(idx: usize, n: usize, message: &str) -> String {
    format!(
        "You are node {idx} of {n} in a message-passing ring. Ring protocol: append 'node {idx}' to the trail, then send the full updated message to your successor, node {next}. The task inside the message says how long the ring should keep circulating; you decide whether to keep passing or to finish. You received:\n\n{message}\n\n{ADDRESS_RULES}",
        next = idx % n + 1,
    )
}

/// Postal worker: answer the task, then report to the aggregator unprompted.
pub fn postal_worker(idx: usize, n: usize, question: &str) -> String {
    format!(
        "You are worker {idx} of {n} answering a shared task independently. Worker protocol: work out your answer, then send it to the aggregator, node {agg}. Only the aggregator finishes the run and reports the final result; your part ends once your answer is sent. Task:\n\n{question}\n\n{ADDRESS_RULES}",
        agg = n + 1,
    )
}

/// Postal aggregator: one worker report per message; it decides when enough
/// have arrived, with no barrier and no end-of-input signal.
pub fn postal_aggregator(n: usize, gather_q: &str, from: usize, body: &str) -> String {
    format!(
        "You are the aggregator, node {agg}, for {n} workers (nodes 1..{n}). Workers send you their answers as separate messages; nobody will tell you when reports stop coming. Question to answer: {gather_q}\n\nNew message from node {from}:\n\n{body}\n\nConsidering every message you have received so far in this session, either finish the run with your final answer or wait for more reports.\n\n{ADDRESS_RULES}",
        agg = n + 1,
    )
}

/// Scatter worker: answer the shared task independently of the other workers.
pub fn worker(idx: usize, n: usize, question: &str) -> String {
    format!(
        "You are worker {idx} of {n} answering a shared task independently. Task:\n\n{question}\n\nGive your answer bracketed between <answer> and </answer>.",
    )
}

/// Grading rubrics verbatim from HARDMath's create_grading_prompt
fn rubric(question_type: &str) -> &'static str {
    match question_type {
        "polynomial_roots" | "polynomial_roots_corrections" => "1) Check both the small and large $\\epsilon$ solutions. 2) For each solution, give full credit if it completely matches the elements in the answer key; give partial credit proportional to the number of matching roots between the response and the answer key; give no credit if it is completely wrong. 3) For both partial and no credit briefly state the error reason. 4) Average the scores for the small and large epsilon solutions to obtain a final score between 0 and 1.",
        "integral" | "integral_traditional" => "1) Check both the small and large $\\epsilon$ solutions. 2) For each solution, give full credit if it matches the formula in the answer key; give no credit if it is completely wrong and briefly state the reason for the error. 3) Average the scores for the small and large epsilon solutions to obtain a final score between 0 and 1.",
        "integral_laplace" => "1) Check the large $x$ final solution. 2) Give full credit if it matches the formula in the answer key; give half credit if the response get to the checkpoint where it correctly identifies \\(t_0\\) where $f$ attains its maximum and attempt performing Taylor's expansion around it but the final answer is wrong; give no credit if it is completely wrong. 3) For both partial and no credit briefly state the error reason.",
        "ODE" => "1) Check both the small and large $\\epsilon$ solutions. 2) For small regime solution, only give full credit if it matches the formula in the answer key exactly; give no credit if it is doesn't match the form. For large regime solution, give full credit if it matches the formula in the answer key exactly; give partial credit if it doesn't match but the numerical evaluation is not far from solution at this regime; give no credit if neither satisfies. 3) Average the scores for the small and large epsilon solutions to obtain a final score between 0 and 1.",
        _ => "Give full credit (1.0) if the response's final expression is mathematically equivalent to the answer key, partial credit if close, and no credit (0.0) if wrong. Final score between 0 and 1.",
    }
}

/// HARDMath grades integrals under two rubrics. The subtype is recoverable
/// from the question text: Laplace problems ask for $I(x)$ (an $e^{\pm x f(t)}$
/// kernel, large-$x$ asymptotics), traditional ones for $I(\epsilon)$.
pub fn grading_type(question_type: &str, question: &str) -> String {
    if question_type == "integral" {
        if question.contains("I(x)") {
            "integral_laplace".to_string()
        } else {
            "integral_traditional".to_string()
        }
    } else {
        question_type.to_string()
    }
}

/// Anonymous label for a batched-grade candidate: 0 -> A, 1 -> B, ...
pub fn label(i: usize) -> char {
    (b'A' + i as u8) as char
}

/// Batched grader: one call scores every candidate against the gold solution
/// under the same rubric, each judged independently (no cross-answer ranking).
pub fn judge_batch(answers: &[String], solution: &str, question_type: &str) -> String {
    let mut listed = String::new();
    for (i, a) in answers.iter().enumerate() {
        listed.push_str(&format!("[Answer {}]\n{}\n\n", label(i), a));
    }
    let keys: Vec<String> = (0..answers.len())
        .map(|i| format!("{}: <0-1>", label(i)))
        .collect();
    format!(
        "Score each candidate answer independently against the ground-truth solution using the rubric. Judge each answer ONLY against the ground truth, never relative to the other answers.\n\nGround truth solution: {solution}\n\nRubric:\n{rubric}\n\n{listed}Output every answer's float in [0,1] on one line bracketed between <answer> and </answer>, formatted exactly as: {keys}.",
        rubric = rubric(question_type),
        keys = keys.join(", "),
    )
}

/// HARDMath LLM-judge prompt: the judge sees the response and the ground-truth
/// solution, applies the type rubric, and returns a 0-1 float.
pub fn judge(predicted: &str, solution: &str, question_type: &str) -> String {
    format!(
        "Please take this response: {predicted}\n\nand this ground truth solution: {solution}\n\nand grade the response based on the following criteria:\n{rubric}\n\nGive only the final grade as a float between 0 and 1 bracketed between <answer> and </answer>.",
        rubric = rubric(question_type),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn predictor_embeds_question_and_tag() {
        let p = predictor("What is 2+2?", None, 1);
        assert!(p.contains("Let's think step by step."));
        assert!(p.contains("Question: What is 2+2?"));
        assert!(p.contains("<answer>"));
        assert!(!p.contains("Context (summarized)"));
    }

    #[test]
    fn predictor_includes_summary_when_present() {
        let p = predictor("Q", Some("key facts"), 1);
        assert!(p.contains("Context (summarized): key facts"));
    }

    #[test]
    fn predictor_slot1_is_neutral_others_perturbed() {
        // Slot 1 must stay identical so single-chain methods are unchanged.
        let s1 = predictor("Q", None, 1);
        assert!(s1.starts_with("Let's think step by step. Show your final answer"));
        // Slots >1 get a distinct deterministic hint.
        let s2 = predictor("Q", None, 2);
        let s3 = predictor("Q", None, 3);
        assert!(s2.contains("numerical approach"));
        assert!(s3.contains("edge cases"));
        assert_ne!(s1, s2);
        assert_ne!(s2, s3);
        // Reproducible: same slot yields the same prompt.
        assert_eq!(s2, predictor("Q", None, 2));
    }

    #[test]
    fn every_sc9_slot_gets_a_distinct_prompt() {
        // Two slots sharing a hint would be one trace counted twice in the
        // vote, since there is no temperature to separate them.
        let prompts: Vec<String> = (1..=9).map(|s| predictor("Q", None, s)).collect();
        let unique: std::collections::BTreeSet<&String> = prompts.iter().collect();
        assert_eq!(unique.len(), 9, "sc9 slots must not share a strategy hint");
    }

    #[test]
    fn debator_slots_diversify_across_sc9_width() {
        let sols = vec!["a".to_string(), "b".to_string(), "c".to_string()];
        let prompts: Vec<String> = (1..=9).map(|s| debator("Q", &sols, 0, s)).collect();
        let unique: std::collections::BTreeSet<&String> = prompts.iter().collect();
        assert_eq!(unique.len(), 9);
    }

    #[test]
    fn reflector_and_refiner_carry_inputs() {
        let r = reflector("Q1", "candidate text");
        assert!(r.contains("Text: candidate text"));
        assert!(r.contains("Correctness: True/False"));

        let f = refiner("Q1", "4", "Feedback: wrong sign", false);
        assert!(f.contains("Previous answer: 4"));
        assert!(f.contains("Reflection: Feedback: wrong sign"));
        assert!(f.contains("Correctness: false"));
        assert!(f.contains("<answer>"));
    }

    #[test]
    fn debator_lists_all_solutions() {
        let sols = vec!["sol a".into(), "sol b".into(), "sol c".into()];
        let d = debator("Q", &sols, 0, 1);
        // The recipient's own solution is marked, never passed off as another agent's.
        assert!(d.contains("[Your previous solution] sol a"));
        assert!(!d.contains("[Agent 1]"));
        assert!(d.contains("[Agent 2] sol b"));
        assert!(d.contains("[Agent 3] sol c"));
        assert!(d.contains("<answer>"));
        // <answer> must stay a self-contained result, no agent-reference leak.
        assert!(d.contains("self-contained final result only"));
        // A different recipient gets its own slot marked instead.
        let d2 = debator("Q", &sols, 2, 3);
        assert!(d2.contains("[Agent 1] sol a"));
        assert!(d2.contains("[Your previous solution] sol c"));
        // Slot 1 neutral; slot 2 carries the strategy hint.
        assert!(!d.contains("numerical approach"));
        assert!(debator("Q", &["x".into()], 0, 2).contains("numerical approach"));
    }

    #[test]
    fn summarizer_and_aggregator_render() {
        let s = summarizer("Q", "long context here");
        assert!(s.contains("Context: long context here"));
        let a = aggregator("Q", &["1".into(), "2".into()]);
        assert!(a.contains("[Candidate 2] 2"));
        assert!(a.contains("<answer>"));
        assert!(a.contains("never refer to a candidate"));

        let e = executor("def add(a,b): return a+b", "assert add(1,2)==3");
        assert!(e.contains("assert add(1,2)==3"));
        assert!(e.contains("Execution result: PASS or FAIL"));
    }

    #[test]
    fn graph_node_prompts_carry_role_and_message() {
        let c = chain_node(2, 5, "payload");
        assert!(c.contains("node 2 of 5"));
        assert!(c.contains("payload"));
        assert!(c.contains("<answer>"));
        let r = ring_node(3, 4, 7, "trail");
        assert!(r.contains("ring (hop 7)"));
        assert!(r.contains("Decision: PASS"));
        assert!(r.contains("Decision: STOP"));
        let w = worker(1, 3, "solve me");
        assert!(w.contains("worker 1 of 3"));
        assert!(w.contains("solve me"));
    }

    #[test]
    fn postal_prompts_route_by_agent_not_runtime() {
        let custom = postal_custom("From {sender}: {body}", 0, "start");
        assert!(custom.starts_with("From the experimenter: start"));
        assert!(postal_custom("{sender}", 3, "").starts_with("node 3"));
        assert!(custom.contains(ADDRESS_RULES));
        // Every postal prompt carries the addressing rules and no runtime routing.
        let c = postal_chain(2, 5, "payload");
        assert!(c.contains("node 2 of 5"));
        assert!(c.contains("node 3")); // successor named, agent must address it
        assert!(c.contains("To: node K"));
        assert!(c.contains("nobody routes"));
        // Ring successor wraps.
        let r = postal_ring(4, 4, "trail");
        assert!(r.contains("your successor, node 1"));
        assert!(r.contains("Decision: DONE"));
        // Worker knows the aggregator's address; blind question flows through.
        let w = postal_worker(1, 3, "the code is 7731");
        assert!(w.contains("node 4"));
        assert!(w.contains("7731"));
        // Aggregator gets no barrier and no end-of-input promise.
        let a = postal_aggregator(3, "report the code", 2, "7731");
        assert!(a.contains("node 4"));
        assert!(a.contains("nobody will tell you when reports stop"));
        assert!(a.contains("from node 2"));
    }

    #[test]
    fn judge_batch_anonymizes_and_demands_independent_scores() {
        let j = judge_batch(&["ans one".into(), "ans two".into()], "gold", "integral");
        assert!(j.contains("[Answer A]\nans one"));
        assert!(j.contains("[Answer B]\nans two"));
        assert!(j.contains("never relative to the other answers"));
        assert!(j.contains("A: <0-1>, B: <0-1>"));
        // Traditional-integral rubric carried.
        assert!(j.contains("give full credit if it matches the formula in the answer key"));
        // No method names leak into the prompt.
        assert!(!j.to_lowercase().contains("cot") && !j.contains("debate"));
    }

    #[test]
    fn judge_carries_solution_and_type_rubric() {
        let j = judge("eps=0.14", "gold derivation", "polynomial_roots");
        assert!(j.contains("eps=0.14"));
        assert!(j.contains("gold derivation"));
        assert!(j.contains("small and large epsilon"));
        assert!(j.contains("<answer>"));
        // Unknown type falls back to the equivalence rubric.
        assert!(
            judge("a", "b", "nondimensionalization_numeric").contains("mathematically equivalent")
        );
    }

    #[test]
    fn rubrics_are_verbatim_hardmath() {
        // ODE keeps the source's harsh small-regime clause (exact form or zero)
        // and the numeric-closeness partial credit only on the large regime.
        let ode = rubric("ODE");
        assert!(ode
            .contains("only give full credit if it matches the formula in the answer key exactly"));
        assert!(ode.contains("partial credit if it doesn't match but the numerical evaluation is not far from solution"));
        // Roots: proportional partial credit, corrections reuse it.
        let roots = rubric("polynomial_roots");
        assert!(roots.contains("partial credit proportional to the number of matching roots"));
        assert_eq!(roots, rubric("polynomial_roots_corrections"));
        // Integral splits by subtype: traditional is exact-or-nothing per regime,
        // laplace has the half-credit Taylor checkpoint.
        assert!(rubric("integral_traditional").contains("give no credit if it is completely wrong"));
        assert_eq!(rubric("integral"), rubric("integral_traditional"));
        assert!(rubric("integral_laplace").contains("Taylor's expansion"));
        assert!(rubric("integral_laplace").contains("half credit"));
    }

    #[test]
    fn grading_type_splits_integral_subtypes_only() {
        let laplace_q = r"Consider the integral \begin{equation} I(x) = \int (- 1.3 t^{5}) e^{- x (1.5 \sin{(t)})} dt \end{equation}";
        let trad_q =
            r"Consider the integral $I(\epsilon) = \int_0^{98} \frac{1}{\epsilon + x^4} dx$.";
        assert_eq!(grading_type("integral", laplace_q), "integral_laplace");
        assert_eq!(grading_type("integral", trad_q), "integral_traditional");
        // Every other type passes through untouched.
        assert_eq!(grading_type("ODE", laplace_q), "ODE");
        assert_eq!(grading_type("polynomial_roots", trad_q), "polynomial_roots");
    }
}
