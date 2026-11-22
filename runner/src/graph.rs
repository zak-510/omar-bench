//! Graph topologies (chain, ring, scatter-gather) over the same agent runtime.
//! Nodes are generic Role::Node workers; the runner routes every hop centrally.

use crate::protocol::Role;
use crate::topology::SessionSpec;
use anyhow::{bail, Result};

/// A large-scale topology to spawn and drive. The DSL will compile down to this.
#[derive(Debug, Clone, Copy)]
pub enum Topology {
    /// Head -> ... -> tail relay. Probes liveness (does the tail emit?).
    Chain { n: usize },
    /// A ring with a hop budget. Probes termination (does passing ever stop?).
    Ring { n: usize, max_hops: usize },
    /// Fan out to n workers, gather after ALL reply. Probes synchronization.
    /// `fail_count` workers go missing; `corrupt_count` reply with garbage;
    /// `relaxed` drops the all-N barrier.
    ScatterGather {
        n: usize,
        fail_count: usize,
        corrupt_count: usize,
        relaxed: bool,
    },
    /// n agents whose prompts come from a spec file (the Savina benchmarks).
    /// Agent-coordinated only: the runner delivers addressed mail, nothing else.
    Custom { n: usize },
}

impl Topology {
    pub fn validate(&self) -> Result<()> {
        match self {
            Topology::Chain { n } => {
                if *n < 1 {
                    bail!("need at least 1 node (got {n})");
                }
            }
            Topology::Ring { n, max_hops } => {
                if *n < 2 {
                    bail!("a ring needs at least 2 nodes (got {n})");
                }
                if *max_hops < 1 {
                    bail!("max_hops must be >= 1");
                }
            }
            Topology::ScatterGather {
                n,
                fail_count,
                corrupt_count,
                ..
            } => {
                if *n < 1 {
                    bail!("need at least 1 node (got {n})");
                }
                if fail_count + corrupt_count > *n {
                    bail!(
                        "fail_count + corrupt_count ({fail_count}+{corrupt_count}) exceeds n ({n})"
                    );
                }
            }
            Topology::Custom { n } => {
                if *n < 2 {
                    bail!("a custom topology needs at least 2 agents (got {n})");
                }
            }
        }
        Ok(())
    }

    /// Persistent agents to spawn. Scatter-gather adds one gather node (slot n+1).
    pub fn session_specs(&self) -> Vec<SessionSpec> {
        let (count, role) = match self {
            Topology::Chain { n } | Topology::Ring { n, .. } => (*n, Role::Node),
            Topology::ScatterGather { n, .. } => (n + 1, Role::Node),
            Topology::Custom { n } => (*n, Role::Actor),
        };
        (1..=count).map(|slot| SessionSpec { role, slot }).collect()
    }
}

/// Whether the gather node fires and a human-readable reason. Strict needs all
/// `expected` inputs; relaxed fires on any, hiding that inputs were lost.
pub fn gather_decision(arrived: usize, expected: usize, relaxed: bool) -> (bool, String) {
    let fired = if relaxed {
        arrived >= 1
    } else {
        arrived == expected
    };
    let policy = if relaxed { "relaxed" } else { "strict" };
    let verb = if fired { "gathered" } else { "refused" };
    (
        fired,
        format!("{policy} gather, {arrived}/{expected} arrived, {verb}"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sg(n: usize) -> Topology {
        Topology::ScatterGather {
            n,
            fail_count: 0,
            corrupt_count: 0,
            relaxed: false,
        }
    }

    #[test]
    fn session_counts_match_topology() {
        let custom = Topology::Custom { n: 6 }.session_specs();
        assert_eq!(custom.len(), 6);
        assert!(custom.iter().all(|s| s.role == Role::Actor));
        assert!(Topology::Custom { n: 1 }.validate().is_err());
        assert_eq!(Topology::Chain { n: 5 }.session_specs().len(), 5);
        assert_eq!(
            Topology::Ring { n: 4, max_hops: 10 }.session_specs().len(),
            4
        );
        // Scatter-gather spawns the workers plus a gather node.
        let specs = sg(3).session_specs();
        assert_eq!(specs.len(), 4);
        assert!(specs.iter().all(|s| s.role == Role::Node));
        assert_eq!(specs.last().unwrap().slot, 4);
    }

    #[test]
    fn gather_decision_barrier_vs_relaxed() {
        // Strict fires only when every input arrived.
        assert!(gather_decision(3, 3, false).0);
        assert!(!gather_decision(2, 3, false).0);
        // Relaxed fires on partial input, silently.
        assert!(gather_decision(2, 3, true).0);
        assert!(!gather_decision(0, 3, true).0);
        assert!(gather_decision(2, 3, false).1.contains("refused"));
    }

    #[test]
    fn validate_rejects_too_many_faults() {
        assert!(Topology::ScatterGather {
            n: 3,
            fail_count: 2,
            corrupt_count: 2,
            relaxed: false
        }
        .validate()
        .is_err());
        assert!(Topology::ScatterGather {
            n: 3,
            fail_count: 1,
            corrupt_count: 1,
            relaxed: true
        }
        .validate()
        .is_ok());
    }

    #[test]
    fn validate_rejects_degenerate_shapes() {
        assert!(Topology::Chain { n: 0 }.validate().is_err());
        assert!(Topology::Ring { n: 1, max_hops: 5 }.validate().is_err());
        assert!(Topology::Ring { n: 3, max_hops: 0 }.validate().is_err());
        assert!(Topology::Chain { n: 8 }.validate().is_ok());
        assert!(Topology::Ring { n: 6, max_hops: 20 }.validate().is_ok());
    }
}
