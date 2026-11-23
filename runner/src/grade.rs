//! Batched grading pass: one judge call per problem scores every method's
//! answer against the gold, anonymized and shuffled so no method is favored.

use crate::bench::{self, BenchSummary, Method};
use crate::mailbox;
use crate::runner::{ModelConfig, Runner, RunnerOptions};
use crate::topology::{AggregatorMode, TopologyConfig};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::time::Duration;

#[derive(Debug, Default, Serialize, Deserialize)]
pub struct GradeReport {
    pub seed: u64,
    pub methods: Vec<String>,
    /// task_id -> method -> score.
    pub scores: BTreeMap<String, BTreeMap<String, f64>>,
    /// task_id -> question_type.
    pub types: BTreeMap<String, String>,
}

pub struct GradeArgs {
    pub seed: u64,
    pub data: PathBuf,
    pub methods: Vec<Method>,
    pub dir: PathBuf,
    pub model: ModelConfig,
    pub timeout: Duration,
}

/// Deterministic per-problem permutation (Fisher-Yates seeded by task id) so the
/// answer order is shuffled but reproducible.
fn shuffled_indices(n: usize, key: &str) -> Vec<usize> {
    let mut s = key.bytes().fold(0xcbf29ce484222325u64, |h, b| {
        (h ^ b as u64).wrapping_mul(0x100000001b3)
    });
    let mut idx: Vec<usize> = (0..n).collect();
    for i in (1..n).rev() {
        s = s
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        let j = (s >> 33) as usize % (i + 1);
        idx.swap(i, j);
    }
    idx
}

fn mean(vals: impl Iterator<Item = f64>) -> f64 {
    let (sum, n) = vals.fold((0.0, 0usize), |(s, c), v| (s + v, c + 1));
    if n == 0 {
        0.0
    } else {
        sum / n as f64
    }
}

/// Method -> score map for one problem, or None if any label came back
/// unparseable, the problem stays ungraded so a re-run retries it, instead
/// of baking a false 0.0 into the report forever.
fn collect_scores(
    entries: &[(String, String)],
    order: &[usize],
    scores: &[Option<f64>],
) -> Option<BTreeMap<String, f64>> {
    let mut tmap = BTreeMap::new();
    for (pos, &i) in order.iter().enumerate() {
        tmap.insert(entries[i].0.clone(), scores.get(pos).copied().flatten()?);
    }
    Some(tmap)
}

impl GradeReport {
    fn accuracy(&self, method: &str) -> f64 {
        mean(self.scores.values().filter_map(|m| m.get(method).copied()))
    }

    fn by_type(&self, method: &str) -> BTreeMap<String, f64> {
        let mut buckets: BTreeMap<String, Vec<f64>> = BTreeMap::new();
        for (task, m) in &self.scores {
            if let Some(&s) = m.get(method) {
                let t = self.types.get(task).cloned().unwrap_or_default();
                buckets.entry(t).or_default().push(s);
            }
        }
        buckets
            .into_iter()
            .map(|(t, v)| (t, mean(v.into_iter())))
            .collect()
    }
}

pub fn run_grade(args: GradeArgs) -> Result<GradeReport> {
    let problems = bench::load_problems(&args.data)?;
    // (solution, question_type for reporting, grading type for the rubric —
    // integrals split into traditional/laplace subtypes).
    let meta: BTreeMap<String, (String, String, String)> = problems
        .iter()
        .map(|p| {
            (
                p.id.clone(),
                (
                    p.solution.clone(),
                    p.question_type.clone(),
                    crate::prompts::grading_type(&p.question_type, &p.question),
                ),
            )
        })
        .collect();

    // Load each method's predictions (saved by `bench --no-grade`).
    let mut preds: BTreeMap<String, BTreeMap<String, String>> = BTreeMap::new();
    let mut method_names = Vec::new();
    for m in &args.methods {
        let name = m.name().to_string();
        let path = args.dir.join(format!("{name}.seed{}.json", args.seed));
        let raw = std::fs::read_to_string(&path)
            .with_context(|| format!("missing predictions {}", path.display()))?;
        let summary: BenchSummary = serde_json::from_str(&raw)
            .with_context(|| format!("bad predictions {}", path.display()))?;
        method_names.push(name.clone());
        for inst in summary.instances {
            if let Some(p) = inst.predicted {
                preds
                    .entry(inst.task_id)
                    .or_default()
                    .insert(name.clone(), p);
            }
        }
    }

    let report_path = args.dir.join(format!("graded.seed{}.json", args.seed));
    let mut report: GradeReport = std::fs::read_to_string(&report_path)
        .ok()
        .and_then(|r| serde_json::from_str(&r).ok())
        .unwrap_or_default();
    report.seed = args.seed;
    report.methods = method_names;

    // Grade a problem only once every method has produced it, so all methods are
    // scored together in one paired call (out-of-sync files just wait their turn).
    let todo: Vec<String> = preds
        .iter()
        .filter(|(t, m)| {
            !report.scores.contains_key(*t)
                && report.methods.iter().all(|name| m.contains_key(name))
        })
        .map(|(t, _)| t.clone())
        .collect();

    if todo.is_empty() {
        eprintln!(
            "[grade] nothing new in sync ({} already scored)",
            report.scores.len()
        );
    } else {
        eprintln!(
            "[grade] {} problems to grade ({} already done)",
            todo.len(),
            report.scores.len()
        );
        let mut runner = Runner::setup(RunnerOptions {
            topology: TopologyConfig::default(),
            aggregator: AggregatorMode::Rule,
            model: args.model.clone(),
            timeout: args.timeout,
            max_concurrent: 0,
            with_grader: true,
            reset_each_problem: false,
            run_root: None,
            spawn_window: 0,
            deadline: None,
        })?;

        for task in &todo {
            let (solution, qt, gtype) = match meta.get(task) {
                Some(m) => m,
                None => {
                    eprintln!("[grade] WARN no dataset entry for {task}; skipping");
                    continue;
                }
            };
            // Stable method list, then shuffle into anonymous A/B/C order.
            let entries: Vec<(String, String)> = preds[task]
                .iter()
                .map(|(m, p)| (m.clone(), p.clone()))
                .collect();
            let order = shuffled_indices(entries.len(), task);
            let answers: Vec<String> = order.iter().map(|&i| entries[i].1.clone()).collect();
            let scores = runner.grade_batch(task, &answers, solution, gtype);

            let tmap = match collect_scores(&entries, &order, &scores) {
                Some(t) => t,
                None => {
                    eprintln!(
                        "[grade] WARN {task}: judge gave no parseable score for some answers; \
                         left ungraded so a re-run retries it"
                    );
                    continue;
                }
            };
            eprintln!("[grade] {task}: {tmap:?}");
            report.types.insert(task.clone(), qt.clone());
            report.scores.insert(task.clone(), tmap);
            mailbox::write_json_atomic(&report_path, &report)?;
        }
        runner.teardown()?;
    }

    eprintln!("[grade] -> {}", report_path.display());
    for m in &report.methods {
        eprintln!("[grade] {m}: acc {:.3}", report.accuracy(m));
        for (t, a) in report.by_type(m) {
            eprintln!("[grade]     {t}: {a:.3}");
        }
    }
    Ok(report)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shuffle_is_deterministic_and_a_permutation() {
        let a = shuffled_indices(4, "polynomial_roots-000");
        assert_eq!(a, shuffled_indices(4, "polynomial_roots-000"));
        let mut sorted = a.clone();
        sorted.sort();
        assert_eq!(sorted, vec![0, 1, 2, 3]);
        // Different tasks generally differ; at least one of two does.
        let b = shuffled_indices(4, "ODE-000");
        assert!(a != b || shuffled_indices(4, "integral-000") != a);
    }

    #[test]
    fn collect_scores_maps_back_through_shuffle_and_rejects_missing() {
        let entries = vec![
            ("cot".to_string(), "p1".to_string()),
            ("debate".to_string(), "p2".to_string()),
        ];
        // Shuffled order [1, 0]: position 0 holds debate, position 1 holds cot.
        let tmap = collect_scores(&entries, &[1, 0], &[Some(0.25), Some(1.0)]).unwrap();
        assert_eq!(tmap["debate"], 0.25);
        assert_eq!(tmap["cot"], 1.0);
        // Any unparseable label leaves the whole problem ungraded (retry later),
        // never a silent 0.0.
        assert!(collect_scores(&entries, &[1, 0], &[Some(0.25), None]).is_none());
    }

    #[test]
    fn accuracy_and_by_type_average_present_methods() {
        let mut r = GradeReport::default();
        r.types.insert("t1".into(), "ODE".into());
        r.types.insert("t2".into(), "ODE".into());
        r.scores
            .insert("t1".into(), BTreeMap::from([("cot".into(), 1.0)]));
        r.scores
            .insert("t2".into(), BTreeMap::from([("cot".into(), 0.0)]));
        assert!((r.accuracy("cot") - 0.5).abs() < 1e-9);
        assert!((r.by_type("cot")["ODE"] - 0.5).abs() < 1e-9);
    }
}
