//! Deterministic grading for coding tasks. `match_output` compares a predicted
//! value to the expected one (LCB test-output-prediction); `stdin_tests` runs a
//! predicted program against sample cases (LCB Pro). No LLM judge involved.

use serde::Deserialize;
use std::io::Write;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

const TIMEOUT: Duration = Duration::from_secs(10);

/// One sample case for a stdin/stdout problem.
#[derive(Debug, Clone, Deserialize)]
pub struct TestCase {
    pub input: String,
    pub output: String,
}

/// Pull the program out of a reply, preferring a fenced block.
pub fn extract_code(answer: &str) -> String {
    let mut rest = answer;
    while let Some(open) = rest.find("```") {
        let after = &rest[open + 3..];
        let body = match after.find('\n') {
            Some(nl) => &after[nl + 1..],
            None => break,
        };
        if let Some(close) = body.find("```") {
            let block = body[..close].trim();
            if !block.is_empty() {
                return block.to_string();
            }
            rest = &body[close + 3..];
        } else {
            return body.trim().to_string();
        }
    }
    answer.trim().to_string()
}

/// Competitive-programming output comparison: ignore trailing whitespace per
/// line and trailing blank lines.
fn normalize_output(s: &str) -> Vec<&str> {
    let mut lines: Vec<&str> = s.lines().map(|l| l.trim_end()).collect();
    while lines.last().is_some_and(|l| l.is_empty()) {
        lines.pop();
    }
    lines
}

/// 1.0 only if the program passes every sample case, 0.0 otherwise. A timeout,
/// crash, or missing python3 counts as a failure.
pub fn stdin_tests(answer: &str, tests: &[TestCase]) -> f64 {
    let code = extract_code(answer);
    if code.is_empty() || tests.is_empty() {
        return 0.0;
    }
    for case in tests {
        match run_program(&code, &case.input) {
            Some((true, out)) if normalize_output(&out) == normalize_output(&case.output) => {}
            _ => return 0.0,
        }
    }
    1.0
}

/// Run `code` with `input` on stdin. None if python3 could not be launched.
fn run_program(code: &str, input: &str) -> Option<(bool, String)> {
    let mut child = Command::new("python3")
        .arg("-c")
        .arg(code)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    // A program that ignores stdin closes the pipe early; that is not an error.
    let _ = child.stdin.take()?.write_all(input.as_bytes());
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let out = child.wait_with_output().ok()?;
                return Some((
                    out.status.success(),
                    String::from_utf8_lossy(&out.stdout).into_owned(),
                ));
            }
            Ok(None) => {
                if start.elapsed() > TIMEOUT {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Some((false, String::new()));
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => return Some((false, String::new())),
        }
    }
}

/// Compare two Python literals the way LiveCodeBench does: literal_eval both
/// and compare structurally, falling back to trimmed string equality.
const MATCH_PY: &str = r#"
import ast, json, sys
d = json.load(sys.stdin)
def norm(s):
    s = s.strip()
    if s.startswith("```"):
        s = "\n".join(
            l for l in s.splitlines() if not l.strip().startswith("```")
        ).strip()
    # Whole-string first, so multi-line literals survive intact.
    try:
        return ast.literal_eval(s)
    except Exception:
        pass
    # Otherwise the value is on line one and prose follows.
    first = s.splitlines()[0].strip() if s.splitlines() else s
    try:
        return ast.literal_eval(first)
    except Exception:
        return first
try:
    print("1" if norm(d["a"]) == norm(d["b"]) else "0")
except Exception:
    print("0")
"#;

/// 1.0 if the predicted value equals the expected one, 0.0 otherwise (also on
/// timeout, crash, or a missing python3).
pub fn match_output(answer: &str, expected: &str) -> f64 {
    let payload = serde_json::json!({ "a": answer, "b": expected }).to_string();
    match run_match(&payload) {
        Some(out) if out.trim() == "1" => 1.0,
        _ => 0.0,
    }
}

/// None if python3 could not be launched at all.
fn run_match(input: &str) -> Option<String> {
    let mut child = Command::new("python3")
        .arg("-c")
        .arg(MATCH_PY)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    child.stdin.take()?.write_all(input.as_bytes()).ok()?;
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => {
                let out = child.wait_with_output().ok()?;
                return Some(String::from_utf8_lossy(&out.stdout).into_owned());
            }
            Ok(None) => {
                if start.elapsed() > TIMEOUT {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(50));
            }
            Err(_) => return None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn compares_python_literals_not_strings() {
        // Formatting differences must not count as wrong answers.
        assert_eq!(match_output("[1, 2, 3]", "[1,2,3]"), 1.0);
        assert_eq!(match_output(" 35 ", "35"), 1.0);
        assert_eq!(match_output("\"abc\"", "'abc'"), 1.0);
        assert_eq!(match_output("True", "True"), 1.0);
        assert_eq!(match_output("[1, 2]", "[2, 1]"), 0.0);
        assert_eq!(match_output("36", "35"), 0.0);
    }

    #[test]
    fn tolerates_wrapping_and_prose() {
        assert_eq!(match_output("```\n35\n```", "35"), 1.0);
        assert_eq!(match_output("```python\n35\n```", "35"), 1.0);
        assert_eq!(match_output("35\nBecause the max is 35.", "35"), 1.0);
        assert_eq!(match_output("", "35"), 0.0);
        // A multi-line literal must survive rather than truncate to line one.
        assert_eq!(match_output("[1,\n 2,\n 3]", "[1, 2, 3]"), 1.0);
    }

    fn case(input: &str, output: &str) -> TestCase {
        TestCase {
            input: input.into(),
            output: output.into(),
        }
    }

    #[test]
    fn stdin_tests_runs_program_against_cases() {
        let prog = "```python\nn=int(input())\nprint(n*2)\n```";
        assert_eq!(stdin_tests(prog, &[case("5", "10")]), 1.0);
        assert_eq!(stdin_tests(prog, &[case("5", "11")]), 0.0);
        // Every case must pass, not just the first.
        assert_eq!(stdin_tests(prog, &[case("5", "10"), case("7", "14")]), 1.0);
        assert_eq!(stdin_tests(prog, &[case("5", "10"), case("7", "99")]), 0.0);
    }

    #[test]
    fn stdin_tests_normalizes_trailing_whitespace() {
        let prog = "print('3 2 1')";
        assert_eq!(stdin_tests(prog, &[case("", "3 2 1  \n\n")]), 1.0);
        // Multi-line output compares line by line.
        let multi = "print('a')\nprint('b')";
        assert_eq!(stdin_tests(multi, &[case("", "a\nb\n")]), 1.0);
        assert_eq!(stdin_tests(multi, &[case("", "b\na")]), 0.0);
    }

    #[test]
    fn stdin_tests_fail_on_crash_timeout_and_empty() {
        assert_eq!(stdin_tests("raise SystemExit(1)", &[case("", "x")]), 0.0);
        assert_eq!(stdin_tests("while True: pass", &[case("", "x")]), 0.0);
        assert_eq!(stdin_tests("", &[case("", "x")]), 0.0);
        // No cases means nothing was verified, so it cannot score as a pass.
        assert_eq!(stdin_tests("print(1)", &[]), 0.0);
    }

    #[test]
    fn stdin_tests_tolerate_program_ignoring_input() {
        // A program that never reads stdin closes the pipe early; not an error.
        assert_eq!(stdin_tests("print(42)", &[case("1\n2\n3\n", "42")]), 1.0);
    }
}
