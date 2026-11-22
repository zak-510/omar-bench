//! File mailbox: requests are JSON envelopes under <run_dir>/inbox/; agents
//! write replies to <run_dir>/outbox/ atomically. The runner polls reply paths.

use anyhow::{Context, Result};
use serde::Serialize;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

pub fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_nanos() as u64
}

#[derive(Debug, Clone)]
pub struct RunDir {
    pub root: PathBuf,
}

impl RunDir {
    pub fn create(root: PathBuf) -> Result<RunDir> {
        for sub in ["inbox", "outbox", "ready", "results", "charters"] {
            std::fs::create_dir_all(root.join(sub))
                .with_context(|| format!("Failed to create {}/{}", root.display(), sub))?;
        }
        Ok(RunDir { root })
    }

    pub fn charter(&self, agent: &str) -> PathBuf {
        self.root.join("charters").join(format!("{agent}.md"))
    }

    pub fn inbox(&self, id: &str) -> PathBuf {
        self.root.join("inbox").join(format!("{id}.json"))
    }

    pub fn outbox(&self, id: &str) -> PathBuf {
        self.root.join("outbox").join(format!("{id}.json"))
    }

    pub fn ready(&self, agent: &str) -> PathBuf {
        self.root.join("ready").join(agent)
    }

    pub fn result(&self, task_id: &str) -> PathBuf {
        self.root.join("results").join(format!("{task_id}.json"))
    }
}

/// Atomic JSON write (tmp + rename) so a reader never sees a torn file.
pub fn write_json_atomic<T: Serialize>(path: &Path, value: &T) -> Result<()> {
    let tmp = path.with_extension("json.tmp");
    let bytes = serde_json::to_vec_pretty(value)?;
    std::fs::write(&tmp, bytes).with_context(|| format!("Failed to write {}", tmp.display()))?;
    std::fs::rename(&tmp, path)
        .with_context(|| format!("Failed to rename {} into place", tmp.display()))?;
    Ok(())
}

/// Outcome of waiting for one reply file.
#[derive(Debug)]
pub enum WaitOutcome {
    /// Raw file contents.
    Ready(String),
    TimedOut,
}

/// Wait until every path exists and is non-empty, or timeout elapses. on_nudge
/// fires per pending path (after nudge_after, then every nudge_every) to re-send.
pub fn wait_for_files(
    paths: &[PathBuf],
    timeout: Duration,
    poll: Duration,
    nudge_after: Duration,
    nudge_every: Duration,
    mut on_nudge: impl FnMut(usize),
) -> Vec<WaitOutcome> {
    let start = Instant::now();
    let mut outcomes: Vec<Option<String>> = vec![None; paths.len()];
    let mut last_nudge: Option<Instant> = None;
    loop {
        let mut pending = false;
        for (i, path) in paths.iter().enumerate() {
            if outcomes[i].is_some() {
                continue;
            }
            match std::fs::read_to_string(path) {
                Ok(content) if !content.trim().is_empty() => outcomes[i] = Some(content),
                _ => pending = true,
            }
        }
        if !pending || start.elapsed() >= timeout {
            break;
        }
        if start.elapsed() >= nudge_after && last_nudge.is_none_or(|t| t.elapsed() >= nudge_every) {
            last_nudge = Some(Instant::now());
            for (i, outcome) in outcomes.iter().enumerate() {
                if outcome.is_none() {
                    on_nudge(i);
                }
            }
        }
        std::thread::sleep(poll);
    }
    outcomes
        .into_iter()
        .map(|o| match o {
            Some(content) => WaitOutcome::Ready(content),
            None => WaitOutcome::TimedOut,
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_dir_layout() {
        let tmp = tempfile::tempdir().unwrap();
        let run = RunDir::create(tmp.path().join("r1")).unwrap();
        assert!(run.root.join("inbox").is_dir());
        assert!(run
            .outbox("abc")
            .to_string_lossy()
            .ends_with("outbox/abc.json"));
    }

    #[test]
    fn atomic_write_then_read() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("x.json");
        write_json_atomic(&path, &serde_json::json!({"k": "v"})).unwrap();
        let raw = std::fs::read_to_string(&path).unwrap();
        assert!(raw.contains("\"k\""));
        assert!(!path.with_extension("json.tmp").exists());
    }

    #[test]
    fn wait_sees_file_written_later() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("late.json");
        let writer_path = path.clone();
        let handle = std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(120));
            std::fs::write(writer_path, "{\"content\":\"hi\"}").unwrap();
        });
        let outcomes = wait_for_files(
            &[path],
            Duration::from_secs(5),
            Duration::from_millis(20),
            Duration::from_secs(1),
            Duration::from_secs(1),
            |_| {},
        );
        handle.join().unwrap();
        assert!(matches!(&outcomes[0], WaitOutcome::Ready(c) if c.contains("hi")));
    }

    #[test]
    fn wait_times_out_and_renudges() {
        let tmp = tempfile::tempdir().unwrap();
        let mut nudges = 0;
        let outcomes = wait_for_files(
            &[tmp.path().join("never.json")],
            Duration::from_millis(300), // timeout
            Duration::from_millis(10),  // poll
            Duration::from_millis(20),  // nudge_after
            Duration::from_millis(50),  // nudge_every
            |_| nudges += 1,
        );
        assert!(matches!(outcomes[0], WaitOutcome::TimedOut));
        // First nudge ~20ms, then every ~50ms until timeout: several, not one.
        assert!(nudges >= 2, "expected repeated nudges, got {nudges}");
    }

    #[test]
    fn wait_does_not_nudge_when_resolved_before_grace() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("quick.json");
        std::fs::write(&path, "done").unwrap();
        let mut nudges = 0;
        let outcomes = wait_for_files(
            &[path],
            Duration::from_millis(300),
            Duration::from_millis(10),
            Duration::from_millis(50), // grace longer than the resolve
            Duration::from_millis(50),
            |_| nudges += 1,
        );
        assert!(matches!(&outcomes[0], WaitOutcome::Ready(c) if c.contains("done")));
        assert_eq!(nudges, 0);
    }
}
