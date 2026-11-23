//! omar-mass: MASS topology building blocks for OMAR. Run a task through a
//! topology, benchmark HARDMath, exercise a block, or clean up. See README.

mod bench;
mod blocks;
mod coding;
mod grade;
mod graph;
mod mailbox;
mod math;
mod omar;
mod postal;
mod prompts;
mod protocol;
mod runner;
mod topology;

use anyhow::{Context, Result};
use clap::{Parser, Subcommand, ValueEnum};
use graph::Topology;
use runner::{ModelConfig, Runner, RunnerOptions, TaskInstance};
use std::path::PathBuf;
use std::time::{Duration, Instant};
use topology::{AggregatorMode, TopologyConfig};

#[derive(Parser)]
#[command(
    name = "omar-mass",
    about = "MASS topology building blocks on OMAR",
    version
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum Block {
    Aggregate,
    Reflect,
    Debate,
    Summarize,
    Execute,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum CliAggregator {
    Rule,
    Llm,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum GraphKind {
    Chain,
    Ring,
    ScatterGather,
    /// Agents and seed messages from a spec file (`--spec`); agent-coordinated.
    Custom,
}

#[derive(Debug, Clone, Copy, ValueEnum)]
enum CoordMode {
    Runtime,
    Agent,
}

impl From<CoordMode> for runner::Coordination {
    fn from(m: CoordMode) -> Self {
        match m {
            CoordMode::Runtime => runner::Coordination::Runtime,
            CoordMode::Agent => runner::Coordination::Agent,
        }
    }
}

#[derive(Subcommand)]
enum Command {
    /// Run one task instance through a topology config.
    Run {
        /// Topology JSON, e.g. '{"aggregate":5}' or '{"debate":2,"aggregate":3}'.
        #[arg(long)]
        topology: String,
        /// The question / problem statement.
        #[arg(long)]
        question: String,
        /// Optional long-context file (Summarize block input).
        #[arg(long)]
        context_file: Option<PathBuf>,
        /// Optional public test cases (Execute block input).
        #[arg(long)]
        tests: Option<String>,
        /// Aggregation: rule-based majority vote or LLM aggregator.
        #[arg(long, value_enum, default_value = "rule")]
        aggregator: CliAggregator,
        /// Max agents resident/called at once (0 = unlimited).
        #[arg(long, default_value_t = 0)]
        max_concurrent: usize,
        #[command(flatten)]
        model: ModelArgs,
        /// Per-wave reply timeout in seconds.
        #[arg(long, default_value_t = 300)]
        timeout_secs: u64,
    },
    /// Run a HARDMath baseline (validation harness).
    Bench {
        /// Baseline method.
        #[arg(long, value_enum)]
        method: bench::Method,
        /// Number of problems.
        #[arg(long, default_value_t = 100)]
        n: usize,
        /// Subset rotation seed.
        #[arg(long, default_value_t = 0)]
        seed: u64,
        /// Max agents resident/called at once (0 = unlimited). Below the
        /// topology width, wide waves run in sequential batches.
        #[arg(long, default_value_t = 0)]
        max_concurrent: usize,
        /// HARDMath JSONL dataset (default: vendored subset).
        #[arg(long)]
        data: Option<PathBuf>,
        /// Output summary path (default: <run_dir>/summary.json).
        #[arg(long)]
        out: Option<PathBuf>,
        #[command(flatten)]
        model: ModelArgs,
        /// Resume a previous run: load the .partial.json file next to --out,
        /// skip already-completed problem IDs, and merge new results in.
        #[arg(long)]
        resume: bool,
        /// Reuse the warm agent pool across problems instead of respawning
        /// before each one: ~2x fewer Haiku turns, but trades per-problem
        /// isolation (a reused session carries prior context forward).
        #[arg(long)]
        no_reset: bool,
        /// Skip inline grading: save predictions only, to be scored later by the
        /// batched `grade` pass.
        #[arg(long)]
        no_grade: bool,
        /// Per-wave reply timeout in seconds.
        #[arg(long, default_value_t = 300)]
        timeout_secs: u64,
    },
    /// Batch-grade saved predictions: one blind, shuffled judge call per problem
    /// scores every method against the gold (paired evaluation).
    Grade {
        /// Methods to grade together (must have <method>.seed<seed>.json in --dir).
        #[arg(long, value_enum, value_delimiter = ',', default_values = ["cot", "self-refine", "sc5", "debate"])]
        methods: Vec<bench::Method>,
        #[arg(long, default_value_t = 0)]
        seed: u64,
        /// Directory holding the per-method prediction files.
        #[arg(long)]
        dir: PathBuf,
        /// HARDMath JSONL dataset (default: vendored subset).
        #[arg(long)]
        data: Option<PathBuf>,
        /// Backend to spawn the grader with.
        #[arg(long, default_value = "claude")]
        backend: String,
        /// Grader model: defaults to Sonnet for stronger instruction-following
        /// (the solvers under test stay on whatever they ran with).
        #[arg(long, default_value = "claude-sonnet-5")]
        model: Option<String>,
        #[arg(long, default_value_t = 300)]
        timeout_secs: u64,
    },
    /// Spawn and drive a graph topology (chain / ring / scatter-gather).
    Graph {
        /// Topology shape.
        #[arg(long, value_enum)]
        kind: GraphKind,
        /// Number of nodes (workers, for scatter-gather).
        #[arg(long, default_value_t = 4)]
        n: usize,
        /// Seed message / shared task (unused by --kind custom).
        #[arg(long, default_value = "")]
        question: String,
        /// --kind custom only: JSON file with `name`, `prompts` and `seeds`.
        #[arg(long)]
        spec: Option<PathBuf>,
        /// Ring only: hop budget that bounds message-passing.
        #[arg(long, default_value_t = 12)]
        max_hops: usize,
        /// Scatter-gather only: this many workers go missing (never reply).
        #[arg(long, default_value_t = 0)]
        fail_count: usize,
        /// Scatter-gather only: this many workers reply with corrupted output.
        #[arg(long, default_value_t = 0)]
        corrupt_count: usize,
        /// Scatter-gather only: drop the all-N barrier and gather on partial input.
        #[arg(long)]
        relaxed: bool,
        /// Scatter-gather only: what the gather node is asked (default: the
        /// workers' question). Omit the answer here to blind the aggregator, so
        /// the probe measures aggregation rather than the model's own recall.
        #[arg(long)]
        gather_question: Option<String>,
        /// Scatter-gather only: value corrupted workers return (default 999).
        /// Match the honest answer's shape or it's discarded on format alone.
        #[arg(long)]
        corrupt_answer: Option<String>,
        /// Who owns routing and termination: runtime (enforced arm, default)
        /// or agent (before-DSL arm; nodes address their own mail).
        #[arg(long, value_enum, default_value_t = CoordMode::Runtime)]
        coordination: CoordMode,
        /// Agent mode only: total delivery cap, a safety fuse the agents are
        /// never told about (0 = auto, 3*nodes + 10).
        #[arg(long, default_value_t = 0)]
        max_messages: usize,
        /// Rejected for graph runs: pooling remaps node slots and would misroute
        /// hops. Accepted only as 0 so the flag fails loudly instead of silently.
        #[arg(long, default_value_t = 0)]
        max_concurrent: usize,
        /// Chain/ring only: cap resident agents, spawning slots on demand and
        /// reaping the coldest. Unlocks n=100 on a 16GB host (~10 processes
        /// instead of 100). 0 = spawn all upfront.
        #[arg(long, default_value_t = 0)]
        spawn_window: usize,
        #[command(flatten)]
        model: ModelArgs,
        /// Per-wave reply timeout in seconds.
        #[arg(long, default_value_t = 300)]
        timeout_secs: u64,
        /// Agent mode only: stop the run this many seconds after it starts,
        /// keeping its record and tearing its agents down (0 = no limit).
        #[arg(long, default_value_t = 0)]
        deadline_secs: u64,
    },
    /// Run a spec-defined agent-coordinated topology with no live agents:
    /// replies come from a recorded run or from scripted agents. Prints the
    /// same result as `graph --kind custom`.
    Simulate {
        /// JSON file with `name`, `prompts`, `seeds` and `finishers`.
        #[arg(long)]
        spec: PathBuf,
        /// A recorded run: a JSON object with `messages`, each a delivery
        /// with its `from`, `to`, `body` and `reply`.
        #[arg(long, conflicts_with = "agents")]
        replay: Option<PathBuf>,
        /// A shell command running scripted agents: it reads one JSON line
        /// per delivery and writes one JSON line `{"reply": ...}` back.
        #[arg(long)]
        agents: Option<String>,
        /// How replies are read: v2 (what agents are told now) or v1 (the
        /// original study's, to retrace a recorded run as it ran).
        #[arg(long, value_enum, default_value_t = postal::Rules::V2)]
        rules: postal::Rules,
        /// Total delivery cap (0 = auto, 3*nodes + 10).
        #[arg(long, default_value_t = 0)]
        max_messages: usize,
    },
    /// Exercise one building block end-to-end on a built-in tiny instance.
    DemoBlock {
        #[arg(long, value_enum)]
        block: Block,
        #[command(flatten)]
        model: ModelArgs,
        /// Per-wave reply timeout in seconds.
        #[arg(long, default_value_t = 300)]
        timeout_secs: u64,
    },
    /// Kill leaked MASS agents (mass<run>-<role>-<slot>).
    Teardown {
        /// Only agents of this run id; default: every mass* agent.
        #[arg(long)]
        run: Option<String>,
    },
}

#[derive(Debug, clap::Args)]
struct ModelArgs {
    /// OMAR backend to spawn agents with.
    #[arg(long, default_value = "claude")]
    backend: String,
    /// Backend model. Defaults to Haiku 4.5, the validated MASS backbone.
    #[arg(long, default_value = "claude-haiku-4-5-20251001")]
    model: Option<String>,
    /// The folder agents start in (default: the invoking dir). It must be one
    /// the backend CLI already trusts.
    #[arg(long)]
    workdir: Option<PathBuf>,
}

impl ModelArgs {
    fn to_config(&self) -> ModelConfig {
        ModelConfig {
            backend: self.backend.clone(),
            model: self.model.clone(),
            workdir: self.workdir.clone(),
        }
    }
}

fn main() -> Result<()> {
    match Cli::parse().command {
        Command::Run {
            topology,
            question,
            context_file,
            tests,
            aggregator,
            max_concurrent,
            model,
            timeout_secs,
        } => {
            let topology: TopologyConfig =
                serde_json::from_str(&topology).context("Invalid --topology JSON")?;
            let context = match context_file {
                Some(path) => Some(
                    std::fs::read_to_string(&path)
                        .with_context(|| format!("Failed to read {}", path.display()))?,
                ),
                None => None,
            };
            let task = TaskInstance {
                id: format!("adhoc-{}", &uuid::Uuid::new_v4().simple().to_string()[..6]),
                question,
                context,
                tests,
            };
            let result = run_once(
                topology,
                match aggregator {
                    CliAggregator::Rule => AggregatorMode::Rule,
                    CliAggregator::Llm => AggregatorMode::Llm,
                },
                model.to_config(),
                Duration::from_secs(timeout_secs),
                max_concurrent,
                &task,
            )?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::Bench {
            method,
            n,
            seed,
            max_concurrent,
            resume,
            no_reset,
            no_grade,
            data,
            out,
            model,
            timeout_secs,
        } => {
            let data = data.unwrap_or_else(default_dataset);
            // With --out the full summary is already persisted to that file; only
            // echo the JSON to stdout when there's no out file to read it from,
            // so resumable runs don't flood the log with placeholder score:0.0.
            let echo_json = out.is_none();
            let summary = bench::run_bench(bench::BenchArgs {
                method,
                data,
                n,
                seed,
                max_concurrent,
                resume,
                reset_each_problem: !no_reset,
                grade: !no_grade,
                model: model.to_config(),
                timeout: Duration::from_secs(timeout_secs),
                out,
            })?;
            if echo_json {
                println!("{}", serde_json::to_string_pretty(&summary)?);
            }
            Ok(())
        }
        Command::Grade {
            methods,
            seed,
            dir,
            data,
            backend,
            model,
            timeout_secs,
        } => {
            let data = data.unwrap_or_else(default_dataset);
            grade::run_grade(grade::GradeArgs {
                seed,
                data,
                methods,
                dir,
                model: ModelConfig {
                    backend,
                    model,
                    workdir: None,
                },
                timeout: Duration::from_secs(timeout_secs),
            })?;
            Ok(())
        }
        Command::Graph {
            kind,
            n,
            question,
            spec,
            max_hops,
            fail_count,
            corrupt_count,
            relaxed,
            gather_question,
            corrupt_answer,
            coordination,
            max_messages,
            max_concurrent,
            spawn_window,
            model,
            timeout_secs,
            deadline_secs,
        } => {
            // Counted from here, so starting the agents is inside the limit.
            let deadline = (deadline_secs > 0)
                .then(|| Instant::now() + Duration::from_secs(deadline_secs));
            check_graph_concurrency(max_concurrent)?;
            // Fail before spawning agents, not after.
            if matches!(coordination, CoordMode::Agent) && relaxed {
                anyhow::bail!(
                    "--relaxed is a runtime-arm barrier policy; in agent mode the aggregator decides"
                );
            }
            let topology = match kind {
                GraphKind::Chain => Topology::Chain { n },
                GraphKind::Ring => Topology::Ring { n, max_hops },
                GraphKind::ScatterGather => Topology::ScatterGather {
                    n,
                    fail_count,
                    corrupt_count,
                    relaxed,
                },
                GraphKind::Custom => Topology::Custom { n: 0 },
            };
            let custom: Option<runner::CustomSpec> = match kind {
                GraphKind::Custom => Some(read_spec(&spec.context("--kind custom needs --spec")?)?),
                _ => None,
            };
            let topology = match &custom {
                Some(s) => Topology::Custom { n: s.prompts.len() },
                None => topology,
            };
            if custom.is_some() && !matches!(coordination, CoordMode::Agent) {
                anyhow::bail!("--kind custom is agent-coordinated only: pass --coordination agent");
            }
            let task = TaskInstance {
                id: format!("graph-{}", &uuid::Uuid::new_v4().simple().to_string()[..6]),
                question,
                context: None,
                tests: None,
            };
            run_graph_once(
                topology,
                model.to_config(),
                Duration::from_secs(timeout_secs),
                max_concurrent,
                spawn_window,
                deadline,
                &task,
                &runner::GraphOpts {
                    gather_question,
                    corrupt_answer,
                    coordination: coordination.into(),
                    max_messages,
                    custom,
                },
            )
        }
        Command::Simulate {
            spec,
            replay,
            agents,
            rules,
            max_messages,
        } => simulate(&spec, replay.as_deref(), agents.as_deref(), rules, max_messages),
        Command::DemoBlock {
            block,
            model,
            timeout_secs,
        } => {
            let (topology, aggregator, task) = demo_instance(block);
            let result = run_once(
                topology,
                aggregator,
                model.to_config(),
                Duration::from_secs(timeout_secs),
                0,
                &task,
            )?;
            println!("{}", serde_json::to_string_pretty(&result)?);
            Ok(())
        }
        Command::Teardown { run } => teardown(run),
    }
}

fn run_once(
    topology: TopologyConfig,
    aggregator: AggregatorMode,
    model: ModelConfig,
    timeout: Duration,
    max_concurrent: usize,
    task: &TaskInstance,
) -> Result<runner::InstanceResult> {
    let mut runner = Runner::setup(RunnerOptions {
        topology,
        aggregator,
        model,
        timeout,
        max_concurrent,
        with_grader: false,
        reset_each_problem: true,
        run_root: None,
        spawn_window: 0,
        deadline: None,
    })?;
    eprintln!(
        "[mass] run {} ready; results under {}",
        runner.run_id,
        runner.run_dir.root.display()
    );
    let result = runner.run_instance(task);
    // Report teardown residue but never let it discard a completed run.
    if let Err(err) = runner.teardown() {
        eprintln!("[mass] {err}");
    }
    result
}

/// Graph topologies address nodes by slot, but a capped pool remaps sub-waves
/// onto slots 1..=k (`runner::remap_to_pool`), so hop 41 would land on node 1
/// while the record still claims node 41. Reject rather than misroute.
fn check_graph_concurrency(max_concurrent: usize) -> Result<()> {
    if max_concurrent != 0 {
        anyhow::bail!(
            "--max-concurrent is not supported for graph runs (got {max_concurrent}): \
             pooling remaps node slots and would misroute hops. Use 0 (unlimited)."
        );
    }
    Ok(())
}

/// How long ending a run's agents may take before the runner exits anyway.
const TEARDOWN_LIMIT: Duration = Duration::from_secs(180);

fn read_spec(path: &std::path::Path) -> Result<runner::CustomSpec> {
    let text =
        std::fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
    let spec: runner::CustomSpec = serde_json::from_str(&text)?;
    let n = spec.prompts.len();
    if let Some(bad) = spec.finishers.iter().find(|&&f| f < 1 || f > n) {
        anyhow::bail!("spec names finisher {bad}, but has agents 1 to {n}");
    }
    Ok(spec)
}

/// Run a spec-defined topology against a recorded run or scripted agents.
fn simulate(
    spec: &std::path::Path,
    replay: Option<&std::path::Path>,
    agents: Option<&str>,
    rules: postal::Rules,
    max_messages: usize,
) -> Result<()> {
    let spec = read_spec(spec)?;
    let topology = Topology::Custom {
        n: spec.prompts.len(),
    };
    topology.validate()?;
    let opts = runner::GraphOpts {
        coordination: runner::Coordination::Agent,
        max_messages,
        custom: Some(spec),
        ..Default::default()
    };
    let mut value = match (replay, agents) {
        (Some(path), _) => {
            #[derive(serde::Deserialize)]
            struct Record {
                messages: Vec<postal::Delivery>,
            }
            let text = std::fs::read_to_string(path)
                .with_context(|| format!("reading {}", path.display()))?;
            let record: Record = serde_json::from_str(&text)?;
            let recorded = record.messages.len();
            let mut post = postal::sim::Replay::new(record.messages);
            let out = postal::run(rules, topology, &opts, "", &mut post)?;
            let mut value =
                serde_json::to_value(runner::GraphResult::postal(topology, out, Vec::new()))?;
            // How this run compares with the record it replayed.
            value["replay"] = serde_json::json!({
                "recorded": recorded,
                "unused": post.unused(),
                "ran_out": post.exhausted,
                "reworded": post.reworded,
                "diverged": post.diverged.as_ref().map(|(at, was, now)| {
                    serde_json::json!({"delivery": at, "recorded": was, "replayed": now})
                }),
            });
            value
        }
        (None, Some(command)) => {
            let mut post = postal::sim::Script::start(command)?;
            let out = postal::run(rules, topology, &opts, "", &mut post)?;
            serde_json::to_value(runner::GraphResult::postal(topology, out, Vec::new()))?
        }
        (None, None) => anyhow::bail!("simulate needs --replay FILE or --agents COMMAND"),
    };
    value["rules"] = serde_json::json!(format!("{rules:?}").to_lowercase());
    println!("{}", serde_json::to_string_pretty(&value)?);
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn run_graph_once(
    topology: Topology,
    model: ModelConfig,
    timeout: Duration,
    max_concurrent: usize,
    spawn_window: usize,
    deadline: Option<Instant>,
    task: &TaskInstance,
    graph_opts: &runner::GraphOpts,
) -> Result<()> {
    let mut runner = Runner::setup_graph(
        RunnerOptions {
            topology: TopologyConfig::default(),
            aggregator: AggregatorMode::Rule,
            model,
            timeout,
            max_concurrent,
            with_grader: false,
            reset_each_problem: true,
            run_root: None,
            spawn_window,
            deadline,
        },
        topology,
    )?;
    eprintln!(
        "[mass] graph run {} ready; results under {}; journal {}",
        runner.run_id,
        runner.run_dir.root.display(),
        runner.journal().display()
    );
    let result = runner.run_graph(task, topology, graph_opts);
    // The result is printed before the agents are ended, so that ending them
    // can neither discard a finished run nor hold it up: a teardown that has
    // not finished in three minutes is abandoned, and whoever started the run
    // clears what is left.
    let code = match &result {
        Ok(r) => {
            println!("{}", serde_json::to_string_pretty(r)?);
            std::io::Write::flush(&mut std::io::stdout())?;
            0
        }
        Err(_) => 1,
    };
    std::thread::spawn(move || {
        std::thread::sleep(TEARDOWN_LIMIT);
        eprintln!("[mass] the agents did not stop within {TEARDOWN_LIMIT:?}; leaving them");
        std::process::exit(code);
    });
    if let Err(err) = runner.teardown() {
        eprintln!("[mass] {err}");
    }
    result.map(|_| ())
}

/// Built-in tiny instances that exercise each block's path end-to-end.
fn demo_instance(block: Block) -> (TopologyConfig, AggregatorMode, TaskInstance) {
    let math_task = |id: &str| TaskInstance {
        id: id.to_string(),
        question: "What is the sum of the first 10 positive integers?".to_string(),
        context: None,
        tests: None,
    };
    match block {
        Block::Aggregate => (
            TopologyConfig { aggregate: 3, ..Default::default() },
            AggregatorMode::Rule,
            math_task("demo-aggregate"),
        ),
        Block::Reflect => (
            TopologyConfig { reflect: 1, ..Default::default() },
            AggregatorMode::Rule,
            math_task("demo-reflect"),
        ),
        Block::Debate => (
            TopologyConfig { debate: 1, ..Default::default() },
            AggregatorMode::Rule,
            math_task("demo-debate"),
        ),
        Block::Summarize => (
            TopologyConfig { summarize: 1, ..Default::default() },
            AggregatorMode::Rule,
            TaskInstance {
                id: "demo-summarize".to_string(),
                question: "In which year did Ada Lovelace publish her notes on the Analytical Engine?".to_string(),
                context: Some(demo_long_context()),
                tests: None,
            },
        ),
        Block::Execute => (
            TopologyConfig { reflect: 1, execute: 1, ..Default::default() },
            AggregatorMode::Rule,
            TaskInstance {
                id: "demo-execute".to_string(),
                question: "Write a Python function `is_palindrome(s)` that returns True when the lowercased alphanumeric characters of s read the same forwards and backwards. Provide only the code inside <answer></answer> tags.".to_string(),
                context: None,
                tests: Some(
                    "assert is_palindrome('A man, a plan, a canal: Panama') == True\nassert is_palindrome('hello') == False\nassert is_palindrome('') == True".to_string(),
                ),
            },
        ),
    }
}

fn demo_long_context() -> String {
    let filler = "The Analytical Engine was a proposed mechanical general-purpose computer designed by Charles Babbage. \
It incorporated an arithmetic logic unit, control flow in the form of conditional branching and loops, and integrated memory. \
Many unrelated details about Victorian engineering, brass gears, punched cards inspired by the Jacquard loom, and funding disputes with the British government filled the historical record. ";
    let key = "Ada Lovelace translated Luigi Menabrea's memoir on the engine and published her extensive notes, including what is regarded as the first computer program, in 1843. ";
    format!("{}{}{}", filler.repeat(30), key, filler.repeat(30))
}

fn default_dataset() -> PathBuf {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data/hardmath_subset.jsonl");
    if manifest.is_file() {
        manifest
    } else {
        PathBuf::from("mass/data/hardmath_subset.jsonl")
    }
}

/// Kill leaked MASS agents by name prefix.
fn teardown(run: Option<String>) -> Result<()> {
    // A run id scopes to that MASS run; without one, sweep every mass* agent.
    let (agent_prefixes, project_prefixes): (Vec<String>, Vec<&str>) = match &run {
        Some(id) => (vec![format!("mass{}-", id)], vec!["mass-"]),
        None => (vec!["mass".to_string()], vec!["mass-"]),
    };
    let mut mcp = omar::OmarClient::start()?;
    let agents = mcp.list_agents()?;
    let victims: Vec<String> = agents
        .into_iter()
        .filter(|name| agent_prefixes.iter().any(|p| name.starts_with(p)))
        .collect();
    if victims.is_empty() {
        println!("No agents matching {agent_prefixes:?}.");
        return Ok(());
    }
    mcp.log_justification(
        "omar-mass",
        "mass_teardown",
        &format!(
            "Cleaning up {} leaked agents (prefixes {:?}).",
            victims.len(),
            agent_prefixes
        ),
    )?;
    for name in &victims {
        match mcp.kill_agent(name) {
            Ok(()) => println!("killed {name}"),
            Err(err) => println!("failed to kill {name}: {err}"),
        }
    }
    for (id, name) in mcp.list_projects()? {
        if project_prefixes.iter().any(|p| name.starts_with(p)) && mcp.complete_project(id).is_ok()
        {
            println!("completed project {name} ({id})");
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bench_no_reset_flag_parses() {
        let on = Cli::parse_from(["omar-mass", "bench", "--method", "cot", "--no-reset"]);
        match on.command {
            Command::Bench { no_reset, .. } => assert!(no_reset),
            _ => panic!("expected bench"),
        }
        // Default keeps the per-problem respawn (statelessness) on.
        let off = Cli::parse_from(["omar-mass", "bench", "--method", "cot"]);
        match off.command {
            Command::Bench { no_reset, .. } => assert!(!no_reset),
            _ => panic!("expected bench"),
        }
    }

    #[test]
    fn graph_rejects_pooling() {
        // Unlimited is the only sound setting; anything else misroutes hops.
        assert!(check_graph_concurrency(0).is_ok());
        assert!(check_graph_concurrency(1).is_err());
        let err = check_graph_concurrency(4).unwrap_err().to_string();
        assert!(err.contains("misroute"), "{err}");
    }
}
