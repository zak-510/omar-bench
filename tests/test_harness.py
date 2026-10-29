import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from omar_bench import cli, config, harbor, ledger, report, topology
from omar_bench.config import ROOT

PRICES = {"as_of": "2026-10-06", "models": {"claude-opus-5-5": {
    "input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write_5m": 5.0,
    "cache_write_1h": 8.0}}}
MANIFEST = {"run_id": "run-1", "operator": "zak-510", "auth": "subscription",
            "study": "pilot", "cell_sha256": "cell-a",
            "suite": "terminal-bench", "arm": "solo", "model": "opus",
            "suite_label": "Terminal-Bench 4.0, CPU subset (63 of 66 tasks)",
            "model_id": "claude-opus-5-5", "config_sha256": "abc"}


def pinned() -> dict:
    """bench.toml with known values in every pin, so command construction can be tested."""
    cfg = config.load()
    cfg["cli"] = {"claude": "2.1.293", "codex": "0.142.1"}
    for omar in cfg["omar"].values():
        omar.update(version="0.4.2", sha256_linux_amd64="f" * 64)
    cfg["suites"]["swe-atlas-rf"]["ref"] = "deadbeef"
    return cfg


def trial(run_dir: Path, name: str, task: str, reward, *, exception=None, output_tokens=1000,
          started="2026-10-08T10:00:00+00:00", finished="2026-10-08T10:05:00+00:00",
          result="written") -> Path:
    """A trial directory as Harbor leaves it: config.json at the start, result.json at the end."""
    trial_dir = run_dir / "jobs" / "run-1" / name
    (trial_dir / "agent/sessions/projects/-app").mkdir(parents=True)
    (trial_dir / "config.json").write_text(json.dumps(
        {"trial_name": name, "task": {"name": task, "path": None}}))
    body = json.dumps({
        "id": f"uuid-{name}", "task_name": task, "trial_name": name, "task_checksum": "c0ffee",
        "verifier_result": None if reward is None else {"rewards": {"reward": reward}},
        "exception_info": exception, "started_at": started, "finished_at": finished})
    if result == "written":
        (trial_dir / "result.json").write_text(body)
    elif result == "truncated":
        (trial_dir / "result.json").write_text(body[:40])
    line = {"type": "assistant", "message": {"id": f"msg-{name}", "model": "claude-opus-5-5",
            "usage": {"input_tokens": 0, "output_tokens": output_tokens}}}
    (trial_dir / "agent/sessions/projects/-app/s.jsonl").write_text(
        json.dumps(line) + "\n" + json.dumps(line) + "\n")
    return trial_dir


def job_files(run_dir: Path, planned: dict[str, int]) -> None:
    """What Harbor writes at the job level, beside the trial directories."""
    job = run_dir / "jobs" / "run-1"
    job.mkdir(parents=True, exist_ok=True)
    (job / "config.json").write_text(json.dumps({"job_name": "run-1", "datasets": []}))
    (job / "result.json").write_text(json.dumps({"id": "job", "n_total_trials": 0, "stats": {}}))
    (job / "lock.json").write_text(json.dumps({"trials": [
        {"task": {"name": task, "digest": "sha256:1"}} for task, n in planned.items()
        for _ in range(n)]}))


class Config(unittest.TestCase):
    def test_a_cell_names_exactly_the_pins_it_is_missing(self):
        cfg = pinned()
        self.assertEqual(config.unpinned(config.cell(cfg, "terminal-bench", "omar-solo", "opus")), [])
        cfg["cli"]["claude"] = ""
        cfg["omar"]["candidate"].update(version="", sha256_linux_amd64="")
        cfg["suites"]["terminal-bench"]["harbor"] = ""
        del cfg["suites"]["terminal-bench"]["agent_timeout_sec"]
        missing = config.unpinned(config.cell(cfg, "terminal-bench", "omar-solo", "opus"))
        self.assertEqual(missing, ["cli.claude", "omar.candidate.sha256_linux_amd64",
                                   "omar.candidate.version",
                                   "suites.terminal-bench.agent_timeout_sec",
                                   "suites.terminal-bench.harbor"])
        # The solo arm does not depend on OMAR's pins.
        self.assertEqual(config.unpinned(config.cell(cfg, "terminal-bench", "solo", "opus")),
                         ["cli.claude", "suites.terminal-bench.harbor"])

    def test_the_pilot_cells_are_fully_pinned_as_shipped(self):
        cfg = config.load()
        for suite in ("terminal-bench", "terminal-bench-2", "apex-agents", "swe-atlas-rf"):
            for arm in ("solo", "omar-solo", "omar-plan-build-check"):
                for model in ("opus", "haiku"):
                    self.assertEqual(config.unpinned(config.cell(cfg, suite, arm, model)), [],
                                     f"{suite} / {arm} / {model}")

    def test_the_codex_models_are_pinned_and_priced_for_terminal_bench(self):
        cfg = config.load()
        for model in ("sol", "luna", "astra"):
            for suite in ("terminal-bench", "terminal-bench-2"):
                for arm in ("solo", "omar-solo", "omar-plan-build-check"):
                    self.assertEqual(config.unpinned(config.cell(cfg, suite, arm, model)), [],
                                     f"{suite} / {arm} / {model}")
            self.assertIn(cfg["models"][model]["id"], cfg["prices"]["models"], model)

    def test_an_allowlisted_suite_needs_the_models_api_host(self):
        cfg = pinned()
        self.assertEqual(config.unpinned(config.cell(cfg, "swe-atlas-rf", "solo", "sol")),
                         ["models.sol.api_host"])
        self.assertEqual(config.unpinned(config.cell(cfg, "apex-agents", "solo", "sol")), [])

    def test_an_unimplemented_arm_says_why(self):
        with self.assertRaisesRegex(config.ConfigError, "headless entry point"):
            config.cell(config.load(), "terminal-bench", "omar-swarm", "opus")

    def test_a_local_path_suite_pins_its_commit_instead_of_a_dataset(self):
        cfg = pinned()
        cfg["suites"]["swe-atlas-rf"]["ref"] = ""
        missing = config.unpinned(config.cell(cfg, "swe-atlas-rf", "solo", "opus"))
        self.assertEqual(missing, ["suites.swe-atlas-rf.ref"])


class DotEnv(unittest.TestCase):
    def test_values_survive_the_ways_env_files_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# comment\n"
                            "PLAIN=abc\n"
                            "export EXPORTED=def\n"
                            "FLAG=1          # a trailing comment\n"
                            "QUOTED='{\"effort\": \"max\"}'\n"
                            "EMPTY=\n"
                            "# COMMENTED=no\n")
            self.assertEqual(cli._dotenv(path), {
                "PLAIN": "abc", "EXPORTED": "def", "FLAG": "1",
                "QUOTED": '{"effort": "max"}', "EMPTY": ""})


class Auth(unittest.TestCase):
    BOTH = {"CLAUDE_CODE_OAUTH_TOKEN": "tok", "ANTHROPIC_API_KEY": "grader-key"}

    def test_what_the_agents_bill_to_must_be_declared(self):
        with self.assertRaisesRegex(config.ConfigError, "OMAR_BENCH_AUTH"):
            cli._auth("claude", self.BOTH)

    def test_subscription_forces_the_token_even_beside_a_graders_api_key(self):
        env = {**self.BOTH, "OMAR_BENCH_AUTH": "subscription"}
        self.assertEqual(cli._auth("claude", env), ("subscription", {"CLAUDE_FORCE_OAUTH": "1"}))

    def test_subscription_without_a_login_stops_before_anything_runs(self):
        with self.assertRaisesRegex(config.ConfigError, "claude setup-token"):
            cli._auth("claude", {"OMAR_BENCH_AUTH": "subscription", "ANTHROPIC_API_KEY": "k"})

    def test_a_switch_that_contradicts_the_declaration_is_an_error_either_way(self):
        with self.assertRaisesRegex(config.ConfigError, "contradicts"):
            cli._auth("claude", {**self.BOTH, "OMAR_BENCH_AUTH": "subscription",
                                 "CLAUDE_FORCE_OAUTH": "0"})
        with self.assertRaisesRegex(config.ConfigError, "contradicts"):
            cli._auth("claude", {**self.BOTH, "OMAR_BENCH_AUTH": "api-key",
                                 "CLAUDE_FORCE_OAUTH": "1"})

    def test_api_key_mode_needs_the_backends_key_and_sets_no_switch(self):
        self.assertEqual(cli._auth("claude", {**self.BOTH, "OMAR_BENCH_AUTH": "api-key"}),
                         ("api-key", {}))
        with self.assertRaisesRegex(config.ConfigError, "API key"):
            cli._auth("codex", {"OMAR_BENCH_AUTH": "api-key"})
        with self.assertRaisesRegex(config.ConfigError, "contradicts"):
            cli._auth("codex", {"OMAR_BENCH_AUTH": "api-key", "OPENAI_API_KEY": "k",
                                "CODEX_AUTH_JSON_PATH": "/x/auth.json"})

    def test_a_codex_subscription_can_point_at_an_explicit_login_file(self):
        env = {"OMAR_BENCH_AUTH": "subscription", "CODEX_AUTH_JSON_PATH": "/x/auth.json"}
        self.assertEqual(cli._auth("codex", env), ("subscription", {"CODEX_FORCE_AUTH_JSON": "1"}))


class HarborCommand(unittest.TestCase):
    def test_solo_arm_runs_harbors_own_agent_at_the_suites_harbor_version(self):
        c = config.cell(pinned(), "apex-agents", "solo", "sol")
        cmd = harbor.command(c, Path("runs/r/jobs"), "r")
        self.assertEqual(cmd[:7], ["uvx", "--from", "harbor==0.20.0", "--python", "3.12", "harbor", "run"])
        self.assertEqual(cmd[7:13], ["-d", "mercor/apex-agents-1-1@1.1.1", "-a", "codex",
                                     "-m", "openai/gpt-6.1-sol"])
        # Three attempts, Harbor never retries on its own, and never stops to ask.
        self.assertEqual(cmd[13:19], ["-k", "3", "-n", "1", "-r", "0"])
        self.assertIn("-y", cmd)
        self.assertNotIn("--allow-agent-host", cmd)
        self.assertEqual(cmd[-2:], ["--ak", "version=0.142.1"])

    def test_omar_arm_passes_the_topology_and_every_pin_to_the_adapter(self):
        c = config.cell(pinned(), "swe-atlas-rf", "omar-plan-build-check", "opus")
        cmd = harbor.command(c, Path("runs/r/jobs"), "r", attempts=1, tasks=["task-69*"], limit=2,
                             extra=["--override-cpus", "8"])
        self.assertEqual(cmd[2], "harbor==0.22.0")
        self.assertEqual(cmd[7:11], ["-p", "vendor/SWE-Atlas/data/rf", "-a",
                                    "omar_bench.agents.omar_agent:OmarAgent"])
        for expected in (["-k", "1"], ["-i", "task-69*"], ["-l", "2"],
                         ["--allow-agent-host", "api.anthropic.com"],
                         ["--ak", f"topology={ROOT / 'topologies/PlanBuildCheck.omar'}"],
                         ["--ak", "backend=claude"], ["--ak", "cli_version=2.1.293"],
                         ["--ak", "omar_version=0.4.2"], ["--ak", "omar_sha256=" + "f" * 64],
                         # SWE Atlas gives an agent three hours; so does OMAR, per agent turn.
                         ["--ak", "invocation_timeout_sec=10800"]):
            self.assertIn(expected, [cmd[i:i + 2] for i in range(len(cmd) - 1)])
        self.assertEqual(cmd[-2:], ["--override-cpus", "8"])

    def test_mission_control_gets_the_brief_and_the_build_of_its_commit(self):
        c = config.cell(pinned(), "terminal-bench", "mission-control", "sol")
        cmd = harbor.command(c, Path("j"), "r")
        pairs = [cmd[i:i + 2] for i in range(len(cmd) - 1)]
        for expected in (["--ak", f"brief={ROOT / 'topologies/mission-control.md'}"],
                         ["--ak", "max_mutations=3"], ["--ak", "backend=codex"],
                         ["--ak", f"omar_tarball={ROOT / 'vendor/omar/omar-03633ed-linux-amd64.tar.gz'}"],
                         ["--ak", f"omar_spec={ROOT / 'vendor/omar/omar-03633ed-spec.md'}"],
                         # The suite's eight hours per turn, as on every OMAR arm.
                         ["--ak", "invocation_timeout_sec=28800"]):
            self.assertIn(expected, pairs)
        self.assertNotIn("topology=", " ".join(cmd))
        # A build of a commit names the commit and its reference among its pins.
        cfg = pinned()
        cfg["omar"]["main"].update(commit="", spec="")
        self.assertEqual(config.unpinned(config.cell(cfg, "terminal-bench", "mission-control", "sol")),
                         ["omar.main.commit", "omar.main.spec"])

    def test_every_arm_gets_the_suites_own_cli_settings(self):
        # SWE Atlas runs its agents at high effort with web tools off; a team must not get more.
        settings = [["--ak", "reasoning_effort=high"], ["--ak", "disallowed_tools=WebSearch,WebFetch"]]
        for arm in ("solo", "omar-solo", "omar-plan-build-check"):
            cmd = harbor.command(config.cell(pinned(), "swe-atlas-rf", arm, "opus"), Path("j"), "r")
            for expected in settings:
                self.assertIn(expected, [cmd[i:i + 2] for i in range(len(cmd) - 1)], arm)
        codex = harbor.command(config.cell(pinned(), "apex-agents", "solo", "sol"), Path("j"), "r")
        self.assertNotIn("reasoning_effort=high", " ".join(codex))

    def test_terminal_bench_follows_the_official_protocol_where_a_subscription_can(self):
        def pairs(arm, model):
            cmd = harbor.command(config.cell(config.load(), "terminal-bench", arm, model),
                                 Path("j"), "r")
            return cmd, [cmd[i:i + 2] for i in range(len(cmd) - 1)]

        cmd, solo = pairs("solo", "opus")
        self.assertEqual(cmd[2], "harbor==0.24.0")
        for expected in (["-d", "terminal-bench/terminal-bench@v4.0.0"], ["-k", "5"],
                         ["--ak", "reasoning_effort=max"],
                         ["--ae", "CLAUDE_CODE_MAX_OUTPUT_TOKENS=128000"]):
            self.assertIn(expected, solo)
        # The three GPU tasks cannot run on this instance; no arm attempts them.
        self.assertEqual(sum(pair[0] == "-x" for pair in solo), 3)
        # Bare mode would make the CLI refuse a subscription login.
        self.assertNotIn("CLAUDE_CODE_SIMPLE", " ".join(cmd))
        # Harbor's own agent has no such option; its limit is the task's.
        self.assertNotIn("invocation_timeout_sec", " ".join(cmd))

        _, team = pairs("omar-plan-build-check", "opus")
        for expected in solo[solo.index(["-k", "5"]):]:
            if expected[0] in ("-x", "--ae") or expected == ["--ak", "reasoning_effort=max"]:
                self.assertIn(expected, team)
        # Eight hours, as the suite gives a solo agent: OMAR's own limit never binds first.
        self.assertIn(["--ak", "invocation_timeout_sec=28800"], team)

        codex, _ = pairs("solo", "sol")
        self.assertNotIn("CLAUDE_CODE_MAX_OUTPUT_TOKENS", " ".join(codex))

    def test_the_older_terminal_bench_sets_agents_up_as_4_0_does(self):
        def pairs(suite, arm):
            cmd = harbor.command(config.cell(config.load(), suite, arm, "haiku"), Path("j"), "r")
            return [cmd[i:i + 2] for i in range(len(cmd) - 1)]

        def settings(suite, arm):
            return [pair for pair in pairs(suite, arm) if pair[0] == "--ae"
                    or (pair[0] == "--ak" and not pair[1].startswith("invocation_timeout_sec="))]

        for arm in ("solo", "omar-solo", "omar-plan-build-check"):
            self.assertEqual(settings("terminal-bench-2", arm), settings("terminal-bench", arm), arm)
        solo = pairs("terminal-bench-2", "solo")
        for expected in (["--from", "harbor==0.24.0"], ["-d", "terminal-bench@2.0"], ["-k", "5"]):
            self.assertIn(expected, solo)
        # Every task of this set runs on a machine without a GPU.
        self.assertFalse([pair for pair in solo if pair[0] == "-x"])
        # The longest any of its tasks allows: OMAR's own limit never binds before a task's.
        self.assertIn(["--ak", "invocation_timeout_sec=12000"],
                      pairs("terminal-bench-2", "omar-plan-build-check"))

    def test_a_models_endpoint_variables_win_over_the_suites(self):
        cfg = pinned()
        cfg["suites"]["terminal-bench"]["agent_env"]["claude"]["ANTHROPIC_BASE_URL"] = "suite"
        cfg["models"]["glm"].update(id="glm-5.3", env={"ANTHROPIC_BASE_URL": "https://glm.example"})
        cmd = harbor.command(config.cell(cfg, "terminal-bench", "solo", "glm"), Path("j"), "r")
        self.assertIn("ANTHROPIC_BASE_URL=https://glm.example", cmd)
        self.assertNotIn("ANTHROPIC_BASE_URL=suite", cmd)

    def test_a_suite_can_ask_for_harbor_extras(self):
        cfg = pinned()
        cfg["suites"]["swe-atlas-rf"]["harbor_extras"] = "modal"
        cmd = harbor.command(config.cell(cfg, "swe-atlas-rf", "solo", "opus"), Path("j"), "r")
        self.assertEqual(cmd[2], "harbor[modal]==0.22.0")


class Ledger(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_dir = self.dir / "runs" / "run-1"
        trial(self.run_dir, "t1__a", "t1", 1.0)
        trial(self.run_dir, "t1__b", "t1", 0.0, output_tokens=2000)
        trial(self.run_dir, "t2__a", "t2", None,
              exception={"exception_type": "AgentTimeoutError", "exception_message": "x"})

    def test_every_trial_becomes_a_row_including_the_one_that_errored(self):
        rows = {r["trial"]: r for r in harbor.rows(self.run_dir, MANIFEST, PRICES)}
        self.assertEqual({k: v["outcome"] for k, v in rows.items()},
                         {"t1__a": "pass", "t1__b": "fail", "t2__a": "error"})
        self.assertEqual(rows["t2__a"]["error_type"], "AgentTimeoutError")
        # Usage comes from the session logs, deduplicated, priced at the pinned table.
        self.assertEqual(rows["t1__a"]["tokens"]["output"], 1000)
        self.assertEqual(rows["t1__a"]["cost_usd"], 0.02)
        self.assertEqual(rows["t1__a"]["cost_source"], "list-price@2026-10-06")
        self.assertEqual(rows["t1__a"]["served_models"], ["claude-opus-5-5"])
        self.assertEqual(rows["t1__a"]["wall_secs"], 300.0)
        self.assertEqual(rows["t1__a"]["trial_dir"], "run-1/jobs/run-1/t1__a")
        self.assertEqual((rows["t1__a"]["operator"], rows["t1__a"]["auth"]),
                         ("zak-510", "subscription"))
        # The id is the run and the trial, so one trial directory is one row for good.
        self.assertEqual((rows["t1__a"]["attempt_id"], rows["t1__a"]["harbor_trial_id"]),
                         ("run-1/t1__a", "uuid-t1__a"))
        self.assertEqual((rows["t1__a"]["study"], rows["t1__a"]["cell_sha256"]),
                         ("pilot", "cell-a"))

    def test_a_trial_that_was_cut_off_is_still_an_attempt(self):
        # The machine stopped: one trial never wrote a result, another was caught mid-write.
        trial(self.run_dir, "t3__a", "t3", 1.0, result="missing", output_tokens=5000)
        trial(self.run_dir, "t3__b", "t3", 1.0, result="truncated")
        job_files(self.run_dir, {"t1": 2, "t2": 1, "t3": 2})
        rows = {r["trial"]: r for r in harbor.rows(self.run_dir, MANIFEST, PRICES)}
        self.assertEqual(len(rows), 5)  # the job's own config.json and result.json are not trials
        self.assertEqual({name: (rows[name]["outcome"], rows[name]["error_type"], rows[name]["task"])
                          for name in ("t3__a", "t3__b")},
                         {"t3__a": ("error", "NoResult", "t3"),
                          "t3__b": ("error", "UnreadableResult", "t3")})
        # What it spent before it was cut off still counts.
        self.assertEqual((rows["t3__a"]["tokens"]["output"], rows["t3__a"]["cost_usd"]), (5000, 0.1))

    def test_a_file_named_like_a_result_inside_a_trials_own_output_is_not_a_trial(self):
        inside = trial(self.run_dir, "t4__a", "t4", 1.0) / "artifacts/app"
        inside.mkdir(parents=True)
        (inside / "config.json").write_text(json.dumps({"trial_name": "decoy", "task": {"name": "x"}}))
        (inside / "result.json").write_text(json.dumps({"trial_name": "decoy", "task_name": "x"}))
        self.assertEqual(len(harbor.rows(self.run_dir, MANIFEST, PRICES)), 4)

    def test_a_run_is_complete_only_when_every_planned_trial_reached_a_result(self):
        rows = harbor.rows(self.run_dir, MANIFEST, PRICES)
        # No lock file: what was planned is unknown, so completeness is not claimed.
        self.assertEqual(harbor.account(self.run_dir, rows)["complete"], False)
        job_files(self.run_dir, {"t1": 2, "t2": 1})
        self.assertEqual(harbor.account(self.run_dir, rows), {
            "planned": 3, "attempted": 3, "without_result": [], "unstarted": 0, "complete": True})
        # Two more were planned and never started: not attempts, and not a finished run.
        job_files(self.run_dir, {"t1": 2, "t2": 3})
        account = harbor.account(self.run_dir, rows)
        self.assertEqual((account["planned"], account["unstarted"], account["complete"]),
                         (5, 2, False))
        trial(self.run_dir, "t2__b", "t2", 1.0, result="missing")
        trial(self.run_dir, "t2__c", "t2", 1.0)
        account = harbor.account(self.run_dir, harbor.rows(self.run_dir, MANIFEST, PRICES))
        self.assertEqual((account["unstarted"], account["without_result"], account["complete"]),
                         (0, ["run-1/t2__b"], False))

    def test_files_an_omar_team_left_in_its_own_workspace_are_counted(self):
        rows = harbor.rows(self.run_dir, MANIFEST, PRICES)
        self.assertEqual({r["workspace_files"] for r in rows}, {None})  # not an OMAR run
        (self.run_dir / "jobs/run-1/t1__a/agent/omar-workspace-files.txt").write_text(
            "/root/.omar/workspaces/w1/worktree/solution.py\n/root/.omar/workspaces/w1/worktree/x\n")
        (self.run_dir / "jobs/run-1/t1__b/agent/omar-workspace-files.txt").write_text("")
        rows = {r["trial"]: r["workspace_files"] for r in harbor.rows(self.run_dir, MANIFEST, PRICES)}
        self.assertEqual(rows, {"t1__a": 2, "t1__b": 0, "t2__a": None})

    def test_ingesting_twice_adds_nothing_and_rewrites_nothing(self):
        directory = self.dir / "results" / "attempts"
        rows = harbor.rows(self.run_dir, MANIFEST, PRICES)
        self.assertEqual(ledger.append(directory, "run-1", rows), 3)
        path = directory / "run-1.jsonl"
        before = path.read_bytes()
        self.assertEqual(ledger.append(directory, "run-1", rows), 0)
        self.assertEqual(path.read_bytes(), before)
        trial(self.run_dir, "t2__b", "t2", 1.0)
        more = harbor.rows(self.run_dir, MANIFEST, PRICES)
        self.assertEqual(ledger.append(directory, "run-1", more), 1)
        self.assertTrue(path.read_bytes().startswith(before))

    def test_two_peoples_runs_never_share_a_file_and_read_back_as_one_ledger(self):
        directory = self.dir / "results" / "attempts"
        mine = harbor.rows(self.run_dir, MANIFEST, PRICES)
        theirs = [{**row, "attempt_id": "theirs-" + row["attempt_id"], "operator": "someone-else"}
                  for row in mine]
        ledger.append(directory, "run-1", mine)
        ledger.append(directory, "run-2", theirs)
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ["run-1.jsonl", "run-2.jsonl"])
        self.assertEqual(len(ledger.read(directory)), 6)
        # A row already in someone else's file is not added again under another run.
        self.assertEqual(ledger.append(directory, "run-3", mine + theirs), 0)
        self.assertFalse((directory / "run-3.jsonl").exists())


def attempt(arm, task, outcome, cost, n=[0], **over):
    n[0] += 1
    return {"attempt_id": f"a{n[0]}", "run_id": "run-1", "study": "main", "cell_sha256": arm,
            "suite": "s", "arm": arm, "model": "m", "task": task,
            "outcome": outcome, "cost_usd": cost, "cost_source": "list-price@2026-10-06",
            "wall_secs": 10.0, "started_at": f"2026-10-08T10:{n[0]:02d}:00",
            "tokens": {"output": 100}} | over


class Ingest(unittest.TestCase):
    """`omar-bench ingest`, against run directories laid out as `run` leaves them."""

    SNAPSHOT = (b'[prices]\nas_of = "2026-01-01"\n[prices.models."claude-opus-5-5"]\n'
                b'input = 4.0\noutput = 1000.0\ncache_read = 0.2\n'
                b'cache_write_5m = 5.0\ncache_write_1h = 8.0\n')

    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.run_dir = self.dir / "runs" / "run-1"
        trial(self.run_dir, "t1__a", "t1", 1.0)
        job_files(self.run_dir, {"t1": 1})
        (self.run_dir / "bench.toml").write_bytes(self.SNAPSHOT)
        (self.run_dir / "manifest.json").write_text(json.dumps(MANIFEST | {
            "created_at": "2026-10-09T00:00:00+00:00", "selection": {"attempts": 1},
            "config_sha256": hashlib.sha256(self.SNAPSHOT).hexdigest()}))
        for name, path in (("LEDGER", "results/attempts"), ("RUN_RECORDS", "results/runs")):
            self.enterContext(mock.patch.object(cli, name, self.dir / path))

    def ingest(self, *args) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cli.main(["ingest", *args, str(self.run_dir)])

    def ended(self):
        (self.run_dir / "exit.json").write_text(json.dumps({"returncode": 0, "interrupted": False}))

    def test_a_run_is_priced_from_the_config_it_ran_under_not_todays(self):
        self.ended()
        self.assertEqual(self.ingest(), 0)
        (row,) = ledger.read(self.dir / "results/attempts")
        # 1000 output tokens at the run's own $1000 per million, not the $20 in bench.toml now.
        self.assertEqual((row["cost_usd"], row["cost_source"]), (1.0, "list-price@2026-01-01"))
        (record,) = ledger.records(self.dir / "results/runs").values()
        self.assertEqual((record["planned"], record["attempted"], record["complete"]), (1, 1, True))

    def test_a_snapshot_that_is_not_the_one_the_run_recorded_is_refused(self):
        self.ended()
        (self.run_dir / "bench.toml").write_bytes(self.SNAPSHOT.replace(b"1000.0", b"1.0"))
        self.assertEqual(self.ingest(), 2)
        self.assertEqual(ledger.read(self.dir / "results/attempts"), [])

    def test_a_run_with_no_end_on_record_is_left_alone_until_declared_dead(self):
        trial(self.run_dir, "t1__b", "t1", 1.0, result="missing")  # in flight, or cut off
        self.assertEqual(self.ingest(), 1)
        self.assertEqual(ledger.read(self.dir / "results/attempts"), [])
        self.assertEqual(self.ingest("--interrupted"), 0)
        outcomes = {r["trial"]: r["error_type"] for r in ledger.read(self.dir / "results/attempts")}
        self.assertEqual(outcomes, {"t1__a": None, "t1__b": "NoResult"})
        (record,) = ledger.records(self.dir / "results/runs").values()
        self.assertEqual((record["interrupted"], record["complete"]), (True, False))
        # Declared once; reading it again adds and changes nothing.
        self.assertEqual(self.ingest(), 0)
        self.assertEqual(len(ledger.read(self.dir / "results/attempts")), 2)


class Report(unittest.TestCase):
    def test_success_reliability_and_cost_per_verified_success(self):
        rows = [attempt("team", "t1", "pass", 3.0), attempt("team", "t1", "pass", 3.0),
                attempt("team", "t2", "pass", 3.0), attempt("team", "t2", "error", 3.0)]
        (line,) = report.summarize(rows, {})
        self.assertEqual((line["tasks"], line["attempts"], line["errors"], line["k"]), (2, 4, 1, 2))
        self.assertEqual(line["pass_rate"], 0.75)       # mean of per-task 1.0 and 0.5
        self.assertEqual(line["pass_all_k"], 0.5)       # only t1 passed both attempts
        self.assertEqual(line["pass_any_k"], 1.0)
        self.assertEqual(line["cost_per_attempt"], 3.0)
        self.assertEqual(line["cost_per_success"], 4.0)  # $12 spent, 3 verified successes

    def test_an_attempt_that_raised_is_an_error_even_when_the_verifier_scored_it(self):
        # The agent crashed and the tests then ran on an untouched task: a fail, and an error.
        rows = [attempt("team", "t1", "fail", 1.0, error_type="NonZeroAgentExitCodeError"),
                attempt("team", "t2", "pass", 1.0, error_type="AgentTimeoutError"),
                attempt("team", "t3", "fail", 1.0, error_type=None)]
        (line,) = report.summarize(rows, {})
        self.assertEqual((line["errors"], line["pass_rate"]), (2, 1 / 3))

    def test_an_exclusion_leaves_the_denominator_and_is_counted(self):
        rows = [attempt("team", "t1", "pass", 1.0), attempt("team", "t1", "error", 1.0)]
        (line,) = report.summarize(rows, {rows[1]["attempt_id"]: "provider outage"})
        self.assertEqual((line["attempts"], line["excluded"], line["pass_rate"]), (1, 1, 1.0))

    def test_a_pilot_and_the_evaluation_it_led_to_are_never_one_result(self):
        rows = [attempt("team", "t1", "fail", 1.0, study="pilot"),
                attempt("team", "t1", "pass", 1.0, study="main")]
        rates = {l["study"]: l["pass_rate"] for l in report.summarize(rows, {})}
        self.assertEqual(rates, {"main": 1.0, "pilot": 0.0})

    def test_attempts_made_under_different_settings_are_not_pooled(self):
        old = attempt("team", "t1", "fail", 1.0, cell_sha256="effort-high", run_id="run-old")
        new = attempt("team", "t1", "pass", 1.0, cell_sha256="effort-max", run_id="run-new")
        with self.assertRaisesRegex(report.ReportError, "run-old.*run-new|run-new.*run-old"):
            report.summarize([old, new], {})
        # Taking one side out, with a reason, is the way through.
        (line,) = report.summarize([old, new], {old["attempt_id"]: "superseded settings"})
        self.assertEqual((line["pass_rate"], line["excluded"]), (1.0, 1))
        repriced = attempt("team", "t1", "pass", 1.0, cost_source="list-price@2027-01-01")
        with self.assertRaisesRegex(report.ReportError, "price tables"):
            report.summarize([new | {"cell_sha256": "team"}, repriced], {})

    def test_rows_on_different_tasks_are_not_ranked_against_each_other(self):
        rows = [attempt("solo", "t1", "fail", 1.0), attempt("solo", "t2", "fail", 1.0),
                attempt("team", "t1", "pass", 2.0)]  # the team ran one task of the two
        lines = report.summarize(rows, {})
        self.assertEqual({(l["comparable"], l["pareto"]) for l in lines}, {(False, False)})
        self.assertIn("not comparable", report.markdown(lines))

    def test_a_run_that_did_not_finish_is_marked_and_kept_off_the_frontier(self):
        rows = [attempt("solo", "t1", "fail", 1.0), attempt("team", "t1", "pass", 2.0, run_id="cut")]
        lines = {l["arm"]: l for l in report.summarize(rows, {}, incomplete={"cut"})}
        self.assertEqual((lines["team"]["incomplete"], lines["team"]["pareto"]), (True, False))
        self.assertTrue(lines["solo"]["pareto"])
        text = report.markdown(list(lines.values()))
        self.assertIn("| team † | m | 1 | 1 | 1 |", text)   # tasks, k, attempts
        self.assertIn("Not a final result", text)

    def test_the_heading_says_which_study_and_which_part_of_the_suite(self):
        row = attempt("solo", "t1", "pass", 1.0, study="pilot",
                      suite_label="Terminal-Bench 4.0, CPU subset (63 of 66 tasks)")
        self.assertIn("## pilot: Terminal-Bench 4.0, CPU subset (63 of 66 tasks)",
                      report.markdown(report.summarize([row], {})))

    def test_pareto_frontier_keeps_only_undominated_cells(self):
        rows = ([attempt("cheap-weak", "t1", "fail", 1.0), attempt("cheap-weak", "t2", "pass", 1.0)]
                # Dearer than dear-weak, so only cheap-weak, at the same pass rate, beats dear-weak.
                + [attempt("dear-strong", "t1", "pass", 7.0), attempt("dear-strong", "t2", "pass", 7.0)]
                + [attempt("dear-weak", "t1", "fail", 6.0), attempt("dear-weak", "t2", "pass", 6.0)])
        frontier = {l["arm"]: l["pareto"] for l in report.summarize(rows, {})}
        self.assertEqual(frontier, {"cheap-weak": True, "dear-strong": True, "dear-weak": False})

    def test_the_interval_is_the_same_on_every_reading_of_the_same_ledger(self):
        # Seeded: 2000 resamples of these per-task pass fractions always give this interval.
        per_task = [0.0, 0.1, 0.25, 0.3, 0.55, 0.61, 0.8, 1.0, 1 / 3, 2 / 3, 0.47]
        self.assertEqual(report._bootstrap_ci(per_task), (0.2924242424242424, 0.629090909090909))

    def test_an_unpriced_attempt_makes_the_cells_cost_unknown(self):
        rows = [attempt("team", "t1", "pass", 1.0), attempt("team", "t2", "pass", None)]
        (line,) = report.summarize(rows, {})
        self.assertIsNone(line["cost_per_success"])
        self.assertFalse(line["pareto"])
        self.assertIn("n/a", report.markdown([line]))


class Topologies(unittest.TestCase):
    def test_rendering_moves_every_agent_to_the_backend_and_nothing_else(self):
        source = (ROOT / "topologies/PlanBuildCheck.omar").read_text()
        self.assertEqual(topology.render(source, "claude"), source)
        codex = topology.render(source, "codex")
        self.assertEqual(codex.count(": Codex]"), 3)  # planner, builder, checker
        self.assertNotIn(": Claude", codex)
        self.assertEqual(codex.replace(": Codex]", ": Claude]"), source)

    def test_every_topology_honours_the_harness_contract(self):
        for path in sorted((ROOT / "topologies").glob("*.omar")):
            source = path.read_text()
            self.assertRegex(source, r"main Bench \{\s+t = \w+\(\)\s+\}", path.name)
            for port in ("input statement : string", "input cwd : string", "output answer : string"):
                self.assertIn(port, source, path.name)

    def test_whoever_does_the_work_is_told_it_is_graded_outside_the_omar_workspace(self):
        # OMAR's own instructions say to keep files in the workspace; the task is graded elsewhere.
        for name in ("Solo.omar", "PlanBuildCheck.omar"):
            text = " ".join((ROOT / "topologies" / name).read_text().split())
            self.assertEqual(
                text.count("not in your OMAR workspace. Make its changes in place"), 1, name)


if __name__ == "__main__":
    unittest.main()
