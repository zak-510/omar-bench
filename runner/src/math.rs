//! Rule-based majority vote for self-consistency. Grading is done by the LLM
//! judge (see prompts::judge), so this only buckets candidate answers.

use std::collections::HashMap;

/// Crude normalization for vote bucketing: drop wrappers, whitespace, case.
fn normalize_for_vote(answer: &str) -> String {
    answer
        .trim()
        .replace("\\boxed", "")
        .replace(['$', '{', '}', ' ', '\n', '\t'], "")
        .to_lowercase()
}

/// Majority vote over candidates (self-consistency). Returns the winning
/// bucket's original string, ties broken toward the earliest answer.
pub fn majority_vote(answers: &[String]) -> Option<String> {
    if answers.is_empty() {
        return None;
    }
    let mut buckets: HashMap<String, (usize, usize)> = HashMap::new();
    for (i, answer) in answers.iter().enumerate() {
        let entry = buckets.entry(normalize_for_vote(answer)).or_insert((0, i));
        entry.0 += 1;
    }
    let (_, &(_, first)) = buckets.iter().max_by(|a, b| {
        (a.1 .0, std::cmp::Reverse(a.1 .1)).cmp(&(b.1 .0, std::cmp::Reverse(b.1 .1)))
    })?;
    Some(answers[first].clone())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn majority_vote_picks_most_common() {
        let answers = vec![
            "64".to_string(),
            "$64$".to_string(),
            "63".to_string(),
            "\\boxed{64}".to_string(),
        ];
        assert_eq!(majority_vote(&answers).as_deref(), Some("64"));
    }

    #[test]
    fn majority_vote_tie_breaks_to_earliest() {
        let answers = vec!["7".to_string(), "9".to_string()];
        assert_eq!(majority_vote(&answers).as_deref(), Some("7"));
        assert_eq!(majority_vote(&[]), None);
    }
}
