//! Topology config, validation, and plan sizing. Blocks activate in order
//! [summarize, reflect, debate, aggregate] (+execute); `aggregate` = chains.

use crate::protocol::Role;
use anyhow::{bail, Result};
use serde::{Deserialize, Serialize};

/// Hard cap on LLM agent calls per inference, matching the paper.
pub const MAX_AGENTS: usize = 10;

/// How the final aggregation is performed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AggregatorMode {
    /// Rule-based majority vote (self-consistency). Costs no LLM call.
    Rule,
    /// LLM aggregator call (used by the Multi-Agent Debate baseline).
    Llm,
}

/// Block activations, e.g. `{"aggregate":9}`. Missing keys are inactive
/// (aggregate defaults to 1, a single chain).
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TopologyConfig {
    #[serde(default)]
    pub summarize: usize,
    #[serde(default)]
    pub reflect: usize,
    #[serde(default)]
    pub debate: usize,
    #[serde(default = "default_aggregate")]
    pub aggregate: usize,
    #[serde(default)]
    pub execute: usize,
}

fn default_aggregate() -> usize {
    1
}

impl Default for TopologyConfig {
    fn default() -> Self {
        TopologyConfig {
            summarize: 0,
            reflect: 0,
            debate: 0,
            aggregate: 1,
            execute: 0,
        }
    }
}

/// One persistent agent session: a (role, chain-slot) pair. Reused across
/// rounds and instances; every request it gets is self-contained.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct SessionSpec {
    pub role: Role,
    /// 1-based chain index (0 for the single aggregator).
    pub slot: usize,
}

impl SessionSpec {
    pub fn short_name(&self, run_id: &str) -> String {
        format!("mass{}-{}-{}", run_id, self.role.token(), self.slot)
    }
}

impl TopologyConfig {
    /// Validate the search dimensions.
    pub fn validate(&self) -> Result<()> {
        if self.summarize > 4 {
            bail!("summarize must be in 0..=4 (got {})", self.summarize);
        }
        if self.reflect > 4 {
            bail!("reflect must be in 0..=4 (got {})", self.reflect);
        }
        if self.debate > 4 {
            bail!("debate must be in 0..=4 (got {})", self.debate);
        }
        if !matches!(self.aggregate, 1 | 3 | 5 | 7 | 9) {
            bail!(
                "aggregate must be one of {{1,3,5,7,9}} (got {})",
                self.aggregate
            );
        }
        if self.execute > 1 {
            bail!("execute must be 0 or 1 (got {})", self.execute);
        }
        Ok(())
    }

    /// Number of parallel chains. Debate needs two opinions, so a
    /// single-chain config with debate active widens to 2.
    pub fn width(&self) -> usize {
        if self.debate > 0 && self.aggregate == 1 {
            2
        } else {
            self.aggregate
        }
    }

    /// Worst-case LLM calls for one inference (reflect may stop early, the
    /// cap uses the worst case).
    pub fn llm_calls(&self, aggregator: AggregatorMode) -> usize {
        let w = self.width();
        let per_chain = self.summarize + 1 + self.execute + 2 * self.reflect;
        let debate = self.debate * w;
        let agg = match aggregator {
            AggregatorMode::Llm => 1,
            AggregatorMode::Rule => 0,
        };
        w * per_chain + debate + agg
    }

    /// Enforce the 10-agent cap before any spawn happens.
    pub fn check_cap(&self, aggregator: AggregatorMode) -> Result<()> {
        let calls = self.llm_calls(aggregator);
        if calls > MAX_AGENTS {
            bail!(
                "topology {:?} needs {} LLM calls per inference, exceeding the paper's cap of {}",
                self,
                calls,
                MAX_AGENTS
            );
        }
        Ok(())
    }

    /// The persistent sessions the runner must spawn for this topology.
    pub fn session_specs(&self, aggregator: AggregatorMode) -> Vec<SessionSpec> {
        let w = self.width();
        let mut specs = Vec::new();
        for slot in 1..=w {
            if self.summarize > 0 {
                specs.push(SessionSpec {
                    role: Role::Summarizer,
                    slot,
                });
            }
            specs.push(SessionSpec {
                role: Role::Predictor,
                slot,
            });
            if self.execute > 0 {
                specs.push(SessionSpec {
                    role: Role::Executor,
                    slot,
                });
            }
            if self.reflect > 0 || self.execute > 0 {
                specs.push(SessionSpec {
                    role: Role::Reflector,
                    slot,
                });
            }
            if self.reflect > 0 {
                specs.push(SessionSpec {
                    role: Role::Refiner,
                    slot,
                });
            }
            if self.debate > 0 {
                specs.push(SessionSpec {
                    role: Role::Debator,
                    slot,
                });
            }
        }
        if aggregator == AggregatorMode::Llm {
            specs.push(SessionSpec {
                role: Role::Aggregator,
                slot: 0,
            });
        }
        specs
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg(s: usize, r: usize, d: usize, a: usize, e: usize) -> TopologyConfig {
        TopologyConfig {
            summarize: s,
            reflect: r,
            debate: d,
            aggregate: a,
            execute: e,
        }
    }

    #[test]
    fn parses_partial_json() {
        let c: TopologyConfig = serde_json::from_str(r#"{"aggregate":9}"#).unwrap();
        assert_eq!(c.aggregate, 9);
        assert_eq!(c.reflect, 0);
        c.validate().unwrap();

        let d: TopologyConfig = serde_json::from_str(r#"{"reflect":2}"#).unwrap();
        assert_eq!(d.aggregate, 1);
        d.validate().unwrap();
    }

    #[test]
    fn rejects_off_dimension_values() {
        assert!(cfg(0, 0, 0, 2, 0).validate().is_err());
        assert!(cfg(5, 0, 0, 1, 0).validate().is_err());
        assert!(cfg(0, 0, 0, 1, 2).validate().is_err());
        assert!(cfg(4, 4, 4, 9, 1).validate().is_ok()); // dims ok (cap fails later)
    }

    #[test]
    fn baseline_call_counts_match_paper() {
        // CoT: 1 call.
        assert_eq!(cfg(0, 0, 0, 1, 0).llm_calls(AggregatorMode::Rule), 1);
        // SC@9 with the paper's rule vote: 9 calls, the vote is free.
        assert_eq!(cfg(0, 0, 0, 9, 0).llm_calls(AggregatorMode::Rule), 9);
        // SC@5 with an LLM selector instead: 5 + 1.
        assert_eq!(cfg(0, 0, 0, 5, 0).llm_calls(AggregatorMode::Llm), 6);
        // MAD: 3 agents, 2 debate rounds after initial answers, LLM judge
        // = 3 + 6 + 1 = 10 (paper: 3 x 3 + 1).
        assert_eq!(cfg(0, 0, 2, 3, 0).llm_calls(AggregatorMode::Llm), 10);
        // Self-refine with 4 rounds: 1 + 2*4 = 9.
        assert_eq!(cfg(0, 4, 0, 1, 0).llm_calls(AggregatorMode::Rule), 9);
    }

    #[test]
    fn cap_enforced() {
        assert!(cfg(0, 0, 0, 9, 0).check_cap(AggregatorMode::Rule).is_ok());
        assert!(cfg(0, 0, 2, 3, 0).check_cap(AggregatorMode::Llm).is_ok());
        // 9 chains x (1 summarize + 1 predict) = 18 > 10.
        assert!(cfg(1, 0, 0, 9, 0).check_cap(AggregatorMode::Rule).is_err());
        // SC@9 with an LLM aggregator = 10, still fine.
        assert!(cfg(0, 0, 0, 9, 0).check_cap(AggregatorMode::Llm).is_ok());
    }

    #[test]
    fn debate_widens_single_chain() {
        assert_eq!(cfg(0, 0, 1, 1, 0).width(), 2);
        assert_eq!(cfg(0, 0, 1, 3, 0).width(), 3);
        assert_eq!(cfg(0, 0, 0, 1, 0).width(), 1);
    }

    #[test]
    fn session_specs_cover_active_roles() {
        let wide = cfg(0, 0, 0, 9, 0).session_specs(AggregatorMode::Rule);
        assert_eq!(wide.len(), 9);
        assert!(wide.iter().all(|s| s.role == Role::Predictor));

        let mad = cfg(0, 0, 2, 3, 0).session_specs(AggregatorMode::Llm);
        // 3 predictors + 3 debators + 1 aggregator.
        assert_eq!(mad.len(), 7);
        assert_eq!(mad.iter().filter(|s| s.role == Role::Debator).count(), 3);
        assert_eq!(mad.iter().filter(|s| s.role == Role::Aggregator).count(), 1);

        let refl = cfg(0, 2, 0, 1, 0).session_specs(AggregatorMode::Rule);
        // predictor + reflector + refiner.
        assert_eq!(refl.len(), 3);

        let exec = cfg(0, 1, 0, 1, 1).session_specs(AggregatorMode::Rule);
        // predictor + executor + reflector + refiner.
        assert_eq!(exec.len(), 4);
    }

    #[test]
    fn session_names_are_deterministic() {
        let spec = SessionSpec {
            role: Role::Predictor,
            slot: 3,
        };
        assert_eq!(spec.short_name("ab12"), "massab12-pred-3");
    }
}
